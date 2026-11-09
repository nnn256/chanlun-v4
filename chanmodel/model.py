from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import math
import re
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Config:
    stroke_mode: str = "loose"
    price_decimals: int = 8
    max_depth: int = 3
    second_policy: str = "original_101"
    third_boundary: str = "strict_outside"
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    macd_multiplier: float = 1.0

    def __post_init__(self):
        if self.stroke_mode not in {"strict", "loose"}:
            raise ValueError("stroke_mode must be strict or loose")
        if not 0 <= self.price_decimals <= 12:
            raise ValueError("price_decimals must be between 0 and 12")
        if not 1 <= self.max_depth <= 8:
            raise ValueError("max_depth must be between 1 and 8")
        if self.second_policy not in {"original_101", "conservative"}:
            raise ValueError("unknown second_policy")
        if self.third_boundary not in {"strict_outside", "not_break"}:
            raise ValueError("unknown third_boundary")
        if not 0 < self.macd_fast < self.macd_slow or self.macd_signal < 1:
            raise ValueError("MACD requires 0 < fast < slow and signal >= 1")
        if not math.isfinite(self.macd_multiplier) or self.macd_multiplier <= 0:
            raise ValueError("MACD multiplier must be finite and positive")


@dataclass(frozen=True)
class Candle:
    pos: int
    open: float
    high: float
    low: float
    close: float
    closed_at: str | None = None
    volume: float | None = None
    closed: bool = True

    def __post_init__(self):
        values = (self.open, self.high, self.low, self.close)
        if not all(math.isfinite(x) for x in values):
            raise ValueError(f"nonfinite OHLC at index {self.pos}")
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close):
            raise ValueError(f"invalid OHLC at index {self.pos}")
        if self.volume is not None and (not math.isfinite(self.volume) or self.volume < 0):
            raise ValueError(f"invalid volume at index {self.pos}")


def parse_time(value: str, assumed_timezone: str = "+08:00") -> tuple[str, bool]:
    text = value.strip()
    if len(text) == 10:
        raise ValueError("date-only labels do not establish a bar closing time; provide timestamp/closed_at")
    point = datetime.fromisoformat(text.replace("Z", "+00:00"))
    assumed = point.tzinfo is None
    if assumed:
        match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", assumed_timezone)
        zone = timezone((1 if match[1] == "+" else -1) * timedelta(hours=int(match[2]), minutes=int(match[3]))) if match else ZoneInfo(assumed_timezone)
        point = point.replace(tzinfo=zone)
    return point.astimezone(timezone.utc).isoformat(), assumed


def time_value(value: str) -> datetime:
    point = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if point.tzinfo is None:
        raise ValueError("comparison requires timezone-aware closing timestamps")
    return point.astimezone(timezone.utc)


def load_csv(path: Path, assumed_timezone: str = "+08:00") -> tuple[list[Candle], dict]:
    raw = path.read_bytes()
    bars, assumed_count, date_labels = [], 0, 0
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        normalized = [name.strip().lower() for name in (reader.fieldnames or [])]
        if len(normalized) != len(set(normalized)):
            raise ValueError("duplicate normalized CSV headers")
        names = {name.strip().lower(): name for name in (reader.fieldnames or [])}
        if not {"open", "high", "low", "close"} <= names.keys():
            raise ValueError("CSV requires open,high,low,close headers")
        time_key = next((key for key in ["closed_at", "timestamp", "datetime", "time", "date"] if key in names), None)
        previous = None
        for index, row in enumerate(reader):
            try:
                if None in row:
                    raise ValueError("row has more fields than the header")
                close_time = None
                if time_key:
                    value = (row[names[time_key]] or "").strip()
                    if time_key == "date" and len(value) == 10:
                        datetime.fromisoformat(value)
                        date_labels += 1
                        if previous is not None and value <= previous:
                            raise ValueError("date labels must strictly increase")
                        previous = value
                    else:
                        close_time, assumed = parse_time(value, assumed_timezone)
                        assumed_count += int(assumed)
                        if previous is not None and close_time <= previous:
                            raise ValueError("bar closing timestamps must strictly increase")
                        previous = close_time
                is_closed = True
                if "is_closed" in names:
                    value = (row[names["is_closed"]] or "").strip().lower()
                    if value not in {"1", "0", "true", "false"}:
                        raise ValueError("is_closed must be true/false or 1/0")
                    is_closed = value in {"1", "true"}
                if bars and not bars[-1].closed:
                    raise ValueError("only the last input candle may be open")
                o, h, l, c = (float(row[names[key]]) for key in ["open", "high", "low", "close"])
                volume = float(row[names["volume"]]) if "volume" in names and (row[names["volume"]] or "").strip() else None
                bars.append(Candle(index, o, h, l, c, close_time, volume, is_closed))
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError(f"CSV row {index + 2}: {exc}") from exc
    if not bars:
        raise ValueError("CSV has no data rows")
    return bars, {"file": str(path.resolve()), "input_sha256": hashlib.sha256(raw).hexdigest(),
                  "rows": len(bars), "timestamp_semantics": "bar_close" if time_key and not date_labels else "unavailable",
                  "assumed_closed_when_column_missing": "is_closed" not in names,
                  "assumed_timezone": assumed_timezone if assumed_count else None,
                  "timezone_assumed_rows": assumed_count, "date_labels_without_close_time": date_labels}


def as_config(config: Config) -> dict:
    return asdict(config)
