import unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from collect import collect
SOURCE={'symbol':'000000','name':'TEST ETF','url':'https://example.org/test'}
def page(tax='0',payment='152',paid='2026.10.02'):
    return f'''TEST ETF 000000<table><tr><th>지급 기준일</th><th>실 지급일</th><th>분배금 (원)</th><th>주당과세표준액 (원)</th></tr><tr><td>2026.09.30</td><td>{paid}</td><td>{payment}</td><td>{tax}</td></tr></table>'''
class CollectionTests(unittest.TestCase):
    def test_zero_and_unknown(self):
        self.assertEqual(collect(page(),SOURCE)['rows'][0]['taxBase'],'0')
        self.assertIsNone(collect(page('-'),SOURCE)['rows'][0]['taxBase'])
    def test_bad_tax(self):
        with self.assertRaises(ValueError):collect(page('153'),SOURCE)
    def test_bad_date(self):
        with self.assertRaises(ValueError):collect(page(paid='2026.09.01'),SOURCE)
    def test_wrong_product(self):
        with self.assertRaises(ValueError):collect(page().replace('000000','111111'),SOURCE)
    def test_duplicate_table(self):
        with self.assertRaises(ValueError):collect(page()+page(),SOURCE)
    def test_malformed_amount(self):
        with self.assertRaises(ValueError):collect(page(payment='152원'),SOURCE)
if __name__=='__main__':unittest.main()
