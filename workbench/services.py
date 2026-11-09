"""Start and verify the bundled TDX service without a shell/Python dependency."""
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import urllib.error
import urllib.parse

from chanmodel.tdx_data import TdxClient,now_iso,atomic_json
from .store import ROOT,STATE

LOCK=threading.Lock()
LAST_READY=0.0
LAST_RESULT=None
OWNED_PROCESS=None


def endpoint():
    return os.environ.get('CHANLUN_TDX_BASE_URL','http://127.0.0.1:18080')


def probe(timeout=3):
    data,receipt,_=TdxClient(timeout=timeout).quotes(['sz300949'])
    if not data or data[0].get('Code')!='300949':
        raise ValueError('行情接口未返回指定证券')
    return {'ready':True,'url':endpoint(),'received_at':receipt['received_at'],'quote_code':'300949'}


def ensure_tdx(timeout=90,force=False):
    global LAST_READY,LAST_RESULT,OWNED_PROCESS
    process_live=OWNED_PROCESS is None or OWNED_PROCESS.poll() is None
    if not force and process_live and LAST_RESULT and time.monotonic()-LAST_READY<5:
        return LAST_RESULT
    with LOCK:
        if not force and (OWNED_PROCESS is None or OWNED_PROCESS.poll() is None) and LAST_RESULT and time.monotonic()-LAST_READY<5:return LAST_RESULT
        try:
            result=probe()
        except (OSError,ValueError):
            binary=ROOT/'runtime/tdx-api-revised.exe'
            if not binary.exists():
                raise ValueError('未找到随程序提供的TDX行情服务，请使用完整EXE或完整解压包。')
            parsed=urllib.parse.urlsplit(endpoint())
            if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or not parsed.port:
                raise ValueError('自动启动只支持本机127.0.0.1行情服务地址')
            directory=ROOT/'runtime/state';directory.mkdir(parents=True,exist_ok=True)
            logs=STATE/'service-logs';logs.mkdir(parents=True,exist_ok=True)
            stamp=time.time_ns();stdout=logs/f'tdx-{stamp}.stdout.log';stderr=logs/f'tdx-{stamp}.stderr.log'
            if OWNED_PROCESS is None or OWNED_PROCESS.poll() is not None:
                with stdout.open('ab') as out,stderr.open('ab') as err:
                    OWNED_PROCESS=subprocess.Popen([str(binary)],cwd=directory,stdin=subprocess.DEVNULL,stdout=out,stderr=err,
                        env={**os.environ,'TDX_API_ADDR':f'127.0.0.1:{parsed.port}','TDX_API_LITE':'1','TZ':'Asia/Shanghai'},
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                atomic_json(STATE/'tdx-service.json',{'pid':OWNED_PROCESS.pid,'url':endpoint(),'stdout':str(stdout),'stderr':str(stderr),'started_at':now_iso()})
            deadline=time.monotonic()+timeout;last_error=''
            while time.monotonic()<deadline:
                try:result=probe(timeout=2);break
                except (OSError,ValueError) as exc:last_error=str(exc)
                if OWNED_PROCESS.poll() is not None:
                    tail=stderr.read_text(encoding='utf-8',errors='replace')[-900:] if stderr.exists() else ''
                    raise ValueError('行情服务启动失败。'+tail+f' 日志目录：{logs}')
                time.sleep(.4)
            else:
                raise ValueError('行情服务尚未连接到上游，请检查网络并点击重试。'+last_error+f' 日志目录：{logs}')
        LAST_READY=time.monotonic();LAST_RESULT=result
        return result


def worker_command(job_id):
    import sys
    if getattr(sys,'frozen',False):return [sys.executable,'--worker',job_id]
    return [sys.executable,'-m','workbench.jobs','--job',job_id]
