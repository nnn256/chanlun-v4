"""Preserve all returned native TDX timeframes plus per-day historical time shares."""
import argparse
from concurrent.futures import ThreadPoolExecutor,as_completed
import gzip
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from chanmodel.tdx_data import TdxClient,atomic_bytes,atomic_json,now_iso,symbol


def capture(code,out,progress=None):
    out.mkdir(parents=True,exist_ok=True)
    client=TdxClient(timeout=90)
    manifest={'symbol':code,'source':'TDX','requested_at':now_iso(),'periods':{},'execution_allowed':False}
    labels={'day':'日线','minute1':'1分钟','minute5':'5分钟','minute15':'15分钟','minute30':'30分钟','hour':'60分钟','week':'周线','month':'月线','quarter':'季线','year':'年线'}
    def notify(stage,message,**fields):
        if progress:progress(stage,message,**fields)
    def retry_callback(period):
        def retry(info):
            notify('retry',f"{labels.get(period,period)}请求超时或连接中断，正在第{info['next_attempt']}/{info['max_attempts']}次尝试",**info)
            print(json.dumps({'request_retry':period,**info},ensure_ascii=False),flush=True)
        return retry
    for index,period in enumerate(('day','minute1','minute5','minute15','minute30','hour','week','month','quarter','year')):
        notify('kline',f'正在获取{labels[period]}（{index+1}/10个原生周期）',completed=index,total=10,unit='周期')
        target=out/'raw'/f'{period}.json.gz';receipt_path=out/'receipts'/f'{period}.json'
        if target.exists() and receipt_path.exists():
            envelope=json.loads(gzip.decompress(target.read_bytes()));data=envelope['data'];receipt=json.loads(receipt_path.read_text(encoding='utf-8'))
        else:
            data,receipt,raw=client.request_retry('/api/kline-all/tdx',{'code':code,'type':period},on_retry=retry_callback(period))
            atomic_bytes(target,gzip.compress(raw,mtime=0));atomic_json(receipt_path,receipt)
        rows=data.get('list',data.get('List',[]))
        item={'status':'received' if rows else 'empty','count':len(rows),'first':rows[0].get('Time') if rows else None,'last':rows[-1].get('Time') if rows else None,'receipt':receipt}
        manifest['periods'][period]=item;atomic_json(out/'acquisition.json',manifest)
        print(json.dumps({'period':period,**{k:item[k] for k in ('count','first','last')}},ensure_ascii=False),flush=True)
    quote,receipt,raw=client.quotes([code]);atomic_bytes(out/'quote-response.json',raw);atomic_json(out/'quote-receipt.json',receipt)
    daily=json.loads(gzip.decompress((out/'raw/day.json.gz').read_bytes()))['data']['list']
    dates=[r['Time'][:10].replace('-','') for r in daily]
    def one(date):
        target=out/'timeshare'/f'{date}.json.gz'
        metadata=out/'timeshare'/f'{date}.receipt.json'
        if target.exists() and metadata.exists():
            previous=json.loads(metadata.read_text(encoding='utf-8'))
            if previous['status'] in ('received','empty'):return previous
        try:
            data,receipt,raw=TdxClient(timeout=20).request_retry('/api/minute',{'code':code,'date':date},attempts=3)
            rows=data.get('List',[])
            if data.get('date')!=date:raise ValueError('requested date differs from returned date')
            if data.get('Count')!=len(rows):raise ValueError('time-share Count mismatch')
            atomic_bytes(target,gzip.compress(raw,mtime=0))
            result={'date':date,'status':'received' if rows else 'empty','count':len(rows),'first':rows[0] if rows else None,'last':rows[-1] if rows else None,'receipt':receipt}
        except Exception as exc:result={'date':date,'status':'failed','error':f'{type(exc).__name__}: {exc}'}
        atomic_json(metadata,result);return result
    # Test old and recent requests before archiving every observed trading date.
    for date in (dates[0],dates[-1],dates[len(dates)//2]):
        r=one(date);print(json.dumps({'time_share_probe':date,**{k:r.get(k) for k in ('status','count','first','last','error')}},ensure_ascii=False),flush=True)
    rows=[]
    last_progress=0;began=time.monotonic()
    notify('timeshare',f'正在采集历史分时：0/{len(dates):,}个交易日',completed=0,total=len(dates),unit='交易日')
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures=[pool.submit(one,date) for date in dates]
        for future in as_completed(futures):
            r=future.result()
            rows.append(r)
            now=time.monotonic()
            if now-last_progress>=.4 or len(rows)==len(dates):
                failed=sum(x['status']=='failed' for x in rows)
                notify('timeshare',f'正在采集历史分时：{len(rows):,}/{len(dates):,}个交易日，失败{failed}天',completed=len(rows),total=len(dates),unit='交易日',
                       failed_dates=failed,remaining_seconds=round((now-began)/len(rows)*(len(dates)-len(rows))))
                last_progress=now
            if len(rows)%50==0 or len(rows)==len(dates):
                states={state:sum(x['status']==state for x in rows) for state in ('received','empty','failed')}
                atomic_json(out/'timeshare-progress.json',{'done':len(rows),'total':len(dates),'states':states})
                print(json.dumps({'time_share_done':len(rows),'total':len(dates),**states}),flush=True)
    rows.sort(key=lambda r:r['date'])
    manifest['time_share_history']={'requested_dates':len(dates),'received_dates':sum(r['status']=='received' for r in rows),'empty_dates':sum(r['status']=='empty' for r in rows),'failed_dates':sum(r['status']=='failed' for r in rows),'dates':rows}
    manifest['auction']={'status':'unsupported','reason':'This tdx-api exposes quotes, continuous-session time shares and trades; no historical auction/order-book snapshot endpoint. Do not relabel a 09:31 minute as auction.'}
    manifest['finished_at']=now_iso();atomic_json(out/'acquisition.json',manifest)
    notify('acquired','全部可用周期和历史分时已采集，正在计算结构')
    print(json.dumps({'finished':str(out),'time_share_dates':manifest['time_share_history']['received_dates']},ensure_ascii=False),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--symbol',required=True);parser.add_argument('--output',required=True,type=Path);a=parser.parse_args()
    capture(symbol(a.symbol),a.output.resolve())
