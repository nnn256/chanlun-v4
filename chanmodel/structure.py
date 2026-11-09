"""Directional inclusion, strict extrema and documented stroke selection."""
from __future__ import annotations

from .model import Candle, Config


def contains(a: dict, b: dict) -> bool:
    return (a["high"] >= b["high"] and a["low"] <= b["low"]) or (b["high"] >= a["high"] and b["low"] <= a["low"])


def local_direction(reference: dict | None, left: dict, right: dict, initial: str | None = None) -> str:
    if reference is not None:
        if left["high"] > reference["high"] and left["low"] > reference["low"]:
            return "up"
        if left["high"] < reference["high"] and left["low"] < reference["low"]:
            return "down"
        return "up" if left["high"] >= reference["high"] else "down"
    if initial:
        return initial
    left_mid, right_mid = left["high"] + left["low"], right["high"] + right["low"]
    if right_mid != left_mid:
        return "up" if right_mid > left_mid else "down"
    return "up" if right["close"] >= left["close"] else "down"


def merge_pair(left: dict, right: dict, direction: str) -> dict:
    result = {**left, "end": right["end"], "basis_at": max(left.get("basis_at", left["end"]), right.get("basis_at", right["end"])),
              "closed": left.get("closed", True) and right.get("closed", True)}
    if "close" in right:
        result["close"] = right["close"]
    for field in ["high", "low"]:
        chosen = (max if direction == "up" else min)(left[field], right[field])
        origins = [item[field + "_source"] for item in [left, right] if item[field] == chosen]
        result[field], result[field + "_source"] = chosen, min(origins)
    result["components"] = left["components"] + right["components"]
    return result


def merge_bars(bars: list[Candle], config: Config) -> list[dict]:
    merged = []
    for bar in bars:
        current = {"start": bar.pos, "end": bar.pos, "basis_at": bar.pos, "high": round(bar.high, config.price_decimals),
                   "low": round(bar.low, config.price_decimals), "high_source": bar.pos,
                   "low_source": bar.pos, "open": bar.open, "close": bar.close,
                   "closed": bar.closed, "components": [bar.pos]}
        while merged and contains(merged[-1], current):
            left = merged.pop()
            direction = local_direction(merged[-1] if merged else None, left, current)
            current = merge_pair(left, current, direction)
        merged.append(current)
    return [{**bar, "index": index, "id": f"k:{bar['start']}:{bar['end']}"} for index, bar in enumerate(merged)]


def fractals_at(merged: list[dict]) -> list[dict]:
    result = []
    for index in range(1, len(merged) - 1):
        left, mid, right = merged[index - 1:index + 2]
        top = mid["high"] > max(left["high"], right["high"]) and mid["low"] > max(left["low"], right["low"])
        bottom = mid["high"] < min(left["high"], right["high"]) and mid["low"] < min(left["low"], right["low"])
        if not (top or bottom):
            continue
        kind, field = ("top", "high") if top else ("bottom", "low")
        source = mid[field + "_source"]
        result.append({"id": f"f:{kind}:{source}", "kind": kind, "merged_index": index,
                       "source": source, "price": mid[field], "center_high": mid["high"], "center_low": mid["low"],
                       "group_start": mid["start"], "group_end": mid["end"],
                       "basis_at": right["end"], "state": "closed_basis" if all(b["closed"] for b in [left, mid, right]) else "open_basis",
                       "components": [left["id"], mid["id"], right["id"]], "rule": "F01"})
    return result


def stroke_check(a: dict, b: dict, mode: str) -> tuple[bool, str]:
    if a["kind"] == b["kind"]:
        return False, "same_kind"
    if b["merged_index"] - a["merged_index"] <= 2:
        return False, "shared_standard_candle"
    top, bottom = (a, b) if a["kind"] == "top" else (b, a)
    if top["center_high"] <= bottom["center_high"]:
        return False, "center_high_price_check"
    distance = b["merged_index"] - a["merged_index"] if mode == "strict" else b["source"] - a["source"]
    if distance < 4:
        return False, "insufficient_spacing"
    return True, "valid"


def more_extreme(candidate: dict, previous: dict) -> bool:
    return candidate["price"] > previous["price"] if candidate["kind"] == "top" else candidate["price"] < previous["price"]


def strokes_at(fractals: list[dict], config: Config) -> tuple[list[dict], list[dict]]:
    result, rejections = [], []
    anchor = endpoint = None

    def leg(a, b, confirmation=None):
        return {"id": f"p:{a['source']}:{b['source']}", "direction": "up" if a["kind"] == "bottom" else "down",
                "start": a["source"], "end": b["source"], "start_price": a["price"], "end_price": b["price"],
                "low": min(a["price"], b["price"]), "high": max(a["price"], b["price"]),
                "state": "confirmed" if confirmation is not None and confirmation["state"] == "closed_basis" and a["state"] == b["state"] == "closed_basis" else "provisional",
                "basis_at": max(a["basis_at"], b["basis_at"], confirmation["basis_at"] if confirmation else 0),
                "basis_closed": a["state"] == b["state"] == "closed_basis" and (confirmation is None or confirmation["state"] == "closed_basis"),
                "components": [a["id"], b["id"]], "confirmation_basis": confirmation["id"] if confirmation else None,
                "definition_mode": "structguide_documented_stroke", "rule": "P01-P04"}

    for candidate in fractals:
        if anchor is None:
            anchor = candidate
            continue
        if endpoint is None:
            if candidate["kind"] == anchor["kind"]:
                if not result and more_extreme(candidate, anchor):
                    anchor = candidate
                continue
            legal, reason = stroke_check(anchor, candidate, config.stroke_mode)
            if legal:
                endpoint = candidate
            else:
                rejections.append({"from": anchor["id"], "to": candidate["id"], "reason": reason})
            continue
        if candidate["kind"] == endpoint["kind"]:
            if more_extreme(candidate, endpoint):
                endpoint = candidate
                if not stroke_check(anchor, endpoint, config.stroke_mode)[0] and not result:
                    anchor, endpoint = endpoint, None
            continue
        legal_old, reason = stroke_check(anchor, endpoint, config.stroke_mode)
        legal_new, new_reason = stroke_check(endpoint, candidate, config.stroke_mode)
        if legal_old and legal_new:
            result.append(leg(anchor, endpoint, candidate))
            anchor, endpoint = endpoint, candidate
        else:
            rejections.append({"from": endpoint["id"], "to": candidate["id"], "reason": reason if not legal_old else new_reason})
            if not result and more_extreme(candidate, anchor):
                anchor, endpoint = candidate, None
    if anchor is not None and endpoint is not None and stroke_check(anchor, endpoint, config.stroke_mode)[0]:
        result.append(leg(anchor, endpoint))
    return result, rejections
