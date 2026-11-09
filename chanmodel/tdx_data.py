"""Explicit TDX REST adapter; prices, raw evidence and coverage stay auditable."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import csv
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import threading
import time as time_module
import urllib.error
import urllib.parse
import urllib.request

from .model import Candle

CN = timezone(timedelta(hours=8))


def now_iso():
    return datetime.now(CN).isoformat(timespec='seconds')


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def atomic_bytes(path: Path, raw: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.tmp-{threading.get_ident()}')
    temporary.write_bytes(raw)
    for attempt in range(8):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if attempt==7:raise
            # Windows readers/virus scanners can briefly deny replace on a
            # progress file. The existing file remains valid until retry wins.
            time_module.sleep(.025*(attempt+1))


def atomic_json(path: Path, value):
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode('utf-8'))


def symbol(value):
    value = str(value).strip().lower()
    if not re.fullmatch(r'(?:sh|sz|bj)\d{6}', value):
        raise ValueError('require exchange-prefixed stock code, for example sh600000')
    ex, code = value[:2], value[2:]
    valid = (ex == 'sh' and code.startswith('6')) or (ex == 'sz' and code.startswith(('0', '30'))) or (ex == 'bj' and code.startswith(('92', '43', '8')))
    if not valid:
        raise ValueError(f'not an A-share code under this universe definition: {value}')
    return value


class TdxError(RuntimeError):
    pass


class TdxClient:
    def __init__(self, base_url=None, timeout=45):
        base_url = base_url or os.environ.get('CHANLUN_TDX_BASE_URL','http://127.0.0.1:18080')
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError('base_url must be an HTTP(S) service origin without query/fragment')
        if timeout <= 0:
            raise ValueError('timeout must be positive')
        self.base_url, self.timeout = base_url.rstrip('/'), timeout
        # Windows desktop processes can inherit a stale system proxy which
        # incorrectly sends 127.0.0.1 requests to another local port. Local
        # service traffic must never depend on that proxy being available.
        self._local_opener=urllib.request.build_opener(urllib.request.ProxyHandler({})) if parsed.hostname in {'127.0.0.1','localhost','::1'} else None

    def request(self, path, parameters=None, payload=None):
        if not path.startswith('/api/'):
            raise ValueError('only documented /api/ paths are accepted')
        url = self.base_url + path
        if parameters:
            url += '?' + urllib.parse.urlencode(parameters)
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        request = urllib.request.Request(url, data=body, headers={'Accept':'application/json', 'Content-Type':'application/json', 'User-Agent':'ChanlunResearchTDX/1.0'})
        requested = now_iso()
        open_request=self._local_opener.open if self._local_opener else urllib.request.urlopen
        with open_request(request, timeout=self.timeout) as response:
            raw = response.read()
            content_type = response.headers.get('Content-Type', '')
            status = response.status
        received = now_iso()
        try:
            envelope = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise TdxError(f'{path}: response is not JSON (HTTP {status}, {content_type}, {len(raw)} bytes)') from exc
        if not isinstance(envelope, dict) or envelope.get('code') != 0 or 'data' not in envelope:
            message = envelope.get('message') if isinstance(envelope, dict) else 'invalid response envelope'
            raise TdxError(f'{path}: {message}')
        receipt = {'url':url, 'requested_at':requested, 'received_at':received,
                   'http_status':status, 'content_type':content_type, 'response_bytes':len(raw), 'response_sha256':sha256(raw)}
        return envelope['data'], receipt, raw

    def request_retry(self,path,parameters=None,payload=None,*,attempts=3,on_retry=None):
        """Retry transient upstream reads; malformed data still fail validation."""
        failures=[]
        for index in range(attempts):
            try:
                data,receipt,raw=self.request(path,parameters,payload)
                if failures:receipt['transient_failures_before_success']=failures
                receipt['request_attempts']=index+1
                return data,receipt,raw
            except (OSError,TdxError) as exc:
                transient=isinstance(exc,OSError) or any(token in str(exc).lower() for token in ('超时','timeout','timed out','连接','connection','temporar','eof'))
                if not transient or index+1==attempts:raise
                failure={'attempt':index+1,'error':f'{type(exc).__name__}: {exc}','at':now_iso()}
                failures.append(failure)
                if on_retry:on_retry({**failure,'next_attempt':index+2,'max_attempts':attempts})
                time_module.sleep(min(.5*(index+1),2))

    def universe(self):
        return self.request('/api/codes', {'exchange':'all'})

    def history(self, code, source='tdx'):
        if source not in {'tdx', 'ths'}:
            raise ValueError('source must be explicit tdx or ths')
        return self.request('/api/kline-all/'+source, {'code':symbol(code), 'type':'day'})

    def quotes(self, codes):
        if not 1 <= len(codes) <= 50:
            raise ValueError('quote batch size must be 1..50')
        return self.request('/api/batch-quote', payload={'codes':[symbol(code) for code in codes]})


def normalize_universe(data):
    if not isinstance(data, dict):
        raise TdxError('universe payload must be an object')
    rows = data.get('Codes', data.get('codes'))
    if not isinstance(rows, list):
        raise TdxError('universe payload has no Codes list')
    declared = data.get('Total', data.get('total'))
    if declared is not None and int(declared) != len(rows):
        raise TdxError('universe Total does not match returned rows')
    selected, exclusions = {}, []
    for row in rows:
        if not isinstance(row, dict):
            raise TdxError('invalid universe row')
        exchange = str(row.get('exchange', row.get('Exchange', ''))).lower()
        bare = str(row.get('code', row.get('Code', ''))).strip()
        try:
            full = symbol(bare if re.fullmatch(r'(sh|sz|bj)\d{6}', bare.lower()) else exchange+bare)
        except ValueError:
            exclusions.append(row)
            continue
        record = {'symbol':full, 'exchange':full[:2], 'code':full[2:], 'name':str(row.get('name', row.get('Name', '')))}
        if full in selected and selected[full] != record:
            raise TdxError(f'conflicting universe records for {full}')
        selected[full] = record
    if not selected:
        raise TdxError('empty A-share universe')
    counts = {exchange:sum(row['exchange']==exchange for row in selected.values()) for exchange in ['sh','sz','bj']}
    missing = [exchange for exchange, count in counts.items() if count == 0]
    upstream_coverage = data.get('coverage', data.get('Coverage'))
    coverage = {'exchanges':counts, 'missing_exchanges':missing, 'upstream_coverage':upstream_coverage,
                'source_rows':len(rows), 'selected_rows':len(selected), 'excluded_rows':len(exclusions),
                'complete_exchange_presence':not missing,
                'historical_universe_complete':False,
                'universe_semantics':'current provider-listed A-share codes; not a point-in-time or all-delisted historical universe'}
    return sorted(selected.values(), key=lambda row:row['symbol']), coverage, exclusions


def yuan(value, field):
    if isinstance(value, bool):
        raise ValueError(f'{field}: invalid boolean price')
    try:
        result = float(Decimal(str(value))/Decimal(1000))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f'{field}: invalid protocol price') from exc
    if not math.isfinite(result):
        raise ValueError(f'{field}: non-finite price')
    return result


def normalize_daily(data, as_of: datetime, source='tdx'):
    if as_of.tzinfo is None:
        raise ValueError('as_of requires an explicit timezone')
    if not isinstance(data, dict):
        raise TdxError('history payload must be an object')
    rows = data.get('list', data.get('List'))
    count = data.get('count', data.get('Count'))
    if rows is None and count == 0:
        rows = []
    if not isinstance(rows, list):
        raise TdxError('history payload has no list')
    if count is not None and int(count) != len(rows):
        raise TdxError('history count does not match rows; possible truncation')
    metadata = data.get('meta', {})
    if metadata.get('source') not in {None, source}:
        raise TdxError('provider returned a different source than requested')
    if metadata.get('type') not in {None, 'day'}:
        raise TdxError('provider returned a different bar period')
    by_date, duplicates, input_dates = {}, 0, []
    for row in rows:
        if not isinstance(row, dict):
            raise TdxError('history row must be an object')
        raw_time = str(row.get('Time', ''))
        try:
            timestamp = datetime.fromisoformat(raw_time.replace('Z','+00:00'))
        except ValueError as exc:
            raise ValueError(f'invalid provider daily Time: {raw_time}') from exc
        # Daily Time labels encode the session date. Construct the declared
        # exchange close explicitly; never treat a receiver clock as a trade date.
        session_date = timestamp.date()
        close_at = datetime.combine(session_date, time(15,0), CN)
        values = [yuan(row.get(field), field) for field in ['Open','High','Low','Close']]
        if any(price <= 0 for price in values):
            raise ValueError(f'{session_date}: nonpositive OHLC; do not fill missing/suspended bars')
        lots_value = row.get('Volume')
        if isinstance(lots_value, bool) or lots_value is None:
            raise ValueError(f'{session_date}: missing/invalid volume')
        lots = float(lots_value)
        if not math.isfinite(lots) or lots < 0:
            raise ValueError(f'{session_date}: invalid volume')
        amount = None if row.get('Amount') is None else yuan(row['Amount'],'Amount')
        if amount is not None and amount < 0:
            raise ValueError(f'{session_date}: negative amount')
        record = {'closed_at':close_at.isoformat(), 'trade_date':session_date.isoformat(),
                  'open':values[0], 'high':values[1], 'low':values[2], 'close':values[3],
                  'volume':lots*100, 'volume_lots':lots, 'amount_yuan':amount,
                  'is_closed':close_at<=as_of, 'source_time_raw':raw_time}
        Candle(0, *values, close_at.isoformat(), lots*100, record['is_closed'])
        if record['trade_date'] in by_date:
            if by_date[record['trade_date']] != record:
                raise ValueError(f'conflicting duplicate daily bar on {session_date}')
            duplicates += 1
        by_date[record['trade_date']] = record
        input_dates.append(record['trade_date'])
    ordered = [by_date[key] for key in sorted(by_date)]
    usable = [row for row in ordered if row['is_closed']]
    current = [row for row in ordered if not row['is_closed']]
    beyond_today = [row for row in current if row['trade_date']>as_of.astimezone(CN).date().isoformat()]
    if beyond_today:
        raise ValueError('provider returned future session dates')
    bars = [Candle(i, row['open'],row['high'],row['low'],row['close'],row['closed_at'],row['volume'],True) for i,row in enumerate(usable)]
    quality = {'raw_rows':len(rows),'unique_rows':len(ordered),'closed_rows':len(usable),'open_rows_excluded':len(current),
               'identical_duplicates_removed':duplicates,'input_order':'ascending' if input_dates==sorted(input_dates) else 'reordered',
               'first_date':usable[0]['trade_date'] if usable else None,'last_date':usable[-1]['trade_date'] if usable else None,
               'adjustment':'unadjusted' if source=='tdx' else 'qfq_current_vintage',
               'price_unit':'CNY','volume_unit':'shares','original_volume_unit':'lots_of_100_shares',
               'amount_unit':'CNY','session_timezone':'+08:00','daily_close':'15:00:00',
               'provider_meta':metadata,'coverage_claim':'all bars returned by provider; listing-inception coverage not verified',
               'as_of':as_of.isoformat()}
    return bars, ordered, quality


def cache_history(directory: Path, code, source, rows, raw, receipt, quality):
    folder = directory / source / 'day'
    raw_path, csv_path, meta_path = folder/'raw'/f'{code}.json.gz', folder/'ohlcv'/f'{code}.csv', folder/'metadata'/f'{code}.json'
    atomic_bytes(raw_path, gzip.compress(raw, mtime=0))
    stream = io.StringIO(newline='')
    fields=['closed_at','open','high','low','close','volume','is_closed','trade_date','volume_lots','amount_yuan','source_time_raw']
    writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
    for row in rows:
        writer.writerow({key:str(row[key]).lower() if key=='is_closed' else row[key] for key in fields})
    atomic_bytes(csv_path,stream.getvalue().encode('utf-8'))
    meta = {**receipt, **quality, 'symbol':code, 'source':source,
            'csv_path':str(csv_path.resolve()),'raw_path':str(raw_path.resolve()),'csv_sha256':sha256(csv_path.read_bytes()),
            'raw_storage_sha256':sha256(raw_path.read_bytes()),'normalization_schema':'tdx_daily_v1'}
    atomic_json(meta_path,meta)
    return meta


def normalized_quotes(rows, requested, receipt):
    if not isinstance(rows,list):
        raise TdxError('quote payload must be a list')
    wanted=set(requested);selected={};extras=[]
    for row in rows:
        exchange={0:'sz',1:'sh',2:'bj'}.get(row.get('Exchange'))
        full=(exchange or '')+str(row.get('Code',''))
        if full not in wanted:
            extras.append(full)
            continue
        k=row.get('K') or {}
        point={'symbol':full,'received_at':receipt['received_at'],'server_time_raw':row.get('ServerTime'),
               'price':yuan(k.get('Close'),'Close'),'previous_close':yuan(k.get('Last'),'Last'),
               'open':yuan(k.get('Open'),'Open'),'high':yuan(k.get('High'),'High'),'low':yuan(k.get('Low'),'Low'),
               'volume_lots':row.get('TotalHand'),'amount_yuan':row.get('Amount'),
               'exchange_trade_timestamp':None,'timestamp_quality':'provider raw field is not a decoded full trade timestamp',
               'freshness':'not_proven_by_received_at','source':'tdx','adjustment':'unadjusted'}
        if point['price']<=0:
            point['quote_status']='no_positive_price'
        else:
            point['quote_status']='observed'
        if full in selected:
            raise TdxError(f'duplicate quote for {full}')
        selected[full]=point
    return list(selected.values()),sorted(wanted-set(selected)),extras
