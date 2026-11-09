"""Named force comparisons and research candidates with explicit prerequisites."""
from __future__ import annotations

import math

from .model import Candle, Config


def macd(bars: list[Candle], config: Config) -> list[dict]:
    if not bars:
        return []
    fast = slow = bars[0].close
    dea = 0.0
    result = []
    for bar in bars:
        fast += 2 / (config.macd_fast + 1) * (bar.close - fast)
        slow += 2 / (config.macd_slow + 1) * (bar.close - slow)
        dif = fast - slow
        dea += 2 / (config.macd_signal + 1) * (dif - dea)
        result.append({"dif": dif, "dea": dea, "histogram": config.macd_multiplier * (dif - dea)})
    return result


def attach_ranges(moves: list[dict], bars: list[Candle], histogram: list[dict]):
    for move in moves:
        covered = bars[move["start"]:move["end"] + 1]
        if not covered:
            raise ValueError("movement range is outside input candles")
        low, high = min(covered, key=lambda b: b.low), max(covered, key=lambda b: b.high)
        sign = 1 if move["direction"] == "up" else -1
        area = math.fsum(max(0.0, sign * value["histogram"]) for value in histogram[move["start"]:move["end"] + 1])
        move.update(actual_low=low.low, actual_high=high.high, actual_low_source=low.pos, actual_high_source=high.pos,
                    macd_area=area, amplitude=abs(move["end_price"] - move["start_price"]),
                    bars_count=move["end"] - move["start"] + 1,
                    slope=abs(move["end_price"] - move["start_price"]) / max(1, move["end"] - move["start"]))


def structural_signals(moves: list[dict], centers: list[dict], bars: list[Candle], config: Config, level: str) -> tuple[list[dict], list[dict]]:
    ss = [move for move in moves if move["state"] == "confirmed"]
    signals, comparisons = [], []
    seen = set()

    def emit(kind, move, center, basis_at, **evidence):
        side = "buy" if "buy" in kind else "sell"
        ident = f"{kind}:{center['id']}:{move['end']}"
        if ident in seen:
            return None
        seen.add(ident)
        invalidation = evidence.pop("invalidation", move["actual_low"] if side == "buy" else move["actual_high"])
        condition = "le" if side == "buy" else "ge"
        point = {"id": ident, "signal_class": kind + "_candidate", "side": side,
                 "anchor": move["end"], "price": move["end_price"], "basis_at": basis_at,
                 "terminal_range": [move["start"], move["end"]], "center_id": center["id"],
                 "zd": center["zd"], "zg": center["zg"], "invalidation": invalidation,
                 "invalidation_operator": condition, "definition_mode": "signal_proxy",
                 "trade_level": level, "confirm_level": "missing", "trigger_level": "missing",
                 "structure_completeness": "documented_structure_with_unproven_trend_recursion",
                 "lower_level_trigger": "missing", "execution_allowed": False,
                 "approximation_loss": ["signal force uses an indicator proxy", "sublevel trend-type recursion and trigger require separate evidence"],
                 "next_observation": "lower-level structural turn, center relation and invalidation boundary", **evidence}
        # Check the whole known delay, not only the newest candle. This prevents
        # resurrecting a setup already invalidated before it became observable.
        touch_invalidates = config.third_boundary == "strict_outside" or not kind.startswith("third")
        later = bars[point["anchor"] + 1:]
        if side == "buy":
            broken = any((b.low <= invalidation if touch_invalidates else b.low < invalidation) for b in later)
        else:
            broken = any((b.high >= invalidation if touch_invalidates else b.high > invalidation) for b in later)
        if not touch_invalidates:
            point["invalidation_operator"] = "lt" if side == "buy" else "gt"
        point["status"] = "invalidated_in_known_delay" if broken else "candidate"
        signals.append(point)
        return point

    for center in centers:
        for attempt in center["attempts"]:
            qualifies = attempt["relation"] == "outside" or (config.third_boundary == "not_break" and attempt["relation"] == "touch")
            if qualifies:
                ret = ss[attempt["return"]]
                emit(f"third_{attempt['side']}", ret, center, attempt["basis_at"],
                     leave_move=ss[attempt["leave"]]["id"], return_move=ret["id"],
                     invalidation=attempt["boundary"], boundary_policy=config.third_boundary,
                     rule="B03", first_qualifying_return=True)
                break

    for n, center in enumerate(centers):
        entering = center["seed_indices"][0] - 1
        if entering < 0:
            continue
        a = ss[entering]
        upper_limit = centers[n + 1]["seed_indices"][0] if n + 1 < len(centers) else len(ss)
        for leaving in range(center["seed_indices"][-1] + 1, upper_limit, 2):
            c = ss[leaving]
            if a["direction"] != c["direction"]:
                continue
            side = "buy" if c["direction"] == "down" else "sell"
            new_extreme = c["end_price"] < a["end_price"] if side == "buy" else c["end_price"] > a["end_price"]
            weak = a["macd_area"] > 0 and c["macd_area"] < a["macd_area"]
            prior = centers[n - 1] if n else None
            trend = prior is not None and (center["gg"] < prior["dd"] if side == "buy" else center["dd"] > prior["gg"])
            comparison = {"id": f"d:{center['id']}:{a['id']}:{c['id']}", "center_id": center["id"],
                          "kind": "trend_force_candidate" if trend else "consolidation_force_candidate",
                          "compared_a": a["id"], "compared_c": c["id"], "direction": c["direction"],
                          "macd_areas": [a["macd_area"], c["macd_area"]], "amplitudes": [a["amplitude"], c["amplitude"]],
                          "slopes": [a["slope"], c["slope"]], "weaker_macd_area": weak, "new_extreme": new_extreme,
                          "basis_at": max(a["basis_at"], c["basis_at"], center["basis_at"]),
                          "definition_mode": "indicator_force_on_named_structure", "rule": "B01"}
            comparisons.append(comparison)
            if not (trend and new_extreme and weak):
                continue
            first = emit(f"first_{side}", c, center, comparison["basis_at"],
                         comparison_id=comparison["id"], prior_center_id=prior["id"], rule="B01")
            if first is None or leaving + 2 >= len(ss):
                continue
            rebound, ret = ss[leaving + 1:leaving + 3]
            if rebound["direction"] == c["direction"] or ret["direction"] != c["direction"]:
                continue
            first_limit = c["actual_low"] if side == "buy" else c["actual_high"]
            ret_limit = ret["actual_low"] if side == "buy" else ret["actual_high"]
            survives = ret_limit > first_limit if side == "buy" else ret_limit < first_limit
            weaker_retest = c["macd_area"] > 0 and ret["macd_area"] < c["macd_area"]
            if survives or (config.second_policy == "original_101" and weaker_retest):
                emit(f"second_{side}", ret, center, max(first["basis_at"], rebound["basis_at"], ret["basis_at"]),
                     first_signal=first["id"], first_anchor=first["anchor"], first_return=True,
                     second_policy=config.second_policy,
                     retest_kind="higher_low_or_lower_high" if survives else "weak_or_equal_retest_101",
                     invalidation=first_limit if config.second_policy == "conservative" else ret_limit, rule="B02")
    return sorted(signals, key=lambda x: (x["basis_at"], x["id"])), comparisons
