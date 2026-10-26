#!/usr/bin/env python3
"""Build public market JSON. Never publishes credentials or user portfolio data."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal, InvalidOperation
import getpass
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import unquote, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://apis.data.go.kr/1160100/'
SOURCES = {
    'etf': dict(name='금융위원회·공공데이터포털', origin='한국거래소', url='https://www.data.go.kr/data/15094806/openapi.do'),
    'stock': dict(name='금융위원회·공공데이터포털', origin='한국거래소', url='https://www.data.go.kr/data/15094808/openapi.do'),
    'dividend': dict(name='금융위원회·공공데이터포털', origin='한국예탁결제원', url='https://www.data.go.kr/data/15043284/openapi.do'),
}
ENDPOINTS = {'etf': 'GetSecuritiesProductInfoService_V2/getETFPriceInfo_V2',
             'stock': 'GetStockSecuritiesInfoService_V2/getStockPriceInfo_V2',
             'dividend': 'GetStocDiviInfoService_V2/getDiviInfo_V2'}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class API:
    def __init__(self, key):
        self.key = unquote(key.strip())

    def page(self, kind, **filters):
        params = dict(serviceKey=self.key, resultType='json', numOfRows=1000, pageNo=1, **{})
        params.update(filters)
        request = Request(BASE + ENDPOINTS[kind] + '?' + urlencode(params))
        for attempt in range(3):
            try:
                with build_opener(NoRedirect).open(request, timeout=45) as response:
                    raw = response.read(5_000_001)
                if len(raw) > 5_000_000:
                    raise ValueError('응답 크기 초과')
                data = json.loads(raw)
                break
            except HTTPError as error:
                if error.code not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                    raise ValueError(f'{kind}: HTTP {error.code}') from None
            except (URLError, TimeoutError, OSError, json.JSONDecodeError):
                if attempt == 2:
                    raise ValueError(f'{kind}: 네트워크 또는 JSON 응답 오류') from None
            time.sleep(2 ** attempt)
        response = data.get('response', {})
        header = response.get('header', {})
        if header.get('resultCode') != '00':
            raise ValueError(f'{kind}: API 결과 코드 {header.get("resultCode", "없음")}')
        body = response['body']
        items = (body.get('items') or {}).get('item') or []
        return int(body['totalCount']), items if isinstance(items, list) else [items]


def date(value):
    return datetime.strptime(value, '%Y%m%d').strftime('%Y-%m-%d')


def latest_rows(api, kind):
    _, probe = api.page(kind, numOfRows=5)
    if not probe:
        raise ValueError(f'{kind}: 빈 응답')
    latest = max(row['basDt'] for row in probe)
    date(latest)
    total, rows = api.page(kind, basDt=latest)
    minimum = {'etf': 1000, 'stock': 1500, 'dividend': 10000}[kind]
    if not minimum <= total <= (150000 if kind == 'dividend' else 20000):
        raise ValueError(f'{kind}: 예상 범위를 벗어난 건수 {total}')
    def next_page(page):
        count, values = api.page(kind, basDt=latest, pageNo=page)
        if count != total:
            raise ValueError(f'{kind}: 조회 도중 건수 변경')
        return values
    with ThreadPoolExecutor(max_workers=4) as pool:
        for values in pool.map(next_page, range(2, (total + 999) // 1000 + 1)):
            rows.extend(values)
    if len(rows) != total or any(r['basDt'] != latest for r in rows):
        raise ValueError(f'{kind}: 기준일 또는 전체 건수 검증 실패')
    return date(latest), rows


def amount(value):
    if value in (None, ''):
        return None
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise InvalidOperation()
        return str(value)
    except InvalidOperation:
        raise ValueError('유효하지 않은 공시 금액') from None


def market_entries(rows, kind, as_of):
    entries = []
    for row in rows:
        code, name = str(row['srtnCd']), row['itmsNm'].strip()
        if not re.fullmatch('[A-Z0-9]{6,9}', code) or not name or len(name) > 300:
            raise ValueError('종목명·코드 검증 실패')
        entries.append(dict(code=code, name=name, issuer='', kind=kind, asOf=as_of,
                            isin=row['isinCd'], market=row.get('mrktCtg'),
                            close=amount(row.get('clpr')), nav=amount(row.get('nav')),
                            changePercent=row.get('fltRt'), volume=amount(row.get('trqu')),
                            sourceURL=SOURCES[kind]['url']))
    if len({row['code'] for row in entries}) != len(entries):
        raise ValueError('중복 종목코드')
    return entries


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n')
    temporary.replace(path)


def collect(api, output, dividends=False):
    generated = datetime.now().astimezone().isoformat(timespec='seconds')
    dated = {kind: latest_rows(api, kind) for kind in ('etf', 'stock')}
    entries = [entry for kind, (day, rows) in dated.items() for entry in market_entries(rows, kind, day)]
    if len({e['code'] for e in entries}) != len(entries):
        raise ValueError('ETF·주식 간 중복 코드')
    snapshot = dict(schemaVersion=1, asOf=min(day for day, _ in dated.values()), generatedAt=generated,
                    sourceURL=SOURCES['etf']['url'], sources=SOURCES,
                    counts={kind: len(rows) for kind, (_, rows) in dated.items()},
                    entries=sorted(entries, key=lambda x: (x['name'].casefold(), x['code'])))
    # Complete validation precedes replacement of any public artifact.
    dividend_files = {}
    dividend_day = None
    if dividends:
        try:
            dividend_day, rows = latest_rows(api, 'dividend')
        except ValueError:
            if not (output / 'dividends/index.json').exists():
                raise  # Initial publication must include a valid dividend snapshot.
            rows = []
            print('::warning::기업 배당 수집 실패: 기존 배당자료를 유지하고 정상 종목 목록만 갱신합니다.', file=sys.stderr)
        for row in rows:
            isin = row.get('isinCd', '')
            if not re.fullmatch('[A-Z0-9]{12}', isin):
                continue  # Rows lacking a reliable security identifier are never matched by name.
            dividend_files.setdefault(isin, []).append(dict(name=row.get('isinCdNm'),
                recordDate=date(row['dvdnBasDt']) if row.get('dvdnBasDt') else None,
                paymentDate=date(row['cashDvdnPayDt']) if row.get('cashDvdnPayDt') else None,
                amountPerShare=amount(row.get('stckGenrDvdnAmt')),
                reason=row.get('stckDvdnRcdNm'), shareClass=row.get('scrsItmsKcdNm')))
    for isin, records in dividend_files.items():
        write_json(output / 'dividends' / (isin + '.json'), dict(schemaVersion=1, asOf=dividend_day,
            source=SOURCES['dividend'], isin=isin, records=records))
    if dividend_day is not None:
        write_json(output / 'dividends/index.json', dict(schemaVersion=1, asOf=dividend_day,
            source=SOURCES['dividend'], securities=sorted(dividend_files)))
    write_json(output / 'catalog.json', snapshot)
    print(json.dumps(dict(asOf=snapshot['asOf'], counts=snapshot['counts'],
        dividendSecurities=len(dividend_files)), ensure_ascii=False), flush=True)
    return snapshot


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'release/site/data/market/v1')
    parser.add_argument('--dividends', action='store_true')
    parser.add_argument('--bundle', action='store_true')
    args = parser.parse_args()
    key = os.environ.get('DATA_GO_KR_SERVICE_KEY', '')
    if not key and sys.stdin.isatty():
        key = getpass.getpass('공공데이터 인증키 (저장하지 않음): ')
    if not key:
        raise SystemExit('DATA_GO_KR_SERVICE_KEY 설정이 필요합니다.')
    try:
        snapshot = collect(API(key), args.output, args.dividends)
        if args.bundle:
            for path in ('Packages/DivFlowCore/Sources/Domain/Resources/etf-names.json', 'android/app/src/main/assets/etf-names.json'):
                write_json(ROOT / path, snapshot)
    except Exception as error:
        # Never print URL-bearing exception details or the key.
        print('수집 실패: ' + (str(error).replace(key, '[REDACTED]') if isinstance(error, ValueError) else type(error).__name__), file=sys.stderr)
        raise SystemExit(1)
