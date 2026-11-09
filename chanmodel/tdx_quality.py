"""Explicit, evidence-preserving policies for repairing historical acquisitions.

The strict normalizer remains unchanged. This stage only quarantines a narrow
class of zero-activity off-grid rows and optionally excludes observations after
a saved historical cutoff. Prices/timestamps are never rewritten or padded.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json

from .tdx_data import CN, TdxError
from .tdx_intraday import PERIOD_MINUTES, valid_session_close

POLICY = 'quarantine_flat_zero_activity_offgrid_v1'


def finite_number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def prepare_intraday_response(data, as_of: datetime, period: str, *,
                              quarantine_zero_activity=False,
                              exclude_after_cutoff=False):
    if period not in PERIOD_MINUTES or as_of.tzinfo is None:
        raise ValueError('known minute period and timezone-aware cutoff required')
    if not isinstance(data, dict):
        raise TdxError('history data must be an object')
    rows = data.get('list', data.get('List'))
    declared = data.get('count', data.get('Count'))
    if rows is None and declared == 0:
        rows = []
    if not isinstance(rows, list) or (declared is not None and int(declared) != len(rows)):
        raise TdxError('original response count/list is inconsistent')
    metadata = data.get('meta') or {}
    if metadata.get('source') not in {None, 'tdx'} or metadata.get('type') not in {None, period}:
        raise TdxError('source or period differs from explicit request')
    retained, excluded = [], []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise TdxError('source row is not an object')
        point = datetime.fromisoformat(str(row.get('Time', '')).replace('Z', '+00:00'))
        if point.tzinfo is None:
            raise ValueError('source row lacks timezone')
        reason = None
        if exclude_after_cutoff and point > as_of:
            reason = 'after_saved_cutoff'
        elif quarantine_zero_activity and not valid_session_close(point, PERIOD_MINUTES[period]):
            prices = [finite_number(row.get(key)) for key in ['Open', 'High', 'Low', 'Close']]
            flat = all(price is not None and price > 0 for price in prices) and len(set(prices)) == 1
            no_activity = finite_number(row.get('Volume')) == 0 and finite_number(row.get('Amount')) == 0
            # A date at midnight or a weekend can indicate a different feed
            # convention. Do not silently quarantine arbitrary time errors.
            local = point.astimezone(CN)
            known_anomaly = local.weekday() < 5 and local.hour == 13 and local.minute == 0 and local.second == 0 and local.microsecond == 0
            if flat and no_activity and known_anomaly:
                reason = 'zero_activity_flat_1300_offgrid'
        if reason:
            raw_row = json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
            excluded.append({'source_row': index, 'reason': reason, 'original': row,
                             'row_sha256': hashlib.sha256(raw_row).hexdigest()})
        else:
            retained.append(row)
    prepared = {key: value for key, value in data.items() if key not in {'list', 'List', 'count', 'Count'}}
    prepared.update(count=len(retained), list=retained)
    quarantine = sum(item['reason'] == 'zero_activity_flat_1300_offgrid' for item in excluded)
    audit = {'policy': POLICY if quarantine_zero_activity else 'strict_unchanged',
             'original_rows': len(rows), 'retained_rows': len(retained), 'excluded_rows': excluded,
             'quarantined_rows': quarantine, 'after_cutoff_rows': len(excluded)-quarantine,
             'cutoff': as_of.isoformat(), 'original_response_preserved': True,
             'prices_changed': False, 'timestamps_changed': False, 'rows_fabricated': False,
             'decision_scope': 'known zero-activity 13:00 anomalies only; any nonzero or nonflat off-grid bar still fails strict normalization'}
    return prepared, audit
