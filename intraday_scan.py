#!/usr/bin/env python3
"""Checkpointed daily/30m/5m current-cutoff Chan structure audit using TDX."""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from chanmodel.engine import multilevel
from chanmodel.model import Config
from chanmodel.tdx_data import CN,TdxClient,TdxError,atomic_bytes,atomic_json,now_iso,sha256,symbol
from chanmodel.tdx_intraday import cache_intraday,fetch_intraday,load_daily_cache,load_intraday_cache,normalize_intraday

ROOT=Path(__file__).resolve().parent


def fingerprint():
    files=sorted((ROOT/'chanmodel').glob('*.py'))+[Path(__file__).resolve()]
    return sha256(b''.join(p.name.encode()+p.read_bytes() for p in files))


def cache_files_valid(result):
    for level,metadata in result['sources'].items():
        raw=Path(metadata['raw_path']);csv=Path(metadata['csv_path'])
        if not raw.is_file() or not csv.is_file():return False
        if sha256(raw.read_bytes())!=metadata['raw_storage_sha256']:return False
        expected=metadata['csv_sha256'] if level=='day' else metadata['csv_storage_sha256']
        if sha256(csv.read_bytes())!=expected:return False
    return True


def range_coverage(point,bars,child_bars):
    start,end=point['terminal_range']
    lower=datetime.fromisoformat(bars[start].closed_at)
    upper=datetime.fromisoformat(bars[end].closed_at)
    available_lower=datetime.fromisoformat(child_bars[0].closed_at)
    available_upper=datetime.fromisoformat(child_bars[-1].closed_at)
    complete=available_lower<=lower<=upper<=available_upper
    return {'parent_terminal_start':lower.isoformat(),'parent_terminal_end':upper.isoformat(),
            'child_history_start':available_lower.isoformat(),'child_history_end':available_upper.isoformat(),
            'terminal_range_covered':complete,'status':'covered' if complete else 'insufficient_child_history'}


def scan_stock(record,settings):
    started=time.monotonic();code=record['symbol'];root=Path(settings['run_dir']);cache=Path(settings['cache_dir'])
    as_of=datetime.fromisoformat(settings['as_of']);client=TdxClient(settings['base_url'],settings['timeout'])
    sources={};datasets={};errors=[]
    try:
        datasets['day'],sources['day']=load_daily_cache(Path(settings['daily_cache_dir']),code,as_of)
        if not datasets['day']:
            raise ValueError('no closed daily bars at cutoff')
        for period,level in [('minute30','30m'),('minute5','5m')]:
            bars=None
            if not settings['refresh']:
                try:
                    bars,rows,quality,source_meta,raw=load_intraday_cache(cache,code,period,as_of)
                    # Reusing a future cache in a saved run is not a historical
                    # data-vintage guarantee. Require the same run acquisition.
                    if source_meta.get('cache_run_as_of')!=settings['as_of']:
                        bars=None
                except (FileNotFoundError,KeyError):pass
            if bars is None:
                for attempt in range(settings['retries']+1):
                    try:
                        data,receipt,raw=fetch_intraday(client,code,period)
                        compressed_raw=gzip.compress(raw,compresslevel=6,mtime=0)
                        atomic_bytes(root/'responses'/period/f'{code}.json.gz',compressed_raw)
                        bars,rows,quality=normalize_intraday(data,as_of,period)
                        quality['cache_run_as_of']=settings['as_of']
                        source_meta=cache_intraday(cache,code,period,rows,raw,receipt,quality,compressed_raw=compressed_raw)
                        break
                    except Exception as exc:
                        errors.append({'period':period,'attempt':attempt+1,'error':f'{type(exc).__name__}: {exc}','at':now_iso()})
                        if attempt==settings['retries']:raise
                        time.sleep(min(0.5*2**attempt,3))
            if not bars:
                result={**record,'status':'empty_history','empty_period':period,'sources':sources,
                        'quality':quality,'error':f'{period}: provider returned no closed bars; not padded',
                        'elapsed_seconds':round(time.monotonic()-started,3),'engine_sha256':settings['engine_sha256']}
                atomic_json(root/'stocks'/f'{code}.json',result)
                return result
            datasets[level]=bars;sources[level]=source_meta
        report=multilevel(datasets,('day','30m','5m'),settings['as_of'],Config(**settings['config']))
        parent_signals={point['id']:point for point in report['levels']['day']['signal_candidates']}
        confirm_signals={point['id']:point for point in report['levels']['30m']['signal_candidates']}
        for audit in report['interval_nesting_audits']:
            parent=parent_signals[audit['parent_signal']]
            audit['daily_terminal_30m_coverage']=range_coverage(parent,datasets['day'],datasets['30m'])
            for match in audit['matches']:
                match['confirm_terminal_5m_coverage']=range_coverage(confirm_signals[match['confirm_id']],datasets['30m'],datasets['5m'])
        summary={**record,'status':'success','as_of':settings['as_of'],'processed_at':now_iso(),
                 'periods':{level:{'bars':len(bars),'first_close':bars[0].closed_at,'last_close':bars[-1].closed_at,
                                   'strokes':len(report['levels'][level]['strokes']),'segments':len(report['levels'][level]['segments']),
                                   'centers':len(report['levels'][level]['centers']),
                                   'state':report['levels'][level]['current_state'],
                                   'active_candidate_count':sum(p['status']=='candidate' for p in report['levels'][level]['signal_candidates'])}
                            for level,bars in datasets.items()},
                 'interval_nesting_audits':report['interval_nesting_audits'],
                 'nested_evidence_count':sum(a['nested_candidate_evidence'] for a in report['interval_nesting_audits']),
                 'daily_candidates_without_30m_history':sum(not a['daily_terminal_30m_coverage']['terminal_range_covered'] for a in report['interval_nesting_audits']),
                 'sources':sources,'engine_sha256':settings['engine_sha256'],'adjustment':'unadjusted',
                 'definition_mode':report['definition_mode'],'execution_allowed':False,
                 'elapsed_seconds':round(time.monotonic()-started,3),
                 'coverage_claim':'provider-available minute histories; each timeframe has independent finite range'}
        atomic_json(root/'stocks'/f'{code}.json',summary)
        if settings['full_reports']:
            atomic_bytes(root/'details'/f'{code}.json.gz',gzip.compress(json.dumps(report,ensure_ascii=False,allow_nan=False).encode(),mtime=0))
        return summary
    except Exception as exc:
        result={**record,'status':'failed','error':f'{type(exc).__name__}: {exc}','attempts':errors,
                'elapsed_seconds':round(time.monotonic()-started,3),'engine_sha256':settings['engine_sha256']}
        atomic_json(root/'stocks'/f'{code}.json',result)
        return result


def open_checkpoint(path):
    db=sqlite3.connect(path);db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS stocks(symbol TEXT PRIMARY KEY,status TEXT,engine_sha256 TEXT,result TEXT,updated_at TEXT)')
    db.commit();return db


def export(db,root,metadata):
    desired={r['symbol'] for r in metadata['universe']};engine=metadata['settings']['engine_sha256']
    results=[json.loads(row[0]) for row in db.execute('SELECT result FROM stocks WHERE engine_sha256=?',(engine,))]
    results=[r for r in results if r['symbol'] in desired]
    success=[r for r in results if r['status']=='success'];failures=[r for r in results if r['status']!='success']
    summary={'run_dir':str(root),'as_of':metadata['settings']['as_of'],'updated_at':now_iso(),'universe_count':len(desired),
             'processed':len(results),'success':len(success),'failed':len(failures),'pending':len(desired)-len(results),
             'all_symbols_finished':len(results)==len(desired),'all_symbols_succeeded':len(success)==len(desired),
             'total_bars':{level:sum(r['periods'][level]['bars'] for r in success) for level in ['day','30m','5m']},
             'nested_evidence_count':sum(r['nested_evidence_count'] for r in success),'failures':failures,
             'by_exchange':{ex:{'universe':sum(r['exchange']==ex for r in metadata['universe']),
                                 'success':sum(r['exchange']==ex for r in success)} for ex in ['sh','sz','bj']},
             'definition_mode':'multi_timeframe_structure_proxy','execution_allowed':False,
             'notes':['Daily and intraday availability ranges differ; finite minute history is reported, not invented.',
                      'Nested candidates do not prove original recursive trend types or past live availability.']}
    atomic_json(root/'summary.json',summary)
    atomic_bytes(root/'failures.jsonl',''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in failures).encode())
    return summary


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--universe',type=Path,default=ROOT/'runs/tdx-all-a-20261106/universe.json')
    p.add_argument('--run-dir',type=Path,required=True);p.add_argument('--cache-dir',type=Path,default=ROOT/'market_data')
    p.add_argument('--daily-cache-dir',type=Path,default=ROOT/'market_data');p.add_argument('--base-url',default='http://127.0.0.1:18080')
    p.add_argument('--as-of');p.add_argument('--workers',type=int,default=8);p.add_argument('--timeout',type=float,default=60)
    p.add_argument('--retries',type=int,default=2);p.add_argument('--resume',action='store_true');p.add_argument('--refresh',action='store_true')
    p.add_argument('--full-reports',action='store_true');p.add_argument('--symbols');p.add_argument('--limit',type=int)
    p.add_argument('--stroke-mode',choices=['strict','loose'],default='loose');p.add_argument('--max-depth',type=int,default=3)
    args=p.parse_args()
    if not 1<=args.workers<=16 or args.timeout<=0 or args.retries<0:p.error('workers=1..16, timeout>0, retries>=0 required')
    if args.limit is not None and args.limit<1:p.error('limit must be positive')
    if args.symbols and args.limit:p.error('use symbols or limit, not both')
    root=args.run_dir.resolve();root.mkdir(parents=True,exist_ok=True);record_path=root/'run.json'
    config=Config(stroke_mode=args.stroke_mode,max_depth=args.max_depth).__dict__;engine=fingerprint()
    if args.resume:
        metadata=json.loads(record_path.read_text(encoding='utf-8'));settings=metadata['settings']
        if settings['config']!=config or settings['base_url']!=args.base_url or settings['cache_dir']!=str(args.cache_dir.resolve()) or settings['daily_cache_dir']!=str(args.daily_cache_dir.resolve()):
            raise ValueError('resume input/source/config differs; create a new run')
        if args.as_of and args.as_of!=settings['as_of']:raise ValueError('resume cutoff cannot change')
    else:
        if record_path.exists():raise ValueError('existing run; use resume or another directory')
        original=args.universe.read_bytes();universe=json.loads(original);records=universe.get('records')
        if not isinstance(records,list) or not records:raise ValueError('universe requires nonempty records')
        if args.symbols:
            desired={symbol(x) for x in args.symbols.split(',')};records=[r for r in records if r['symbol'] in desired]
            if {r['symbol'] for r in records}!=desired:raise ValueError('symbols missing from supplied universe')
        elif args.limit:records=records[:args.limit]
        as_of=args.as_of or datetime.now(CN).isoformat(timespec='seconds')
        if datetime.fromisoformat(as_of).tzinfo is None:raise ValueError('as_of must include timezone')
        settings={'as_of':as_of,'base_url':args.base_url,'cache_dir':str(args.cache_dir.resolve()),'daily_cache_dir':str(args.daily_cache_dir.resolve()),'config':config}
        metadata={'started_at':now_iso(),'universe':records,'universe_file':str(args.universe.resolve()),
                  'universe_sha256':sha256(original),'coverage':universe.get('coverage'),'settings':settings}
    settings.update(run_dir=str(root),engine_sha256=engine,timeout=args.timeout,retries=args.retries,full_reports=args.full_reports,refresh=args.refresh)
    metadata['settings']=settings;atomic_json(record_path,metadata)
    db=open_checkpoint(root/'checkpoint.sqlite3');desired={r['symbol'] for r in metadata['universe']}
    done=set()
    for code,result_raw in db.execute("SELECT symbol,result FROM stocks WHERE status='success' AND engine_sha256=?",(engine,)):
        if code not in desired or args.refresh:continue
        result=json.loads(result_raw);file=root/'stocks'/f'{code}.json'
        try:
            same=json.loads(file.read_text(encoding='utf-8'))==result
            cached=cache_files_valid(result)
            if same and cached and (not args.full_reports or (root/'details'/f'{code}.json.gz').is_file()):done.add(code)
        except (OSError,ValueError,KeyError):pass
    todo=iter(r for r in metadata['universe'] if r['symbol'] not in done);paused=False;count=0;last=time.monotonic()
    print(json.dumps({'event':'start','universe':len(desired),'reused':len(done),'workers':args.workers,'run_dir':str(root)},ensure_ascii=False),flush=True)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        pending={}
        def submit_more():
            nonlocal paused
            if (root/'STOP').exists():paused=True;return
            while len(pending)<args.workers*2:
                record=next(todo,None)
                if record is None:return
                pending[pool.submit(scan_stock,record,settings)]=record
        submit_more()
        while pending:
            finished,_=wait(pending,timeout=1,return_when=FIRST_COMPLETED)
            for future in finished:
                record=pending.pop(future)
                try:result=future.result()
                except Exception as exc:result={**record,'status':'failed','error':f'worker {type(exc).__name__}: {exc}','engine_sha256':engine}
                db.execute('INSERT OR REPLACE INTO stocks VALUES(?,?,?,?,?)',(record['symbol'],result['status'],engine,json.dumps(result,ensure_ascii=False,allow_nan=False),now_iso()));db.commit();count+=1
            if count and (finished and count%25==0 or time.monotonic()-last>=20):
                summary=export(db,root,metadata);last=time.monotonic()
                print(json.dumps({'event':'progress',**{k:summary[k] for k in ['processed','success','failed','pending','total_bars']}},ensure_ascii=False),flush=True)
            submit_more()
    summary=export(db,root,metadata);db.close()
    print(json.dumps({'event':'paused' if paused else 'finished','summary':str(root/'summary.json'),'success':summary['success'],'failed':summary['failed']},ensure_ascii=False),flush=True)
    return 3 if paused else 0 if summary['all_symbols_succeeded'] else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError,TdxError) as exc:
        print(f'error: {type(exc).__name__}: {exc}',file=sys.stderr);raise SystemExit(2)
