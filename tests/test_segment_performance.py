"""Optimization equivalence against the preserved pre-integration implementation.

The public pending_hypotheses list now includes only candidates at/before
the selected confirmed endpoint. A pending alternative path at the same
endpoint remains visible because it can change confirmation provenance.
Later endpoints cannot beat the selection order and are deliberately omitted.
"""
import copy
import importlib.util
import math
from pathlib import Path
import random
import sys
import unittest
from unittest.mock import patch

from chanmodel.engine import analyze
from chanmodel.model import Candle, Config
from chanmodel.segments import build_segments


BASELINE_PATH = Path(__file__).resolve().parent / "fixtures/segments_reference.py"
_spec = importlib.util.spec_from_file_location("chanmodel._tdx_baseline_segments", BASELINE_PATH)
baseline = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = baseline
_spec.loader.exec_module(baseline)


def normalize_competitors(value):
    """Remove only later losing pending candidates from the old report."""
    if isinstance(value, list):
        return [normalize_competitors(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: normalize_competitors(item) for key, item in value.items()}
    if result.get("confirmation") is not None and "pending_hypotheses" in result:
        result["pending_hypotheses"] = [candidate for candidate in result["pending_hypotheses"]
                                        if candidate["endpoint_move"] <= result["end_move"]]
    return result


def movement_input(vertices):
    return [{"id": f"p:{index}", "start": index*10, "end": (index+1)*10,
             "direction": "up" if end > start else "down", "start_price": start,
             "end_price": end, "low": min(start, end), "high": max(start, end),
             "basis_at": (index+1)*10+3, "basis_closed": True, "state": "confirmed"}
            for index, (start, end) in enumerate(zip(vertices, vertices[1:]))]


def sample_bars(count, kind="wave", seed=47):
    rng, previous, result = random.Random(seed), 100.0, []
    for index in range(count):
        price = (100+30*math.sin(index/140)+9*math.sin(index/25)+3*math.sin(index/5)
                 if kind == "wave" else previous+rng.uniform(-1.8, 1.8))
        high = max(price, previous)+rng.uniform(0.05, 0.65)
        low = min(price, previous)-rng.uniform(0.05, 0.65)
        result.append(Candle(index, previous, high, low, price))
        previous = price
    return result


class SegmentSelectionOptimizationTests(unittest.TestCase):
    def assert_report_equivalent(self, bars, mode="loose"):
        config = Config(stroke_mode=mode)
        with patch("chanmodel.engine.build_segments", baseline.build_segments):
            old = analyze(bars, config, "daily")
        current = analyze(bars, config, "daily")
        self.assertEqual(normalize_competitors(old), current)
        return current

    def test_public_numerical_cases_match_original(self):
        cases = [[10, 20, 12, 25, 15, 22, 13],
                 [0, 10, 5, 30, 20, 25, 15, 24, 18, 27],
                 [10, 20, 14, 25, 18],
                 [10, 20, 14, 25, 18, 22, 16],
                 [0, 10, 5, 30, 20, 25, 8, 15, 6, 16, 9, 18]]
        for values in cases:
            for mirrored in [values, [-value for value in values]]:
                with self.subTest(values=mirrored):
                    source = movement_input(mirrored)
                    self.assertEqual(normalize_competitors(baseline.build_segments(source)), build_segments(source))

    def test_random_reports_match_all_selected_fields_centers_and_signals(self):
        for seed in range(12):
            for mode in ["strict", "loose"]:
                with self.subTest(seed=seed, mode=mode):
                    self.assert_report_equivalent(sample_bars(350, "random", seed), mode)

    def test_8000_bar_wave_and_random_reports_match(self):
        for kind in ["wave", "random"]:
            with self.subTest(kind=kind):
                result = self.assert_report_equivalent(sample_bars(8000, kind))
                self.assertGreater(len(result["segments"]), 1)

    def test_revisable_earlier_hypothesis_is_retained(self):
        before = movement_input([0, 10, 5, 30, 20, 25, 8, 15, 6])
        early = build_segments(before)[0]
        self.assertEqual(early["end_price"], 25)
        self.assertTrue(any(candidate["endpoint_move"] == 2 for candidate in early["pending_hypotheses"]))
        after = movement_input([0, 10, 5, 30, 20, 25, 8, 15, 6, 16, 9, 18])
        self.assertEqual(build_segments(after)[0]["end_price"], 30)

    def test_no_later_endpoint_is_examined_after_confirmed_winner(self):
        source = movement_input([0, 10, 5, 30, 20, 25, 8, 15, 6, 16, 9, 18])
        import chanmodel.segments as current
        original = current.characteristic_candidate
        calls = []

        def counted(moves, start, endpoint):
            calls.append((start, endpoint))
            return original(moves, start, endpoint)

        with patch("chanmodel.segments.characteristic_candidate", side_effect=counted):
            result = build_segments(source)
        selected = result[0]["end_move"]
        self.assertFalse(any(start == 0 and endpoint > selected for start, endpoint in calls))


if __name__ == "__main__":
    unittest.main()
