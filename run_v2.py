#!/usr/bin/env python3
"""Run recursive movement types and validated timeframe/trigger contracts."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

from chanmodel.model import Config
from chanmodel.tdx_data import atomic_json,atomic_bytes
from chanmodel.v2.io import load_cache_series,load_series
from chanmodel.v2.series import derive_series,validate_multiframe
from chanmodel.v2.engine import analyze,multiframe,replay

ROOT=Path(__file__).resolve().parent


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['analyze','validate','multiframe','replay'])
    parser.add_argument('--symbol',required=True)
    parser.add_argument('--cache',type=Path,default=ROOT/'market_data')
    parser.add_argument('--period',choices=['day','30m','5m','1m'],default='day')
    parser.add_argument('--csv',type=Path)
    parser.add_argument('--metadata',type=Path)
    parser.add_argument('--adjustment',choices=['unadjusted','qfq','hfq'],default='unadjusted')
    parser.add_argument('--source',default='user_csv')
    parser.add_argument('--derive-from',choices=['5m','1m'],help='derive day/30m/5m from one actual finest source; incomplete aggregation buckets stay absent')
    parser.add_argument('--cutoff',required=True,help='ISO timestamp with explicit timezone')
    parser.add_argument('--max-rank',type=int,default=3)
    parser.add_argument('--price-tolerance',type=float,default=0.005)
    parser.add_argument('--volume-tolerance-shares',type=float,default=1.0,help='explicit absolute parent-vs-children volume tolerance; recorded in every reconciliation')
    parser.add_argument('--stroke-mode',choices=['strict','loose'],default='loose')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--events',type=Path)
    args=parser.parse_args()
    try:
        config=Config(stroke_mode=args.stroke_mode)
        if not args.csv and args.adjustment!='unadjusted':raise ValueError('the cached TDX inputs are unadjusted; supply an explicit matching CSV/receipt for another basis')
        if args.command in {'analyze','replay'}:
            series,receipt=(load_series(args.csv,args.symbol,args.period,args.adjustment,args.source,args.metadata) if args.csv else load_cache_series(args.cache,args.symbol,args.period))
            if args.csv and args.output.resolve()==args.csv.resolve():raise ValueError('output cannot overwrite input')
            if args.command=='analyze':result=analyze(series,args.cutoff,config,args.max_rank)
            else:
                if not args.events:raise ValueError('replay requires --events')
                if args.events.resolve()==args.output.resolve():raise ValueError('event ledger and report paths must differ')
                if args.csv and args.events.resolve()==args.csv.resolve():raise ValueError('events cannot overwrite the input CSV')
                result,events=replay(series.prefix(args.cutoff),config,args.max_rank)
                atomic_bytes(args.events,''.join(json.dumps(e,ensure_ascii=False,allow_nan=False)+'\n' for e in events).encode())
                if result is None:raise ValueError('no completed observations at cutoff')
                result['event_file']=str(args.events.resolve());result['event_count']=len(events)
            result['source_receipt']=receipt
        else:
            receipts={}
            if args.derive_from:
                smallest,receipt=load_cache_series(args.cache,args.symbol,args.derive_from)
                five=smallest if args.derive_from=='5m' else derive_series(smallest,'5m',args.cutoff)
                thirty=derive_series(five,'30m',args.cutoff);daily=derive_series(thirty,'day',args.cutoff)
                inputs={'day':daily,'30m':thirty,'5m':five};receipts={'finest':receipt,'aggregation':'only fully populated exchange-session buckets'}
            else:
                inputs={}
                for period in ['day','30m','5m']:
                    inputs[period],receipts[period]=load_cache_series(args.cache,args.symbol,period)
            tolerance={'price_tolerance':args.price_tolerance,'volume_tolerance':args.volume_tolerance_shares}
            result=validate_multiframe(inputs,('day','30m','5m'),args.cutoff,**tolerance) if args.command=='validate' else multiframe(inputs,('day','30m','5m'),args.cutoff,config,args.max_rank,**tolerance)
            result['source_receipts']=receipts
            result['data_origin']='observed_TDX_cache_aggregated' if args.derive_from else 'observed_TDX_cache_independent_timeframes'
        atomic_json(args.output,result)
        print(json.dumps({'output':str(args.output.resolve()),'state':result.get('state',result.get('definition_mode')),
                          'rank_types':[(x['rank'],len(x['completed_types'])) for x in result.get('hierarchy',{}).get('levels',[])],
                          'signals':len(result.get('signals',[]))},ensure_ascii=False))
        return 0
    except (ValueError,OSError,KeyError) as exc:
        print(f'error: {type(exc).__name__}: {exc}',file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
