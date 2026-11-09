from __future__ import annotations

import copy
import hashlib
import json

from .centers import build_centers, classify
from .model import Candle, Config, as_config, time_value
from .segments import build_segments
from .signals import attach_ranges, macd, structural_signals
from .structure import fractals_at, merge_bars, strokes_at

LIMITATIONS = [
    "Feature-sequence implementation is checked against published numerical examples; plugin source/output equivalence is not proven.",
    "Higher layers recursively segment movements; they are a segment-recursion proxy, not proven recursive sublevel trend types.",
    "MACD force and first/second/third points remain research candidates requiring separate lower-level evidence.",
    "The raw timeframe label does not by itself establish a theoretical Chan level.",
]


def validate_bars(bars: list[Candle]):
    for index, bar in enumerate(bars):
        if bar.pos != index:
            raise ValueError("input positions must be consecutive and start at zero")
        if not bar.closed and index != len(bars) - 1:
            raise ValueError("only the last input candle can be open")
    times = [bar.closed_at for bar in bars]
    if any(times) and not all(times):
        raise ValueError("closing timestamps must be supplied for every bar or omitted for every bar")
    if times and all(times):
        values = [time_value(value) for value in times]
        if any(a >= b for a, b in zip(values, values[1:])):
            raise ValueError("closing timestamps must strictly increase")


def analyze(bars: list[Candle], config: Config | None = None, level: str = "unbound") -> dict:
    config = config or Config()
    validate_bars(bars)
    # Normalize once so containment, segment ranges and center comparisons use
    # one declared equality policy rather than mixing rounded and raw prices.
    bars = [Candle(b.pos, *(round(value, config.price_decimals) for value in (b.open, b.high, b.low, b.close)),
                   b.closed_at, b.volume, b.closed) for b in bars]
    merged = merge_bars(bars, config)
    fractals = fractals_at(merged)
    strokes, rejected = strokes_at(fractals, config)
    histogram = macd(bars, config)
    layers, graph, underlying = [], [], strokes
    for depth in range(config.max_depth):
        segments = build_segments(underlying, depth)
        attach_ranges(segments, bars, histogram)
        centers = build_centers(segments, depth)
        layers.append({"depth": depth, "segments": segments, "centers": centers, "classification": classify(centers),
                       "definition_mode": "documented_base_structure" if depth == 0 else "segment_recursion_proxy"})
        for segment in segments:
            graph.extend({"parent": segment["id"], "child": child, "relation": "composed_of"} for child in segment["components"])
        for center in centers:
            graph.extend({"parent": center["id"], "child": child, "relation": "rectangle_member"} for child in center["components"])
        underlying = [segment for segment in segments if segment["state"] == "confirmed"]
        if len(underlying) < 3:
            break
    base = layers[0]
    signals, comparisons = structural_signals(base["segments"], base["centers"], bars, config, level)
    as_of = len(bars) - 1 if bars else None
    report = {"schema_version": "1.0", "mode": "structure_proxy", "definition_profile": "structguide_documented_research_v1",
              "as_of": as_of, "as_of_time": bars[-1].closed_at if bars else None, "input_bars": len(bars),
              "trade_level": level, "review_level": "missing", "confirm_level": "missing", "trigger_level": "missing",
              "config": as_config(config), "definition_mode": "structure_proxy",
              "structure_completeness": "documented_base_chain_with_explicit_recursive_and_signal_proxies",
              "approximation_loss": LIMITATIONS, "merged": merged, "fractals": fractals, "strokes": strokes,
              "stroke_rejections": rejected, "layers": layers, "segments": base["segments"], "centers": base["centers"],
              "recursion_graph": graph, "current_state": base["classification"], "divergence_comparisons": comparisons,
              "signal_candidates": signals, "execution_allowed": False, "action": "observe",
              "availability_semantics": "snapshot fields are observed at this cutoff; replay supplies actual first-seen/version times"}
    for kind, objects in collections(report):
        for obj in objects:
            obj["available_at"] = as_of
            obj["confirmed_at"] = as_of if obj.get("state") == "confirmed" else None
            obj["source_range"] = [obj.get("start", obj.get("source", obj.get("anchor"))), obj.get("end", obj.get("source", obj.get("anchor")))]
    return report


def collections(report: dict):
    yield "merged", report["merged"]
    yield "fractal", report["fractals"]
    yield "stroke", report["strokes"]
    for layer in report["layers"]:
        yield f"segment:{layer['depth']}", layer["segments"]
        yield f"center:{layer['depth']}", layer["centers"]
    yield "divergence", report["divergence_comparisons"]
    yield "signal", report["signal_candidates"]


def object_body(obj: dict) -> dict:
    return {key: value for key, value in obj.items() if key not in {"available_at", "confirmed_at", "first_seen_at", "first_confirmed_at", "revision"}}


class ReplayEngine:
    """Append-only event ledger over prefix recomputation, including revisions.

    This is a reference/audit engine: the conservative full recomputation is
    deliberate because newly confirmed earlier endpoints can change downstream
    segmentation. It is not advertised as a tick-rate incremental engine.
    """
    def __init__(self, config: Config | None = None, level: str = "unbound"):
        self.config, self.level = config or Config(), level
        self.bars, self.events, self.registry = [], [], {}
        self.snapshot = None
        self.input_revision = 0

    def update(self, bar: Candle) -> list[dict]:
        if bar.pos == len(self.bars):
            if self.bars and not self.bars[-1].closed:
                raise ValueError("close/replace the forming last bar before appending")
            proposed = self.bars + [bar]
        elif self.bars and bar.pos == len(self.bars) - 1 and not self.bars[-1].closed:
            proposed = self.bars[:-1] + [bar]
        else:
            raise ValueError("append the next bar or replace the currently forming last bar")
        validate_bars(proposed)
        if len(proposed) == len(self.bars):
            self.input_revision += 1
        self.bars = proposed
        return self._observe()

    def revise(self, position: int, replacement: Candle) -> list[dict]:
        if position != replacement.pos or not 0 <= position < len(self.bars):
            raise ValueError("revision position does not match the replacement candle")
        proposed = self.bars.copy()
        proposed[position] = replacement
        validate_bars(proposed)
        self.bars = proposed
        self.input_revision += 1
        return self._observe()

    def _observe(self) -> list[dict]:
        snapshot = analyze(self.bars, self.config, self.level)
        now = snapshot["as_of"]
        before_count = len(self.events)
        present = set()
        for kind, objects in collections(snapshot):
            for obj in objects:
                key = kind + "/" + obj["id"]
                present.add(key)
                body = object_body(obj)
                signature = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                record = self.registry.get(key)
                confirmed = body.get("state") == "confirmed"
                if record is None:
                    record = {"first_seen_at": now, "revision": -1, "first_confirmed_at": None, "active": False, "signature": None, "body": {}}
                    self.registry[key] = record
                changed = not record["active"] or signature != record["signature"]
                if changed:
                    prior_state = record["body"].get("state")
                    prior_status = record["body"].get("status")
                    event = "confirmed" if confirmed and prior_state != "confirmed" else "appeared" if record["revision"] == -1 else "reappeared" if not record["active"] else "confirmation_withdrawn" if prior_state == "confirmed" and not confirmed else "invalidated" if kind == "signal" and prior_status == "candidate" and body.get("status") != "candidate" else "revised"
                    record.update(revision=record["revision"] + 1, active=True, signature=signature,
                                  body=copy.deepcopy(body), available_at=now, confirmed_at=now if confirmed else None)
                    if confirmed and record["first_confirmed_at"] is None:
                        record["first_confirmed_at"] = now
                    self.events.append({"sequence": len(self.events), "event": event, "kind": kind, "id": obj["id"],
                                        "observed_at": now, "observed_time": snapshot["as_of_time"], "available_at": now,
                                        "input_revision": self.input_revision, "revision": record["revision"], "value": copy.deepcopy(body)})
                obj.update(first_seen_at=record["first_seen_at"], available_at=record["available_at"],
                           first_confirmed_at=record["first_confirmed_at"], confirmed_at=record["confirmed_at"], revision=record["revision"])
        for key, record in self.registry.items():
            if record["active"] and key not in present:
                record["active"] = False
                record["revision"] += 1
                kind, ident = key.split("/", 1)
                self.events.append({"sequence": len(self.events), "event": "withdrawn", "kind": kind, "id": ident,
                                    "observed_at": now, "observed_time": snapshot["as_of_time"], "available_at": now,
                                    "input_revision": self.input_revision, "revision": record["revision"], "reason": "current structure no longer contains this object"})
        snapshot["availability_semantics"] = "version-observed times from prefix replay; earlier records are immutable"
        snapshot["input_revision"] = self.input_revision
        self.snapshot = snapshot
        return self.events[before_count:]


def replay(bars: list[Candle], config: Config | None = None, level: str = "unbound") -> tuple[dict, list[dict]]:
    engine = ReplayEngine(config, level)
    for bar in bars:
        engine.update(bar)
    return engine.snapshot or analyze([], config, level), engine.events


def multilevel(inputs: dict[str, list[Candle]], roles: tuple[str, str, str], cutoff: str, config: Config | None = None) -> dict:
    trade, confirm, trigger = roles
    if len(set(roles)) != 3 or any(role not in inputs for role in roles):
        raise ValueError("provide three distinct input levels for trade/confirm/trigger")
    cut = time_value(cutoff)
    snapshots, used = {}, {}
    for level, bars in inputs.items():
        validate_bars(bars)
        if not bars or any(bar.closed_at is None for bar in bars):
            raise ValueError(f"{level} requires explicit closing timestamps for multi-level alignment")
        available = [bar for bar in bars if time_value(bar.closed_at) <= cut and bar.closed]
        used[level] = available
        snapshots[level] = analyze(available, config, level)

    # Different names are not evidence of different data resolutions. This
    # legacy entry cannot reconcile identity/aggregation metadata; reject the
    # reproduced duplicate-series exploit and direct certified users to v2.
    role_times=[tuple(bar.closed_at for bar in used[level]) for level in roles]
    for index,times in enumerate(role_times):
        if len(times)>=2 and times in role_times[:index]:
            raise ValueError('identical observation times cannot stand for distinct timeframes; use v2 Series contracts')

    def interval(point, level):
        a, b = point["terminal_range"]
        return time_value(used[level][a].closed_at), time_value(used[level][b].closed_at)

    def children(parent, parent_level, child_level):
        lo, hi = interval(parent, parent_level)
        return [child for child in snapshots[child_level]["signal_candidates"]
                if child["side"] == parent["side"] and child["status"] == "candidate"
                and lo <= interval(child, child_level)[0] <= interval(child, child_level)[1] <= hi]

    audits = []
    for parent in snapshots[trade]["signal_candidates"]:
        confirmation = children(parent, trade, confirm)
        matches = [{"confirm_id": child["id"], "trigger_ids": [x["id"] for x in children(child, confirm, trigger)]} for child in confirmation]
        nested = any(match["trigger_ids"] for match in matches)
        audits.append({"parent_signal": parent["id"], "parent_status": parent["status"], "matches": matches,
                       "nested_candidate_evidence": nested and parent["status"] == "candidate",
                       "strict_original_proven": False, "execution_allowed": False,
                       "reason": "cross-timeframe nested research candidates do not prove original trend-type recursion"})
    return {"as_of_time": cut.isoformat(), "trade_level": trade, "confirm_level": confirm, "trigger_level": trigger,
            "cutoff_policy": "closed bars whose declared closing time is no later than shared cutoff",
            "levels": snapshots, "interval_nesting_audits": audits, "definition_mode": "multi_timeframe_structure_proxy",
            "execution_allowed": False, "action": "observe",
            "input_contract": "legacy role labels only; use chanmodel.v2 for symbol/adjustment/session/OHLCV reconciliation"}
