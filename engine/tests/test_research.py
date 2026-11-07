"""离线验收学习时点、候选数据覆盖、影子验证及显式启用。"""
import os
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ["AQ5_MOCK"] = "1"
os.environ["AQ5_DATA"] = os.environ["AQ5_TEST_DATA"]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aq5 import db, factors, fundamentals, learn, market, research  # noqa: E402
from aq5.config import CN_TZ  # noqa: E402


def dates(n):
    start = date(2026, 7, 1)
    return [(start + timedelta(days=i)).isoformat() for i in range(n * 2)
            if (start + timedelta(days=i)).weekday() < 5][:n]


class ResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        research.init()
        fundamentals.init()

    def setUp(self):
        with db.tx() as c:
            for table in ("samples", "picks", "research_label_windows", "research_samples",
                          "research_runs", "research_disclosures", "research_controls",
                          "research_factor_state", "models"):
                c.execute(f"DELETE FROM {table}")

    def seed(self, conn, day, code="sh600000", *, run=0, session="close", ret=None, f=None, score=70):
        return conn.execute("""INSERT INTO samples(run_id,date,session,code,price,score,factors,
            strategies,ret_3d,evaluated) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                            (run, day, session, code, 10, score, db.dumps(f or {}), "[]", ret,
                             int(ret is not None))).lastrowid

    def test_only_completed_labels_and_no_skip_missing_day(self):
        d = dates(6)
        with db.tx() as c:
            self.seed(c, d[0])
            self.seed(c, d[0], "sh600001")
            legacy = self.seed(c, d[0], "sh600002", ret=77)
            self.seed(c, "2020-01-01", "sh600003")
        rows = [{"date": x, "close": i + 10, "high": i + 11, "low": i + 9} for i, x in enumerate(d)]

        def kline(code, *_a, **_kw):
            return [r for r in rows if code != "sh600001" or r["date"] != d[1]]

        noon = datetime.fromisoformat(d[-1] + "T12:00:00").replace(tzinfo=CN_TZ)
        with patch.object(market, "kline", side_effect=kline), patch.object(learn, "now", return_value=noon):
            learn.fill_outcomes()
        first = db.q1("SELECT * FROM samples WHERE code='sh600000'")
        self.assertEqual(first["ret_3d"], 30)
        self.assertIsNone(first["ret_5d"])
        self.assertFalse(first["evaluated"])
        self.assertFalse(db.q("SELECT * FROM research_label_windows"))
        with patch.object(market, "kline", side_effect=kline), patch.object(learn, "now", return_value=noon.replace(hour=15, minute=30)):
            learn.fill_outcomes()
        self.assertEqual(db.q1("SELECT ret_5d FROM samples WHERE code='sh600000'")["ret_5d"], 50)
        self.assertEqual(db.q1("SELECT end_5d FROM research_label_windows")["end_5d"], d[5])
        self.assertIsNone(db.q1("SELECT ret_3d FROM samples WHERE code='sh600001'")["ret_3d"])
        self.assertEqual(db.q1("SELECT ret_3d FROM samples WHERE id=?", (legacy,))["ret_3d"], 77)
        self.assertIsNone(db.q1("SELECT ret_3d FROM samples WHERE code='sh600003'")["ret_3d"])

    def test_nonmock_cache_half_bar_is_not_a_closed_bar(self):
        rows = [{"date": "2026-07-02", "close": 10}, {"date": "2026-07-03", "close": 20}]
        with db.tx() as c:
            c.execute("INSERT OR REPLACE INTO kline_meta VALUES('sh600000','2026-07-03 11:00',120)")
        with patch.object(market, "MOCK", False), patch.object(market, "kline", return_value=rows):
            cutoff = datetime(2026, 7, 6, 16, tzinfo=CN_TZ)
            self.assertEqual(learn._closed_rows("sh600000", cutoff), rows[:1])

    def test_train_isolates_dates_sources_and_statistics(self):
        d = dates(30)
        with db.tx() as c:
            for i in range(25):
                run = i + 1
                c.execute("INSERT INTO research_runs VALUES(?,?,?,?)", (run, d[i], research.VERSION, "{}"))
                for j in range(10):
                    sign = 1 if j % 2 else -1
                    features = {"momentum": sign, "trend": 10000 if i >= 20 else 0}
                    sid = self.seed(c, d[i], run=run, ret=sign * 3, f=features)
                    c.execute("INSERT INTO research_label_windows VALUES(?,?,?)", (sid, d[i + 3], d[i + 5]))
                # 即使带有时点记录，历史回放会被排除。
                sid = self.seed(c, d[i], run=run, session="history", ret=-5, f={"trend": 99999})
                c.execute("INSERT INTO research_label_windows VALUES(?,?,?)", (sid, d[i + 3], d[i + 5]))
                self.seed(c, d[i], ret=11, f={"trend": 99999})  # 未登记的旧线上样本
        self.assertEqual(len(learn._mature_live()), 250)
        tr = learn.train()
        self.assertTrue(tr["trained"], tr)
        self.assertTrue(tr["metrics"]["usable"], tr)
        self.assertLess(tr["metrics"]["train_label_end"], tr["metrics"]["test_start"])
        self.assertEqual(tr["metrics"]["train"], 150)
        self.assertEqual(tr["metrics"]["test"], 50)
        model = db.q1("SELECT version, coef FROM models")
        self.assertEqual(db.loads(model["coef"])["mu"][factors.KEYS.index("trend")], 0)
        self.assertEqual(learn.predict({"momentum": 1}, [], 70)[1], "model")
        learn.select_model(0)
        self.assertEqual(learn.predict({"momentum": 1}, [], 70)[1], "heuristic")
        learn.select_model(model["version"])
        self.assertEqual(learn.predict({"momentum": 1}, [], 70)[1], "model")

    def test_many_rows_one_date_do_not_qualify(self):
        with db.tx() as c:
            c.execute("INSERT INTO research_runs VALUES(1,'2026-07-01',?,'{}')", (research.VERSION,))
            for i in range(220):
                sid = self.seed(c, "2026-07-01", run=1, ret=i % 3)
                c.execute("INSERT INTO research_label_windows VALUES(?,?,?)", (sid, "2026-07-06", "2026-07-08"))
        self.assertFalse(learn.train()["trained"])
        self.assertIsNone(db.q1("SELECT version FROM models"))

    def test_disclosure_asof_source_and_distinct_boards(self):
        quotes = [
            {"code": "sh600001", "name": "甲", "price": 10, "prev": 10, "pct": 1, "amount": 300, "pe": 12},
            {"code": "sh600002", "name": "乙", "price": 10, "prev": 10, "pct": 2, "amount": 200},
            {"code": "sh600003", "name": "丙", "price": 10, "prev": 10, "pct": -1, "amount": 100, "pe": 50},
        ]
        r = {"code": "sh600001", "period_end": "2026-06-30", "published_at": "2026-07-20T09:00:00+08:00",
             "source_url": "https://www.cninfo.com.cn/new/disclosure/mock", "basis": "ttm_cny",
             "roe": 0.2, "debt_ratio": 0.4, "revenue_ttm": 1e9, "profit_ttm": 1e8, "operating_cashflow_ttm": 1.1e8}
        self.assertEqual(fundamentals.import_disclosures([r])["inserted"], 1)
        self.assertEqual(fundamentals.import_disclosures([r])["inserted"], 0)
        with self.assertRaises(ValueError):
            fundamentals.import_disclosures([dict(r, source_url="https://cninfo.com.cn.example/mock")])
        with self.assertRaises(ValueError):
            fundamentals.import_disclosures([dict(r, profit_ttm=2e8)])
        early, _ = fundamentals.screen(quotes, "2026-07-20T08:00:00+08:00")
        self.assertEqual(early["status"], "unavailable")
        report, obs = research.screen(quotes, "2026-07-20T09:00:00+08:00", {q["code"]: "测试行业" for q in quotes})
        self.assertEqual(report["value_quality"]["status"], "partial")
        self.assertFalse(report["value_quality"]["full_industry_screen"])
        self.assertEqual(report["value_quality"]["candidates"][0]["code"], "sh600001")
        self.assertIn("industry_leadership", obs["sh600002"])
        self.assertNotIn("disclosure_quality", obs["sh600002"])
        raw, ind = factors.raw_factors(market.mock.kline("sh600001", 80), {})
        self.assertEqual(raw["value"], 0.6)
        self.assertFalse(ind["extra"]["pe_available"])

    def test_public_annual_disclosures_and_three_quote_pool(self):
        # 年报中均为合并口径，负债率由合并负债除以合并资产；报告日期来自公告链接。
        # 行情采用 2026-11-06 同一公开页面服务的收盘价和 PE(TTM)，用于复现接口。
        records = [
            {"code": "sh600519", "period_end": "2025-12-31", "published_at": "2026-04-17",
             "source_url": "https://static.cninfo.com.cn/finalpage/2026-04-17/1225114741.PDF",
             "basis": "ttm_cny", "roe": 0.3253, "debt_ratio": 49875590112.37 / 303834844021.44,
             "revenue_ttm": 168838102514.79, "profit_ttm": 82320067101.68, "operating_cashflow_ttm": 61522204989.35},
            {"code": "sh601088", "period_end": "2025-12-31", "published_at": "2026-03-31",
             "source_url": "https://static.cninfo.com.cn/finalpage/2026-03-31/1225064293.PDF",
             "basis": "ttm_cny", "roe": 0.1276, "debt_ratio": 146310 / 627761,
             "revenue_ttm": 294916e6, "profit_ttm": 52849e6, "operating_cashflow_ttm": 75059e6},
        ]
        quote_base = "https://www.zhunshangshi.com/caiwu/?ts_code="
        quotes = [
            {"code": "sh600519", "name": "贵州茅台", "price": 1453.00, "pe": 22.11, "source": quote_base + "600519.SH"},
            {"code": "sh601088", "name": "中国神华", "price": 48.54, "pe": 19.33, "source": quote_base + "601088.SH"},
            {"code": "sh600036", "name": "招商银行", "price": 42.88, "pe": 7.07, "source": quote_base + "600036.SH"},
        ]
        self.assertEqual(fundamentals.import_disclosures(records)["inserted"], 2)
        same_day, _ = fundamentals.screen([quotes[0]], "2026-04-17T15:00:00+08:00")
        self.assertEqual(same_day["disclosure_count"], 0)
        asof = "2026-11-06T15:01:00+08:00"
        report, _ = research.screen(quotes, asof, {})
        value = report["value_quality"]
        self.assertEqual(value["universe_count"], 3)
        self.assertEqual(value["disclosure_count"], 2)
        self.assertEqual(value["valuation_count"], 2)
        self.assertFalse(value["full_industry_screen"])
        by_code = {x["code"]: x for x in value["candidates"]}
        self.assertEqual(set(by_code), {"sh600519", "sh601088"})
        self.assertEqual(by_code["sh600519"]["available_at"], "2026-04-18T00:00:00+08:00")
        self.assertEqual(by_code["sh601088"]["period_end"], "2025-12-31")

    def test_preview_uses_provider_time_and_does_not_create_selection(self):
        quotes = [
            {"code": "sh600001", "name": "甲", "price": 10, "prev": 9.9, "pct": 1, "amount": 120,
             "time": "20261106150000"},
            {"code": "sh600002", "name": "乙", "price": 10, "prev": 10, "pct": 0, "amount": 100},
        ]
        tables = ("pick_runs", "picks", "samples", "research_runs", "research_label_windows")
        before = [db.q1(f"SELECT COUNT(*) AS n FROM {table}")["n"] for table in tables]
        with patch.object(market, "snapshot", return_value=quotes) as source, patch.object(
            research, "timestamp", return_value="2026-11-07T10:30:00+08:00"
        ):
            result = research.preview()
            source.assert_called_once_with()
        self.assertEqual(result["kind"], "research_preview")
        self.assertTrue(result["not_new_selection"])
        self.assertEqual(result["asof"], "2026-11-06T15:00:00+08:00")
        self.assertEqual(result["value_quality"]["asof"], result["asof"])
        self.assertEqual(result["generated_at"], "2026-11-07T10:30:00+08:00")
        self.assertEqual(result["quote_time_status"], "partial")
        self.assertEqual(result["short_term"]["quote_count"], 2)
        self.assertEqual([db.q1(f"SELECT COUNT(*) AS n FROM {table}")["n"] for table in tables], before)

    def test_preview_keeps_untimed_quote_unknown_and_excludes_future(self):
        untimed = {"code": "sh600001", "price": 10, "pe": 10}
        future = {"code": "sh600002", "price": 10, "pe": 10, "time": "20261106151000"}
        with patch.object(research, "timestamp", return_value="2026-11-07T10:30:00+08:00"):
            result = research.preview([untimed, future], "2026-11-06T15:00:00+08:00")
        self.assertIsNone(result["asof"])
        self.assertEqual(result["quote_time_status"], "unknown")
        self.assertEqual(result["short_term"]["snapshot_count"], 1)
        self.assertEqual(result["value_quality"]["asof"], "2026-11-06T15:00:00+08:00")

    def test_promotion_gated_on_frozen_out_of_sample_groups(self):
        with self.assertRaises(ValueError):
            research.promote_factor("short_strength", research.VERSION)
        d = dates(50)
        with db.tx() as c:
            for i in range(45):
                run = i + 1
                c.execute("INSERT INTO research_runs VALUES(?,?,?,?)", (run, d[i], research.VERSION, "{}"))
                for j in range(20):
                    code = f"sh600{j:03d}"
                    candidate = j >= 10
                    sid = self.seed(c, d[i], code, run=run, ret=4 if candidate else -2)
                    c.execute("INSERT INTO research_label_windows VALUES(?,?,?)", (sid, d[i + 3], d[i + 5]))
                    c.execute("INSERT INTO research_samples VALUES(?,?,?,?,?)",
                              (run, code, 69 if candidate else 70, j + 1,
                               db.dumps({"short_strength": 100 if candidate else 0})))
        result = research.shadow_report("short_strength")["factors"]["short_strength"]
        self.assertTrue(result["eligible"], result)
        self.assertEqual(research.active_factors(), set())
        research.promote_factor("short_strength", research.VERSION)
        self.assertEqual(research.active_factors(), {"short_strength"})
        research.promote_factor("short_strength", research.VERSION, enabled=False)
        self.assertEqual(research.active_factors(), set())
        with db.tx() as c:
            c.execute("DELETE FROM research_label_windows WHERE sample_id IN (SELECT id FROM samples WHERE code='sh600010')")
        result = research.shadow_report("short_strength")["factors"]["short_strength"]
        self.assertFalse(result["eligible"], result)


if __name__ == "__main__":
    unittest.main()
