#!/usr/bin/env python3
"""Unified complete structures, multi-timeframe evidence, and causal study view."""
import argparse
import csv
from dataclasses import asdict,replace
import gzip
import base64
import hashlib
import io
import json
from pathlib import Path
import sys

from chanmodel.unified import analyze_all,CompleteReplay,replay_schedule,demo_inputs,DEFAULT_CONFIG
from chanmodel.model import time_value
from chanmodel.v2.io import load_series,load_cache_series
from chanmodel.v2.series import derive_series,PERIODS

ROOT=Path(__file__).resolve().parent


def integer(value):
    number=int(value)
    if number<1:raise argparse.ArgumentTypeError('must be positive')
    return number


def assignments(values):
    result={}
    for item in values:
        period,path=item.split('=',1)
        if period not in PERIODS or period in result:raise ValueError('unique supported period=path required')
        result[period]=Path(path)
    return result


def compact(snapshot):
    periods={}
    for period,r in snapshot['reports'].items():
        # Detailed proofs remain in report.json. The view retains every base
        # object, center, type and candidate needed to inspect raw provenance.
        periods[period]={k:r[k] for k in ['base','hierarchy','signals','divergence_comparisons','center_evolution',
                                        'center_monitors','construction_phases','feature_sequences','decompositions']}
        periods[period]['input_count']=r['input_audit']['bars_used']
    return {'as_of':snapshot['as_of'],'reports':periods,'links':snapshot['links'],
            'six_class_coverage':snapshot['six_class_coverage']}


def export(out,inputs,report,frames,events,schedule,receipts):
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError('output directory must be new or empty')
    out.mkdir(parents=True,exist_ok=True)
    report['source_receipts']=receipts;report['replay_schedule']=schedule
    manifest={k:report[k] for k in ['schema','profile','as_of','symbol','config','max_rank','input_audits',
                                   'definition_mode','structure_completeness','approximation_loss','execution_allowed']}
    manifest.update(source_receipts=receipts,replay_schedule=schedule,
                    synthetic=all(s.source=='synthetic_teaching' or 'synthetic_teaching' in s.source for s in inputs.values()),
                    code_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in sorted((ROOT/'chanmodel').rglob('*.py'))})
    payload={'manifest':manifest,'bars':{p:[asdict(b) for b in s.prefix(report['as_of']).bars] for p,s in inputs.items()},
             'frames':frames or [compact(report)],'events':events}
    packed=base64.b64encode(gzip.compress(json.dumps(payload,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode(),mtime=0)).decode()
    template=(ROOT/'tools/complete_study_view.html').read_text(encoding='utf-8')
    (out/'完整算法复习.html').write_text(template.replace('__COMPLETE_DATA__',packed),encoding='utf-8')
    for name,value in [('report.json',report),('manifest.json',manifest)]:
        (out/name).write_text(json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf-8')
    (out/'events.jsonl').write_text(''.join(json.dumps(e,ensure_ascii=False,allow_nan=False)+'\n' for e in events),encoding='utf-8')
    with (out/'signals.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        columns=['period','class','rank','state','anchor','price','available_time','invalidation','invalidation_operator',
                 'trade_level','confirm_level','trigger_level','definition_mode','structure_completeness','approximation_loss']
        writer=csv.DictWriter(stream,fieldnames=columns);writer.writeheader()
        roles=report['links'].get('selected_roles',[])
        for period,r in report['reports'].items():
            for signal in r['signals']:
                row={k:signal.get(k) for k in columns}
                row.update(period=period,trade_level=period,confirm_level=roles[1] if len(roles)==3 and roles[0]==period else 'missing',
                           trigger_level=roles[2] if len(roles)==3 and roles[0]==period else 'missing',
                           definition_mode=r['definition_mode'],structure_completeness=report['structure_completeness'],
                           approximation_loss=json.dumps(r['approximation_loss'],ensure_ascii=False))
                writer.writerow(row)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['demo','analyze','replay'])
    parser.add_argument('--symbol')
    parser.add_argument('--input',action='append',default=[],help='period=CSV; repeat for real multi-timeframe inputs')
    parser.add_argument('--metadata',action='append',default=[],help='period=provider_metadata.json')
    parser.add_argument('--source',default='user_csv')
    parser.add_argument('--adjustment',choices=['unadjusted','qfq','hfq'],default='unadjusted')
    parser.add_argument('--cache',type=Path,default=ROOT/'market_data')
    parser.add_argument('--period',choices=list(PERIODS),default='day')
    parser.add_argument('--derive-from',choices=['1m','5m'])
    parser.add_argument('--multiframe',action='store_true')
    parser.add_argument('--roles',help='trade,confirm,trigger periods')
    parser.add_argument('--cutoff')
    parser.add_argument('--bars',type=integer,help='explicit first N observations; single input before any aggregation')
    parser.add_argument('--replay-bars',type=integer,default=64,help='scheduled finest closes at end; all warm history retained')
    parser.add_argument('--replay-every',type=integer,default=1)
    parser.add_argument('--max-rank',type=integer,default=8)
    parser.add_argument('--second-policy',choices=['original_101','conservative'],default='original_101')
    parser.add_argument('--third-boundary',choices=['strict_outside','not_break'],default='strict_outside')
    parser.add_argument('--volume-tolerance',type=float,default=1.)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    try:
        out=args.output_dir.resolve()
        if out.exists() and (not out.is_dir() or any(out.iterdir())):raise ValueError('output directory must be new or empty')
        receipts={}
        if args.command=='demo':inputs=demo_inputs()
        else:
            if not args.symbol:raise ValueError('--symbol is required for observed data')
            paths=assignments(args.input);metadata=assignments(args.metadata);inputs={}
            if set(metadata)-set(paths):raise ValueError('metadata must name an explicitly supplied CSV input')
            if paths:
                for period,path in paths.items():
                    inputs[period],receipts[period]=load_series(path,args.symbol,period,args.adjustment,args.source,metadata.get(period))
            else:
                if args.adjustment!='unadjusted':raise ValueError('cache is unadjusted; supply matching CSV for other adjustments')
                periods=[args.derive_from] if args.derive_from else ['day','30m','5m'] if args.multiframe else [args.period]
                for period in periods:inputs[period],receipts[period]=load_cache_series(args.cache,args.symbol,period)
            if args.bars:
                if len(inputs)!=1:raise ValueError('--bars requires one input before aggregation')
                p=next(iter(inputs));inputs[p]=replace(inputs[p],bars=inputs[p].bars[:args.bars])
            if args.derive_from:
                if args.derive_from not in inputs:raise ValueError('--derive-from source is missing')
                smallest=inputs[args.derive_from];end=args.cutoff or smallest.bars[-1].closed_at
                five=smallest if smallest.period=='5m' else derive_series(smallest,'5m',end)
                thirty=derive_series(five,'30m',end);daily=derive_series(thirty,'day',end)
                inputs={'day':daily,'30m':thirty,'5m':five}
                if smallest.period=='1m':inputs['1m']=smallest
                receipts['aggregation']='complete observed child buckets only; source receipt retained'
        cutoff=args.cutoff or min((s.bars[-1].closed_at for s in inputs.values()),key=time_value)
        roles=tuple(args.roles.split(',')) if args.roles else ('day','30m','5m') if {'day','30m','5m'}<=inputs.keys() else None
        config=replace(DEFAULT_CONFIG,second_policy=args.second_policy,third_boundary=args.third_boundary)
        frames=[];events=[];schedule={'observations':1,'semantics':'single cutoff snapshot, not a historical replay'}
        if args.command in {'replay','demo'}:
            times,schedule=replay_schedule(inputs,cutoff,args.replay_bars,args.replay_every)
            engine=CompleteReplay(inputs,config,args.max_rank,roles,args.volume_tolerance)
            for i,moment in enumerate(times):
                report=engine.observe(moment);frames.append(compact(report))
                if i%20==0:print(f'replay {i+1}/{len(times)}',file=sys.stderr)
            if not times:raise ValueError('no shared replay schedule')
            events=engine.events
            if time_value(report['as_of'])<time_value(cutoff):
                report=engine.observe(cutoff);frames.append(compact(report));events=engine.events
        else:report=analyze_all(inputs,cutoff,config,args.max_rank,roles,args.volume_tolerance)
        export(out,inputs,report,frames,events,schedule,receipts)
        print(json.dumps({'output':str(out/'完整算法复习.html'),'periods':list(inputs),'frames':len(frames) or 1,
                          'events':len(events),'six_classes':report['six_class_coverage']},ensure_ascii=False))
        return 0
    except (OSError,ValueError,KeyError,IndexError) as exc:
        print(f'ERROR: {type(exc).__name__}: {exc}',file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
