#!/usr/bin/env python3
"""Download a provider-wide A-share daily universe and run Chan structure scans.

Checkpointed current-cutoff structure scan. This is not historical event replay
or a survivorship-free strategy backtest. Explicit source never silently changes.
"""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import time
import traceback

from chanmodel.engine import analyze
from chanmodel.model import Config
from chanmodel.tdx_data import (CN,TdxClient,TdxError,atomic_bytes,atomic_json,cache_history,
                               normalize_daily,normalize_universe,normalized_quotes,now_iso,sha256,symbol)

ROOT=Path(__file__).resolve().parent


def engine_fingerprint():
    paths=sorted((ROOT/'chanmodel').glob('*.py'))+[Path(__file__).resolve()]
    return sha256(b''.join(p.name.encode()+p.read_bytes() for p in paths))


def date_age(as_of, last_date):
    return (as_of.astimezone(CN).date()-datetime.fromisoformat(last_date).date()).days if last_date else None


def summarize(report,bars,source,record):
    candidates=report['signal_candidates']
    active=[point for point in candidates if point['status']=='candidate']
    latest=[]
    for point in sorted(active,key=lambda p:(p['anchor'],p['basis_at']),reverse=True)[:12]:
        latest.append({key:point.get(key) for key in ['id','signal_class','side','price','anchor','basis_at','available_at','status','invalidation','invalidation_operator','center_id','zd','zg','definition_mode']})
        latest[-1].update(anchor_time=bars[point['anchor']].closed_at,age_bars=len(bars)-1-point['anchor'])
    return {'symbol':record['symbol'],'name':record['name'],'exchange':record['exchange'],'status':'success',
            'period':'day','source':source['source'],'adjustment':source['adjustment'],
            'first_date':source['first_date'],'last_date':source['last_date'],'bars':len(bars),
            'received_at':source['received_at'],'csv_sha256':source['csv_sha256'],'csv_path':source['csv_path'],
            'current_state':report['current_state'], 'merged_count':len(report['merged']),'fractal_count':len(report['fractals']),
            'stroke_count':len(report['strokes']),'segment_count':len(report['segments']),
            'confirmed_segment_count':sum(x['state']=='confirmed' for x in report['segments']),
            'center_count':len(report['centers']),'layer_count':len(report['layers']),
            'all_historical_candidate_count':len(candidates),'active_candidate_count':len(active),'recent_candidates':latest,
            'active_candidate_counts':{kind:sum(p['signal_class']==kind for p in active) for kind in sorted({p['signal_class'] for p in active})},
            'latest_center':report['centers'][-1] if report['centers'] else None,
            'definition_mode':report['definition_mode'],'structure_completeness':report['structure_completeness'],
            'approximation_loss':report['approximation_loss'],'execution_allowed':False,'action':'observe',
            'analysis_semantics':'full provider history at current cutoff; historical candidate anchors are not historical first-seen signals'}


def quarantine_details(folder,code):
    """Preserve a previously successful detail without presenting it as current."""
    detail=folder/'details'/f'{code}.json.gz'
    if detail.is_file():
        destination=folder/'quarantine'/'details'/f'{code}.{time.time_ns()}.json.gz'
        destination.parent.mkdir(parents=True,exist_ok=True)
        detail.replace(destination)
        return str(destination.resolve())
    return None


def process_stock(record,settings):
    started=time.monotonic();code=record['symbol']
    client=TdxClient(settings['base_url'],settings['timeout'])
    as_of=datetime.fromisoformat(settings['as_of']);source=settings['source']
    cache=Path(settings['cache_dir'])
    raw_path=cache/source/'day/raw'/f'{code}.json.gz'
    meta_path=cache/source/'day/metadata'/f'{code}.json'
    errors=[]
    for attempt in range(settings['retries']+1):
        try:
            cached=False
            if attempt==0 and not settings['refresh'] and meta_path.exists() and raw_path.exists():
                previous=json.loads(meta_path.read_text(encoding='utf-8'))
                if previous.get('cache_run_as_of')==settings['as_of'] and previous.get('source')==source:
                    compressed=raw_path.read_bytes()
                    if sha256(compressed)!=previous.get('raw_storage_sha256'):
                        raise ValueError('cached raw storage hash mismatch')
                    raw=gzip.decompress(compressed)
                    if sha256(raw)!=previous['response_sha256']:
                        raise ValueError('cached API response hash mismatch')
                    envelope=json.loads(raw);data=envelope['data']
                    receipt={key:previous[key] for key in ['url','requested_at','received_at','http_status','content_type','response_bytes','response_sha256']}
                    cached=True
            if not cached:
                data,receipt,raw=client.history(code,source)
            # Persist bytes before validation, so malformed provider data remain
            # inspectable and a failure is never silently replaced with zeros.
            evidence=Path(settings['run_dir'])/'responses'/f'{code}.json.gz'
            atomic_bytes(evidence,gzip.compress(raw,mtime=0))
            bars,rows,quality=normalize_daily(data,as_of,source)
            if (as_of-datetime.fromisoformat(receipt['received_at'])).total_seconds()>0:
                # Provider prices are a current vintage. This flag matters if
                # a saved run is resumed much later, especially for qfq.
                quality['source_received_before_run_cutoff']=True
            quality['cache_run_as_of']=settings['as_of']
            source_meta=cache_history(cache,code,source,rows,raw,receipt,quality)
            if not bars:
                result={**record,'status':'empty_history','elapsed_seconds':round(time.monotonic()-started,3),
                        'quality':quality,'source':source,'engine_sha256':settings['engine_sha256'],
                        'error':'provider returned no closed daily bars; not padded'}
                quarantined=quarantine_details(Path(settings['run_dir']),code)
                if quarantined:result['quarantined_detail']=quarantined
                atomic_json(Path(settings['run_dir'])/'stocks'/f'{code}.json',result)
                return result
            config=Config(**settings['config'])
            report=analyze(bars,config,'day')
            result=summarize(report,bars,source_meta,record)
            result.update(elapsed_seconds=round(time.monotonic()-started,3),cache_hit=cached,
                          last_bar_calendar_age_days=date_age(as_of,source_meta['last_date']),
                          quality=quality,engine_sha256=settings['engine_sha256'])
            folder=Path(settings['run_dir'])
            atomic_json(folder/'stocks'/f'{code}.json',result)
            if settings['full_reports']:
                report['source']=source_meta
                report['engine_sha256']=settings['engine_sha256']
                atomic_bytes(folder/'details'/f'{code}.json.gz',gzip.compress(json.dumps(report,ensure_ascii=False,allow_nan=False).encode(),mtime=0))
            return result
        except Exception as exc:
            errors.append({'attempt':attempt+1,'error':f'{type(exc).__name__}: {exc}','at':now_iso()})
            if attempt<settings['retries']:
                time.sleep(min(0.5*(2**attempt),3))
    result={**record,'status':'failed','elapsed_seconds':round(time.monotonic()-started,3),
            'error':errors[-1]['error'],'attempts':errors,'source':source,'engine_sha256':settings['engine_sha256']}
    quarantined=quarantine_details(Path(settings['run_dir']),code)
    if quarantined:result['quarantined_detail']=quarantined
    atomic_json(Path(settings['run_dir'])/'stocks'/f'{code}.json',result)
    return result


def collect_quotes(client,records,folder):
    results,missing,failures=[],[],[]
    codes=[record['symbol'] for record in records]
    for offset in range(0,len(codes),50):
        batch=codes[offset:offset+50]
        try:
            data,receipt,raw=client.quotes(batch)
            atomic_bytes(folder/'quote-responses'/f'{offset//50:04d}.json.gz',gzip.compress(raw,mtime=0))
            quotes,absent,extra=normalized_quotes(data,batch,receipt)
            results.extend(quotes);missing.extend(absent)
            if extra:failures.append({'batch':batch,'unexpected_codes':extra})
        except Exception as exc:
            failures.append({'batch':batch,'error':f'{type(exc).__name__}: {exc}'})
            missing.extend(batch)
    report={'started_universe_count':len(codes),'quotes':results,'missing_symbols':sorted(set(missing)),
            'failures':failures,'observed_at':now_iso(),'trade_timestamp_verified':False,
            'notes':'Quotes are acquired now; provider does not supply a decoded full trade timestamp. Received time does not prove same-day freshness.'}
    atomic_json(folder/'quotes.json',report)
    return report


def checkpoint(path):
    db=sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS stocks (symbol TEXT PRIMARY KEY,status TEXT NOT NULL,engine_sha256 TEXT NOT NULL,result TEXT NOT NULL,updated_at TEXT NOT NULL)')
    db.commit();return db


def exports(db,folder,metadata,quote_report=None,pending_symbols=None):
    universe={item['symbol'] for item in metadata['universe']}
    fingerprint=metadata['settings']['engine_sha256']
    pending_symbols=set(pending_symbols or ())
    results=[];excluded=[]
    for code,engine,encoded in db.execute('SELECT symbol,engine_sha256,result FROM stocks ORDER BY symbol'):
        reason='outside_recorded_universe' if code not in universe else 'old_engine_fingerprint' if engine!=fingerprint else 'scheduled_for_reprocessing' if code in pending_symbols else None
        if reason:
            excluded.append({'symbol':code,'reason':reason});continue
        item=json.loads(encoded)
        if item.get('symbol')!=code:
            excluded.append({'symbol':code,'reason':'checkpoint_identity_mismatch'});continue
        results.append(item)
    success=[r for r in results if r['status']=='success'];bad=[r for r in results if r['status']!='success']
    total=len(metadata['universe']);done=len(results)
    by_exchange={exchange:{'universe':sum(x['exchange']==exchange for x in metadata['universe']),
                           'success':sum(x['exchange']==exchange for x in success),
                           'failed_or_empty':sum(x['exchange']==exchange for x in bad)} for exchange in ['sh','sz','bj']}
    summary={'run_dir':str(folder.resolve()),'started_at':metadata['started_at'],'updated_at':now_iso(),
             'as_of':metadata['settings']['as_of'],'source':metadata['settings']['source'],'period':'day',
             'universe_count':total,'processed':done,'success':len(success),'failed_or_empty':len(bad),'pending':total-done,
             'all_symbols_finished':done==total,'all_symbols_succeeded':done==total and not bad,
             'coverage':metadata['coverage'],'by_exchange':by_exchange,'total_history_bars':sum(r['bars'] for r in success),
             'history_first_date':min((r['first_date'] for r in success),default=None),
             'history_latest_date':max((r['last_date'] for r in success),default=None),
             'states':{},'active_candidate_counts':{},
             'quotes_count':len(quote_report['quotes']) if quote_report else None,
             'quote_missing_count':len(quote_report['missing_symbols']) if quote_report else None,
             'history_inception_complete':False,'historical_delisted_universe_complete':False,
             'signal_semantics':'current-cutoff structure candidates; no historical event replay or strategy profitability claim',
             'adjustment':'unadjusted' if metadata['settings']['source']=='tdx' else 'qfq_current_vintage',
             'definition_mode':'structure_proxy','execution_allowed':False,'failures':bad}
    summary['checkpoint_rows_excluded_from_current_summary']=excluded
    summary['paused']=False
    for result in success:
        state=result['current_state']['state'];summary['states'][state]=summary['states'].get(state,0)+1
        for kind,count in result['active_candidate_counts'].items():
            summary['active_candidate_counts'][kind]=summary['active_candidate_counts'].get(kind,0)+count
    atomic_json(folder/'summary.json',summary)
    fields=['symbol','name','exchange','status','bars','first_date','last_date','adjustment','stroke_count','segment_count','center_count','active_candidate_count','last_bar_calendar_age_days','elapsed_seconds','error']
    stream=io.StringIO(newline='');writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
    for result in results:writer.writerow({key:result.get(key) for key in fields})
    atomic_bytes(folder/'summary.csv',stream.getvalue().encode('utf-8-sig'))
    atomic_bytes(folder/'failures.jsonl',''.join(json.dumps(item,ensure_ascii=False)+'\n' for item in bad).encode('utf-8'))
    return summary


def run(args):
    root=args.run_dir.resolve();root.mkdir(parents=True,exist_ok=True)
    client=TdxClient(args.base_url,args.timeout)
    config=Config(stroke_mode=args.stroke_mode,max_depth=args.max_depth)
    run_file=root/'run.json'
    fingerprint=engine_fingerprint()
    if args.resume:
        if not run_file.exists():raise ValueError('--resume requires an existing run.json')
        metadata=json.loads(run_file.read_text(encoding='utf-8'))
        old=metadata['settings']
        if old['source']!=args.source or old['config']!=config.__dict__ or old['base_url']!=args.base_url:
            raise ValueError('resume settings differ from recorded source/config/base URL; create a new run')
        if args.cache_dir.resolve()!=Path(old['cache_dir']).resolve():
            raise ValueError('resume cache directory differs from the original run')
        settings={**old,'engine_sha256':fingerprint,'timeout':args.timeout,'retries':args.retries,'full_reports':args.full_reports,'refresh':args.refresh}
        records=metadata['universe']
    else:
        if run_file.exists():raise ValueError('run directory already exists; use --resume or select a new run directory')
        as_of=datetime.now(CN).isoformat(timespec='seconds')
        data,receipt,raw=client.universe()
        universe,coverage,excluded=normalize_universe(data)
        records=universe
        if args.symbols:
            wanted={symbol(code) for code in args.symbols.split(',')};records=[row for row in universe if row['symbol'] in wanted]
            if {row['symbol'] for row in records}!=wanted:raise ValueError('requested symbols not in provider universe')
            coverage['selection']='explicit_symbols'
        elif args.limit:
            records=records[:args.limit];coverage['selection']='limited_smoke'
        else:coverage['selection']='all_provider_listed_a_shares'
        settings={'base_url':args.base_url,'timeout':args.timeout,'retries':args.retries,'source':args.source,'as_of':as_of,
                  'cache_dir':str(args.cache_dir.resolve()),'run_dir':str(root),'config':config.__dict__,'engine_sha256':fingerprint,
                  'full_reports':args.full_reports,'refresh':args.refresh}
        metadata={'started_at':now_iso(),'universe':records,'coverage':coverage,'settings':settings,
                  'universe_receipt':receipt,'excluded_instruments':excluded,'repository_commit':'ea07dccb67aeb92ebde851ac31503b4bf457a318'}
        atomic_bytes(root/'universe-response.json.gz',gzip.compress(raw,mtime=0))
        atomic_json(root/'universe.json',{'records':records,'coverage':coverage,'receipt':receipt})
    metadata['settings']=settings;atomic_json(run_file,metadata)
    db=checkpoint(root/'checkpoint.sqlite3')
    complete=set() if args.refresh else {row[0] for row in db.execute("SELECT symbol FROM stocks WHERE status='success' AND engine_sha256=?",(fingerprint,))}
    # A stale checkpoint row is not enough: verify the per-stock result and
    # normalized CSV bytes still exist before reusing success.
    for code in list(complete):
        try:
            item=json.loads(db.execute('SELECT result FROM stocks WHERE symbol=?',(code,)).fetchone()[0])
            saved=json.loads((root/'stocks'/f'{code}.json').read_text(encoding='utf-8'))
            csv_path=Path(item.get('csv_path',''))
            if saved!=item or item.get('symbol')!=code or not csv_path.is_file() or sha256(csv_path.read_bytes())!=item.get('csv_sha256'):
                raise ValueError('stock artifact or normalized CSV differs from the checkpoint')
            if args.full_reports:
                detail=json.loads(gzip.decompress((root/'details'/f'{code}.json.gz').read_bytes()))
                if detail.get('engine_sha256')!=fingerprint or detail.get('source',{}).get('csv_sha256')!=item.get('csv_sha256'):
                    raise ValueError('full detail artifact is missing or stale')
        except (OSError,ValueError,KeyError,TypeError,EOFError):
            complete.remove(code)
    todo=[record for record in records if record['symbol'] not in complete]
    print(json.dumps({'event':'start','universe':len(records),'remaining':len(todo),'workers':args.workers,'run_dir':str(root),'as_of':settings['as_of']},ensure_ascii=False),flush=True)
    quote_path=root/'quotes.json';quote_report=None
    stop_path=Path(getattr(args,'stopfile',None) or root/'STOP').resolve()
    paused=stop_path.exists()
    if not args.skip_quotes and not paused:
        quote_report=collect_quotes(client,records,root)
        print(json.dumps({'event':'quotes','returned':len(quote_report['quotes']),'missing':len(quote_report['missing_symbols'])},ensure_ascii=False),flush=True)
    elif quote_path.exists():quote_report=json.loads(quote_path.read_text(encoding='utf-8'))
    last=time.monotonic();pending_codes={record['symbol'] for record in todo}
    exports(db,root,metadata,quote_report,pending_codes)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        pending={};cursor=0;completed=0
        while pending or (cursor<len(todo) and not paused):
            if stop_path.exists():paused=True
            while not paused and cursor<len(todo) and len(pending)<args.workers*2:
                if stop_path.exists():paused=True;break
                record=todo[cursor];cursor+=1
                pending[pool.submit(process_stock,record,settings)]=record
            if not pending:break
            ready,_=wait(pending,timeout=1,return_when=FIRST_COMPLETED)
            for future in ready:
                record=pending.pop(future)
                try:result=future.result()
                except Exception as exc:
                    result={**record,'status':'failed','error':f'worker: {type(exc).__name__}: {exc}','engine_sha256':fingerprint}
                    quarantined=quarantine_details(root,record['symbol'])
                    if quarantined:result['quarantined_detail']=quarantined
                    atomic_json(root/'stocks'/f"{record['symbol']}.json",result)
                db.execute('INSERT OR REPLACE INTO stocks VALUES (?,?,?,?,?)',(record['symbol'],result['status'],fingerprint,json.dumps(result,ensure_ascii=False,allow_nan=False),now_iso()))
                db.commit();completed+=1;pending_codes.discard(record['symbol'])
                if completed%25==0 or time.monotonic()-last>=20 or completed==len(todo):
                    summary=exports(db,root,metadata,quote_report,pending_codes);last=time.monotonic()
                    print(json.dumps({'event':'progress','processed':summary['processed'],'total':len(records),'success':summary['success'],
                                      'failed_or_empty':summary['failed_or_empty'],'history_bars':summary['total_history_bars']},ensure_ascii=False),flush=True)
    summary=exports(db,root,metadata,quote_report,pending_codes);db.close()
    if paused:
        summary.update(paused=True,stopfile=str(stop_path),pause_reason='Stop sentinel requested; in-flight work completed, remaining work was not submitted')
        atomic_json(root/'summary.json',summary)
    print(json.dumps({'event':'paused' if paused else 'finished','summary':str(root/'summary.json'),'success':summary['success'],'failures':summary['failed_or_empty']},ensure_ascii=False),flush=True)
    if paused:return 3
    return 0 if summary['all_symbols_succeeded'] else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://127.0.0.1:18080')
    parser.add_argument('--run-dir',type=Path,required=True)
    parser.add_argument('--cache-dir',type=Path,default=ROOT/'market_data')
    parser.add_argument('--source',choices=['tdx','ths'],default='tdx')
    parser.add_argument('--workers',type=int,default=4)
    parser.add_argument('--timeout',type=float,default=45)
    parser.add_argument('--retries',type=int,default=2)
    parser.add_argument('--stroke-mode',choices=['strict','loose'],default='loose')
    parser.add_argument('--max-depth',type=int,default=3)
    parser.add_argument('--resume',action='store_true');parser.add_argument('--refresh',action='store_true')
    parser.add_argument('--full-reports',action='store_true');parser.add_argument('--skip-quotes',action='store_true')
    parser.add_argument('--stopfile',type=Path,help='optional stop sentinel path; default RUN_DIR/STOP. Remove it before resuming.')
    parser.add_argument('--limit',type=int);parser.add_argument('--symbols')
    args=parser.parse_args()
    if not 1<=args.workers<=16 or args.retries<0 or args.timeout<=0 or (args.limit is not None and args.limit<1):
        parser.error('workers=1..16; retries>=0; timeout>0; limit>0')
    if args.limit and args.symbols:parser.error('use --limit or --symbols, not both')
    try:return run(args)
    except (ValueError,TdxError,OSError) as exc:
        print(f'error: {type(exc).__name__}: {exc}',file=sys.stderr,flush=True);return 2


if __name__=='__main__':raise SystemExit(main())
