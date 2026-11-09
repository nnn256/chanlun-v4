"""Documented segment-range rectangles and their leave/return history."""
from __future__ import annotations


def build_centers(moves: list[dict], layer: int = 0) -> list[dict]:
    source = [move for move in moves if move["state"] == "confirmed"]
    for move in source:
        if "actual_low" not in move or "actual_high" not in move:
            raise ValueError("centers require the actual covered OHLC range, not endpoint-only ranges")
    result, start = [], 0
    while start + 2 < len(source):
        seed = source[start:start + 3]
        zd, zg = max(m["actual_low"] for m in seed), min(m["actual_high"] for m in seed)
        if zd >= zg:
            start += 1
            continue
        if result:
            previous = result[-1]
            expected = ["down", "up", "down"] if zd > previous["zg"] else ["up", "down", "up"] if zg < previous["zd"] else None
            if expected and [m["direction"] for m in seed] != expected:
                start += 1
                continue
            if start - 1 <= previous["seed_indices"][-1]:
                start += 1
                continue
        members = list(range(start, start + 3))
        attempts, pending, departed, cursor = [], None, None, start + 3
        while cursor < len(source):
            move = source[cursor]
            if pending is None:
                outside = move["actual_high"] > zg if move["direction"] == "up" else move["actual_low"] < zd
                if outside:
                    pending = cursor
                else:
                    members.append(cursor)
                cursor += 1
                continue
            leave = source[pending]
            if move["start"] != leave["end"] or move["direction"] == leave["direction"]:
                raise ValueError("leave/return movements must be connected and opposite")
            upward = leave["direction"] == "up"
            value = move["actual_low"] if upward else move["actual_high"]
            boundary = zg if upward else zd
            relation = "outside" if (value > zg if upward else value < zd) else "touch" if value == boundary else "crossed" if (value < zd if upward else value > zg) else "inside"
            attempt = {"leave": pending, "return": cursor, "side": "buy" if upward else "sell",
                       "relation": relation, "return_extreme": value, "boundary": boundary,
                       "basis_at": max(leave["basis_at"], move["basis_at"])}
            attempts.append(attempt)
            if relation == "outside":
                departed = attempt
                break
            if relation == "crossed":
                members.append(pending)
                pending = cursor
            else:
                members.extend([pending, cursor])
                pending = None
            cursor += 1
        selected = [source[index] for index in members]
        record = {"id": f"c{layer}:{seed[0]['start']}:{seed[2]['end']}", "layer": layer,
                  "start": seed[0]["start"], "end": selected[-1]["end"], "zd": zd, "zg": zg,
                  "dd": min(m["actual_low"] for m in selected), "gg": max(m["actual_high"] for m in selected),
                  "seed_indices": list(range(start, start + 3)), "member_indices": members,
                  "components": [m["id"] for m in selected],
                  "state": "departed" if departed else "leaving" if pending is not None else "extension" if len(members) > 3 else "formed",
                  "basis_at": max(m["basis_at"] for m in seed), "attempts": attempts,
                  "departure": departed, "stop_basis_at": departed["basis_at"] if departed else None,
                  "pending_leave": source[pending]["id"] if pending is not None and not departed else None,
                  "definition_mode": "confirmed_segment_rectangle" if layer == 0 else "recursive_rectangle_proxy",
                  "rule": "Z01-Z05"}
        result.append(record)
        if departed:
            start = departed["return"]
        else:
            break
    return result


def classify(centers: list[dict]) -> dict:
    if not centers:
        return {"state": "untradable_unclear", "relation": None}
    latest = centers[-1]
    state = "center_extension" if latest["state"] == "extension" else "center_breakout_candidate" if latest["state"] in {"leaving", "departed"} else "center_oscillation"
    relation = None
    if len(centers) >= 2:
        a, b = centers[-2:]
        if b["dd"] > a["gg"]:
            state, relation = "uptrend_structure_candidate", "outer_ranges_separated_up"
        elif b["gg"] < a["dd"]:
            state, relation = "downtrend_structure_candidate", "outer_ranges_separated_down"
        elif b["zd"] > a["zg"] or b["zg"] < a["zd"]:
            state, relation = "higher_level_expansion_candidate", "cores_separate_outer_ranges_overlap"
        else:
            relation = "core_overlap"
    return {"state": state, "relation": relation, "center_id": latest["id"], "zd": latest["zd"], "zg": latest["zg"]}
