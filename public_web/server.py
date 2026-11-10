"""Read-only public presentation gateway. Desktop controls remain on loopback."""
from collections import OrderedDict, deque
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import argparse
import gzip
import json
import mimetypes
import os
import re
import threading
import time
import urllib.parse
import urllib.request

WEB=Path(__file__).resolve().parent/'web'
SOURCE=Path(__file__).resolve().parents[1]
CN=timezone(timedelta(hours=8))
OPENER=urllib.request.build_opener(urllib.request.ProxyHandler({}))
CACHE=OrderedDict();LOCK=threading.Lock();INFLIGHT=threading.BoundedSemaphore(2)
REQUESTS={};RATE_LOCK=threading.Lock()


def now():return datetime.now(CN).isoformat(timespec='seconds')


def desktop_origin(status=None):
    status=Path(status or os.environ.get('CHANLUN_PUBLIC_DESKTOP_STATUS',str(Path.home()/'Documents/ChanlunV4PublicData/desktop-status.json')))
    data=json.loads(status.read_text(encoding='utf-8'));origin=data['url']
    parsed=urllib.parse.urlsplit(origin)
    if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or not parsed.port or parsed.path not in ('','/'):
        raise ValueError('invalid desktop service origin')
    return origin.rstrip('/')


def local_json(path,timeout=150,status=None):
    with OPENER.open(desktop_origin(status)+path,timeout=timeout) as r:return json.load(r)


def public_bundle(data,period):
    summary=data['summary'];chart=data['chart']
    panels=[]
    for p in chart['panels']:
        if p['period']!=period:continue
        panels.append({k:p.get(k) for k in ('period','bars','input_bars','strokes','segments','higher_segments',
            'centers','all_signals','engine','engine_provenance','geometry_review','kline_audit','confirmed_through',
            'received_at','observed_at','latest_bar_at','forming_count','note','structure_layers')})
        provenance=p.get('engine_provenance') or {}
        panels[-1]['engine_provenance']={k:provenance[k] for k in ('commit','license','repository','profile','settings') if k in provenance}
    if len(panels)!=1:raise ValueError('requested panel is unavailable')
    return {'summary':{k:summary.get(k) for k in ('symbol','code','name','exchange','price','previous_close',
            'change_pct','last_date','as_of','live','definition_mode','execution_allowed')},
            'chart':{'panels':panels,'input_sha256':chart.get('input_sha256'),
                     'engine_commit':chart.get('engine_commit'),'adjustment':'unadjusted'},
            'public_access':'read_only','source':'TDX原始OHLCV · 不复权'}


def stock(code,period):
    if not re.fullmatch(r'(sh6\d{5}|sz(?:0\d{5}|30\d{4})|bj(?:92\d{4}|43\d{4}|8\d{5}))',code):
        raise ValueError('股票代码格式错误')
    if period not in ('day','30m','5m'):raise ValueError('支持日线、30分钟、5分钟')
    key=(code,period)
    with LOCK:
        cached=CACHE.get(key)
        if cached and time.monotonic()-cached[0]<12:return cached[1]
    if not INFLIGHT.acquire(blocking=False):raise BlockingIOError('行情计算繁忙，请稍后重试')
    try:
        data=public_bundle(local_json('/api/live-stock/'+code+'?period='+period),period)
        with LOCK:
            CACHE[key]=(time.monotonic(),data);CACHE.move_to_end(key)
            while len(CACHE)>24:CACHE.popitem(last=False)
        return data
    finally:INFLIGHT.release()


def universe():
    path=SOURCE/'public_web/data/universe.json'
    data=json.loads(path.read_text(encoding='utf-8'))
    return {'records':[{k:r[k] for k in ('symbol','code','name','exchange')} for r in data['records']],
            'scope':'沪深京当前A股股票池；历史退市成分不在此范围'}


def scan():
    saved=Path.home()/'Documents/ChanlunV4ReviewData/desktop-status.json'
    source=os.environ.get('CHANLUN_PUBLIC_SCAN_STATUS',str(saved) if saved.is_file() else None)
    data=local_json('/api/monitor',timeout=15,status=source)
    runs=[{k:r.get(k) for k in ('state','started','updated')}|{
        'kind':r['payload'].get('kind'),'done':r['payload'].get('done',0),
        'total':r['payload'].get('total',0),'failed':r['payload'].get('failed',0)} for r in data['runs']]
    signals=[]
    for r in data['signals'][:60]:
        p=r['payload'];signals.append({k:r.get(k) for k in ('symbol','state','last_seen')}|{
            k:p.get(k) for k in ('class','label','price','invalidation','available_time','period','rank')})
    return {'counts':data['counts'],'session':data['session'],'runs':runs,'signals':signals,
            'received_at':now(),'scope':'公开查看本机扫描进度和研究候选；此页不启动或修改计算任务'}


def backtest():
    path=SOURCE/'public_web/data/backtest.json'
    return {'summary':json.loads(path.read_text(encoding='utf-8')),'model_version':'4.0.0 archive',
            'label':'旧模型全市场历史回测归档','current_engine_performance':False}


def allowed_rate(address):
    moment=time.monotonic()
    with RATE_LOCK:
        queue=REQUESTS.setdefault(address,deque())
        while queue and moment-queue[0]>60:queue.popleft()
        if len(queue)>=60:return False
        queue.append(moment)
        if len(REQUESTS)>2000:
            for key in list(REQUESTS):
                if not REQUESTS[key] or moment-REQUESTS[key][-1]>60:REQUESTS.pop(key,None)
        return True


class Handler(BaseHTTPRequestHandler):
    server_version='ChanlunPublic/1.0'
    def log_message(self,fmt,*args):
        if len(args)>1 and str(args[1]) not in ('200','304'):super().log_message(fmt,*args)
    def send(self,data,status=200,content_type='application/json; charset=utf-8'):
        raw=data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
        compressed='gzip' in self.headers.get('Accept-Encoding','') and len(raw)>3000
        if compressed:raw=gzip.compress(raw,compresslevel=3,mtime=0)
        self.send_response(status);self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(raw)));self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Cache-Control','no-store' if content_type.startswith('application/json') else 'public,max-age=300')
        self.send_header('Vary','Accept-Encoding, Origin')
        if compressed:self.send_header('Content-Encoding','gzip')
        origin=self.headers.get('Origin')
        if origin=='https://nnn256.github.io':self.send_header('Access-Control-Allow-Origin',origin)
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self' https://*.trycloudflare.com; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
        self.end_headers()
        try:self.wfile.write(raw)
        except (BrokenPipeError,ConnectionResetError):pass
    def do_OPTIONS(self):self.send({'error':'method not allowed'},405)
    def do_POST(self):self.send({'error':'公网仅提供查询，任务管理请使用本机程序'},405)
    do_PUT=do_POST;do_DELETE=do_POST;do_PATCH=do_POST
    def do_GET(self):
        try:
            parsed=urllib.parse.urlsplit(self.path);path=parsed.path;query=urllib.parse.parse_qs(parsed.query)
            if path.startswith('/api/'):
                address=self.headers.get('CF-Connecting-IP',self.client_address[0])
                if not allowed_rate(address):return self.send({'error':'请求过于频繁，请稍后重试'},429)
                if path=='/api/health':return self.send({'service':'chanlun-public-read-only','version':'1.0.0','now':now(),'execution_allowed':False})
                if path=='/api/universe':return self.send(universe())
                if path=='/api/scan':return self.send(scan())
                if path=='/api/backtest':return self.send(backtest())
                if path.startswith('/api/stock/'):
                    return self.send(stock(path.rsplit('/',1)[1],query.get('period',['day'])[0]))
                return self.send({'error':'接口不存在'},404)
            static={'/':'index.html','/index.html':'index.html','/app.js':'app.js','/style.css':'style.css','/config.js':'config.js',
                    '/assets/lightweight-charts.js':'assets/lightweight-charts.js','/assets/LICENSE':'assets/LICENSE',
                    '/assets/NOTICE':'assets/NOTICE','/assets/library-source.json':'assets/library-source.json'}
            if path not in static:return self.send({'error':'页面不存在'},404)
            file=WEB/static[path]
            kind={'.html':'text/html; charset=utf-8','.js':'application/javascript; charset=utf-8','.css':'text/css; charset=utf-8'}.get(file.suffix,'text/plain; charset=utf-8')
            return self.send(file.read_bytes(),content_type=kind)
        except ValueError as e:self.send({'error':str(e)},400)
        except BlockingIOError as e:self.send({'error':str(e)},429)
        except Exception as e:
            print(f'public request failed: {type(e).__name__}',flush=True)
            self.send({'error':'行情服务暂时不可用，请稍后刷新；未生成替代K线'},503)


def main():
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=8810);args=p.parse_args()
    print(f'public presentation http://127.0.0.1:{args.port}',flush=True)
    ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()


if __name__=='__main__':main()
