"""Validate complete raw candles and compare them with hash-verified source CSV."""
from __future__ import annotations

from datetime import datetime,timezone,timedelta
import hashlib
import gzip
import json
import math
from pathlib import Path

from .model import Candle,time_value
from .v2.io import load_series

CN=timezone(timedelta(hours=8))


def candles_from_panel(panel):
    result=[]
    for index,row in enumerate(panel.get('bars',[])):
        if row.get('source_index',index)!=index:raise ValueError('raw candle indices must be contiguous and start at zero')
        text=row.get('closed_at')
        if text is None:
            text=datetime.fromisoformat(row['date_cn']).replace(tzinfo=CN).isoformat()
        moment=time_value(text)
        if result and moment<=time_value(result[-1].closed_at):raise ValueError('duplicate or descending candle close time')
        result.append(Candle(index,*(row[k] for k in ['open','high','low','close']),text,row.get('volume'),row.get('is_closed',True)))
    return result


def audit_panel(panel):
    result={'schema':'kline.audit.v1','period':panel.get('period'),'rows_loaded':len(panel.get('bars',[])),
            'rows_declared':panel.get('input_bars'),'errors':[],'price_precision':2,
            'listing_inception_proven':False,'missing_sessions_proven_absent':False,
            'scope':'all available raw candles; no resampling, padding or inclusion merge in raw display'}
    if panel.get('kind')=='line':return {**result,'state':'not_ohlc','reason':'price snapshots do not contain full OHLC'}
    try:
        bars=candles_from_panel(panel)
        if not bars:raise ValueError('no raw OHLC candles')
        if panel.get('input_bars',len(bars))!=len(bars):raise ValueError('chart history truncated relative to declared candle count')
        precision=2
        for b in bars:
            if min(b.open,b.high,b.low,b.close)<=0:raise ValueError('raw OHLC must be positive')
            for value in [b.open,b.high,b.low,b.close]:
                precision=max(precision,len(f'{value:.8f}'.rstrip('0').split('.')[-1]))
        payload=[[b.closed_at,b.open,b.high,b.low,b.close,b.volume,b.closed] for b in bars]
        result.update(state='raw_validated',first_close=bars[0].closed_at,last_close=bars[-1].closed_at,
                      closed_rows=sum(b.closed for b in bars),forming_rows=sum(not b.closed for b in bars),
                      price_precision=precision,raw_ohlcv_sha256=hashlib.sha256(json.dumps(payload,allow_nan=False,separators=(',',':')).encode()).hexdigest())
    except (ValueError,KeyError,TypeError) as exc:result.update(state='invalid',errors=[str(exc)])
    return result


def verify_source(panel,receipt,symbol,root,cutoff):
    audit=audit_panel(panel)
    if audit['state'] in {'invalid','not_ohlc'}:return audit
    receipt=receipt or {};name=receipt.get('file',receipt.get('csv_path'));audit['source_state']='source_unavailable'
    if not name:return audit
    path=Path(name);path=path if path.is_absolute() else Path(root)/path;path=path.resolve()
    if not path.is_relative_to(Path(root).resolve()):
        audit['source_state']='source_outside_project';return audit
    if not path.is_file():return audit
    try:
        raw=path.read_bytes();expected=receipt.get('file_sha256')
        if expected and hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('source file storage hash changed')
        metadata=receipt.get('metadata_file');meta_path=Path(metadata) if metadata else None
        if meta_path and not meta_path.is_absolute():meta_path=Path(root)/meta_path
        if meta_path and (not meta_path.resolve().is_relative_to(Path(root).resolve()) or not meta_path.is_file()):meta_path=None
        series,checked=load_series(path,symbol,panel['period'],receipt.get('adjustment','unadjusted'),receipt.get('source','tdx'),meta_path)
        if receipt.get('csv_sha256') and checked['csv_sha256']!=receipt['csv_sha256']:raise ValueError('source CSV hash differs from saved receipt')
        source=[b for b in series.bars if time_value(b.closed_at)<=time_value(cutoff)]
        actual=candles_from_panel(panel)
        if len(source)!=len(actual):raise ValueError(f'source/chart row count differs: {len(source)} vs {len(actual)}')
        mismatches=[]
        for index,(a,b) in enumerate(zip(source,actual)):
            if time_value(a.closed_at)!=time_value(b.closed_at) or any(getattr(a,k)!=getattr(b,k) for k in ['open','high','low','close','volume','closed']):
                mismatches.append(index)
        if mismatches:raise ValueError('source/chart OHLCV mismatch at raw indices '+str(mismatches[:8]))
        audit.update(source_state='matched',source_rows=len(source),compared_rows=len(actual),
                     source_csv_sha256=checked['csv_sha256'],source_identity_checked=checked['provider_identity_receipt_checked'])
        audit['provider_response_state']='not_available'
        if meta_path:
            metadata=json.loads(meta_path.read_text(encoding='utf-8-sig'));raw_name=metadata.get('raw_path')
            if raw_name:
                raw_path=Path(raw_name);raw_path=raw_path if raw_path.is_absolute() else Path(root)/raw_path
                if not raw_path.is_file() and meta_path:
                    # A copied/exported cache retains its raw relative layout,
                    # while the original receipt may still name the old root.
                    candidate=meta_path.parent.parent/'raw'/(symbol+'.json.gz')
                    if candidate.is_file():raw_path=candidate
                if raw_path.resolve().is_relative_to(Path(root).resolve()) and raw_path.is_file():
                    stored=raw_path.read_bytes()
                    if metadata.get('raw_storage_sha256') and hashlib.sha256(stored).hexdigest()!=metadata['raw_storage_sha256']:
                        raise ValueError('saved provider response storage hash differs')
                    payload=gzip.decompress(stored) if raw_path.suffix=='.gz' else stored
                    if metadata.get('response_sha256') and hashlib.sha256(payload).hexdigest()!=metadata['response_sha256']:
                        raise ValueError('saved provider response hash differs')
                    if panel['period']=='day':
                        from .tdx_data import normalize_daily
                        data=json.loads(payload);normalized,_,_=normalize_daily(data['data'],time_value(cutoff),metadata.get('source','tdx'))
                        closed=[b for b in actual if b.closed]
                        if len(closed)!=len(normalized):raise ValueError('original provider response/chart closed row count mismatch')
                        for a,b in zip(closed,normalized):
                            if time_value(a.closed_at)!=time_value(b.closed_at) or any(getattr(a,k)!=getattr(b,k) for k in ['open','high','low','close','volume']):
                                raise ValueError('original provider response/chart OHLCV mismatch at '+str(a.pos))
                        audit.update(provider_response_state='hash_and_ohlcv_matched',provider_compared_rows=len(closed),
                                     provider_response_sha256=hashlib.sha256(payload).hexdigest())
                    else:audit['provider_response_state']='hash_matched'
    except (OSError,ValueError,KeyError,TypeError) as exc:
        audit.update(source_state='mismatch');audit['errors'].append(str(exc))
    return audit


def repair_signal_anchors(bundle):
    """Read-time repair preserves the original saved task bytes and rule version."""
    from .signal_anchor import align_signal
    from .causal_replay import source_rule_audit
    reports=bundle.get('evidence',{}).get('reports',{})
    for panel in bundle['chart']['panels']:
        if panel.get('kind')=='line':continue
        audit=audit_panel(panel);panel['kline_audit']=audit
        if audit['state']=='invalid':
            panel['signal_chart']={'version':'signal-chart.v1','objects':[],'rejected':[{'id':'raw-data','reason':'invalid_raw_kline'}]};continue
        bars=candles_from_panel(panel);changes=[];signals=panel.get('all_signals',panel.get('signals',[]))
        report=reports.get(panel['period']);by_id={}
        for point in signals:
            old={k:point.get(k) for k in ['anchor','price','state','invalidation']}
            if point.get('anchor_profile')!='raw_terminal_extremum_v1':point.setdefault('saved_original_point',old)
            align_signal(point,bars)
            current={k:point.get(k) for k in old}
            if current!=old:changes.append({'signal_id':point['id'],'before':old,'after':current})
            by_id[point['id']]=point
        if report:
            report['signals']=[by_id.get(p['id'],p) for p in report.get('signals',signals)]
            report['as_of_index']=len([b for b in bars if b.closed])-1
            for layer in report['hierarchy']['levels']:
                layer['signals']=[by_id.get(p['id'],p) for p in layer.get('signals',[])]
            for point in signals:
                try:
                    result=source_rule_audit(point,report)
                    point['rule_audit']=result
                except (KeyError,ValueError,StopIteration) as exc:
                    point['rule_audit']={'passed':False,'missing':['saved_structure_evidence_incomplete'],'error':str(exc)}
        panel['all_signals']=signals;panel['signals']=[p for p in signals if p.get('rank')==1 and p['state']!='invalidated']
        panel['anchor_corrections']=changes;panel['anchor_profile']='raw_terminal_extremum_v1'
    return bundle
