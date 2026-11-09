"""Resolve a signal's real price pivot independently of its partition endpoint."""
from __future__ import annotations

from .v2.divergence import invalidated_by


def align_signal(point,bars):
    """Mutate only presentation/state fields; preserve identity and evidence time."""
    start,end=point['terminal_range']
    if not 0<=start<=end<len(bars):raise ValueError('signal terminal range outside observed candles')
    if point['side'] not in {'buy','sell'}:raise ValueError('unknown signal side')
    field='low' if point['side']=='buy' else 'high'
    # Ties retain the earliest raw candle. No value after the completed terminal
    # interval can move this pivot backwards or leak into its extremum.
    pivot=(min if field=='low' else max)(bars[start:end+1],key=lambda b:getattr(b,field))
    point.setdefault('decomposition_end',point['anchor'])
    point.setdefault('decomposition_end_price',point['price'])
    point.update(anchor=pivot.pos,price=getattr(pivot,field),
                 identity_semantics='stable decomposition identity; price anchor is raw terminal extremum',
                 anchor_profile='raw_terminal_extremum_v1',
                 extreme_evidence={'range':[start,end],'source_index':pivot.pos,'price':getattr(pivot,field),
                                   'kind':'lowest_low' if field=='low' else 'highest_high','references':[21,53]})
    if not point['class'].startswith('third_'):point['invalidation']=point['price']
    broken=next((b for b in bars[pivot.pos+1:] if invalidated_by(point,b)),None)
    point['state']='invalidated' if broken else 'awaiting_lower_confirmation'
    point['invalidated_at']=broken.pos if broken else None
    if bars[pivot.pos].closed_at:
        point['event_time']=bars[pivot.pos].closed_at
    if 0<=point['available_at']<len(bars) and bars[point['available_at']].closed_at:
        point['available_time']=bars[point['available_at']].closed_at
    return point
