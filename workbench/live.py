"""Actual provider candles for display; only closed candles enter the engine."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime
import gzip
import hashlib
import json
import threading
import time

from chanmodel.model import Candle
from chanmodel.tdx_data import CN, TdxClient, atomic_bytes, normalize_daily, symbol
from chanmodel.tdx_intraday import normalize_intraday
from chanmodel.v2.series import Series
from chanmodel.kline_audit import audit_panel, candles_from_panel
from .compute import make_chart, local
from .reference_service import reference_report
from .store import STATE, connect

PERIODS = {'day': 'day', '30m': 'minute30', '5m': 'minute5'}
CACHE = OrderedDict()
LOCK = threading.Lock()


def acquire(code, period):
    data, receipt, raw = TdxClient(timeout=40).request(
        '/api/kline-all/tdx', {'code': code, 'type': PERIODS[period]})
    # A request made before a candle closes cannot prove its final OHLCV,
    # even if network delivery completes just after the close.
    observed = datetime.fromisoformat(receipt['requested_at'])
    closed, rows, quality = (normalize_daily(data, observed) if period == 'day'
                             else normalize_intraday(data, observed, PERIODS[period]))
    if not rows or not closed:
        raise ValueError(f'{code} {period}: 行情接口没有返回可用的真实K线')
    if period != 'day':
        # Permit the current scheduled interval endpoint, never later intervals.
        from chanmodel.v2.series import session_closes
        upcoming = next((p for p in session_closes(observed.date(), period) if p > observed), None)
        if any(not r['is_closed'] and (upcoming is None or datetime.fromisoformat(r['closed_at']) != upcoming)
               for r in rows):
            raise ValueError('行情接口返回了尚未开始的分钟区间')
    bars = tuple(Candle(i, *(r[k] for k in ('open', 'high', 'low', 'close')),
                        r['closed_at'], r['volume'], r['is_closed']) for i, r in enumerate(rows))
    series = Series(code, period, 'unadjusted', bars, 'tdx_live_ohlcv', receipt['response_sha256'])
    source_path = STATE / 'live' / code / period / (receipt['response_sha256'] + '.json.gz')
    if not source_path.exists():
        atomic_bytes(source_path, gzip.compress(raw, compresslevel=3, mtime=0))
    receipt = {**receipt, 'raw_path': str(source_path.relative_to(STATE)), 'quality': quality,
               'observed_at': observed.isoformat(), 'period': period,
               'response_kind': 'provider_ohlcv_not_quote_synthesis'}
    return series, receipt


def build_live(record, inputs, receipts):
    reports = {}
    for period, series in inputs.items():
        closed = tuple(b for b in series.bars if b.closed)
        # Intrabar ticks do not change the confirmed prefix or its cache key.
        reports[period] = reference_report(series.symbol, period, closed,
                                            closed[-1].closed_at, series.adjustment, series.source)
    cutoff = max(r['observed_at'] for r in receipts.values())
    chart, summary = make_chart(record, inputs, reports, cutoff, include_forming=True)
    coverage = []
    for panel in chart['panels']:
        period = panel['period']; receipt = receipts[period]
        bars = panel['bars']; closed = [b for b in bars if b['is_closed']]
        forming = sum(not b['is_closed'] for b in bars)
        latest = bars[-1]['closed_at']
        panel.update(kline_audit=audit_panel(panel), observed_at=receipt['observed_at'],
                     received_at=receipt['received_at'], confirmed_through=closed[-1]['closed_at'],
                     latest_bar_at=latest, forming_count=forming,
                     source_receipt=receipt,
                     coverage={'source': 'TDX实时原始OHLCV · 不复权', 'forming_count': forming},
                     note=('最新K线形成中 · 确认结构截止 ' if forming else '已收盘结构截止 ')
                          + local(closed[-1]['closed_at']))
        coverage.append({'period': period, 'count': len(bars), 'first': bars[0]['date_cn'],
                         'last': bars[-1]['date_cn'], 'source': 'TDX实时原始OHLCV', 'forming_count': forming})
        summary['periods'][period].update(confirmed_through=local(closed[-1]['closed_at']), forming_count=forming)
    daily = next(p for p in chart['panels'] if p['period'] == 'day')
    meta = {'mode': 'live', 'received_at': receipts['day']['received_at'],
            'observed_at': receipts['day']['observed_at'], 'latest_bar_at': daily['latest_bar_at'],
            'confirmed_through': daily['confirmed_through'], 'forming': bool(daily['forming_count']),
            'latest_session_is_today': daily['latest_bar_at'][:10] == receipts['day']['observed_at'][:10],
            'freshness': 'provider_session_label_only; receipt_time_is_not_last_trade_time',
            'source': 'TDX原始OHLCV · 不复权', 'refresh_seconds': 15}
    summary.update(live=meta, history_coverage=coverage, mode='live',
                   result_text='实时K线；形成中的K线不参与确认结构，买卖候选仍须按结构复核')
    chart.update(live=meta, result_text=summary['result_text'],
                 snapshot_semantics='live provider OHLCV; confirmed engine uses closed candles only')
    return {'available': True, 'summary': summary, 'chart': chart,
            'evidence': {'source_receipts': receipts, 'reference_reports': reports}}


def audit_live_panel(panel):
    result = audit_panel(panel)
    receipt = panel['source_receipt']
    path = (STATE / receipt['raw_path']).resolve()
    if not path.is_relative_to((STATE / 'live').resolve()):
        raise ValueError('实时原始数据路径无效')
    raw = gzip.decompress(path.read_bytes())
    if hashlib.sha256(raw).hexdigest() != receipt['response_sha256']:
        raise ValueError('实时原始响应SHA256不匹配')
    data = json.loads(raw)['data']; observed = datetime.fromisoformat(receipt['observed_at'])
    _, rows, _ = (normalize_daily(data, observed) if panel['period'] == 'day'
                  else normalize_intraday(data, observed, PERIODS[panel['period']]))
    expected = [Candle(i, *(r[k] for k in ('open', 'high', 'low', 'close')),
                       r['closed_at'], r['volume'], r['is_closed']) for i, r in enumerate(rows)]
    if candles_from_panel(panel) != expected:
        raise ValueError('实时图表与保存的原始行情响应不一致')
    return {**result, 'source_state': 'matched', 'compared_rows': len(expected),
            'source_kind': 'provider_raw_response', 'source_csv_sha256': receipt['response_sha256'],
            'provider_response_state': 'hash_verified_and_renormalized', 'received_at': receipt['received_at']}


def live_stock(code, period='day', full=False, fresh=False):
    code = symbol(code)
    if period not in {*PERIODS, 'all'}:
        raise ValueError('实时结构支持日线、30分钟、5分钟')
    selected = tuple(PERIODS) if period == 'all' else tuple(dict.fromkeys(('day', period)))
    identity = (code, selected, TdxClient().base_url)
    # Serialize a single selected-stock refresh so concurrent tabs do not replay
    # the expensive engine simultaneously or race the same source receipts.
    with LOCK:
        cached = CACHE.get(identity)
        if not fresh and cached and time.monotonic() - cached[0] < 10:
            value = copy.deepcopy(cached[1])
        else:
            with connect() as db:
                row = db.execute('SELECT * FROM universe WHERE symbol=?', (code,)).fetchone()
            record = dict(row) if row else {'symbol': code, 'code': code[2:], 'name': code, 'exchange': code[:2]}
            with ThreadPoolExecutor(max_workers=len(selected)) as pool:
                acquired = dict(zip(selected, pool.map(lambda p: acquire(code, p), selected)))
            value = build_live(record, {p: v[0] for p, v in acquired.items()},
                               {p: v[1] for p, v in acquired.items()})
            CACHE[identity] = (time.monotonic(), copy.deepcopy(value)); CACHE.move_to_end(identity)
            while len(CACHE) > 8:
                CACHE.popitem(last=False)
        if not full:
            value.pop('evidence', None)
        return value
