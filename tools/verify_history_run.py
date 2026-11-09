#!/usr/bin/env python3
"""Verify cached-input hashes, prefix-event time and outcome labels of a run.

Reads the run/checkpoint and writes only verification.json / VERIFICATION.md.
All successful stocks have CSV/event/label bytes hashed. Deep checks are a
deterministic sample across exchanges and history lengths, never a claim that
all portfolios or all daily decisions have been independently re-derived.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import asdict
from datetime import datetime,timezone,timedelta
import gzip
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from chanmodel.engine import analyze,object_body
from chanmodel.model import Config,load_csv
from chanmodel.tdx_data import atomic_json,atomic_bytes,now_iso
from history_replay import implementation_fingerprint,label_groups


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def read_jsonl_gzip(path):
    with gzip.open(path,'rt',encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]


def checked_path(folder,kind,code,receipt):
    expected=(folder/kind/f'{code}.jsonl.gz').resolve()
    supplied=Path(receipt['file']).resolve()
    if supplied!=expected:
        raise ValueError(f'{code}: {kind} receipt path is outside its expected run location')
    if digest(expected)!=receipt['sha256']:
        raise ValueError(f'{code}: {kind} file hash differs from receipt')
    return expected


def small_equal(a,b):
    if isinstance(a,bool) or isinstance(b,bool):return a is b
    if isinstance(a,(int,float)) and isinstance(b,(int,float)):
        return math.isfinite(a) and math.isfinite(b) and math.isclose(a,b,rel_tol=1e-11,abs_tol=1e-12)
    return a==b


def check_events(events,bars):
    registry={};first={};last_time=-1;errors=[]
    for sequence,event in enumerate(events):
        ident=event.get('id');observed=event.get('observed_at')
        if event.get('kind')!='signal':errors.append(f'event {sequence}: non-signal kind')
        if not isinstance(observed,int) or not 0<=observed<len(bars):
            errors.append(f'event {sequence}: observed_at outside actual input');continue
        if observed<last_time:errors.append(f'event {sequence}: observation order decreased')
        last_time=observed
        if event.get('sequence')!=sequence:errors.append(f'event {sequence}: sequence mismatch')
        if event.get('observed_time')!=bars[observed].closed_at:errors.append(f'event {sequence}: time differs from CSV close')
        if event.get('available_at')!=observed:errors.append(f'event {sequence}: availability is backdated')
        if event.get('input_revision')!=0:errors.append(f'event {sequence}: unexpected mutable input revision')
        previous=registry.get(ident)
        expected_revision=previous['revision']+1 if previous else 0
        if event.get('revision')!=expected_revision:errors.append(f'event {sequence}: revision sequence mismatch')
        if event.get('event')=='withdrawn':
            if previous is None or not previous['active']:errors.append(f'event {sequence}: withdrawal without active prior object')
            registry[ident]={'revision':event.get('revision'),'active':False,'body':previous['body'] if previous else {}}
            continue
        point=event.get('value')
        if not isinstance(point,dict):errors.append(f'event {sequence}: missing signal body');continue
        if point.get('id')!=ident:errors.append(f'event {sequence}: body identity mismatch')
        anchor=point.get('anchor');basis=point.get('basis_at')
        if not isinstance(anchor,int) or not 0<=anchor<=observed:errors.append(f'event {sequence}: future anchor')
        if not isinstance(basis,int) or not 0<=basis<=observed:errors.append(f'event {sequence}: future confirmation basis')
        terminal=point.get('terminal_range')
        if not isinstance(terminal,list) or len(terminal)!=2 or not 0<=terminal[0]<=terminal[1]<=observed:
            errors.append(f'event {sequence}: invalid terminal range')
        if point.get('execution_allowed') is not False:errors.append(f'event {sequence}: execution unexpectedly allowed')
        status=point.get('status')
        if status=='candidate' and ident not in first:first[ident]=observed
        if ident in first and event.get('first_candidate_at')!=first[ident]:errors.append(f'event {sequence}: inconsistent first candidate time')
        registry[ident]={'revision':event.get('revision'),'active':True,'body':point}
    entries=[]
    seen=set()
    for event in events:
        if event.get('value',{}).get('status')=='candidate' and event.get('first_candidate_at')==event.get('observed_at'):
            if event['id'] in seen:errors.append('duplicate entry observation '+event['id'])
            else:entries.append(event);seen.add(event['id'])
    return errors,entries


def expected_label(bars,event,horizon,cost):
    point=event['value'];entry=event['observed_at']+1;exit_index=entry+horizon-1
    row={'signal_id':event['id'],'signal_class':point['signal_class'],'side':point['side'],
         'observed_at':event['observed_at'],'entry_index':entry,'exit_index':exit_index,
         'horizon_bars':horizon,'definition_mode':point['definition_mode']}
    if exit_index>=len(bars) or not bars[exit_index].closed:
        row['status']='insufficient_future_for_label'
    elif bars[entry].open<=0:row['status']='nonpositive_entry_percentage_undefined'
    elif bars[entry].volume==0:row['status']='zero_volume_entry'
    else:
        sign=1 if point['side']=='buy' else -1
        gross=sign*(bars[exit_index].close/bars[entry].open-1)
        row.update(status='labelled',entry_price=bars[entry].open,exit_price=bars[exit_index].close,
                   gross_directional_return=gross,hypothetical_return_after_constant_cost=gross-cost/10000)
    return row


def check_labels(labels,entries,bars,horizons,cost):
    errors=[];by_key={}
    for row in labels:
        key=(row.get('signal_id'),row.get('horizon_bars'))
        if key in by_key:errors.append(f'duplicate signal/horizon outcome: {key}')
        by_key[key]=row
    expected_keys={(event['id'],h) for event in entries for h in horizons}
    if set(by_key)!=expected_keys:errors.append('label identities/horizons do not equal unique first candidate observations')
    for event in entries:
        for horizon in horizons:
            key=(event['id'],horizon);expected=expected_label(bars,event,horizon,cost);actual=by_key.get(key)
            if actual is None:continue
            if set(actual)!=set(expected):errors.append(f'{key}: label fields differ')
            for field,value in expected.items():
                if field not in actual or not small_equal(actual[field],value):errors.append(f'{key}: recomputed {field} differs')
    return errors


def select_sample(results,count):
    chosen={}
    for exchange in ['sh','sz','bj']:
        rows=sorted([r for r in results if r.get('exchange')==exchange],key=lambda r:(r['history_bars'],r['symbol']))
        if not rows:continue
        for index in {0,len(rows)//2,len(rows)-1}:
            chosen[rows[index]['symbol']]=rows[index]
        eventful=sorted([r for r in rows if r.get('candidate_first_seen_count',0)>0],key=lambda r:(r['history_bars'],r['symbol']))
        if eventful:chosen[eventful[len(eventful)//2]['symbol']]=eventful[len(eventful)//2]
    for code in ['sz000001','sh600519','bj920002']:
        item=next((r for r in results if r['symbol']==code),None)
        if item:chosen[code]=item
    ranked=sorted(results,key=lambda r:hashlib.sha256(r['symbol'].encode()).hexdigest())
    for row in ranked:
        if len(chosen)>=count:break
        chosen[row['symbol']]=row
    if len(chosen)>count:
        # Keep all exchange/length anchors even when caller asks a tiny sample.
        return sorted(chosen.values(),key=lambda r:r['symbol'])
    return sorted(chosen.values(),key=lambda r:r['symbol'])


def ledger_at(events,stop):
    active={}
    for event in events:
        if event['observed_at']>stop:break
        if event['event']=='withdrawn':active.pop(event['id'],None)
        else:active[event['id']]=event['value']
    return active


def choose_prefixes(bars,events):
    stops={len(bars)-1,len(bars)//2}
    entries=[e['observed_at'] for e in events if e.get('value',{}).get('status')=='candidate']
    if entries:
        stops.add(entries[0]);stops.add(entries[len(entries)//2])
        if entries[0]>0:stops.add(entries[0]-1)
    return sorted(stop for stop in stops if 0<=stop<len(bars))


def no_history_evidence(code,daily_run,cache,source):
    if daily_run is None:return {'verified':False,'reason':'no daily collection run supplied'}
    stock=daily_run/'stocks'/f'{code}.json'
    if not stock.is_file():return {'verified':False,'reason':'daily stock receipt missing'}
    result=read_json(stock)
    response=daily_run/'responses'/f'{code}.json.gz'
    meta=cache/source/'day/metadata'/f'{code}.json'
    try:
        metadata=read_json(meta)
        compressed=response.read_bytes();raw=gzip.decompress(compressed);envelope=json.loads(raw)
        data=envelope.get('data') or {};rows=data.get('list',data.get('List'))
        verified=(result.get('status')=='empty_history' and result.get('quality',{}).get('closed_rows')==0
                  and envelope.get('code')==0 and (rows is None or rows==[]) and data.get('count',data.get('Count'))==0
                  and hashlib.sha256(raw).hexdigest()==metadata.get('response_sha256'))
        return {'verified':verified,'daily_status':result.get('status'),'raw_rows':0 if verified else len(rows) if isinstance(rows,list) else None,
                'response_file':str(response.resolve()),'response_sha256':hashlib.sha256(raw).hexdigest(),
                'meaning':'no closed history supplied by source, not a successful replay and not proof the security never traded'}
    except Exception as exc:return {'verified':False,'reason':f'{type(exc).__name__}: {exc}'}


def verify(run_dir,cache_dir=None,daily_run=None,sample_count=20,allow_incomplete=False):
    folder=Path(run_dir).resolve();run=read_json(folder/'run.json');settings=run['settings'];summary=read_json(folder/'summary.json')
    cache=Path(cache_dir or settings['cache_dir']).resolve();source=settings['source'];config=Config(**settings['config'])
    failures=[];warnings=[];checks=[];available=[];unavailable=[];pending=[];unexpected=[]
    rows_by_code={record['symbol']:record for record in run['records']}
    expected=set(rows_by_code)
    db=sqlite3.connect((folder/'checkpoint.sqlite3').as_uri()+'?mode=ro',uri=True)
    db_rows={code:(status,fingerprint,input_sha,text) for code,status,fingerprint,input_sha,text in db.execute('SELECT symbol,status,fingerprint,input_sha256,result_json FROM history_stocks')};db.close()
    actual_implementation=implementation_fingerprint()
    if actual_implementation!=settings['implementation_sha256']:failures.append('current implementation fingerprint differs from frozen replay run')
    if settings.get('start_date') is not None:failures.append('this verifier requires fullspan run with start_date=null')
    snapshot_universe=Path(run['universe_file'])
    if not snapshot_universe.is_file() or digest(snapshot_universe)!=settings['universe_sha256']:failures.append('input universe hash mismatch or missing universe snapshot')
    counts=Counter();histories=0;event_rows=0;label_rows=0
    for code in sorted(expected):
        path=folder/'stocks'/f'{code}.json';checkpoint=db_rows.get(code)
        if checkpoint is None or checkpoint[0]=='pending':pending.append(code);continue
        try:
            result=read_json(path)
            if result.get('symbol')!=code or result.get('fingerprint')!=settings['fingerprint']:raise ValueError('stock receipt identity/fingerprint mismatch')
            if checkpoint[1]!=settings['fingerprint'] or json.loads(checkpoint[3])!=result:raise ValueError('SQLite row differs from stock receipt')
            if result['status']!='success':
                evidence=no_history_evidence(code,Path(daily_run).resolve() if daily_run else None,cache,source)
                if evidence['verified']:
                    unavailable.append({'symbol':code,'history_status':result['status'],'history_error':result.get('error'),'evidence':evidence})
                    counts['source_unavailable']+=1
                else:
                    unexpected.append({'symbol':code,'history_status':result['status'],'history_error':result.get('error'),'evidence':evidence})
                    failures.append(f'{code}: replay failed and no verified empty-source explanation')
                continue
            csv_path=cache/source/'day/ohlcv'/f'{code}.csv';metadata=read_json(cache/source/'day/metadata'/f'{code}.json')
            actual_hash=digest(csv_path)
            if actual_hash!=result.get('input_sha256') or actual_hash!=checkpoint[2] or actual_hash!=metadata.get('csv_sha256'):raise ValueError('CSV hash differs from receipt/checkpoint/provider metadata')
            if metadata.get('symbol')!=code or metadata.get('source')!=source:raise ValueError('metadata symbol or source mismatch')
            with csv_path.open(encoding='utf-8-sig',newline='') as stream:
                closed=sum(str(row.get('is_closed','true')).strip().lower() in {'true','1'} for row in csv.DictReader(stream))
            if result.get('history_bars')!=closed or result.get('visited_prefixes')!=closed or result.get('warmup_bars')!=0 or result.get('start_date') is not None:
                raise ValueError('fullspan visited-prefix count differs from actual closed input rows')
            for kind in ['events','labels']:
                checked_path(folder,kind,code,result[kind])
            counts['success_hashed']+=1;histories+=closed;event_rows+=result['events']['rows'];label_rows+=result['labels']['rows'];available.append(result)
        except Exception as exc:failures.append(f'{code}: {type(exc).__name__}: {exc}')
    extra=set(db_rows)-expected
    if extra:warnings.append('checkpoint contains out-of-universe rows: '+','.join(sorted(extra)))
    if pending and not allow_incomplete:failures.append(f'{len(pending)} stocks have no final replay checkpoint')
    processed=len(available)+len(unavailable)+len(unexpected)
    for field,actual in [('success',len(available)),('processed',processed),('pending',len(pending)),('visited_daily_prefixes',histories)]:
        if summary.get(field)!=actual:
            if allow_incomplete:warnings.append(f'live summary {field}={summary.get(field)}, checkpoint snapshot={actual}')
            else:failures.append(f'summary {field} differs from independently counted checkpoint data')
    checks.append({'check':'all_successful_stock_csv_and_event_label_hashes','stocks':len(available),'closed_daily_prefixes':histories,
                   'event_rows_declared':event_rows,'label_rows_declared':label_rows,'scope':'all completed successful stocks'})
    sample=select_sample(available,sample_count);deep=[]
    for result in sample:
        code=result['symbol']
        try:
            bars,_=load_csv(cache/source/'day/ohlcv'/f'{code}.csv');bars=[bar for bar in bars if bar.closed]
            events=read_jsonl_gzip(folder/'events'/f'{code}.jsonl.gz');labels=read_jsonl_gzip(folder/'labels'/f'{code}.jsonl.gz')
            local,entries=check_events(events,bars)
            if len(events)!=result['events']['rows']:local.append('event row count differs from receipt')
            if len(labels)!=result['labels']['rows']:local.append('label row count differs from receipt')
            if len(entries)!=result['candidate_first_seen_count']:local.append('first candidate entry count differs from receipt')
            local.extend(check_labels(labels,entries,bars,settings['horizons'],settings['round_trip_bps']))
            if label_groups(labels)!=result['label_groups']:local.append('label group aggregation differs from receipt')
            prefix_checks=[]
            for stop in choose_prefixes(bars,events):
                actual=ledger_at(events,stop)
                reference={point['id']:object_body(point) for point in analyze(bars[:stop+1],config,'day')['signal_candidates']}
                same=actual==reference
                prefix_checks.append({'prefix_bars':stop+1,'date':bars[stop].closed_at,'ledger_signal_count':len(actual),'reference_signal_count':len(reference),'equal':same})
                if not same:local.append(f'prefix {stop+1}: ledger current signals differ from full analyze reference')
            deep.append({'symbol':code,'exchange':result['exchange'],'history_bars':len(bars),'events':len(events),'labels':len(labels),
                         'entry_count':len(entries),'prefix_checks':prefix_checks,'status':'pass' if not local else 'fail','errors':local})
            failures.extend(f'{code}: {error}' for error in local)
        except Exception as exc:
            failures.append(f'{code}: sample deep check {type(exc).__name__}: {exc}')
            deep.append({'symbol':code,'status':'fail','error':str(exc)})
    evidence=[]
    for name in ['history-real-prefix-equivalence-final.json','history-sz-real-nonzero-equivalence.json','history-fullspan-8485-benchmark-final.json']:
        path=ROOT/'artifacts/tdx-integration'/name
        if path.is_file():evidence.append({'file':str(path.resolve()),'sha256':digest(path),'evidence':read_json(path)})
    status='fail' if failures else 'partial_verified' if pending else 'pass_with_source_unavailable' if unavailable else 'pass'
    report={'verified_at':now_iso(),'status':status,'run_dir':str(folder),'sample_policy':'deterministic 20-stock sample anchored by exchange and shortest/median/longest histories plus named long/short fixtures',
            'universe_count':len(expected),'success_hashed':len(available),'source_unavailable_count':len(unavailable),'unexpected_failure_count':len(unexpected),'pending_count':len(pending),
            'all_universe_symbols_accounted_for':not pending,'all_symbols_replayed':len(available)==len(expected),'closed_daily_prefixes':histories,
            'checks':checks,'sample_deep_checks':deep,'sample_count':len(deep),'sample_exchanges':dict(Counter(r.get('exchange') for r in sample)),
            'source_unavailable':unavailable,'unexpected_failures':unexpected,'pending_symbols':pending,'failures':failures,'warnings':warnings,
            'frozen_implementation_sha256':settings['implementation_sha256'],'current_implementation_sha256':actual_implementation,
            'executed_prefix_equivalence_evidence':evidence,'execution_allowed':False,
            'limits':['All successful CSV and compressed event/label hashes are checked; event chronology, outcome arithmetic and reference snapshots are sampled.',
                      'The current stock universe has survivorship bias; historical constituents and delisted coverage remain incomplete.',
                      'Fixed-horizon directional labels are not a portfolio backtest or proof of executable profitability.']}
    atomic_json(folder/'verification.json',report)
    lines=['# 逐日历史回放核验','',f"核验时间：{report['verified_at']}",f"结果：**{status}**",'',
           f"股票池{len(expected)}只；全量字节核对通过{len(available)}只；来源无历史且证据核对{len(unavailable)}只；意外失败{len(unexpected)}只；待完成{len(pending)}只。",'',
           f"已成功股票共{histories}个完整历史逐日prefix；start_date=null、warmup=0、visited_prefixes=history_bars与实际闭合CSV行数一致。",'',
           f"深度抽查{len(deep)}只，覆盖交易所{report['sample_exchanges']}：检查事件观察时间、版本顺序、首次候选唯一性、下一根开盘、5/20/60根窗口价格及常数成本重算，并在选定历史prefix重新调用原analyze核对事件账本当前信号。",'',
           '## 无历史来源','']
    lines += [f"- {item['symbol']}：日线采集状态empty_history且原始响应0条，经哈希核对。保留来源不可得，未填为回放成功。" for item in unavailable] or ['无。']
    lines += ['','## 失败与待完成','']+[f'- {error}' for error in failures]
    if not failures:lines+=['无未解释核验失败。']
    lines += ['','## 抽查结果','','| 股票 | 历史K线 | 事件 | 标签 | 核对 |','| --- | --- | --- | --- | --- |']
    lines += [f"| {item['symbol']} | {item.get('history_bars','—')} | {item.get('events','—')} | {item.get('labels','—')} | {item['status']} |" for item in deep]
    lines += ['','完整机器证据：[verification.json](verification.json)。','',
              '全量验证文件哈希；深度事件/标签重算为抽查。来源股票池是当前幸存者清单，仍缺历史退市与逐时点成员。结果为事件研究标签，未验证真实组合成交与利润。','']
    atomic_bytes(folder/'VERIFICATION.md','\n'.join(lines).encode('utf-8'))
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir',type=Path,required=True)
    parser.add_argument('--cache-dir',type=Path)
    parser.add_argument('--daily-run',type=Path)
    parser.add_argument('--sample',type=int,default=20)
    parser.add_argument('--allow-incomplete',action='store_true')
    args=parser.parse_args()
    if args.sample<1:parser.error('--sample must be positive')
    try:
        report=verify(args.run_dir,args.cache_dir,args.daily_run,args.sample,args.allow_incomplete)
    except (OSError,ValueError,KeyError,sqlite3.Error) as exc:parser.exit(2,f'verification error: {exc}\n')
    print(json.dumps({key:report[key] for key in ['status','universe_count','success_hashed','source_unavailable_count','unexpected_failure_count','pending_count','sample_count','closed_daily_prefixes','failures']},ensure_ascii=False,indent=2))
    return 1 if report['failures'] else 0


if __name__=='__main__':raise SystemExit(main())
