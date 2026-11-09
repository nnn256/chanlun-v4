"""TDX adapter contracts tested entirely offline with fixed provider fixtures.

Fixtures use the Go API's integer protocol units, not already-normalized
floating prices. The tests deliberately distinguish daily Amount (milli-yuan)
from quote Amount (yuan), and a receiver timestamp from a trade timestamp.
"""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest
from unittest.mock import patch

from chanmodel.tdx_data import (
    TdxClient, TdxError, normalize_daily, normalize_universe,
    normalized_quotes, symbol,
)


CN = timezone(timedelta(hours=8))


def daily_row(day="2026-01-05", **changes):
    row = {"Time": day + "T00:00:00+08:00", "Open": 12340,
           "High": 12800, "Low": 12000, "Close": 12560,
           "Volume": 125.5, "Amount": 157630000}
    row.update(changes)
    return row


def history(rows, **changes):
    result = {"list": rows, "count": len(rows),
              "meta": {"source": "tdx", "type": "day"}}
    result.update(changes)
    return result


def quote_row(**changes):
    row = {"Exchange": 1, "Code": "600000", "ServerTime": 101530,
           "K": {"Open": 12340, "High": 12800, "Low": 12000,
                 "Close": 12560, "Last": 12400},
           "TotalHand": 125.5, "Amount": 157630}
    row.update(changes)
    return row


class _Response:
    def __init__(self, value, status=200, content_type="application/json"):
        self.raw = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
        self.status = status
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.raw


class DailyNormalizationTests(unittest.TestCase):
    def test_explicit_zero_count_null_list_is_empty_history(self):
        from datetime import datetime, timezone, timedelta
        bars, rows, quality = normalize_daily({'count':0,'list':None,'meta':{'source':'tdx','type':'day'}}, datetime(2026,11,6,tzinfo=timezone(timedelta(hours=8))))
        self.assertEqual((bars,rows),([],[]))
        self.assertEqual(quality['closed_rows'],0)
    def test_protocol_prices_lots_and_daily_amount_are_normalized_once(self):
        bars, rows, quality = normalize_daily(history([daily_row()]),
                                               datetime(2026, 1, 5, 15, 0, tzinfo=CN))
        self.assertEqual(len(bars), 1)
        self.assertEqual((bars[0].open, bars[0].high, bars[0].low, bars[0].close),
                         (12.34, 12.8, 12.0, 12.56))
        self.assertEqual(bars[0].volume, 12550)
        self.assertEqual(rows[0]["volume_lots"], 125.5)
        self.assertEqual(rows[0]["amount_yuan"], 157630)
        self.assertEqual(rows[0]["closed_at"], "2026-01-05T15:00:00+08:00")
        self.assertEqual(quality["price_unit"], "CNY")
        self.assertEqual(quality["volume_unit"], "shares")
        self.assertEqual(quality["adjustment"], "unadjusted")

    def test_open_daily_bar_is_preserved_as_evidence_but_excluded_from_analysis(self):
        payload = history([daily_row("2026-01-02"), daily_row("2026-01-05")])
        bars, rows, quality = normalize_daily(payload,
                                              datetime(2026, 1, 5, 6, 59, tzinfo=timezone.utc))
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["is_closed"] for r in rows], [True, False])
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].closed_at, "2026-01-02T15:00:00+08:00")
        self.assertEqual(quality["open_rows_excluded"], 1)
        closed, _, _ = normalize_daily(payload, datetime(2026, 1, 5, 7, 0, tzinfo=timezone.utc))
        self.assertEqual(len(closed), 2)

    def test_unsorted_daily_rows_are_sorted_and_reindexed(self):
        payload = history([daily_row("2026-01-06"), daily_row("2026-01-02"), daily_row("2026-01-05")])
        bars, rows, quality = normalize_daily(payload, datetime(2026, 1, 6, 16, tzinfo=CN))
        self.assertEqual([r["trade_date"] for r in rows], ["2026-01-02", "2026-01-05", "2026-01-06"])
        self.assertEqual([bar.pos for bar in bars], [0, 1, 2])
        self.assertEqual(quality["input_order"], "reordered")

    def test_identical_duplicates_are_removed_but_conflicts_fail(self):
        row = daily_row()
        bars, _, quality = normalize_daily(history([row, copy.deepcopy(row)]),
                                            datetime(2026, 1, 5, 16, tzinfo=CN))
        self.assertEqual(len(bars), 1)
        self.assertEqual(quality["identical_duplicates_removed"], 1)
        conflicting = copy.deepcopy(row)
        conflicting["Close"] = 12600
        with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
            normalize_daily(history([row, conflicting]), datetime(2026, 1, 5, 16, tzinfo=CN))

    def test_invalid_ohlc_volume_and_nonfinite_values_fail(self):
        invalid = [
            {"High": 12000}, {"Low": 12600}, {"Open": 0},
            {"Close": float("nan")}, {"High": float("inf")},
            {"Volume": -1}, {"Volume": float("nan")}, {"Volume": True},
            {"Amount": -1000}, {"Open": True},
        ]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                normalize_daily(history([daily_row(**changes)]), datetime(2026, 1, 5, 16, tzinfo=CN))

    def test_future_session_and_missing_time_are_not_silently_accepted(self):
        with self.assertRaisesRegex(ValueError, "future session"):
            normalize_daily(history([daily_row("2026-01-06")]), datetime(2026, 1, 5, 16, tzinfo=CN))
        with self.assertRaisesRegex(ValueError, "daily Time"):
            normalize_daily(history([daily_row(Time="not-a-date")]), datetime(2026, 1, 5, 16, tzinfo=CN))
        with self.assertRaisesRegex(ValueError, "explicit timezone"):
            normalize_daily(history([daily_row()]), datetime(2026, 1, 5, 16))

    def test_count_source_and_period_mismatches_fail(self):
        as_of = datetime(2026, 1, 5, 16, tzinfo=CN)
        for payload in [history([daily_row()], count=2),
                        history([daily_row()], meta={"source": "ths", "type": "day"}),
                        history([daily_row()], meta={"source": "tdx", "type": "5m"})]:
            with self.subTest(payload=payload), self.assertRaises(TdxError):
                normalize_daily(payload, as_of)

    def test_ths_current_vintage_adjustment_is_explicit(self):
        payload = history([daily_row()], meta={"source": "ths", "type": "day"})
        _, _, quality = normalize_daily(payload, datetime(2026, 1, 5, 16, tzinfo=CN), source="ths")
        self.assertEqual(quality["adjustment"], "qfq_current_vintage")
        self.assertIn("not verified", quality["coverage_claim"])


class UniverseNormalizationTests(unittest.TestCase):
    def test_all_exchanges_exclude_etfs_indexes_b_shares_and_deduplicate(self):
        rows = [
            {"Exchange": "sh", "Code": "600000", "Name": "上海股票"},
            {"exchange": "sz", "code": "000001", "name": "深圳股票"},
            {"exchange": "sz", "code": "301001", "name": "创业板"},
            {"exchange": "bj", "code": "920001", "name": "北京股票"},
            {"exchange": "bj", "code": "430047", "name": "北京旧代码"},
            {"exchange": "sh", "code": "688001", "name": "科创板"},
            {"exchange": "sh", "code": "510300", "name": "ETF"},
            {"exchange": "sz", "code": "159919", "name": "ETF"},
            {"exchange": "sh", "code": "000001", "name": "指数"},
            {"exchange": "sz", "code": "399001", "name": "指数"},
            {"exchange": "sh", "code": "900901", "name": "B股"},
            {"exchange": "sz", "code": "200001", "name": "B股"},
        ]
        rows.append(copy.deepcopy(rows[0]))
        selected, quality, exclusions = normalize_universe({"Codes": rows, "Total": len(rows)})
        self.assertEqual([item["symbol"] for item in selected],
                         ["bj430047", "bj920001", "sh600000", "sh688001", "sz000001", "sz301001"])
        self.assertEqual(quality["exchanges"], {"sh": 2, "sz": 2, "bj": 2})
        self.assertTrue(quality["complete_exchange_presence"])
        self.assertFalse(quality["historical_universe_complete"])
        self.assertEqual(len(exclusions), 6)

    def test_missing_exchange_is_reported_and_conflicting_names_fail(self):
        row = {"exchange": "sh", "code": "600000", "name": "原名称"}
        selected, quality, _ = normalize_universe({"codes": [row], "total": 1})
        self.assertEqual(quality["missing_exchanges"], ["sz", "bj"])
        self.assertFalse(quality["complete_exchange_presence"])
        self.assertEqual(selected[0]["symbol"], "sh600000")
        with self.assertRaisesRegex(TdxError, "conflicting universe"):
            normalize_universe({"Codes": [row, {**row, "name": "不同名称"}], "Total": 2})
        with self.assertRaisesRegex(TdxError, "Total"):
            normalize_universe({"Codes": [row], "Total": 5000})

    def test_symbol_requires_unambiguous_exchange_and_preserves_leading_zeros(self):
        self.assertEqual(symbol(" SZ000001 "), "sz000001")
        for value in ["000001", "sh510300", "sz159919", "sh000001", "sz399001", "sh900901", "sz200001"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                symbol(value)


class HttpAndQuoteTests(unittest.TestCase):
    def test_business_error_code_minus_one_is_not_a_successful_empty_result(self):
        client = TdxClient("http://127.0.0.1:18080")
        response = _Response({"code": -1, "message": "upstream dial failed", "data": []})
        with patch.object(client._local_opener,"open", return_value=response) as network:
            with self.assertRaisesRegex(TdxError, "upstream dial failed"):
                client.universe()
        self.assertEqual(network.call_count, 1)

    def test_success_receipt_keeps_actual_raw_hash_and_explicit_daily_route(self):
        client = TdxClient("http://127.0.0.1:18080", timeout=3)
        response = _Response({"code": 0, "data": history([daily_row()])})
        with patch.object(client._local_opener,"open", return_value=response) as network:
            data, receipt, raw = client.history("sh600000", source="tdx")
        request = network.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:18080/api/kline-all/tdx?code=sh600000&type=day")
        self.assertEqual(network.call_args.kwargs["timeout"], 3)
        self.assertEqual(receipt["response_sha256"], hashlib.sha256(response.raw).hexdigest())
        self.assertEqual(receipt["response_bytes"], len(response.raw))
        self.assertEqual(raw, response.raw)
        self.assertEqual(data["count"], 1)

    def test_quote_batch_post_and_missing_data_envelope_are_checked(self):
        client = TdxClient()
        response = _Response({"code": 0, "data": [quote_row()]})
        with patch.object(client._local_opener,"open", return_value=response) as network:
            client.quotes(["sh600000", "sz000001"])
        request = network.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"codes": ["sh600000", "sz000001"]})
        with self.assertRaises(ValueError):
            client.quotes([])
        with self.assertRaises(ValueError):
            client.quotes(["sh600000"] * 51)
        for response in [_Response({"code": 0}), _Response(b"maintenance", content_type="text/html")]:
            with patch.object(client._local_opener,"open", return_value=response), self.assertRaises(TdxError):
                client.universe()

    def test_quote_amount_is_yuan_and_receiver_time_does_not_prove_freshness(self):
        receipt = {"received_at": "2026-01-05T15:30:00+08:00"}
        observations, missing, extras = normalized_quotes([quote_row()], ["sh600000", "sz000001"], receipt)
        quote = observations[0]
        self.assertEqual(quote["price"], 12.56)
        self.assertEqual(quote["previous_close"], 12.4)
        self.assertEqual(quote["amount_yuan"], 157630)  # Do not divide quote Amount by 1000.
        self.assertEqual(quote["volume_lots"], 125.5)
        self.assertEqual(quote["server_time_raw"], 101530)
        self.assertIsNone(quote["exchange_trade_timestamp"])
        self.assertEqual(quote["freshness"], "not_proven_by_received_at")
        self.assertEqual(quote["received_at"], receipt["received_at"])
        self.assertEqual(missing, ["sz000001"])
        self.assertEqual(extras, [])

    def test_quotes_use_exchange_to_disambiguate_and_reject_duplicate_symbols(self):
        receipt = {"received_at": "2026-01-05T15:30:00+08:00"}
        rows = [quote_row(Exchange=0, Code="000001"), quote_row(Exchange=2, Code="920001"),
                quote_row(Exchange=1, Code="600000")]
        points, missing, extras = normalized_quotes(rows, ["sz000001", "bj920001"], receipt)
        self.assertEqual([point["symbol"] for point in points], ["sz000001", "bj920001"])
        self.assertEqual(missing, [])
        self.assertEqual(extras, ["sh600000"])
        with self.assertRaisesRegex(TdxError, "duplicate quote"):
            normalized_quotes([quote_row(), quote_row()], ["sh600000"], receipt)


if __name__ == "__main__":
    unittest.main()
