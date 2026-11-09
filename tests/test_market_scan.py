"""Offline checkpoint/cache contracts, including correct intended resume behavior.

No real HTTP or worker processes are used. Tests for refresh, artifact backfill,
empty-result persistence and fingerprint-aware exports intentionally assert the
required behavior rather than codifying the earlier implementation's defects.
"""
from concurrent.futures import Future
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import gzip
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import market_scan
from chanmodel.model import Config
from chanmodel.tdx_data import atomic_json, sha256


CN = timezone(timedelta(hours=8))
AS_OF = "2026-11-06T02:03:45+08:00"
RECORD = {"symbol": "sh600000", "exchange": "sh", "code": "600000", "name": "测试股票"}


def provider_history():
    data = {"list": [{"Time": "2026-11-05T00:00:00+08:00", "Open": 10000,
                      "High": 12000, "Low": 9000, "Close": 11000,
                      "Volume": 100, "Amount": 110000000}],
            "count": 1, "meta": {"source": "tdx", "type": "day"}}
    raw = json.dumps({"code": 0, "data": data}).encode()
    receipt = {"url": "http://offline/api/kline-all/tdx?code=sh600000&type=day",
               "requested_at": "2026-11-06T02:04:00+08:00",
               "received_at": "2026-11-06T02:04:00+08:00", "http_status": 200,
               "content_type": "application/json", "response_bytes": len(raw),
               "response_sha256": sha256(raw)}
    return data, receipt, raw


class InlinePool:
    """Future-compatible stand-in; executes submitted test doubles synchronously."""
    def __init__(self, max_workers):
        self.max_workers = max_workers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def submit(self, fn, *args):
        result = Future()
        try:
            result.set_result(fn(*args))
        except Exception as exc:
            result.set_exception(exc)
        return result


class MarketRunFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="market_resume_test_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run_dir = self.root / "run"
        self.cache_dir = self.root / "cache"
        self.run_dir.mkdir()
        self.settings = {"base_url": "http://offline", "timeout": 3, "retries": 0,
                         "source": "tdx", "as_of": AS_OF, "cache_dir": str(self.cache_dir),
                         "run_dir": str(self.run_dir), "config": Config().__dict__,
                         "engine_sha256": "current-engine", "full_reports": False, "refresh": False}
        self.metadata = {"started_at": AS_OF, "universe": [dict(RECORD)],
                         "coverage": {"selection": "explicit_symbols"}, "settings": dict(self.settings)}
        atomic_json(self.run_dir / "run.json", self.metadata)

    def args(self, **changes):
        values = {"run_dir": self.run_dir, "base_url": "http://offline", "timeout": 3,
                  "retries": 0, "source": "tdx", "stroke_mode": "loose", "max_depth": 3,
                  "resume": True, "cache_dir": self.cache_dir, "workers": 1,
                  "full_reports": False, "refresh": False, "skip_quotes": True,
                  "symbols": None, "limit": None, "stopfile": None}
        values.update(changes)
        return SimpleNamespace(**values)

    def success(self, engine="current-engine"):
        csv_path = self.cache_dir / "stock.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_bytes(b"closed_at,open,high,low,close\n2026-11-05T15:00:00+08:00,10,12,9,11\n")
        return {**RECORD, "status": "success", "bars": 1, "first_date": "2026-11-05",
                "last_date": "2026-11-05", "adjustment": "unadjusted", "source": "tdx",
                "csv_path": str(csv_path), "csv_sha256": sha256(csv_path.read_bytes()),
                "current_state": {"state": "untradable_unclear"}, "active_candidate_counts": {},
                "active_candidate_count": 0, "engine_sha256": engine}

    def seed(self, result, engine="current-engine"):
        db = market_scan.checkpoint(self.run_dir / "checkpoint.sqlite3")
        db.execute("INSERT OR REPLACE INTO stocks VALUES (?,?,?,?,?)",
                   (result["symbol"], result["status"], engine, json.dumps(result), AS_OF))
        db.commit()
        db.close()
        atomic_json(self.run_dir / "stocks" / f"{result['symbol']}.json", result)

    def invoke(self, args, returned):
        with patch("market_scan.engine_fingerprint", return_value="current-engine"), \
             patch("market_scan.TdxClient") as client, \
             patch("market_scan.ProcessPoolExecutor", InlinePool), \
             patch("market_scan.process_stock", return_value=returned) as worker, \
             redirect_stdout(io.StringIO()):
            result = market_scan.run(args)
        client.return_value.universe.assert_not_called()
        return result, worker


class ResumeCheckpointTests(MarketRunFixture):
    def test_valid_success_is_reused_with_same_fixed_cutoff(self):
        result = self.success()
        self.seed(result)
        status, worker = self.invoke(self.args(), result)
        self.assertEqual(status, 0)
        worker.assert_not_called()
        metadata = json.loads((self.run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["settings"]["as_of"], AS_OF)

    def test_corrupted_normalized_csv_is_reprocessed(self):
        result = self.success()
        self.seed(result)
        Path(result["csv_path"]).write_bytes(b"corrupted")
        _, worker = self.invoke(self.args(), result)
        worker.assert_called_once()
        self.assertEqual(worker.call_args.args[1]["as_of"], AS_OF)

    def test_missing_stock_result_is_reprocessed(self):
        result = self.success()
        self.seed(result)
        (self.run_dir / "stocks" / f"{RECORD['symbol']}.json").unlink()
        _, worker = self.invoke(self.args(), result)
        worker.assert_called_once()

    def test_old_engine_result_is_reprocessed(self):
        result = self.success("old-engine")
        self.seed(result, "old-engine")
        _, worker = self.invoke(self.args(), self.success())
        worker.assert_called_once()

    def test_failed_result_is_retried_instead_of_counted_complete(self):
        self.seed({**RECORD, "status": "failed", "error": "upstream timeout"})
        _, worker = self.invoke(self.args(), self.success())
        worker.assert_called_once()

    def test_resume_rejects_different_source_or_cache_directory(self):
        with patch("market_scan.TdxClient"), self.assertRaisesRegex(ValueError, "settings differ"):
            market_scan.run(self.args(source="ths"))
        with patch("market_scan.TdxClient"), self.assertRaisesRegex(ValueError, "cache directory"):
            market_scan.run(self.args(cache_dir=self.root / "other-cache"))

    def test_refresh_reprocesses_even_valid_success(self):
        result = self.success()
        self.seed(result)
        _, worker = self.invoke(self.args(refresh=True), result)
        worker.assert_called_once()

    def test_full_report_option_backfills_missing_details_for_prior_success(self):
        result = self.success()
        self.seed(result)
        self.assertFalse((self.run_dir / "details" / f"{RECORD['symbol']}.json.gz").exists())
        _, worker = self.invoke(self.args(full_reports=True), result)
        worker.assert_called_once()

    def test_corrupted_stock_result_is_not_reused_merely_because_file_exists(self):
        result = self.success()
        self.seed(result)
        (self.run_dir / "stocks" / f"{RECORD['symbol']}.json").write_text("corrupted", encoding="utf-8")
        _, worker = self.invoke(self.args(), result)
        worker.assert_called_once()

    def test_existing_stop_sentinel_submits_nothing_and_exits_paused(self):
        (self.run_dir / "STOP").write_text("stop", encoding="utf-8")
        result = self.success()
        status, worker = self.invoke(self.args(), result)
        self.assertEqual(status, 3)
        worker.assert_not_called()
        summary = json.loads((self.run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertTrue(summary["paused"])
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["processed"], 0)

    def test_custom_stopfile_is_honored(self):
        stopfile = self.root / "custom.pause"
        stopfile.write_text("stop", encoding="utf-8")
        status, worker = self.invoke(self.args(stopfile=stopfile), self.success())
        self.assertEqual(status, 3)
        worker.assert_not_called()
        summary = json.loads((self.run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["stopfile"], str(stopfile.resolve()))

    def test_stop_during_work_drains_submitted_work_without_submitting_rest(self):
        records = [{**RECORD, "symbol": f"sh60000{index}", "code": f"60000{index}"} for index in range(5)]
        metadata = {**self.metadata, "universe": records}
        atomic_json(self.run_dir / "run.json", metadata)
        template = self.success()

        def process(record, settings):
            (self.run_dir / "STOP").write_text("pause", encoding="utf-8")
            return {**template, **record}

        with patch("market_scan.engine_fingerprint", return_value="current-engine"), \
             patch("market_scan.TdxClient"), \
             patch("market_scan.ProcessPoolExecutor", InlinePool), \
             patch("market_scan.process_stock", side_effect=process) as worker, \
             redirect_stdout(io.StringIO()):
            status = market_scan.run(self.args())
        self.assertEqual(status, 3)
        worker.assert_called_once()
        summary = json.loads((self.run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual((summary["processed"], summary["pending"]), (1, 4))
        self.assertTrue(summary["paused"])

    def test_submitted_queue_is_bounded_to_twice_workers(self):
        records = [{**RECORD, "symbol": f"sh60000{index}", "code": f"60000{index}"} for index in range(7)]
        atomic_json(self.run_dir / "run.json", {**self.metadata, "universe": records})
        template = self.success()
        original_wait = market_scan.wait
        queue_sizes = []

        def observed_wait(futures, **options):
            queue_sizes.append(len(futures))
            return original_wait(futures, **options)

        with patch("market_scan.engine_fingerprint", return_value="current-engine"), \
             patch("market_scan.TdxClient"), \
             patch("market_scan.ProcessPoolExecutor", InlinePool), \
             patch("market_scan.process_stock", side_effect=lambda record, settings: {**template, **record}) as worker, \
             patch("market_scan.wait", side_effect=observed_wait), \
             redirect_stdout(io.StringIO()):
            status = market_scan.run(self.args(workers=2))
        self.assertEqual(status, 0)
        self.assertEqual(worker.call_count, 7)
        self.assertTrue(queue_sizes)
        self.assertEqual(max(queue_sizes), 4)


class WorkerCacheTests(MarketRunFixture):
    def test_identical_run_reuses_verified_raw_cache_without_network(self):
        with patch("market_scan.TdxClient") as client:
            client.return_value.history.return_value = provider_history()
            first = market_scan.process_stock(RECORD, self.settings)
            second = market_scan.process_stock(RECORD, self.settings)
        self.assertEqual(first["status"], "success")
        self.assertEqual(second["status"], "success")
        self.assertTrue(second["cache_hit"])
        client.return_value.history.assert_called_once_with("sh600000", "tdx")
        self.assertEqual(first["csv_sha256"], second["csv_sha256"])

    def test_changed_cutoff_forces_new_provider_response(self):
        with patch("market_scan.TdxClient") as client:
            client.return_value.history.return_value = provider_history()
            market_scan.process_stock(RECORD, self.settings)
            modified = {**self.settings, "as_of": "2026-11-06T02:10:00+08:00"}
            result = market_scan.process_stock(RECORD, modified)
        self.assertEqual(client.return_value.history.call_count, 2)
        self.assertFalse(result["cache_hit"])

    def test_invalid_data_raw_evidence_is_saved_and_failure_result_overwrites_success(self):
        previous = self.success()
        atomic_json(self.run_dir / "stocks" / f"{RECORD['symbol']}.json", previous)
        data, receipt, _ = provider_history()
        data["list"][0]["High"] = 5000
        raw = json.dumps({"code": 0, "data": data}).encode()
        receipt.update(response_sha256=sha256(raw), response_bytes=len(raw))
        with patch("market_scan.TdxClient") as client:
            client.return_value.history.return_value = data, receipt, raw
            result = market_scan.process_stock(RECORD, self.settings)
        self.assertEqual(result["status"], "failed")
        evidence = self.run_dir / "responses" / f"{RECORD['symbol']}.json.gz"
        self.assertEqual(gzip.decompress(evidence.read_bytes()), raw)
        persisted = json.loads((self.run_dir / "stocks" / f"{RECORD['symbol']}.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["status"], "failed")

    def test_empty_history_result_replaces_stale_success_artifact(self):
        atomic_json(self.run_dir / "stocks" / f"{RECORD['symbol']}.json", self.success())
        data, receipt, _ = provider_history()
        data["list"] = []
        data["count"] = 0
        raw = json.dumps({"code": 0, "data": data}).encode()
        receipt.update(response_sha256=sha256(raw), response_bytes=len(raw))
        with patch("market_scan.TdxClient") as client:
            client.return_value.history.return_value = data, receipt, raw
            result = market_scan.process_stock(RECORD, self.settings)
        self.assertEqual(result["status"], "empty_history")
        persisted = json.loads((self.run_dir / "stocks" / f"{RECORD['symbol']}.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["status"], "empty_history")

    def test_failed_refresh_quarantines_previous_full_detail(self):
        detail = self.run_dir / "details" / f"{RECORD['symbol']}.json.gz"
        detail.parent.mkdir(parents=True)
        old = gzip.compress(b'{"old":"successful detail"}')
        detail.write_bytes(old)
        with patch("market_scan.TdxClient") as client:
            client.return_value.history.side_effect = RuntimeError("provider unavailable")
            result = market_scan.process_stock(RECORD, {**self.settings, "refresh": True})
        self.assertEqual(result["status"], "failed")
        self.assertFalse(detail.exists())
        quarantined = Path(result["quarantined_detail"])
        self.assertTrue(quarantined.resolve().is_relative_to((self.run_dir / "quarantine").resolve()))
        self.assertEqual(quarantined.read_bytes(), old)


class ExportSemanticsTests(MarketRunFixture):
    def test_current_snapshot_counts_active_separately_from_all_historical_candidates(self):
        result = self.success()
        result.update(active_candidate_count=1, all_historical_candidate_count=8,
                      active_candidate_counts={"third_buy_candidate": 1})
        self.seed(result)
        db = market_scan.checkpoint(self.run_dir / "checkpoint.sqlite3")
        self.addCleanup(db.close)
        summary = market_scan.exports(db, self.run_dir, self.metadata)
        self.assertEqual(summary["active_candidate_counts"], {"third_buy_candidate": 1})
        self.assertEqual(summary["total_history_bars"], 1)
        self.assertFalse(summary["history_inception_complete"])
        self.assertFalse(summary["historical_delisted_universe_complete"])
        self.assertIn("no historical event replay", summary["signal_semantics"])

    def test_old_engine_success_is_pending_until_current_engine_finishes(self):
        self.seed(self.success("old-engine"), "old-engine")
        db = market_scan.checkpoint(self.run_dir / "checkpoint.sqlite3")
        self.addCleanup(db.close)
        summary = market_scan.exports(db, self.run_dir, self.metadata)
        self.assertEqual(summary["success"], 0)
        self.assertEqual(summary["pending"], 1)
        self.assertFalse(summary["all_symbols_finished"])

    def test_checkpoint_rows_outside_recorded_universe_do_not_pollute_summary(self):
        self.seed(self.success())
        alien = {**self.success(), "symbol": "sz000001", "exchange": "sz", "code": "000001"}
        self.seed(alien)
        db = market_scan.checkpoint(self.run_dir / "checkpoint.sqlite3")
        self.addCleanup(db.close)
        summary = market_scan.exports(db, self.run_dir, self.metadata)
        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["success"], 1)
        self.assertEqual(summary["pending"], 0)
        self.assertEqual(summary["total_history_bars"], 1)


if __name__ == "__main__":
    unittest.main()
