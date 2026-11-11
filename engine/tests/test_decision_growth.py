"""Financial-source counterexamples; synthetic fixtures are not market evidence."""
import copy
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ["AQ5_DATA"] = os.environ["AQ5_TEST_DATA"]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aq5 import decision_growth as g
from aq5.config import CN_TZ

AT = datetime(2026, 11, 11, 15, 0, tzinfo=CN_TZ)
SOURCE = {"raw_sha256": "a"*64, "retrieved_at": AT.isoformat(), "source_url": "https://example.org/financial",
          "provider": "synthetic-test", "rule_version": g.VERSION}


def rows():
    # 2025 quarters: revenues100/100/100, profits10/10/10.
    # 2026 quarters: 110/120/150 and11/12/16 => Q3 growth50/60 vsQ2 20/20.
    return [{"SECURITY_CODE": "600000", "REPORT_DATE": period,
             "NOTICE_DATE": "2026-10-30 00:00:00", "TOTAL_OPERATE_INCOME": rev,
             "PARENT_NETPROFIT": profit} for period, rev, profit in (
        ("2026-09-30",380,39),("2026-06-30",230,23),("2026-03-31",110,11),
        ("2025-09-30",300,30),("2025-06-30",200,20),("2025-03-31",100,10))]


def announcements():
    return [{"codes":[{"stock_code":"600000"}], "title":"样例股份2026年第三季度报告",
             "art_code":"AN2026103012345", "display_time":"2026-10-30 19:30:00:123"}]


class GrowthTests(unittest.TestCase):
    def growth(self, data=None, source=None, at=AT):
        return g.growth_record('sh600000', rows() if data is None else data, source or SOURCE, at)

    def test_single_quarter_difference_not_cumulative_yoy(self):
        result=self.growth()
        self.assertTrue(result['value'])
        self.assertAlmostEqual(result['metrics']['revenue_yoy'],50)
        self.assertAlmostEqual(result['metrics']['profit_yoy'],60)
        self.assertAlmostEqual(result['metrics']['prior_revenue_yoy'],20)
        self.assertEqual(result['basis'],'comparable_quarter_yoy')
        self.assertEqual(result['available_at'],AT.isoformat(timespec='seconds'))

    def test_missing_predecessor_is_not_zero(self):
        with self.assertRaisesRegex(ValueError,'missing'):
            self.growth(rows()[:-1])

    def test_loss_base_is_not_fictitious_positive_growth(self):
        data=rows();data[-1]['PARENT_NETPROFIT']=20
        with self.assertRaisesRegex(ValueError,'nonpositive'):
            self.growth(data)

    def test_false_acceleration_is_retained_as_negative_evidence(self):
        data=rows();data[0]['PARENT_NETPROFIT']=35
        self.assertFalse(self.growth(data)['value'])

    def test_future_publication_unavailable(self):
        data=rows()
        for r in data:r['NOTICE_DATE']='2026-12-01'
        with self.assertRaisesRegex(ValueError,'no_available'):
            self.growth(data)

    def test_date_only_publication_not_intraday_known(self):
        cutoff=AT.replace(month=10,day=30)
        source={**SOURCE,'retrieved_at':cutoff.isoformat()}
        with self.assertRaisesRegex(ValueError,'no_available'):
            self.growth(source=source,at=cutoff)

    def test_current_revision_not_backdated(self):
        with self.assertRaisesRegex(ValueError,'not_known'):
            self.growth(at=AT-timedelta(seconds=1))

    def test_duplicate_conflicting_period_rejected(self):
        data=rows();data.append({**data[0],'PARENT_NETPROFIT':40})
        with self.assertRaisesRegex(ValueError,'conflicting'):
            self.growth(data)

    def test_newest_missing_value_does_not_fall_back(self):
        data=rows();data[0]['TOTAL_OPERATE_INCOME']=None
        with self.assertRaisesRegex(ValueError,'missing_financial'):
            self.growth(data)

    def test_event_requires_growth_identity_period_and_date(self):
        growth=self.growth()
        record,events=g.catalyst_record('sh600000',growth,announcements(),SOURCE,AT)
        self.assertTrue(record['value']);self.assertFalse(record['provenance']['catalyst_effect_verified'])
        for changes in ({'title':'样例股份2026年中标合同公告'}, {'title':'样例股份2025年第三季度报告'},
                        {'title':'样例股份2026年第三季度报告更正公告'}, {'codes':[{'stock_code':'000001'}]},
                        {'display_time':'2026-12-01 09:00:00'}):
            ann=[{**announcements()[0],**changes}]
            with self.subTest(changes=changes):
                self.assertIsNone(g.catalyst_record('sh600000',growth,ann,SOURCE,AT)[0])

    def test_headline_alone_not_positive_catalyst(self):
        growth=self.growth();growth['value']=False
        record,events=g.catalyst_record('sh600000',growth,announcements(),SOURCE,AT)
        self.assertIsNone(record);self.assertTrue(events[0]['not_positive_catalyst'])

    def test_report_title_variants_and_fixed_expiry(self):
        record=self.growth()
        later=self.growth(source={**SOURCE,'retrieved_at':(AT+timedelta(days=1)).isoformat()},at=AT+timedelta(days=1))
        self.assertEqual(record['expires_at'],later['expires_at'])
        ann=[{**announcements()[0],'title':'样例股份:2026年三季度报告'}]
        self.assertTrue(g.catalyst_record('sh600000',record,ann,SOURCE,AT)[0]['value'])

    def test_quarter_boundaries(self):
        from datetime import date
        for current,previous in [('2026-03-31','2025-12-31'),('2026-06-30','2026-03-31'),
                                 ('2026-09-30','2026-06-30'),('2026-12-31','2026-09-30')]:
            self.assertEqual(str(g._quarter_before(date.fromisoformat(current))),previous)

    def test_collection_captures_raw_and_reports_partial_failure(self):
        def fetch(url):
            if url.startswith(g.FIN_BASE):
                return json.dumps({'success':True,'result':{'data':rows(),'count':len(rows()),'pages':1}}).encode()
            return json.dumps({'data':{'list':announcements()}}).encode()
        with tempfile.TemporaryDirectory(dir=os.environ['AQ5_TEST_DATA']) as folder, patch.object(g,'now',return_value=AT):
            result=g.collect(['sh600000','sz000001'],fetcher=fetch,storage_dir=folder)
            self.assertEqual(result['coverage']['growth'],1)
            self.assertEqual(result['coverage']['catalyst'],1)
            self.assertEqual(len(result['failures']),1)
            for source in result['sources']:
                raw=Path(source['raw_path']).read_bytes()
                self.assertEqual(g.hashlib.sha256(raw).hexdigest(),source['raw_sha256'])

    def test_invalid_scope_and_large_response_rejected(self):
        with self.assertRaises(ValueError):g.collect(['not-a-stock'])
        with tempfile.TemporaryDirectory(dir=os.environ['AQ5_TEST_DATA']) as folder:
            result=g.collect(['sh600000'],fetcher=lambda _:b'x'*(g.MAX_BYTES+1),storage_dir=folder)
            self.assertEqual(result['records'],[])
            self.assertTrue(result['failures'])

    def test_batch_truncation_not_complete_coverage(self):
        payload=json.dumps({'success':True,'result':{'data':rows(),'count':99,'pages':2}}).encode()
        with tempfile.TemporaryDirectory(dir=os.environ['AQ5_TEST_DATA']) as folder:
            result=g.collect(['sh600000'],fetcher=lambda _:payload,storage_dir=folder)
            self.assertEqual(result['records'],[])
            self.assertEqual(result['failures'][0]['reason'],'financial_batch_incomplete')


if __name__=='__main__':unittest.main()
