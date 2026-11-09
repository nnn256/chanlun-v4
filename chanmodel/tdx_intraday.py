"""Unadjusted TDX intraday normalization with explicit exchange close labels."""
from __future__ import annotations

from datetime import datetime, time
import csv
import gzip
import io
import json
import math
from pathlib import Path

from .model import Candle, load_csv, time_value
from .tdx_data import CN, TdxClient, TdxError, atomic_bytes, atomic_json, sha256, symbol, yuan

PERIOD_MINUTES = {'minute30':30, 'minute5':5}


def fetch_intraday(client: TdxClient, code: str, period: str):
    if period not in PERIOD_MINUTES:
        raise ValueError('intraday period must be minute30 or minute5')
    return client.request('/api/kline-all/tdx', {'code':symbol(code), 'type':period})


def valid_session_close(point: datetime, minutes: int) -> bool:
    """Chinese equity periods close on each session's grid; lunch is not padded."""
    local = point.astimezone(CN)
    if local.second or local.microsecond or local.weekday() >= 5:
        return False
    minute_of_day = local.hour*60 + local.minute
    for start, end in [(9*60+30,11*60+30),(13*60,15*60)]:
        if start < minute_of_day <= end and (minute_of_day-start)%minutes == 0:
            return True
    return False


def normalize_intraday(data, as_of: datetime, period: str):
    if period not in PERIOD_MINUTES:
        raise ValueError('intraday period must be minute30 or minute5')
    if as_of.tzinfo is None:
        raise ValueError('as_of requires an explicit timezone')
    if not isinstance(data, dict):
        raise TdxError('intraday response must be an object')
    rows = data.get('list',data.get('List'))
    count = data.get('count',data.get('Count'))
    if rows is None and count == 0:
        rows = []
    if not isinstance(rows,list):
        raise TdxError('intraday response has no list')
    if count is not None and int(count) != len(rows):
        raise TdxError('intraday count differs from returned rows')
    metadata = data.get('meta',{})
    if metadata.get('source') not in {None,'tdx'} or metadata.get('type') not in {None,period}:
        raise TdxError('intraday source/period differs from explicit request')
    unique, input_times, duplicates = {}, [], 0
    for row in rows:
        if not isinstance(row,dict):
            raise TdxError('invalid intraday row')
        raw_time = str(row.get('Time',''))
        try:
            point = datetime.fromisoformat(raw_time.replace('Z','+00:00'))
        except ValueError as exc:
            raise ValueError('invalid intraday Time: '+raw_time) from exc
        if point.tzinfo is None:
            raise ValueError('intraday Time lacks timezone; cannot prove session-close alignment')
        point = point.astimezone(CN)
        if not valid_session_close(point,PERIOD_MINUTES[period]):
            raise ValueError('off-session or off-grid intraday closing timestamp: '+raw_time)
        if point.date() > as_of.astimezone(CN).date():
            raise ValueError('provider returned a future session date')
        prices = [yuan(row.get(key),key) for key in ['Open','High','Low','Close']]
        if any(price<=0 for price in prices):
            raise ValueError('intraday OHLC must be positive; missing bars are not padded')
        lot_value=row.get('Volume')
        if lot_value is None or isinstance(lot_value,bool):
            raise ValueError('missing/invalid intraday volume')
        lots=float(lot_value)
        if not math.isfinite(lots) or lots<0:
            raise ValueError('invalid intraday volume')
        amount=None if row.get('Amount') is None else yuan(row['Amount'],'Amount')
        if amount is not None and amount<0:
            raise ValueError('negative intraday amount')
        key=point.isoformat()
        result={'closed_at':key,'open':prices[0],'high':prices[1],'low':prices[2],'close':prices[3],
                'volume':lots*100,'volume_lots':lots,'amount_yuan':amount,'is_closed':point<=as_of,
                'source_time_raw':raw_time,'trade_date':point.date().isoformat()}
        Candle(0,*prices,key,lots*100,result['is_closed'])
        if key in unique:
            if unique[key]!=result:
                raise ValueError('conflicting duplicate intraday bar at '+key)
            duplicates+=1
        unique[key]=result;input_times.append(key)
    ordered=[unique[key] for key in sorted(unique)]
    usable=[row for row in ordered if row['is_closed']]
    bars=[Candle(i,row['open'],row['high'],row['low'],row['close'],row['closed_at'],row['volume'],True) for i,row in enumerate(usable)]
    quality={'raw_rows':len(rows),'unique_rows':len(ordered),'closed_rows':len(usable),'open_rows_excluded':len(ordered)-len(usable),
             'identical_duplicates_removed':duplicates,'input_order':'ascending' if input_times==sorted(input_times) else 'reordered',
             'first_close':usable[0]['closed_at'] if usable else None,'last_close':usable[-1]['closed_at'] if usable else None,
             'source':'tdx','period':period,'adjustment':'unadjusted','price_unit':'CNY','volume_unit':'shares',
             'original_volume_unit':'lots_of_100_shares','amount_unit':'CNY','session_timezone':'+08:00',
             'timestamp_semantics':'bar close on separate 09:30-11:30 / 13:00-15:00 session grids',
             'session_gaps_padded':False,'provider_meta':metadata,'as_of':as_of.isoformat(),
             'coverage_claim':'all intraday bars returned by provider; no listing-inception or unlimited-history claim'}
    return bars,ordered,quality


def cache_intraday(directory: Path, code, period, rows, raw, receipt, quality, compressed_raw=None):
    folder=directory/'tdx'/period
    raw_path=folder/'raw'/f'{code}.json.gz'
    csv_path=folder/'ohlcv'/f'{code}.csv.gz'
    meta_path=folder/'metadata'/f'{code}.json'
    compressed=compressed_raw if compressed_raw is not None else gzip.compress(raw,compresslevel=6,mtime=0)
    atomic_bytes(raw_path,compressed)
    fields=['closed_at','open','high','low','close','volume','is_closed','trade_date','volume_lots','amount_yuan','source_time_raw']
    stream=io.StringIO(newline='');writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
    for row in rows:
        writer.writerow({key:str(row[key]).lower() if key=='is_closed' else row[key] for key in fields})
    csv_raw=stream.getvalue().encode('utf-8');csv_compressed=gzip.compress(csv_raw,compresslevel=6,mtime=0)
    atomic_bytes(csv_path,csv_compressed)
    meta={**receipt,**quality,'symbol':code,'raw_path':str(raw_path.resolve()),'csv_path':str(csv_path.resolve()),
          'raw_storage_sha256':sha256(compressed),'csv_sha256':sha256(csv_raw),'csv_storage_sha256':sha256(csv_compressed),
          'normalization_schema':'tdx_intraday_v1','csv_compression':'gzip'}
    atomic_json(meta_path,meta)
    return meta


def load_intraday_cache(directory: Path, code, period, as_of: datetime):
    """Re-normalize verified response bytes at the requested cutoff."""
    folder=directory/'tdx'/period
    meta=json.loads((folder/'metadata'/f'{code}.json').read_text(encoding='utf-8'))
    compressed=(folder/'raw'/f'{code}.json.gz').read_bytes()
    if sha256(compressed)!=meta['raw_storage_sha256']:
        raise ValueError('intraday raw storage hash mismatch')
    raw=gzip.decompress(compressed)
    if sha256(raw)!=meta['response_sha256']:
        raise ValueError('intraday raw response hash mismatch')
    envelope=json.loads(raw)
    if envelope.get('code')!=0:
        raise TdxError('cached provider response was not successful')
    bars,rows,quality=normalize_intraday(envelope['data'],as_of,period)
    return bars,rows,quality,meta,raw


def load_daily_cache(directory: Path, code, as_of: datetime):
    folder=directory/'tdx'/'day'
    metadata=json.loads((folder/'metadata'/f'{code}.json').read_text(encoding='utf-8'))
    csv_path=folder/'ohlcv'/f'{code}.csv'
    if metadata.get('source')!='tdx' or metadata.get('adjustment')!='unadjusted':
        raise ValueError('daily input must be explicitly unadjusted TDX data')
    if sha256(csv_path.read_bytes())!=metadata['csv_sha256']:
        raise ValueError('daily CSV hash mismatch')
    bars,loaded=load_csv(csv_path)
    bars=[bar for bar in bars if bar.closed and time_value(bar.closed_at)<=as_of]
    return bars,metadata
