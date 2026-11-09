#!/usr/bin/env python3
"""CSV-based Chan structure research: analyze, replay, multilevel, evaluate, demo."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import sys

from chanmodel.engine import ReplayEngine, analyze, multilevel
from chanmodel.evaluate import evaluate_events
from chanmodel.model import Candle, Config, load_csv, parse_time, time_value


def save(path: Path, text: str):
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists() and path.read_text(encoding='utf-8') != text:
        stamp=datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        shutil.copy2(path,path.with_name(path.name+'.bak-'+stamp))
    path.write_text(text,encoding='utf-8')


def save_json(path: Path, value):
    save(path,json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')


def configuration(args):
    return Config(stroke_mode=args.stroke_mode,price_decimals=args.price_decimals,max_depth=args.max_depth,
                  second_policy=args.second_policy,third_boundary=args.third_boundary)


def read_cutoff(path, args):
    bars, source=load_csv(path,args.timezone)
    if args.as_of:
        cutoff,_=parse_time(args.as_of,args.timezone)
        if any(bar.closed_at is None for bar in bars):
            raise ValueError('--as-of requires explicit bar closing timestamps')
        bars=[bar for bar in bars if time_value(bar.closed_at)<=time_value(cutoff)]
    source['rows_used']=len(bars)
    return bars,source


def run_replay(bars,config,level):
    engine=ReplayEngine(config,level)
    for bar in bars:engine.update(bar)
    return engine.snapshot or analyze([],config,level),engine.events


def write_candles(path,bars):
    import io
    stream=io.StringIO(newline='')
    writer=csv.writer(stream);writer.writerow(['closed_at','open','high','low','close','volume','is_closed','data_origin'])
    for b in bars:writer.writerow([b.closed_at,b.open,b.high,b.low,b.close,b.volume,'true','synthetic'])
    save(path,stream.getvalue())


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    for command in ['analyze','replay','multilevel','demo']:
        p=sub.add_parser(command)
        p.add_argument('--stroke-mode',choices=['strict','loose'],default='loose')
        p.add_argument('--price-decimals',type=int,default=8)
        p.add_argument('--max-depth',type=int,default=3)
        p.add_argument('--second-policy',choices=['original_101','conservative'],default='original_101')
        p.add_argument('--third-boundary',choices=['strict_outside','not_break'],default='strict_outside')
        p.add_argument('--timezone',default='+08:00')
        if command in ['analyze','replay']:
            p.add_argument('csv',type=Path);p.add_argument('--level',required=True)
            p.add_argument('--as-of');p.add_argument('--output','-o',type=Path,required=True)
            if command=='replay':p.add_argument('--events',type=Path,required=True)
        elif command=='multilevel':
            p.add_argument('--input',action='append',required=True,help='LEVEL=CSV; repeat for each level')
            p.add_argument('--trade-level',required=True);p.add_argument('--confirm-level',required=True)
            p.add_argument('--trigger-level',required=True);p.add_argument('--as-of',required=True)
            p.add_argument('--output','-o',type=Path,required=True)
        else:
            p.add_argument('--output-dir',type=Path,default=Path(__file__).resolve().parent/'examples')
            p.add_argument('--bars',type=int,default=1200)
    p=sub.add_parser('evaluate');p.add_argument('csv',type=Path);p.add_argument('--events',type=Path,required=True)
    p.add_argument('--horizons',default='5,20,60');p.add_argument('--round-trip-bps',type=float,default=20)
    p.add_argument('--output','-o',type=Path,required=True)
    args=parser.parse_args()
    try:
        if args.command in ['analyze','replay']:
            targets=[args.csv.resolve(),args.output.resolve()]+([args.events.resolve()] if args.command=='replay' else [])
            if len(targets)!=len(set(targets)):raise ValueError('input CSV, output JSON and event ledger must be different files')
            bars,source=read_cutoff(args.csv,args);config=configuration(args)
            if args.command=='replay':
                report,events=run_replay(bars,config,args.level)
                save(args.events,''.join(json.dumps(e,ensure_ascii=False,allow_nan=False)+'\n' for e in events))
                report['event_ledger']={'file':str(args.events.resolve()),'count':len(events),'sha256':hashlib.sha256(args.events.read_bytes()).hexdigest()}
            else:report=analyze(bars,config,args.level)
            report['source']=source;save_json(args.output,report)
            print(json.dumps({'output':str(args.output.resolve()),'bars':len(bars),'strokes':len(report['strokes']),
                              'segments':len(report['segments']),'centers':len(report['centers']),'signal_candidates':len(report['signal_candidates'])},ensure_ascii=False))
        elif args.command=='multilevel':
            datasets,sources={},{}
            for spec in args.input:
                level,sep,path=spec.partition('=')
                if not sep or not level or level in datasets:raise ValueError('use distinct LEVEL=CSV inputs')
                if Path(path).resolve()==args.output.resolve():raise ValueError('output must differ from input CSV files')
                datasets[level],sources[level]=load_csv(Path(path),args.timezone)
            cutoff,_=parse_time(args.as_of,args.timezone)
            report=multilevel(datasets,(args.trade_level,args.confirm_level,args.trigger_level),cutoff,configuration(args))
            report['sources']=sources;save_json(args.output,report)
            print(json.dumps({'output':str(args.output.resolve()),'levels':list(datasets),'audits':len(report['interval_nesting_audits'])},ensure_ascii=False))
        elif args.command=='evaluate':
            if len({args.csv.resolve(),args.events.resolve(),args.output.resolve()})!=3:raise ValueError('evaluation input and output paths must differ')
            bars,source=load_csv(args.csv)
            events=[json.loads(line) for line in args.events.read_text(encoding='utf-8').splitlines() if line.strip()]
            report=evaluate_events(bars,events,tuple(int(x) for x in args.horizons.split(',')),args.round_trip_bps)
            report['source']=source;save_json(args.output,report)
            print(json.dumps({'output':str(args.output.resolve()),'labels':len(report['labels'])},ensure_ascii=False))
        else:
            if not 100<=args.bars<=20000:raise ValueError('demo --bars must be between 100 and 20000')
            start=datetime(2026,1,1,tzinfo=timezone.utc)
            bars=[]
            for i in range(args.bars):
                value=100+30*math.sin(i/140)+9*math.sin(i/25)+3*math.sin(i/5)
                bars.append(Candle(i,value,value+.2,value-.2,value,(start+timedelta(minutes=i+1)).isoformat(),1000+100*math.sin(i/11)))
            folder=args.output_dir
            write_candles(folder/'synthetic_1m.csv',bars)
            for width in [5,30]:
                aggregate=[]
                for offset in range(0,len(bars)-width+1,width):
                    group=bars[offset:offset+width]
                    aggregate.append(Candle(len(aggregate),group[0].open,max(b.high for b in group),min(b.low for b in group),group[-1].close,group[-1].closed_at,sum(b.volume for b in group)))
                write_candles(folder/f'synthetic_{width}m.csv',aggregate)
            report,events=run_replay(bars,configuration(args),'synthetic_1m')
            report['source']={'data_origin':'synthetic','formula':'100+30*sin(i/140)+9*sin(i/25)+3*sin(i/5)','rows':len(bars)}
            save_json(folder/'analysis.json',report)
            save(folder/'events.jsonl',''.join(json.dumps(e,ensure_ascii=False,allow_nan=False)+'\n' for e in events))
            save_json(folder/'evaluation.json',evaluate_events(bars,events))
            print(json.dumps({'output_dir':str(folder.resolve()),'data_origin':'synthetic','bars':len(bars),
                              'strokes':len(report['strokes']),'segments':len(report['segments']),'centers':len(report['centers']),
                              'signal_candidates_in_final_snapshot':len(report['signal_candidates']),'events':len(events)},ensure_ascii=False))
    except (ValueError,OSError,KeyError,json.JSONDecodeError) as exc:
        parser.exit(2,f'error: {exc}\n')


if __name__=='__main__':main()
