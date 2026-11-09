"""Load both plain and compressed research CSVs with optional provider receipts."""
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path

from ..model import Candle,parse_time
from .series import Series


def load_series(path,symbol,period,adjustment,source,metadata_path=None):
    path=Path(path);stored=path.read_bytes();raw=gzip.decompress(stored) if path.suffix=='.gz' else stored
    digest=hashlib.sha256(raw).hexdigest()
    metadata=None
    if metadata_path:
        metadata=json.loads(Path(metadata_path).read_text(encoding='utf-8-sig'))
        metadata_period=metadata.get('period',metadata.get('provider_meta',{}).get('type','day'))
        normalized_period={'minute1':'1m','minute5':'5m','minute15':'15m','minute30':'30m','hour':'60m'}.get(metadata_period,metadata_period)
        normalized_adjustment={'qfq_current_vintage':'qfq'}.get(metadata.get('adjustment'),metadata.get('adjustment'))
        if metadata.get('symbol')!=symbol or normalized_period!=period or normalized_adjustment!=adjustment:
            raise ValueError('requested symbol/period/adjustment differ from saved provider receipt')
        if metadata.get('source')!=source:raise ValueError('requested source differs from provider receipt')
        if metadata.get('csv_sha256')!=digest:raise ValueError('CSV contents differ from provider receipt hash')
        if path.suffix=='.gz' and metadata.get('csv_storage_sha256')!=hashlib.sha256(stored).hexdigest():
            raise ValueError('compressed CSV hash mismatch')
    reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig'),newline=''))
    headers=reader.fieldnames or []
    if len(set(headers))!=len(headers) or not {'closed_at','open','high','low','close'}<=set(headers):
        raise ValueError('v2 CSV requires unique closed_at/open/high/low/close columns')
    bars=[]
    for i,row in enumerate(reader):
        if None in row:raise ValueError(f'extra CSV fields at row {i+2}')
        time,assumed=parse_time(row['closed_at'])
        if assumed:raise ValueError('v2 requires explicit timezone; no silent clock assumption')
        flag=(row.get('is_closed') or 'true').lower()
        if flag not in {'true','false','1','0'}:raise ValueError('invalid is_closed flag')
        if row.get('symbol') and row['symbol']!=symbol:raise ValueError('CSV row belongs to another symbol')
        if row.get('period') and row['period']!=period:raise ValueError('CSV row belongs to another period')
        volume=float(row['volume']) if row.get('volume') else None
        bars.append(Candle(i,*(float(row[key]) for key in ['open','high','low','close']),time,volume,flag in {'true','1'}))
    if not bars:raise ValueError('CSV has no rows')
    result=Series(symbol,period,adjustment,tuple(bars),source,digest)
    receipt={'file':str(path.resolve()),'file_sha256':hashlib.sha256(stored).hexdigest(),'csv_sha256':digest,
             'metadata_file':str(Path(metadata_path).resolve()) if metadata_path else None,
             'provider_identity_receipt_checked':metadata is not None,
             'identity_provenance':'saved provider metadata with matching content hash' if metadata else 'explicit caller declaration; not independently authenticated',
             'rows':len(bars)}
    return result,receipt


def load_cache_series(cache,symbol,period):
    cache=Path(cache);part={'day':'day','30m':'minute30','5m':'minute5','1m':'minute1'}.get(period)
    if not part:raise ValueError('cache helper supports day/30m/5m/1m')
    folder=cache/'tdx'/part
    return load_series(folder/'ohlcv'/(symbol+('.csv' if period=='day' else '.csv.gz')),
                       symbol,period,'unadjusted','tdx',folder/'metadata'/(symbol+'.json'))
