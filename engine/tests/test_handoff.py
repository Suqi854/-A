"""Offline checks for final-state freezing and ownership handoff."""
import os
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["AQ5_DATA"] = tempfile.mkdtemp(dir=os.environ["AQ5_DATA"])
os.environ["AQ5_MOCK"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aq5 import backup, cloud, db, live, market, training  # noqa: E402
from aq5.config import CN_TZ, DATA  # noqa: E402


class HandoffTests(unittest.TestCase):
    timestamp = datetime(2026, 11, 9, 10, tzinfo=CN_TZ)
    config = {"enabled": True, "repo": "local-test", "remote": "unused", "last_sync": ""}

    def setUp(self):
        frozen = patch.object(db, "_writes_frozen", False)
        frozen.start()
        self.addCleanup(frozen.stop)
        state = patch.object(cloud, "STATE", {"role": "pc"})
        state.start()
        self.addCleanup(state.stop)
        db.init()
        with db.tx() as c:
            c.execute("DELETE FROM events")

    def test_freeze_waits_for_inflight_commit_and_rejects_later_writers(self):
        started = threading.Event()
        release = threading.Event()
        freeze_started = threading.Event()
        sealed = threading.Event()
        errors = []

        def write():
            try:
                with db.tx() as c:
                    c.execute("INSERT INTO events(kind,title) VALUES('test','inflight')")
                    started.set()
                    if not release.wait(2):
                        raise AssertionError("test did not release in-flight writer")
            except BaseException as e:
                errors.append(e)
            finally:
                db.conn().close()

        def freeze():
            freeze_started.set()
            db.freeze_writes()
            sealed.set()

        writer = threading.Thread(target=write)
        writer.start()
        freezer = threading.Thread(target=freeze)
        try:
            self.assertTrue(started.wait(2))
            freezer.start()
            self.assertTrue(freeze_started.wait(2))
            self.assertFalse(sealed.wait(0.05))
        finally:
            release.set()
            writer.join(2)
            if freezer.ident:
                freezer.join(2)
        self.assertEqual(errors, [])
        self.assertTrue(sealed.is_set())
        with self.assertRaises(db.StateFrozen):
            db.event("test", "after freeze")
        self.assertEqual(db.q("SELECT title FROM events"), [{"title": "inflight"}])

    def test_freeze_inside_a_write_scope_is_rejected(self):
        with db.tx() as c:
            with self.assertRaises(RuntimeError):
                db.freeze_writes()
            c.execute("INSERT INTO events(kind,title) VALUES('test','still writable')")
        self.assertFalse(db.writes_frozen())

    def test_final_call_requires_freeze_before_upload_boundary(self):
        with patch.object(cloud, "_cfg") as cfg:
            result = cloud.upload(active=False, final=True)
        self.assertFalse(result["ok"])
        cfg.assert_not_called()

    def test_failed_state_commit_cannot_report_success_or_push_old_head(self):
        def git(args, _cwd, **_kwargs):
            if args == ["remote"]:
                return 0, "origin"
            return (1, "") if args[0] == "commit" else (0, "")

        with patch.object(cloud, "_cfg", return_value=self.config), patch.object(cloud, "_key", return_value="fake"), \
                patch.object(cloud, "pack", return_value=b"synthetic-only"), patch.object(cloud, "_git", side_effect=git) as mocked:
            result = cloud.upload(active=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "状态提交失败")
        self.assertNotIn("push", [call.args[0][0] for call in mocked.call_args_list])

    def test_unverified_remote_cannot_package_or_reuse_an_old_origin(self):
        def git(args, _cwd, **_kwargs):
            if args == ["remote"]:
                return 0, "origin"
            return (1, "") if args[:2] == ["remote", "set-url"] else (0, "")

        with patch.object(cloud, "_cfg", return_value=self.config), patch.object(cloud, "_key", return_value="fake"), \
                patch.object(cloud, "pack") as pack, patch.object(cloud, "_git", side_effect=git) as mocked:
            result = cloud.upload(active=True)
        self.assertFalse(result["ok"])
        pack.assert_not_called()
        self.assertNotIn("push", [call.args[0][0] for call in mocked.call_args_list])

    def run_cloud(self, result):
        clock = {"elapsed": 0}
        calls = []

        def upload(*, active, final=False):
            calls.append(("final", active, final, db.writes_frozen()))
            self.assertTrue(db.q1("SELECT 1 FROM events WHERE title LIKE '云端代跑结束%'"))
            with self.assertRaises(db.StateFrozen):
                db.event("test", "late")
            if isinstance(result, Exception):
                raise result
            return result

        fake_time = SimpleNamespace(time=lambda: clock["elapsed"],
                                    sleep=lambda seconds: clock.__setitem__("elapsed", clock["elapsed"] + seconds))
        with ExitStack() as stack:
            stack.enter_context(patch.object(cloud, "time", fake_time))
            stack.enter_context(patch.object(cloud, "now", side_effect=lambda: self.timestamp + timedelta(seconds=clock["elapsed"])))
            stack.enter_context(patch.object(cloud, "remote_meta", return_value={"owner": "pc", "active": False}))
            stack.enter_context(patch.object(market, "is_trading_day", return_value=True))
            stack.enter_context(patch.object(cloud, "pc_alive", side_effect=[False, False, False, True]))
            stack.enter_context(patch.object(cloud, "_take_over", side_effect=lambda *_: calls.append(("take_over",))))
            stack.enter_context(patch.object(cloud, "upload", side_effect=upload))
            stack.enter_context(patch.object(live, "stop", side_effect=lambda: calls.append(("stop",))))
            stack.enter_context(patch.object(cloud, "dispatch_next", side_effect=lambda _: calls.append(("dispatch",))))
            rc = cloud.run_cloud(max_minutes=20)
        return rc, calls

    def test_confirmed_final_upload_precedes_next_runner(self):
        rc, calls = self.run_cloud({"ok": True})
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [("take_over",), ("stop",), ("final", False, True, True), ("dispatch",)])

    def test_failed_final_upload_never_dispatches_or_returns_success(self):
        rc, calls = self.run_cloud({"ok": False, "error": "synthetic network failure"})
        self.assertEqual(rc, 1)
        self.assertEqual(calls, [("take_over",), ("stop",), ("final", False, True, True)])
        self.assertTrue(db.writes_frozen())

    def test_exception_during_final_upload_is_unsuccessful(self):
        rc, calls = self.run_cloud(OSError("synthetic disk failure"))
        self.assertEqual(rc, 1)
        self.assertNotIn(("dispatch",), calls)

    def reconcile(self, metadata, *, upload_result=None, max_wait=0):
        with ExitStack() as stack:
            stack.enter_context(patch.object(cloud, "CLOUD", False))
            stack.enter_context(patch.object(cloud, "_cfg", return_value=self.config))
            stack.enter_context(patch.object(cloud, "_key", return_value="synthetic-test-only"))
            stack.enter_context(patch.object(cloud, "now", return_value=self.timestamp))
            stack.enter_context(patch.object(cloud, "time", SimpleNamespace(time=lambda: 0, sleep=lambda _: None)))
            stack.enter_context(patch.object(cloud, "remote_meta", side_effect=metadata))
            stack.enter_context(patch.object(backup, "db_snapshot", return_value=None))
            stack.enter_context(patch.object(cloud, "save_settings"))
            download = stack.enter_context(patch.object(cloud, "_gh_raw", return_value=b"synthetic"))
            unpack = stack.enter_context(patch.object(cloud, "unpack", return_value={"tables": 7}))
            upload = stack.enter_context(patch.object(cloud, "upload", return_value=upload_result or {"ok": True}))
            result = cloud.reconcile(max_wait=max_wait)
        return result, download, unpack, upload

    def meta(self, active, *, stale=False):
        t = self.timestamp - timedelta(hours=3) if stale else self.timestamp
        stamp = t.strftime("%Y-%m-%d %H:%M:%S")
        return {"owner": "cloud", "active": active, "heartbeat": stamp, "updated": stamp}

    def test_unfinished_cloud_owner_is_not_overwritten_after_timeout_or_stale_heartbeat(self):
        for metadata in [self.meta(True), self.meta(True, stale=True)]:
            with self.subTest(metadata=metadata):
                result, download, unpack, upload = self.reconcile([metadata])
                self.assertTrue(result["waiting"])
                self.assertIn("最终状态尚未确认", result["error"])
                for op in (download, unpack, upload):
                    op.assert_not_called()

    def test_active_cloud_metadata_disappearing_does_not_confirm_handoff(self):
        result, download, unpack, upload = self.reconcile([self.meta(True), None], max_wait=1)
        self.assertTrue(result["waiting"])
        for op in (download, unpack, upload):
            op.assert_not_called()

    def test_final_metadata_changing_during_download_blocks_import(self):
        result, download, unpack, upload = self.reconcile([self.meta(False), self.meta(True)])
        self.assertTrue(result["waiting"])
        download.assert_called_once_with("state.bin")
        unpack.assert_not_called()
        upload.assert_not_called()

    def test_unchanged_final_imports_but_owner_publish_must_succeed(self):
        meta = self.meta(False)
        result, download, unpack, upload = self.reconcile([meta, meta])
        self.assertEqual(result, {"pulled": True, "tables": 7})
        download.assert_called_once()
        unpack.assert_called_once_with(b"synthetic", take_settings=False)
        upload.assert_called_once_with(active=False)
        failure, _, _, _ = self.reconcile([meta, meta], upload_result={"ok": False})
        self.assertIn("未能发布", failure["error"])

    def test_cloud_replacement_cannot_import_predecessors_unfinished_ledger(self):
        with patch.object(cloud, "remote_meta", return_value=self.meta(True, stale=True)), \
                patch.object(cloud, "_gh_raw") as download, self.assertRaisesRegex(RuntimeError, "未完成交接"):
            cloud._take_over(None, None, None)
        download.assert_not_called()

    def test_frozen_encrypted_roundtrip_retains_new_tables_and_excludes_raw_samples(self):
        live.init()
        training.start_from_plan({"cash": 10.0, "holdings": [{"code": "sh600000", "qty": 100}]}, asof=self.timestamp)
        training.arm_paper(["balanced"])
        with db.tx() as c:
            c.execute("INSERT INTO intraday_samples VALUES('sh600000','2026-11-09T10:00:00+08:00','{}')")
            c.execute("INSERT INTO accounting_cash(account,date,kind,amount) VALUES('me','2026-11-09','funding',10)")
        db.freeze_writes()
        with patch.object(cloud, "_key", return_value="synthetic-roundtrip-only"):
            encrypted = cloud.pack()
            self.assertTrue(encrypted.startswith(b"AQ5v1"))
            target = DATA / "restored.sqlite3"
            with patch.object(db, "DB_PATH", target), patch.object(db, "_local", threading.local()), \
                    patch.object(db, "_writes_frozen", False):
                db.init()
                self.assertIsNone(db.q1("SELECT 1 FROM sqlite_master WHERE name='training_runs'"))
                restored = cloud.unpack(encrypted, take_settings=False)
                self.assertGreater(restored["tables"], 20)
                self.assertEqual(training.view()["experiments"]["balanced"]["mode"], "paper")
                self.assertEqual(db.q1("SELECT amount FROM accounting_cash"), {"amount": 10.0})
                self.assertEqual(db.q("SELECT * FROM intraday_samples"), [])
                self.assertTrue(db.q1("SELECT 1 FROM sqlite_master WHERE name='research_runs'"))
                db.conn().close()


if __name__ == "__main__":
    unittest.main()
