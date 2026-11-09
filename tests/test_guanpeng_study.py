"""Book counting boundaries, prefix causality and safe offline export."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("guanpeng_study", ROOT / "tools/guanpeng_study.py")
study = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(study)

from chanmodel.structure import stroke_check, contains


class BookStudy(unittest.TestCase):
    def test_teaching_data_really_has_nested_and_engulfing_ranges(self):
        bars = study.synthetic_bars(60)
        interval = lambda b: {"high": b.high, "low": b.low}
        self.assertTrue(contains(interval(bars[28]), interval(bars[29])))
        self.assertLess(bars[29].high, bars[28].high)
        self.assertTrue(contains(interval(bars[46]), interval(bars[47])))
        self.assertGreater(bars[47].high, bars[46].high)

    def test_center_count_five_support_count_seven(self):
        bottom = dict(kind="bottom", merged_index=1, source=1, center_high=10)
        top = dict(kind="top", merged_index=4, source=4, center_high=20)
        self.assertFalse(stroke_check(bottom, top, "strict")[0])
        top.update(merged_index=5, source=5)
        self.assertTrue(stroke_check(bottom, top, "strict")[0])
        self.assertEqual(top["merged_index"] - bottom["merged_index"] + 1, 5)
        self.assertEqual((top["merged_index"] + 1) - (bottom["merged_index"] - 1) + 1, 7)

    def test_export_earlier_frames_and_events_unchanged_by_future(self):
        bars = study.synthetic_bars(100)
        short = study.build_bundle(bars[:65], {"synthetic": True}, "synthetic")
        long = study.build_bundle(bars, {"synthetic": True}, "synthetic")
        self.assertEqual(short["frames"], long["frames"][:65])
        self.assertEqual(short["events"], [e for e in long["events"] if e["observed_at"] < 65])
        self.assertTrue(any(f["strokes"] for f in long["frames"]))
        for frame in long["frames"]:
            for kind in ["fractals", "strokes", "segments", "centers"]:
                for obj in frame[kind]:
                    self.assertLessEqual(obj["basis_at"], frame["as_of"])
                    self.assertLessEqual(obj["available_at"], frame["as_of"])

    def test_metadata_and_json_script_cannot_be_broken_by_filename(self):
        attack = '</script><script>alert("unexpected")</script>'
        bundle = study.build_bundle(study.synthetic_bars(3), {"file": attack}, "synthetic")
        page = study.render_html(bundle)
        payload = page.split('<script id="study-data" type="application/json">', 1)[1].split('</script>', 1)[0]
        self.assertEqual(json.loads(payload)["manifest"]["source"]["file"], attack)
        self.assertNotIn(attack, page)
        self.assertNotIn("__STUDY_DATA__", page)
        self.assertFalse(bundle["manifest"]["rules"]["execution_allowed"])


if __name__ == "__main__":
    unittest.main()
