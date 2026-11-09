"""Durable EXE-safe all-market backtest jobs and progress."""
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from chanmodel.tdx_data import atomic_json,now_iso
from chanmodel.market_backtest import run_market
from .store import ROOT,STATE,connect


def launch(body):
    workers=body.get('workers',6)
    if type(workers) is not int or not 1<=workers<=16:raise ValueError('backtest workers must be 1..16')
    with connect() as db:
        for row in db.execute("SELECT value FROM metadata WHERE key LIKE 'backtest:%'"):
            value=json.loads(row[0])
            from .server import alive
            if value['state']=='running' and alive(value.get('pid')):raise ValueError('a backtest is already running')
        rows=[dict(r) for r in db.execute('SELECT * FROM universe WHERE active=1 ORDER BY symbol')]
        if body.get('scope','all')=='selected':
            selected=body.get('symbols',[])
            if not isinstance(selected,list) or not selected:raise ValueError('selected backtest needs securities')
            rows=[r for r in rows if r['symbol'] in selected]
            if len(rows)!=len(set(selected)):raise ValueError('unknown backtest security')
    cutoff=body.get('cutoff') or now_iso();point=datetime.fromisoformat(cutoff)
    if point.tzinfo is None:raise ValueError('backtest cutoff requires timezone')
    ident='backtest-'+uuid.uuid4().hex[:12];folder=STATE/'backtests'/ident;folder.mkdir(parents=True)
    cache=Path(body.get('cache') or ROOT/'market_data').resolve()
    if not (cache/'tdx/day/ohlcv').is_dir():raise ValueError('history cache must contain tdx/day/ohlcv')
    spec={'id':ident,'created':now_iso(),'cutoff':cutoff,'workers':workers,'cache':str(cache),
          'records':rows,'output':str(folder)}
    atomic_json(folder/'request.json',spec)
    command=[sys.executable,'--backtest-worker',str(folder/'request.json')] if getattr(sys,'frozen',False) else [sys.executable,'-m','workbench.backtests',str(folder/'request.json')]
    with (folder/'worker.stdout.txt').open('ab') as stdout,(folder/'worker.stderr.txt').open('ab') as stderr:
        p=subprocess.Popen(command,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,
                           env={**os.environ,'PYTHONIOENCODING':'utf-8'},creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    value={**{k:spec[k] for k in ['id','created','cutoff','workers','output']},'state':'running','pid':p.pid,'total':len(rows)}
    with connect() as db:db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('backtest:'+ident,json.dumps(value)))
    return value


def worker(path):
    spec=json.loads(Path(path).read_text(encoding='utf-8'))
    def progress(summary):print(json.dumps(summary,ensure_ascii=False),flush=True)
    return run_market(spec['records'],spec['cache'],spec['cutoff'],spec['output'],spec['workers'],progress,Path(spec['output'])/'STOP')


def results():
    with connect() as db:rows=[json.loads(row[0]) for row in db.execute("SELECT value FROM metadata WHERE key LIKE 'backtest:%'")]
    from .server import alive
    for item in rows:
        folder=Path(item['output']);folder=folder if folder.is_absolute() else ROOT/folder
        summary=folder/'summary.json'
        if summary.exists():item['summary']=json.loads(summary.read_text(encoding='utf-8'))
        if item.get('state')=='running' and not alive(item.get('pid')):
            item['state']='complete' if item.get('summary',{}).get('coverage_complete') else 'interrupted'
        error=folder/'worker.stderr.txt'
        if item['state']=='interrupted' and error.exists():item['error']=error.read_text(encoding='utf-8',errors='replace')[-1500:]
    return {'runs':sorted(rows,key=lambda x:x['created'],reverse=True),'execution_allowed':False}


def stop(ident):
    with connect() as db:row=db.execute('SELECT value FROM metadata WHERE key=?',('backtest:'+ident,)).fetchone()
    if row is None:raise ValueError('backtest not found')
    value=json.loads(row[0]);(Path(value['output'])/'STOP').write_text('stop requested',encoding='utf-8')
    return {'state':'stopping','id':ident}


def resume(ident):
    with connect() as db:row=db.execute('SELECT value FROM metadata WHERE key=?',('backtest:'+ident,)).fetchone()
    if row is None:raise ValueError('backtest not found')
    value=json.loads(row[0]);folder=Path(value['output']);request=folder/'request.json'
    from .server import alive
    if alive(value.get('pid')):raise ValueError('backtest process is still running')
    if not request.exists():raise ValueError('this imported report has no resumable source request')
    stop_file=folder/'STOP'
    if stop_file.exists():stop_file.unlink()
    command=[sys.executable,'--backtest-worker',str(request)] if getattr(sys,'frozen',False) else [sys.executable,'-m','workbench.backtests',str(request)]
    with (folder/'worker.stdout.txt').open('ab') as stdout,(folder/'worker.stderr.txt').open('ab') as stderr:
        p=subprocess.Popen(command,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,
                           env={**os.environ,'PYTHONIOENCODING':'utf-8'},creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    value.update(state='running',pid=p.pid)
    with connect() as db:db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('backtest:'+ident,json.dumps(value)))
    return value


if __name__=='__main__':
    import multiprocessing
    multiprocessing.freeze_support();worker(sys.argv[1])
