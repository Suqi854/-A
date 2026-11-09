import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aq5 import db
from aq5.forecast import calendar
from aq5.forecast.calendar import due_at, official_days, iso, stamp


class CalendarTests(unittest.TestCase):
    def setUp(self):
        calendar.init()
        with db.tx() as c:
            c.execute('DELETE FROM forecast_calendars')
            c.execute('DELETE FROM forecast_calendar_refresh')

    def document(self,year=2026,missing=None):
        # Synthetic parser fixture; 2027 values deliberately are not claimed as
        # an actual exchange publication and never leave this isolated database.
        rows=[]
        for name,(start,end) in zip(calendar.HOLIDAYS,calendar._SEED_CLOSURES):
            if name==missing:continue
            sm,sd=map(int,start.split('-'));em,ed=map(int,end.split('-'))
            rows.append(f'<tr><td>{name}：</td><td>{sm}月{sd}日（星期一）至{em}月{ed}日（星期三）休市，起照常开市。</td></tr>')
        return (f'<html><h1>{year}年休市安排</h1><p>{year-1}-12-18</p><table>'+''.join(rows)+'</table></html>').encode()

    def test_holidays_are_explicit(self):
        days = official_days()
        self.assertNotIn('2026-02-23', days)
        self.assertNotIn('2026-10-07', days)
        self.assertNotIn('2026-10-10', days)  # weekend make-up work is not a trading session
        self.assertIn('2026-10-08', days)
        self.assertEqual(official_days(2027), [])

    def test_long_horizon_counts_exchange_days(self):
        self.assertEqual(due_at('2026-09-30T15:00:00+08:00', '1d', official_days()), '2026-10-08T15:00:00+08:00')
        self.assertIsNone(due_at('2026-12-31T10:00:00+08:00', '1d', official_days()))
        self.assertEqual(due_at('2026-11-09T10:00:00+08:00', 'd3', official_days()), '2026-11-12T15:00:00+08:00')

    def test_minutes_skip_lunch_not_overnight(self):
        self.assertEqual(due_at('2026-11-09T11:15:00+08:00', '30m', official_days()), '2026-11-09T13:15:00+08:00')
        self.assertEqual(due_at('2026-11-09T14:00:00+08:00', '60m', official_days()), '2026-11-09T15:00:00+08:00')
        self.assertIsNone(due_at('2026-11-09T14:31:00+08:00', '30m', official_days()))
        self.assertIsNone(due_at('2026-11-09T12:00:00+08:00', '30m', official_days()))

    def test_unknown_calendar_does_not_guess(self):
        self.assertIsNone(due_at('2026-11-09T10:00:00+08:00', '30m', []))
        self.assertIsNone(due_at('2026-11-07T10:00:00+08:00', '30m', official_days()))
        with self.assertRaises(ValueError):
            due_at('2026-11-09T10:00:00+08:00', '2h', official_days())

    def test_preserves_event_precision_and_fractional_lunch_duration(self):
        t='2026-11-09T11:15:00.500123+08:00'
        self.assertEqual(iso(stamp(t)),t)
        self.assertEqual(due_at(t,'30m',official_days()),'2026-11-09T13:15:00.500123+08:00')
        self.assertIsNone(due_at('2026-11-09T14:30:00.000001+08:00','30m',official_days()))
        self.assertEqual(iso('2026-11-09T10:00:00+08:00'),'2026-11-09T10:00:00+08:00')

    def test_full_annual_official_parser_and_audit_import(self):
        parsed=calendar.parse_official_document(self.document(),2026)
        self.assertEqual(set(parsed['closed_ranges']),set(calendar.HOLIDAYS))
        before=official_days(2026)
        result=calendar.import_calendar(2026,source=calendar.CALENDAR_SOURCE,verified=True,
                                        observed_at='2026-11-09T12:00:00+08:00',**parsed)
        self.assertEqual(result['status'],'ready')
        self.assertEqual(official_days(2026),before)
        saved=calendar.status()['years'][0]
        self.assertEqual(saved['source_document_hash'],parsed['source_document_hash'])
        self.assertEqual(saved['published_at'],'2025-12-18')
        self.assertEqual(len(saved['hash']),64)

    def test_incomplete_wrong_year_and_unknown_range_are_rejected(self):
        with self.assertRaises(ValueError):calendar.parse_official_document(self.document(missing='中秋节'),2026)
        with self.assertRaises(ValueError):calendar.parse_official_document(self.document(),2027)
        bad=self.document().replace('1月1日'.encode(),'2025年1月1日'.encode(),1)
        with self.assertRaises(ValueError):calendar.parse_official_document(bad,2026)
        bad=self.document().replace('至1月3日'.encode(),'、1月3日'.encode(),1)
        with self.assertRaises(ValueError):calendar.parse_official_document(bad,2026)
        with self.assertRaises(ValueError):calendar.parse_official_document(b'x'*(calendar.MAX_DOCUMENT_BYTES+1),2026)

    def test_import_requires_explicit_verification_and_official_origin(self):
        parsed=calendar.parse_official_document(self.document(),2026)
        for extra in ({'verified':False,'source':calendar.CALENDAR_SOURCE},
                      {'verified':True,'source':'https://example.com/calendar'},
                      {'verified':True,'source':'https://www.sse.com.cn.evil.test/disclosure/dealinstruc/closed/'},
                      {'verified':True,'source':calendar.CALENDAR_SOURCE+'?redirect=bad'}):
            with self.assertRaises(ValueError):calendar.import_calendar(2026,**parsed,**extra)
        self.assertEqual(calendar.status()['years'],[])

    def test_refresh_missing_next_year_retains_current_calendar(self):
        with patch.object(calendar,'_fetch_document',return_value=self.document()):
            ready=calendar.refresh_calendar(2026,asof='2026-11-09T12:00:00+08:00')
            pending=calendar.refresh_calendar(2027,asof='2026-11-09T12:00:00+08:00')
        self.assertEqual(ready['status'],'ready')
        saved=official_days(2026)
        self.assertEqual(pending['status'],'pending')
        self.assertEqual(official_days(2027),[])
        with patch.object(calendar,'_fetch_document',side_effect=TimeoutError):
            failed=calendar.refresh_calendar(2026,asof='2026-11-16T12:00:00+08:00')
        self.assertEqual(failed['status'],'pending')
        self.assertEqual(official_days(2026),saved)

    def test_refresh_follows_only_exact_annual_official_links(self):
        source='https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20261218_12345678.shtml'
        index=('<a href="https://evil.test/2027">2027年休市安排</a>'
               '<a href="c/c_20261218_12345678.shtml">2027年休市安排</a>').encode()
        def fetch(url):
            return index if url==calendar.CALENDAR_INDEX else self.document(2027)
        with patch.object(calendar,'_fetch_document',side_effect=fetch) as request:
            result=calendar.refresh_calendar(2027,asof='2026-12-20T12:00:00+08:00')
        self.assertEqual(result['status'],'ready')
        self.assertEqual(result['source'],source)
        self.assertEqual([c.args[0] for c in request.call_args_list],[calendar.CALENDAR_INDEX,source])
        self.assertTrue(official_days(2027))
        self.assertIsNotNone(due_at('2026-12-31T10:00:00+08:00','1d',calendar.calendar_window(2026)))

    def test_future_publication_not_imported(self):
        with patch.object(calendar,'_fetch_document',return_value=self.document(2027)):
            result=calendar.refresh_calendar(2027,asof='2026-11-09T12:00:00+08:00')
        self.assertEqual(result['status'],'pending')
        self.assertEqual(official_days(2027),[])


if __name__ == '__main__':
    unittest.main()
