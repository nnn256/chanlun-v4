"""Source-declared strict-stroke research profile over the audited V2 primitives.

Supports calendar candles and observed daily auction-result bars independently.
This profile deliberately retains V2 initialization and does not claim exact
equivalence to chanlunzhan.com's unpublished runtime.
"""
from dataclasses import asdict
from datetime import datetime
import hashlib
import json

from .model import Config, time_value
from .structure import merge_bars, fractals_at, strokes_at
from .segments import build_segments
from .signals import macd, attach_ranges
from .v2.series import Series, validate_series
from .v2.recursive import base_units, build_hierarchy, audit_hierarchy
from .v2.divergence import compare_divergences, make_signals

PROFILE = 'chanlun_source_audited_strict_stroke_study_v1'
CONFIG = Config(stroke_mode='strict', macd_multiplier=2.0, second_policy='conservative')
CALENDAR = {'week', 'month', 'quarter', 'year', '5d', 'auction'}


def filter_signals(points):
    """The old V2 policy admits a force-supported weak retest; this one does not."""
    return [p for p in points if not (p['class'].startswith('second_') and p.get('retest_mode') == 'weak_retest_101')]


def analyze_period(symbol, period, candles, cutoff, input_sha256, source='tdx',*,adjustment='unadjusted',config=None):
    if not candles:
        raise ValueError('No candles supplied')
    for i, bar in enumerate(candles):
        if bar.pos != i or bar.closed_at is None or (i and time_value(bar.closed_at) <= time_value(candles[i-1].closed_at)):
            raise ValueError('Contiguous positions and increasing explicit timestamps required')
        if not bar.closed and i != len(candles) - 1:
            raise ValueError('Only the last candle may be forming')
    selected = [b for b in candles if b.closed and time_value(b.closed_at) <= time_value(cutoff)]
    if not selected:raise ValueError('No closed candles at cutoff')
    config = config or CONFIG
    from dataclasses import replace
    selected = [replace(b, **{k:round(getattr(b,k),config.price_decimals) for k in ['open','high','low','close']}) for b in selected]
    if period not in CALENDAR:
        grid = validate_series(Series(symbol, period, adjustment, tuple(candles), source, input_sha256), cutoff)
    else:
        grid = {'state': 'calendar_or_auction_source_contract', 'bars_used': len(selected),'source':source,'adjustment':adjustment}
    merged = merge_bars(selected, config)
    fractals = fractals_at(merged)
    strokes, rejected = strokes_at(fractals, config)
    histogram = macd(selected, config)
    segments = build_segments(strokes, 0)
    attach_ranges(segments, selected, histogram)
    units = base_units(segments, len(selected)-1)
    hierarchy = build_hierarchy(units, 3, len(selected)-1)
    certificate = audit_hierarchy(hierarchy, len(selected)-1)
    if certificate['errors']:
        raise ValueError('Structural certificate validation failed')
    comparisons, signals, children = [], [], units
    for layer in hierarchy['levels']:
        comp = compare_divergences(children, layer['centers'], selected, histogram, layer['rank'], len(selected)-1)
        points = make_signals(children, layer['centers'], comp, selected, layer['rank'], histogram,
                              second_policy=config.second_policy,third_boundary=config.third_boundary)
        layer.update(comparisons=comp, signals=points)
        comparisons.extend(comp)
        signals.extend(points)
        children = layer['completed_types']
    for signal in signals:
        signal.update(event_time=selected[signal['anchor']].closed_at,
                      available_time=selected[signal['available_at']].closed_at,
                      lower_level_trigger='missing', execution_allowed=False)
    return {'schema': 'chanlun.study.v1', 'profile': PROFILE, 'symbol': symbol, 'period': period,'adjustment':adjustment,'source':source,
            'as_of': cutoff, 'input_sha256': input_sha256, 'config': asdict(config),
            'input_audit': {**grid, 'closed_count': len(selected), 'forming_excluded': sum(not b.closed for b in candles)},
            'base': {'merged': merged, 'fractals': fractals, 'strokes': strokes, 'segments': segments, 'stroke_rejections': rejected},
            'hierarchy': hierarchy, 'certificate_audit': certificate,
            'divergence_comparisons': comparisons, 'signals': signals, 'macd': histogram,
            'definition_mode': 'structure_proxy' if period == 'auction' else 'recursive_completed_types_under_declared_profile',
            'approximation_loss': ['L0 remains an explicitly declared confirmed-segment boundary',
                                   'initial inclusion uses inherited local midpoint/close policy, not site default-up',
                                   'force uses MACD(12,26,9), 2*(DIF-DEA), zero-axis distance fraction 0.1',
                                   'calendar periods are samples, not automatically Chan theoretical levels',
                                   'each chart is independent; lower-level turn and execution trigger are not asserted'],
            'strict_original_equivalence_proven': False, 'execution_allowed': False,
            'snapshot_semantics': 'geometry at final cutoff; event anchor may precede signal availability'}
