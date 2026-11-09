"""Book-linked structure study export; Python 3.11+, standard library only."""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

ALGORITHM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ALGORITHM_ROOT))

from chanmodel.engine import ReplayEngine, validate_bars
from chanmodel.model import Candle, Config, load_csv

PROFILE = "guanpeng_book_study_with_documented_segment_supplement_v1"
CONFIG = Config(stroke_mode="strict", max_depth=1, second_policy="conservative")
CORE = ["model", "structure", "segments", "centers", "signals", "engine"]
RULES = {
    "mode": "structure_proxy",
    "book": "管鹏《一小时漫画缠论实战法》，人民邮电出版社，2022，ISBN 9787115587879",
    "stroke_spacing": "standard fractal centers differ by >=4; 5 inclusive centers, 7 full support bars",
    "equal_extreme": "earliest source retained",
    "initial_direction": "existing engine midpoint, then close; engineering convention, not a book definition",
    "segment": "existing documented feature-sequence/gap and first-pen-break paths; supplemental to book chapter 3",
    "center": "confirmed segment actual ranges, positive width, fixed initial ZD/ZG, touch counts as return",
    "approximation_loss": [
        "Book omits complete segment termination rules; existing engine supplies documented conventions.",
        "Segment rectangles are not a proof of recursive completed sublevel trend types.",
        "No buy/sell labels or trading execution are exported by this study tool.",
        "Current confirmed geometry may be revised; append-only observed events preserve earlier versions.",
    ],
    "execution_allowed": False,
}


def synthetic_bars(count: int = 320) -> list[Candle]:
    origin = datetime(2026, 1, 5, 9, 31, tzinfo=timezone(timedelta(hours=8)))
    bars = []
    for i in range(count):
        value = round(100 + 18 * math.sin(i / 38) + 6 * math.sin(i / 10) + 2 * math.sin(i / 2.5), 6)
        high, low = value + .2, value - .2
        # Include genuine nested/engulfing teaching cases, so the standard
        # candle layer demonstrates directional merging rather than identity.
        if i and (i % 29 == 0 or i % 47 == 0):
            previous = bars[-1]
            change = .03 if i % 29 == 0 else -.08
            high, low = previous.high - change, previous.low + change
            value = (high + low) / 2
        bars.append(Candle(i, value, high, low, value,
                           (origin + timedelta(minutes=i)).isoformat(), 1000., True))
    return bars


def selected(obj: dict, keys: str) -> dict:
    return {key: obj[key] for key in keys.split() if key in obj}


def compact_frame(report: dict) -> dict:
    temporal = "basis_at available_at confirmed_at first_seen_at revision"
    move = "id direction start end start_price end_price state actual_high actual_low source_range " + temporal
    return {
        "as_of": report["as_of"], "as_of_time": report["as_of_time"],
        "merged": [selected(x, "id start end high low high_source low_source " + temporal) for x in report["merged"]],
        "fractals": [selected(x, "id kind source price state " + temporal) for x in report["fractals"]],
        "strokes": [selected(x, move) for x in report["strokes"]],
        "segments": [{**selected(x, move), "path": (x.get("confirmation") or {}).get("path")}
                     for x in report["segments"]],
        "centers": [selected(x, "id start end zd zg dd gg state stop_basis_at components " + temporal)
                    for x in report["centers"]],
    }


def build_bundle(bars: list[Candle], source: dict, level: str) -> dict:
    if not bars:
        raise ValueError("at least one input bar is required")
    validate_bars(bars)
    engine = ReplayEngine(CONFIG, level)
    frames = []
    for bar in bars:
        engine.update(bar)
        frames.append(compact_frame(engine.snapshot))
    events = [e for e in engine.events if e["kind"] in {"merged", "fractal", "stroke", "segment:0", "center:0"}]
    canonical = json.dumps([asdict(b) for b in bars], ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    manifest = {
        "profile": PROFILE, "rules": RULES, "config": asdict(CONFIG),
        "source": source, "level": level, "bars": len(bars),
        "used_bars_sha256": hashlib.sha256(canonical).hexdigest(),
        "engine_sha256": {f"chanmodel/{name}.py": hashlib.sha256((ALGORITHM_ROOT / "chanmodel" / f"{name}.py").read_bytes()).hexdigest()
                          for name in CORE},
        "replay_semantics": "each frame computed using only bars through its as_of; geometry may revise; events append only",
    }
    return {"manifest": manifest, "bars": [asdict(b) for b in bars], "frames": frames, "events": events}


def render_html(bundle: dict) -> str:
    template = Path(__file__).with_name("guanpeng_study_view.html").read_text(encoding="utf-8")
    # A CSV timestamp or source filename may contain HTML delimiters. Keep data
    # inside the inert JSON script even for adversarial text.
    payload = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    payload = payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return template.replace("__STUDY_DATA__", payload)


def positive_int(value: str) -> int:
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="导出《一小时漫画缠论实战法》结构学习回放")
    parser.add_argument("--csv", type=Path, help="真实或用户自备OHLC CSV")
    parser.add_argument("--bars", type=positive_int, help="仅使用输入前N根；合成示例默认320根")
    parser.add_argument("--level", help="图表周期标签；不自动等同严格理论级别")
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/guanpeng-study"))
    args = parser.parse_args()
    try:
        out = args.output_dir.resolve()
        if out.exists() and (not out.is_dir() or any(out.iterdir())):
            raise ValueError("output directory must be new or empty; choose a new path to preserve existing files")
        if args.csv:
            bars, source = load_csv(args.csv)
            source.update(kind="user_csv", synthetic=False)
            if args.bars:
                source["prefix_limit"] = args.bars
                bars = bars[:args.bars]
            level = args.level or "user_unspecified"
        else:
            bars = synthetic_bars(args.bars or 320)
            source = {"kind": "synthetic", "synthetic": True,
                      "generator": "sinusoidal nested waves; high/low=close+/-0.2, every 29th index nested by .03, every 47th engulfing by .08; 29 takes precedence",
                      "timestamp_note": "连续分钟索引用于演示，并非交易所交易日历或实际行情"}
            level = args.level or "synthetic_1m"
        bundle = build_bundle(bars, source, level)
        out.mkdir(parents=True, exist_ok=True)
        (out / "画线复习.html").write_text(render_html(bundle), encoding="utf-8")
        for name, value in [("manifest.json", bundle["manifest"]), ("analysis.json", bundle["frames"][-1])]:
            (out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        (out / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False, allow_nan=False) + "\n" for e in bundle["events"]), encoding="utf-8")
        if not args.csv:
            with (out / "synthetic.csv").open("w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["closed_at", "open", "high", "low", "close", "volume", "is_closed"])
                writer.writerows([b.closed_at, b.open, b.high, b.low, b.close, b.volume, "true"] for b in bars)
        last = bundle["frames"][-1]
        print(json.dumps({"output": str(out / "画线复习.html"), "bars": len(bars), "frames": len(bundle["frames"]),
                          "counts": {key: len(last[key]) for key in ["fractals", "strokes", "segments", "centers"]},
                          "mode": RULES["mode"], "source": source["kind"]}, ensure_ascii=False))
        return 0
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
