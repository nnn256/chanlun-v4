import argparse
from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
import time
from chanmodel.market_backtest import run_market


def main():
    root=Path(__file__).resolve().parent
    p=argparse.ArgumentParser(description='逐股逐根因果回测全部沪深京A股，可断点续跑')
    p.add_argument('--universe',type=Path,required=True);p.add_argument('--cache',type=Path,default=root/'market_data')
    p.add_argument('--cutoff',default=datetime.now(timezone(timedelta(hours=8))).isoformat())
    p.add_argument('--output',type=Path,required=True);p.add_argument('--workers',type=int,default=10)
    args=p.parse_args();records=json.loads(args.universe.read_text(encoding='utf-8'))['records'];last=[0.]
    if not 1<=args.workers<=20:raise ValueError('workers must be 1..20')
    def progress(summary):
        if time.monotonic()-last[0]>10:
            print(json.dumps(summary,ensure_ascii=False),flush=True);last[0]=time.monotonic()
    result=run_market(records,args.cache,args.cutoff,args.output,args.workers,progress,args.output/'STOP')
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return 0 if result['coverage_complete'] else 1


if __name__=='__main__':
    import multiprocessing
    multiprocessing.freeze_support();raise SystemExit(main())
