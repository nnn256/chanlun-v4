#!/usr/bin/env python3
"""Daily prefix replay over cached TDX histories, with signal-only event logs.

Every supplied closed daily candle is visited. Signals use only that day's
prefix; future prices are passed to a separate fixed-horizon outcome labeler.
The universe is a CURRENT provider list and is explicitly survivorship biased.
"""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
import copy
from dataclasses import asdict
from datetime import datetime
import gzip
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
import time

from chanmodel.engine import analyze, object_body
from chanmodel.centers import build_centers
from chanmodel.evaluate import evaluate_events
from chanmodel.model import Candle, Config, load_csv, time_value
from chanmodel.structure import contains, fractals_at, local_direction, merge_bars, merge_pair, strokes_at
from chanmodel.segments import build_segments
from chanmodel.signals import attach_ranges,macd,structural_signals
from chanmodel.tdx_data import atomic_bytes, atomic_json, now_iso, symbol

ROOT = Path(__file__).resolve().parent
VERSION = 'daily_signal_prefix_v2'


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def implementation_fingerprint():
    names = ['engine.py', 'model.py', 'structure.py', 'segments.py', 'centers.py', 'signals.py', 'evaluate.py']
    files = [ROOT/'chanmodel'/name for name in names] + [Path(__file__).resolve()]
    return hashlib.sha256(b''.join(p.name.encode()+p.read_bytes() for p in files)).hexdigest()


def normalized(bar, config):
    return Candle(bar.pos, *(round(value,config.price_decimals) for value in (bar.open,bar.high,bar.low,bar.close)),
                  bar.closed_at,bar.volume,bar.closed)


def invalidates(bar, point):
    value = bar.low if point['side']=='buy' else bar.high
    boundary = point['invalidation']
    op = point['invalidation_operator']
    return {'le':lambda:value<=boundary,'ge':lambda:value>=boundary,
            'lt':lambda:value<boundary,'gt':lambda:value>boundary}[op]()


class SignalOnlyReplay:
    """Same signal snapshots as analyze(prefix), without hashing all objects.

    Safe gate: unchanged full stroke dictionaries imply unchanged segment
    candidates, confirmed segments, actual ranges/force and center membership.
    With closed immutable old bars, only the known-delay invalidation scan can
    change an existing signal. Check that using the newly appended candle.
    Any stroke change triggers the original full-prefix analyze. No truncation,
    sampling, approximate stroke comparison or frozen segment assumption.
    """
    def __init__(self, config=None, level='day', gated=True):
        self.config,self.level,self.gated=config or Config(),level,gated
        self.bars=[]
        self.last_strokes=None
        self.signals=[]
        self.registry={}
        self.events=[]
        self.full_analyses=0
        self.reused_structure_prefixes=0
        self.merged=[]
        self.fractals=[]
        self.histogram=[]
        self.fast=self.slow=None
        self.dea=0.0

    def _append_macd(self, bar):
        if self.fast is None:self.fast=self.slow=bar.close
        self.fast+=2/(self.config.macd_fast+1)*(bar.close-self.fast)
        self.slow+=2/(self.config.macd_slow+1)*(bar.close-self.slow)
        dif=self.fast-self.slow
        self.dea+=2/(self.config.macd_signal+1)*(dif-self.dea)
        self.histogram.append({'dif':dif,'dea':self.dea,'histogram':self.config.macd_multiplier*(dif-self.dea)})

    def _base_signals(self, strokes):
        # Signal generation in analyze reads only layers[0]. Higher recursive
        # layers, chart graph and divergence display do not feed these signals.
        # Invoke exactly the same base functions with exactly the same inputs.
        segments=build_segments(strokes,0)
        attach_ranges(segments,self.bars,self.histogram)
        centers=build_centers(segments,0)
        signals=structural_signals(segments,centers,self.bars,self.config,self.level)[0]
        for point in signals:point['source_range']=[point['anchor'],point['anchor']]
        return signals

    def _append_structure(self, bar):
        current={'start':bar.pos,'end':bar.pos,'basis_at':bar.pos,'high':bar.high,'low':bar.low,
                 'high_source':bar.pos,'low_source':bar.pos,'open':bar.open,'close':bar.close,
                 'closed':True,'components':[bar.pos]}
        while self.merged and contains(self.merged[-1],current):
            left=self.merged.pop()
            direction=local_direction(self.merged[-1] if self.merged else None,left,current)
            current=merge_pair(left,current,direction)
        changed=len(self.merged)
        current.update(index=changed,id=f"k:{current['start']}:{current['end']}")
        self.merged.append(current)
        first_mid=max(1,changed-1)
        previous_tail=[point for point in self.fractals if point['merged_index']>=first_mid]
        self.fractals=[point for point in self.fractals if point['merged_index']<first_mid]
        offset=max(0,first_mid-1)
        tail=fractals_at(self.merged[offset:])
        for point in tail:point['merged_index']+=offset
        self.fractals.extend(tail)
        if tail==previous_tail and self.last_strokes is not None:
            return self.last_strokes
        return strokes_at(self.fractals,self.config)[0]

    def update(self, bar):
        if bar.pos!=len(self.bars) or not bar.closed:
            raise ValueError('history replay requires contiguous, immutable, closed input candles')
        if self.bars and (bar.closed_at is None)!=(self.bars[-1].closed_at is None):
            raise ValueError('closing timestamps must be supplied consistently')
        if self.bars and bar.closed_at and time_value(bar.closed_at)<=time_value(self.bars[-1].closed_at):
            raise ValueError('closing timestamps must strictly increase')
        bar=normalized(bar,self.config)
        self.bars.append(bar)
        strokes=None
        if self.gated:
            self._append_macd(bar)
            strokes=self._append_structure(bar)
        if self.gated and self.last_strokes is not None and strokes==self.last_strokes:
            changed=False
            for point in self.signals:
                if point['status']=='candidate' and bar.pos>point['anchor'] and invalidates(bar,point):
                    point['status']='invalidated_in_known_delay'
                    changed=True
            self.reused_structure_prefixes+=1
        else:
            if self.gated:
                self.signals=self._base_signals(strokes)
            else:
                report=analyze(self.bars,self.config,self.level)
                self.signals=[object_body(point) for point in report['signal_candidates']]
            self.full_analyses+=1
            changed=True
        self.last_strokes=strokes
        if changed:self._record(bar.pos,bar.closed_at)
        return self.signals

    def _record(self, now, observed_time):
        present=set()
        for point in self.signals:
            ident=point['id'];present.add(ident)
            record=self.registry.get(ident)
            if record is None:
                record={'revision':-1,'active':False,'body':{},'first_candidate_at':None}
                self.registry[ident]=record
            if point['status']=='candidate' and record['first_candidate_at'] is None:
                record['first_candidate_at']=now
            if not record['active'] or point!=record['body']:
                previous=record['body'].get('status')
                event='appeared' if record['revision']==-1 else 'reappeared' if not record['active'] else 'invalidated' if previous=='candidate' and point['status']!='candidate' else 'revised'
                record.update(active=True,revision=record['revision']+1,body=copy.deepcopy(point))
                self.events.append({'sequence':len(self.events),'kind':'signal','id':ident,'event':event,
                                    'observed_at':now,'observed_time':observed_time,'available_at':now,
                                    'input_revision':0,'revision':record['revision'],
                                    'first_candidate_at':record['first_candidate_at'],'value':copy.deepcopy(point)})
        for ident,record in self.registry.items():
            if record['active'] and ident not in present:
                record.update(active=False,revision=record['revision']+1)
                self.events.append({'sequence':len(self.events),'kind':'signal','id':ident,'event':'withdrawn',
                                    'observed_at':now,'observed_time':observed_time,'available_at':now,
                                    'input_revision':0,'revision':record['revision'],
                                    'first_candidate_at':record['first_candidate_at'],
                                    'reason':'current structure no longer contains this object'})


def replay_daily(bars, config=None, start_date=None, gated=True):
    engine=SignalOnlyReplay(config,gated=gated)
    first=next((i for i,bar in enumerate(bars) if start_date is None or bar.closed_at[:10]>=start_date),len(bars))
    if first:
        # The baseline uses every earlier candle once. Earlier DAILY decisions
        # are intentionally outside this requested observation window, so their
        # first-seen history is unknown and is never inferred from the baseline.
        engine.bars=[normalized(bar,engine.config) for bar in bars[:first]]
        report=analyze(engine.bars,engine.config,'day')
        engine.signals=[object_body(point) for point in report['signal_candidates']]
        engine.merged=merge_bars(engine.bars,engine.config)
        engine.fractals=fractals_at(engine.merged)
        engine.last_strokes=strokes_at(engine.fractals,engine.config)[0] if gated else None
        for bar in engine.bars:engine._append_macd(bar)
        engine.full_analyses+=1
        for point in engine.signals:
            engine.registry[point['id']]={'revision':0,'active':True,'body':copy.deepcopy(point),
                                           'first_candidate_at':-1,'baseline_existing':True}
    for bar in bars[first:]:
        engine.update(bar)
    engine.visited_prefixes=len(bars)-first
    engine.warmup_bars=first
    events=engine.events
    entry_events=[e for e in events if e.get('value',{}).get('status')=='candidate' and e['first_candidate_at']==e['observed_at']]
    return engine,events,entry_events


def write_jsonl_gzip(path, rows):
    raw=''.join(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n' for row in rows).encode('utf-8')
    atomic_bytes(path,gzip.compress(raw,mtime=0))
    return {'file':str(path.resolve()),'rows':len(rows),'sha256':file_hash(path),'encoding':'gzip UTF-8 JSONL'}


def label_groups(labels):
    groups={}
    for row in labels:
        key=(row['signal_class'],row['horizon_bars'])
        group=groups.setdefault(key,{'signal_class':key[0],'horizon_bars':key[1],'observations':0,'labelled':0,'positive':0,'sum_directional_return':0.0,'sum_after_constant_cost':0.0,'excluded':{}})
        group['observations']+=1
        if row['status']=='labelled':
            group['labelled']+=1
            group['positive']+=int(row['gross_directional_return']>0)
            group['sum_directional_return']+=row['gross_directional_return']
            group['sum_after_constant_cost']+=row['hypothetical_return_after_constant_cost']
        else:
            reason=row['status'];group['excluded'][reason]=group['excluded'].get(reason,0)+1
    for group in groups.values():
        count=group['labelled']
        group['mean_directional_return']=group['sum_directional_return']/count if count else None
        group['mean_after_constant_cost']=group['sum_after_constant_cost']/count if count else None
        group['positive_fraction']=group['positive']/count if count else None
    return sorted(groups.values(),key=lambda item:(item['signal_class'],item['horizon_bars']))


def process_history(record, settings):
    started=time.perf_counter();code=record['symbol'];folder=Path(settings['run_dir'])
    try:
        cache=Path(settings['cache_dir'])/settings['source']/'day'
        path=cache/'ohlcv'/f'{code}.csv';meta_path=cache/'metadata'/f'{code}.json'
        metadata=json.loads(meta_path.read_text(encoding='utf-8'))
        bars,source=load_csv(path)
        if source['input_sha256']!=metadata['csv_sha256']:
            raise ValueError('cached CSV hash does not match provider metadata')
        if metadata.get('source')!=settings['source'] or metadata.get('symbol')!=code:
            raise ValueError('cached source or symbol identity mismatch')
        bars=[bar for bar in bars if bar.closed]
        if not bars:
            raise ValueError('cached history has no closed bars')
        if any(bar.closed_at is None for bar in bars):
            raise ValueError('daily history requires explicit closing timestamps')
        config=Config(**settings['config'])
        engine,events,entry_events=replay_daily(bars,config,settings['start_date'],settings['gated'])
        outcomes=evaluate_events(bars,entry_events,tuple(settings['horizons']),settings['round_trip_bps'])
        events_receipt=write_jsonl_gzip(folder/'events'/f'{code}.jsonl.gz',events)
        labels_receipt=write_jsonl_gzip(folder/'labels'/f'{code}.jsonl.gz',outcomes['labels'])
        event_counts={kind:sum(e['event']==kind for e in events) for kind in sorted({e['event'] for e in events})}
        classes={kind:sum(e['value']['signal_class']==kind for e in entry_events) for kind in sorted({e['value']['signal_class'] for e in entry_events})}
        result={**record,'status':'success','input_sha256':source['input_sha256'],'source':settings['source'],
                'adjustment':metadata.get('adjustment'),'received_at':metadata.get('received_at'),
                'history_bars':len(bars),'visited_prefixes':engine.visited_prefixes,'warmup_bars':engine.warmup_bars,
                'first_date':bars[0].closed_at[:10],
                'last_date':bars[-1].closed_at[:10],'start_date':settings['start_date'],
                'all_earlier_bars_retained_for_warmup':True,'events':events_receipt,'labels':labels_receipt,
                'event_counts':event_counts,'candidate_first_seen_count':len(entry_events),'candidate_classes':classes,
                'label_groups':label_groups(outcomes['labels']),'full_analyses':engine.full_analyses,
                'reused_structure_prefixes':engine.reused_structure_prefixes,
                'elapsed_seconds':round(time.perf_counter()-started,6),'definition_mode':'structure_proxy',
                'execution_allowed':False,'signal_semantics':'first observed in selected daily observation window; baseline-existing candidates excluded; pre-window first occurrences unknown',
                'universe_bias':'current provider-listed survivors; historical membership and delisted names are incomplete',
                'source_vintage_bias':'current cached historical prices; qfq may revise earlier history' if settings['source']=='ths' else 'unadjusted prices; corporate actions can affect structure',
                'evaluation_limitations':outcomes['limitations']}
    except Exception as exc:
        result={**record,'status':'failed','error':f'{type(exc).__name__}: {exc}','elapsed_seconds':round(time.perf_counter()-started,6)}
    result['fingerprint']=settings['fingerprint']
    atomic_json(folder/'stocks'/f'{code}.json',result)
    return result


def open_checkpoint(path):
    connection=sqlite3.connect(path)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('CREATE TABLE IF NOT EXISTS history_stocks (symbol TEXT PRIMARY KEY,status TEXT NOT NULL,fingerprint TEXT NOT NULL,input_sha256 TEXT,result_json TEXT NOT NULL,updated_at TEXT NOT NULL)')
    connection.commit();return connection


def save_checkpoint(db,result):
    db.execute('INSERT OR REPLACE INTO history_stocks VALUES (?,?,?,?,?,?)',
               (result['symbol'],result['status'],result['fingerprint'],result.get('input_sha256'),json.dumps(result,ensure_ascii=False,allow_nan=False),now_iso()))
    db.commit()


def export_summary(db, folder, run, paused=False):
    selected={r['symbol']:r for r in run['records']};results=[];excluded=[]
    for code,fingerprint,text in db.execute('SELECT symbol,fingerprint,result_json FROM history_stocks ORDER BY symbol'):
        if code not in selected or fingerprint!=run['settings']['fingerprint']:
            excluded.append(code);continue
        result=json.loads(text)
        if result.get('status')=='pending':
            excluded.append(code);continue
        results.append(result)
    successes=[r for r in results if r['status']=='success'];failures=[r for r in results if r['status']!='success']
    grouped={}
    for result in successes:
        for group in result['label_groups']:
            key=(group['signal_class'],group['horizon_bars'])
            aggregate=grouped.setdefault(key,{'signal_class':key[0],'horizon_bars':key[1],'observations':0,'labelled':0,'positive':0,'sum_directional_return':0.0,'sum_after_constant_cost':0.0,'excluded':{}})
            for field in ['observations','labelled','positive','sum_directional_return','sum_after_constant_cost']:
                aggregate[field]+=group[field]
            for reason,count in group['excluded'].items():aggregate['excluded'][reason]=aggregate['excluded'].get(reason,0)+count
    for group in grouped.values():
        n=group['labelled'];group['mean_directional_return']=group['sum_directional_return']/n if n else None
        group['mean_after_constant_cost']=group['sum_after_constant_cost']/n if n else None
        group['positive_fraction']=group['positive']/n if n else None
    classes={}
    for result in successes:
        for kind,count in result['candidate_classes'].items():classes[kind]=classes.get(kind,0)+count
    summary={'started_at':run['started_at'],'updated_at':now_iso(),'run_dir':str(folder.resolve()),
             'status':'complete' if len(results)==len(selected) else 'paused' if paused else 'running',
             'universe_count':len(selected),'processed':len(results),'success':len(successes),'failed':len(failures),'pending':len(selected)-len(results),
             'all_symbols_succeeded':len(successes)==len(selected),'history_bars':sum(r['history_bars'] for r in successes),
             'visited_daily_prefixes':sum(r['visited_prefixes'] for r in successes),
             'warmup_bars':sum(r['warmup_bars'] for r in successes),
             'full_analyses':sum(r['full_analyses'] for r in successes),'reused_structure_prefixes':sum(r['reused_structure_prefixes'] for r in successes),
             'event_count':sum(r['events']['rows'] for r in successes),'candidate_first_seen_count':sum(r['candidate_first_seen_count'] for r in successes),
             'symbols_with_candidate':sum(r['candidate_first_seen_count']>0 for r in successes),
             'candidate_classes':classes,'label_groups':sorted(grouped.values(),key=lambda g:(g['signal_class'],g['horizon_bars'])),
             'coverage_by_exchange':{exchange:{'universe':sum(r['exchange']==exchange for r in selected.values()),'success':sum(r['exchange']==exchange for r in successes)} for exchange in ['sh','sz','bj']},
             'coverage':run['coverage'],'failures':failures,'excluded_checkpoint_rows':excluded,
             'settings':run['settings'],'historical_membership_complete':False,'historical_delisted_universe_complete':False,
             'survivorship_bias':True,'execution_allowed':False,'evaluation_mode':'fixed-horizon event labels, not portfolio backtest',
             'notes':['Every closed daily prefix in the observation window is visited; all earlier bars are retained in a full-prefix baseline warm-up.',
                      'Without --start-date all available dates are replayed. With it, pre-window daily decisions and first appearances are unknown, not backfilled.',
                      'Labels start at the next bar open after first candidate observation; later revisions do not backdate or duplicate entry.',
                      'ST, delisting membership, price limits, T+1, lending and realistic fills are not modeled; constant cost is a label assumption.']}
    atomic_json(folder/'summary.json',summary)
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--universe',type=Path,required=True)
    parser.add_argument('--cache-dir',type=Path,default=ROOT/'market_data')
    parser.add_argument('--run-dir',type=Path,required=True)
    parser.add_argument('--source',choices=['tdx','ths'],default='tdx')
    parser.add_argument('--workers',type=int,default=4)
    parser.add_argument('--symbols',help='comma-separated exchange-prefixed A-share symbols')
    parser.add_argument('--limit',type=int)
    parser.add_argument('--horizons',default='5,20,60')
    parser.add_argument('--start-date',help='YYYY-MM-DD; full earlier history is still replayed as warm-up')
    parser.add_argument('--round-trip-bps',type=float,default=20.0)
    parser.add_argument('--stroke-mode',choices=['strict','loose'],default='loose')
    parser.add_argument('--reference',action='store_true',help='force original analyze on every prefix (slower validation route)')
    parser.add_argument('--retry-failed',action='store_true')
    args=parser.parse_args()
    try:
        if args.workers<1 or args.workers>32:raise ValueError('--workers must be 1..32')
        if args.limit is not None and args.limit<1:raise ValueError('--limit must be positive')
        horizons=tuple(sorted(set(int(item) for item in args.horizons.split(','))))
        if not horizons or any(h<1 for h in horizons):raise ValueError('--horizons must be positive bar counts')
        if not math.isfinite(args.round_trip_bps) or args.round_trip_bps<0:raise ValueError('--round-trip-bps must be finite and nonnegative')
        if args.start_date:
            if len(args.start_date)!=10:raise ValueError('--start-date must be YYYY-MM-DD')
            datetime.fromisoformat(args.start_date)
        universe=json.loads(args.universe.read_text(encoding='utf-8-sig'))
        rows=universe.get('records',universe.get('universe')) if isinstance(universe,dict) else universe
        if not isinstance(rows,list) or not rows:raise ValueError('universe must contain a nonempty records list')
        records={}
        for row in rows:
            code=symbol(row['symbol']);record={**row,'symbol':code,'exchange':code[:2]}
            if code in records and records[code]!=record:raise ValueError('conflicting duplicate universe symbol '+code)
            records[code]=record
        if args.symbols:
            selected=[symbol(code) for code in args.symbols.split(',')]
            missing=set(selected)-records.keys()
            if missing:raise ValueError('symbols outside supplied universe: '+','.join(sorted(missing)))
            records={code:records[code] for code in sorted(set(selected))}
        else:records=dict(sorted(records.items()))
        if args.limit:records=dict(list(records.items())[:args.limit])
        folder=args.run_dir.resolve();folder.mkdir(parents=True,exist_ok=True)
        config=Config(stroke_mode=args.stroke_mode)
        settings={'version':VERSION,'source':args.source,'cache_dir':str(args.cache_dir.resolve()),'run_dir':str(folder),
                  'config':asdict(config),'horizons':list(horizons),'round_trip_bps':args.round_trip_bps,'start_date':args.start_date,
                  'gated':not args.reference,'implementation_sha256':implementation_fingerprint(),
                  'universe_sha256':file_hash(args.universe),'symbols':list(records)}
        settings['fingerprint']=hashlib.sha256(json.dumps(settings,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        run={'started_at':now_iso(),'records':list(records.values()),'coverage':universe.get('coverage') if isinstance(universe,dict) else None,
             'universe_file':str(args.universe.resolve()),'settings':settings,'universe_semantics':'current provider survivors; not point-in-time historical constituents'}
        run_path=folder/'run.json'
        if run_path.exists():
            previous=json.loads(run_path.read_text(encoding='utf-8'))
            if previous['settings']['fingerprint']!=settings['fingerprint']:
                raise ValueError('run-dir has different immutable settings or implementation; use a new run-dir')
            run['started_at']=previous['started_at']
        atomic_json(run_path,run)
        db=open_checkpoint(folder/'checkpoint.sqlite3')
        todo=[]
        for code,record in records.items():
            row=db.execute('SELECT status,fingerprint,input_sha256 FROM history_stocks WHERE symbol=?',(code,)).fetchone()
            if row and row[1]==settings['fingerprint'] and (row[0]=='success' or (row[0]=='failed' and not args.retry_failed)):
                cached=args.cache_dir/args.source/'day/ohlcv'/f'{code}.csv'
                if row[0]=='failed' or (cached.is_file() and file_hash(cached)==row[2]):continue
            todo.append(record)
            save_checkpoint(db,{**record,'status':'pending','fingerprint':settings['fingerprint'],
                                'input_sha256':None,'reason':'scheduled for processing with current inputs'})
        summary=export_summary(db,folder,run)
        print(json.dumps({'event':'started','stocks':len(records),'pending':len(todo),'workers':args.workers,'semantics':run['universe_semantics']},ensure_ascii=False),flush=True)
        executor=ProcessPoolExecutor(max_workers=args.workers)
        pending={};iterator=iter(todo);last_log=time.monotonic();completed=0
        def enqueue():
            for record in iterator:
                pending[executor.submit(process_history,record,settings)]=record
                return True
            return False
        for _ in range(min(len(todo),args.workers*2)):enqueue()
        try:
            while pending:
                done,_=wait(pending,timeout=1,return_when=FIRST_COMPLETED)
                for future in done:
                    record=pending.pop(future)
                    try:result=future.result()
                    except Exception as exc:result={**record,'status':'failed','error':f'worker: {type(exc).__name__}: {exc}','fingerprint':settings['fingerprint']}
                    save_checkpoint(db,result);completed+=1;enqueue()
                    print(json.dumps({'event':'stock_complete','symbol':record['symbol'],'status':result['status'],
                                      'prefixes':result.get('visited_prefixes'),'signals':result.get('candidate_first_seen_count'),'seconds':result.get('elapsed_seconds'),'error':result.get('error')},ensure_ascii=False),flush=True)
                if done or time.monotonic()-last_log>=20:
                    summary=export_summary(db,folder,run);last_log=time.monotonic()
            executor.shutdown(wait=True)
        except KeyboardInterrupt:
            for future in pending:future.cancel()
            for process in executor._processes.values():process.terminate()
            executor.shutdown(wait=False,cancel_futures=True)
            export_summary(db,folder,run,paused=True)
            print('Interrupted: completed-stock checkpoints preserved; unfinished stocks replay from their first prefix.',file=sys.stderr)
            return 130
        summary=export_summary(db,folder,run);db.close()
        print(json.dumps({'event':'finished','success':summary['success'],'failed':summary['failed'],'prefixes':summary['visited_daily_prefixes'],
                          'candidate_first_seen_count':summary['candidate_first_seen_count'],'summary':str(folder/'summary.json')},ensure_ascii=False),flush=True)
        return 0 if not summary['failed'] else 1
    except (ValueError,OSError,KeyError,json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__=='__main__':raise SystemExit(main())
