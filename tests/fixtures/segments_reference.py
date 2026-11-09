"""Feature-sequence and first-pen-break segment confirmation paths.

Candidates are evaluated on the current input only. Choosing the earliest
confirmed endpoint can revise a previously selected endpoint; replay owns
the observed-time/version history rather than pretending these are final.
"""
from __future__ import annotations

from .structure import contains, local_direction, merge_pair


def overlap(moves: list[dict]) -> tuple[float, float]:
    return max(move["low"] for move in moves), min(move["high"] for move in moves)


def opposite(direction: str) -> str:
    return "down" if direction == "up" else "up"


def feature(move: dict, index: int) -> dict:
    up = move["end_price"] > move["start_price"]
    return {"start": move["start"], "end": move["end"],
            "low": min(move["start_price"], move["end_price"]),
            "high": max(move["start_price"], move["end_price"]),
            "low_source": move["start"] if up else move["end"],
            "high_source": move["end"] if up else move["start"],
            "components": [index], "closed": move.get("basis_closed", True)}


def append_feature(stack: list[dict], current: dict, initial: str, external_reference: dict | None = None):
    while stack and contains(stack[-1], current):
        left = stack.pop()
        reference = stack[-1] if stack else external_reference
        current = merge_pair(left, current, local_direction(reference, left, current, initial))
    stack.append(current)


def feature_fractal(window: list[dict], kind: str) -> bool:
    a, b, c = window
    if kind == "top":
        return b["high"] > max(a["high"], c["high"]) and b["low"] > max(a["low"], c["low"])
    return b["high"] < min(a["high"], c["high"]) and b["low"] < min(a["low"], c["low"])


def invalidated_before(moves: list[dict], endpoint: int, stop: int) -> bool:
    old = moves[endpoint]
    for index in range(endpoint + 2, min(stop + 1, len(moves)), 2):
        value = moves[index]["end_price"]
        if (value > old["end_price"]) if old["direction"] == "up" else (value < old["end_price"]):
            return True
    return False


def second_sequence(moves: list[dict], endpoint: int, direction: str) -> dict | None:
    stack = []
    for index in range(endpoint + 2, len(moves), 2):
        if invalidated_before(moves, endpoint, index):
            return None
        append_feature(stack, feature(moves[index], index), opposite(direction))
        kind = "bottom" if direction == "up" else "top"
        if len(stack) >= 3 and feature_fractal(stack[-3:], kind):
            return {"confirm_move": index, "window": stack[-3:], "kind": kind}
    return None


def characteristic_candidate(moves: list[dict], start: int, endpoint: int) -> dict | None:
    direction = moves[start]["direction"]
    left = []
    for index in range(start + 1, endpoint, 2):
        append_feature(left, feature(moves[index], index), direction)
    if not left:
        return None
    f1, right = left[-1], []
    window_at = None
    # Stop at the first two standardized right-hand elements. Later elements
    # cannot rewrite this already tested candidate-boundary window.
    for index in range(endpoint + 1, len(moves), 2):
        append_feature(right, feature(moves[index], index), direction, f1)
        if len(right) >= 2:
            window_at = index
            break
    if window_at is None:
        return None
    f2, f3 = right[:2]
    source = f2["high_source"] if direction == "up" else f2["low_source"]
    if source != moves[endpoint]["end"]:
        return None
    kind = "top" if direction == "up" else "bottom"
    window = [f1, f2, f3]
    if not feature_fractal(window, kind):
        return None
    gap = f1["high"] < f2["low"] if direction == "up" else f1["low"] > f2["high"]
    record = {"endpoint_move": endpoint, "path": "feature_gap" if gap else "feature_no_gap",
              "first_window": window, "first_window_at": window_at, "gap": gap,
              "state": "pending", "confirm_move": None, "rule": "X02-X04"}
    if not gap:
        if not invalidated_before(moves, endpoint, window_at):
            record.update(state="confirmed", confirm_move=window_at)
        return record
    second = second_sequence(moves, endpoint, direction)
    if second is not None:
        confirmation = max(window_at, second["confirm_move"])
        if not invalidated_before(moves, endpoint, confirmation):
            record.update(state="confirmed", confirm_move=confirmation, second_sequence=second)
    return record


def direct_candidate(moves: list[dict], start: int, endpoint: int) -> dict | None:
    first = endpoint + 1
    if first >= len(moves):
        return None
    direction = moves[start]["direction"]
    threshold = moves[first]["end_price"]
    earlier = [index for index in range(start, endpoint - 1, 2)
               if (threshold <= moves[index]["end_price"] if direction == "up" else threshold >= moves[index]["end_price"])]
    if not earlier:
        return None
    record = {"endpoint_move": endpoint, "path": "first_pen_break", "first_reverse_move": first,
              "broken_prior_move": earlier[-1], "state": "pending", "confirm_move": None, "rule": "X05"}
    for index in range(endpoint + 2, len(moves)):
        move = moves[index]
        if move["direction"] == direction:
            extreme = move["end_price"] > moves[endpoint]["end_price"] if direction == "up" else move["end_price"] < moves[endpoint]["end_price"]
            if extreme:
                return None
        elif index >= endpoint + 3:
            breaks = move["end_price"] < threshold if direction == "up" else move["end_price"] > threshold
            if breaks:
                record.update(state="confirmed", confirm_move=index)
                return record
    return record


def build_segments(moves: list[dict], layer: int = 0) -> list[dict]:
    if not moves:
        return []
    for a, b in zip(moves, moves[1:]):
        if a["end"] != b["start"] or a["end_price"] != b["start_price"] or a["direction"] == b["direction"]:
            raise ValueError("segment input must be alternating, price-continuous and share endpoints")
    result, start = [], 0
    while start + 2 < len(moves):
        zd, zg = overlap(moves[start:start + 3])
        if zg <= zd:
            if result:
                break
            start += 1
            continue
        direction = moves[start]["direction"]
        candidates = []
        for endpoint in range(start + 2, len(moves), 2):
            good_price = moves[endpoint]["end_price"] > moves[start]["start_price"] if direction == "up" else moves[endpoint]["end_price"] < moves[start]["start_price"]
            if not good_price:
                continue
            for factory in [characteristic_candidate, direct_candidate]:
                candidate = factory(moves, start, endpoint)
                if candidate is not None:
                    candidates.append(candidate)
        confirmed = [item for item in candidates if item["state"] == "confirmed"]
        selected = min(confirmed, key=lambda c: (c["endpoint_move"], c["path"] == "first_pen_break")) if confirmed else None
        if selected is None:
            endpoint = start + 2
            for index in range(start + 4, len(moves), 2):
                extreme = moves[index]["end_price"] > moves[endpoint]["end_price"] if direction == "up" else moves[index]["end_price"] < moves[endpoint]["end_price"]
                if extreme:
                    endpoint = index
            confirmation = endpoint
        else:
            endpoint, confirmation = selected["endpoint_move"], selected["confirm_move"]
        a, b = moves[start], moves[endpoint]
        closed_basis = all(move.get("basis_closed", True) for move in moves[start:confirmation + 1])
        state = "confirmed" if selected and closed_basis else "provisional"
        result.append({"id": f"s{layer}:{a['start']}:{b['end']}", "layer": layer, "direction": direction,
                       "start": a["start"], "end": b["end"], "start_price": a["start_price"], "end_price": b["end_price"],
                       "low": min(a["start_price"], b["end_price"]), "high": max(a["start_price"], b["end_price"]),
                       "state": state, "basis_closed": closed_basis,
                       "basis_at": max(move["basis_at"] for move in moves[start:confirmation + 1]),
                       "components": [move["id"] for move in moves[start:endpoint + 1]],
                       "start_move": start, "end_move": endpoint, "confirm_move": confirmation if selected else None,
                       "confirmation": selected, "pending_hypotheses": [c for c in candidates if c["state"] == "pending"],
                       "definition_mode": "structguide_documented_segment" if layer == 0 else "segment_recursion_proxy",
                       "rule": "X01-X06"})
        if state != "confirmed":
            break
        start = endpoint + 1
    return result
