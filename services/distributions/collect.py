"""Fetch public distribution tables into reviewable per-symbol JSON. No account data."""
import argparse, datetime as dt, hashlib, json, re, urllib.request
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

class Tables(HTMLParser):
    def __init__(self):
        super().__init__(); self.tables=[]; self.table=None; self.row=None; self.cell=None
    def handle_starttag(self, tag, attrs):
        if tag=='table': self.table=[]
        elif self.table is not None and tag=='tr': self.row=[]
        elif self.row is not None and tag in ('td','th'): self.cell=[]
    def handle_data(self, text):
        if self.cell is not None: self.cell.append(text)
    def handle_endtag(self, tag):
        if tag in ('td','th') and self.cell is not None:
            self.row.append(''.join(self.cell).strip()); self.cell=None
        elif tag=='tr' and self.row is not None:
            self.table.append(self.row); self.row=None
        elif tag=='table' and self.table is not None:
            self.tables.append(self.table); self.table=None

def date(text):
    return dt.date.fromisoformat(re.sub(r'[./]', '-', text.strip())).isoformat()

def amount(text, nullable=False):
    text=text.strip().replace(',', '')
    if nullable and text in ('', '-', '미확인'): return None
    if not re.fullmatch(r'\d+(\.\d{1,6})?',text): raise ValueError('invalid amount: '+text)
    return format(Decimal(text),'f')

def collect(html, source):
    if source['symbol'] not in html or source['name'] not in html: raise ValueError('product identity not found')
    parser=Tables(); parser.feed(html)
    matches=[]
    for table in parser.tables:
        if not table: continue
        header=''.join(table[0]).replace(' ','').replace('\n','').replace('\r','')
        if '주당과세' in header and ('실지급일' in header or '지급일' in header): matches.append(table)
    if len(matches)!=1: raise ValueError('ambiguous or missing distribution table')
    table=matches[0]
    headers=[re.sub(r'\s','',v) for v in table[0]]
    def column(test):
        indices=[i for i,v in enumerate(headers) if test(v)]
        if len(indices)!=1: raise ValueError('unrecognized columns')
        return indices[0]
    record=column(lambda v:'기준일' in v)
    paid=column(lambda v:'지급일' in v and '기준' not in v)
    payment=column(lambda v:'분배금' in v and '일' not in v)
    tax=column(lambda v:'과세' in v)
    rows=[]
    for cells in table[1:]:
        if len(cells)!=len(headers): raise ValueError('incomplete row')
        r={'recordDate':date(cells[record]),'paidAt':date(cells[paid]),'payment':amount(cells[payment]),'taxBase':amount(cells[tax],True)}
        if r['paidAt']<r['recordDate']: raise ValueError('invalid payment date')
        if r['taxBase'] is not None and Decimal(r['taxBase'])>Decimal(r['payment']): raise ValueError('tax exceeds payment')
        rows.append(r)
    if not rows or len(rows)>1000 or len({r['recordDate'] for r in rows})!=len(rows): raise ValueError('empty/duplicate/oversized rows')
    return dict(schemaVersion=1,symbol=source['symbol'],name=source['name'],sourceURL=source['url'],
                fetchedAt=dt.datetime.now(dt.timezone.utc).isoformat(),sourceSHA256=hashlib.sha256(html.encode()).hexdigest(),
                coverage='available-source-rows',rows=sorted(rows,key=lambda r:r['paidAt'],reverse=True))

def main():
    p=argparse.ArgumentParser(); p.add_argument('--symbol',required=True); p.add_argument('--html',type=Path); p.add_argument('--output',type=Path,required=True)
    args=p.parse_args(); sources=json.loads(Path(__file__).with_name('sources.json').read_text()); source=next(s for s in sources if s['symbol']==args.symbol)
    if args.html: html=args.html.read_text()
    else:
        req=urllib.request.Request(source['url'],headers={'User-Agent':'AlbaedangDistributionCollector/0.1'})
        with urllib.request.urlopen(req,timeout=30) as response:
            raw=response.read(5_000_001)
            if len(raw)>5_000_000: raise ValueError('page too large')
            html=raw.decode('utf-8')
    data=collect(html,source)
    # A smaller source page must not silently erase older published history.
    if args.output.exists():
        old=json.loads(args.output.read_text())
        if old['symbol']!=data['symbol']: raise ValueError('symbol mismatch')
        merged={r['recordDate']:r for r in old['rows']}; merged.update({r['recordDate']:r for r in data['rows']})
        data['rows']=sorted(merged.values(),key=lambda r:r['paidAt'],reverse=True)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    temp=args.output.with_suffix('.tmp'); temp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n');temp.replace(args.output)
    print(f"{data['symbol']}: {len(data['rows'])} rows validated")
if __name__=='__main__': main()
