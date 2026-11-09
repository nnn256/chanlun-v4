"""Frozen Windows application: one EXE, persistent data, managed local services."""
import argparse
from contextlib import closing
import hashlib
import json
import logging
import multiprocessing
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
import zipfile
from chanmodel.release import APP_VERSION, APP_LABEL, APP_TITLE


def atomic_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(path)


def local_json(url,body=None,timeout=10):
    request=urllib.request.Request(url,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'})
    # Local services must not be routed through a user's remote proxy.
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request,timeout=timeout) as response:return json.load(response)


def available_port(preferred):
    for port in (preferred,0):
        with socket.socket() as sock:
            try:sock.bind(('127.0.0.1',port));return sock.getsockname()[1]
            except OSError:continue
    raise RuntimeError('无法分配本地服务端口')


def app_directory():
    # AppData is virtualized for some packaged Windows launchers. A directory
    # under Documents resolves consistently when started from Explorer or IDEs.
    return Path(os.environ.get('CHANLUN_APP_DATA_DIR') or (Path.home()/'Documents/ChanlunWorkbenchData')).resolve()


def migrate_previous_data(directory,notify):
    if os.environ.get('CHANLUN_APP_DATA_DIR') or (directory/'project/runtime/workbench/workbench.sqlite3').exists():return
    locations=[Path(os.environ.get('LOCALAPPDATA',Path.home()/'AppData/Local'))/'ChanlunWorkbench']
    package_cache=Path.home()/'AppData/Local/Packages'
    if package_cache.exists():locations.extend(package_cache.glob('OpenAI.Codex_*/LocalCache/Local/ChanlunWorkbench'))
    candidates=[]
    import sqlite3
    for root in locations:
        db=root/'project/runtime/workbench/workbench.sqlite3'
        if not db.exists():continue
        try:
            with closing(sqlite3.connect(db)) as connection:count=connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
            candidates.append((count,db.stat().st_mtime,root))
        except sqlite3.Error:continue
    if not candidates:return
    source=max(candidates,key=lambda row:(row[0],row[1]))[2]
    notify('正在迁移已有行情和任务记录…')
    import shutil
    shutil.copytree(source/'project',directory/'project',dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.sqlite3','*.sqlite3-wal','*.sqlite3-shm'))
    db=source/'project/runtime/workbench/workbench.sqlite3';target=directory/'project/runtime/workbench/workbench.sqlite3';target.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(db)) as old:
        with closing(sqlite3.connect(target)) as new:old.backup(new)
    atomic_json(directory/'migration.json',{'source':str(source),'destination':str(directory),'at':time.time()})


def resource_directory():
    return Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent/'artifacts/exe-build-20261108/resources'))


def prepare_project(directory,notify):
    resources=resource_directory();archive=resources/'project-seed.zip';build=json.loads((resources/'build-info.json').read_text(encoding='utf-8'))
    project=directory/'project';marker=directory/'installed-build.json'
    old=json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else {}
    expected=build.get('code_sha256',{})
    damaged=any(not(project/name).is_file() or hashlib.sha256((project/name).read_bytes()).hexdigest()!=digest
                for name,digest in expected.items())
    if damaged or old.get('payload_sha256')!=build['payload_sha256'] or not (project/'workbench/web/index.html').exists():
        notify('正在准备程序与历史示例，首次启动需要片刻…')
        if hashlib.sha256(archive.read_bytes()).hexdigest()!=build['payload_sha256']:raise ValueError('内置程序资源校验失败，请重新下载EXE。')
        with zipfile.ZipFile(archive) as z:
            for item in z.infolist():
                if item.is_dir():continue
                target=(project/item.filename).resolve()
                if not target.is_relative_to(project.resolve()):raise ValueError('无效内置资源路径')
                # Update program code while retaining user jobs, histories and DB.
                immutable=item.filename.startswith(('chanmodel/','workbench/','tools/')) or item.filename.endswith(('.exe','.js','.html','.css','.py','.ps1','.cmd'))
                if target.exists() and not immutable:continue
                target.parent.mkdir(parents=True,exist_ok=True)
                temp=target.with_name(target.name+'.installing');temp.write_bytes(z.read(item));temp.replace(target)
        atomic_json(marker,build)
    if any(not(project/name).is_file() or hashlib.sha256((project/name).read_bytes()).hexdigest()!=digest for name,digest in expected.items()):
        raise ValueError('安装后的程序文件与内置V4构建不一致')
    return project,build


def claim_instance(directory):
    import msvcrt
    handle=(directory/'desktop.lock').open('a+b')
    handle.seek(0);handle.write(b'1');handle.flush();handle.seek(0)
    try:msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:handle.close();return None
    return handle


def request_window_close(pid):
    """Ask only the verified app process to run its own normal close handler."""
    if os.name!='nt' or not isinstance(pid,int) or pid<=0:return False
    import ctypes
    from ctypes import wintypes
    api=ctypes.WinDLL('user32',use_last_error=True)
    api.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
    api.PostMessageW.argtypes=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
    callback_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    sent=[]
    def visit(hwnd,unused):
        owner=wintypes.DWORD();api.GetWindowThreadProcessId(hwnd,ctypes.byref(owner))
        if owner.value==pid and api.IsWindowVisible(hwnd):
            if api.PostMessageW(hwnd,0x0010,0,0):sent.append(int(hwnd))
        return True
    api.EnumWindows(callback_type(visit),0)
    return bool(sent)


def existing_instance_action(controller):
    """Never silently send a new build back into an older frozen server."""
    previous=json.loads(controller.status_path.read_text(encoding='utf-8'))
    url=previous.get('url')
    if not url:return 'starting'
    health=local_json(url+'/api/health',timeout=3)
    expected=(controller.directory/'project/runtime/workbench/workbench.sqlite3').resolve()
    if health.get('service')!='chanlun-workbench-v1' or Path(health.get('database','')).resolve()!=expected:
        raise ValueError('已有本机服务不是当前程序的数据目录，未关闭该服务。')
    if health.get('version')==APP_VERSION and 'marker_visibility_v2' in health.get('features',[]):
        if not controller.args.no_browser:webbrowser.open(url+'/?symbol=sz300949')
        return 'reused'
    overview=local_json(url+'/api/overview',timeout=3)
    if any(j.get('state') in {'queued','starting','running','pausing','cancelling'} for j in overview.get('jobs',[])):
        return 'busy_old_version'
    # Backtest subprocesses are separate jobs. Preserve them too when handing
    # off a completed-workbench process to a new build.
    backtests=local_json(url+'/api/backtests',timeout=3)
    if any(j.get('state')=='running' for j in backtests.get('runs',[])):
        return 'busy_old_version'
    monitor=local_json(url+'/api/monitor',timeout=3)
    if monitor.get('busy'):return 'busy_old_version'
    if health.get('pid')!=previous.get('pid') or not request_window_close(health['pid']):
        return 'old_version_close_required'
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        controller.lock=claim_instance(controller.directory)
        if controller.lock:
            controller.notify('旧版窗口已正常退出，正在升级买卖画图显示；历史任务保留。')
            return 'upgraded'
        time.sleep(.2)
    return 'old_version_close_required'


class DesktopController:
    def __init__(self,args):
        self.args=args;self.directory=app_directory();self.directory.mkdir(parents=True,exist_ok=True)
        self.events=queue.Queue();self.server=None;self.url=None;self.project=None;self.build=None;self.data_ready=False;self.data_error=None
        self.status_path=self.directory/'desktop-status.json';self.http_thread=None
        self.stopping=threading.Event()
        logdir=self.directory/'logs';logdir.mkdir(exist_ok=True)
        logging.basicConfig(filename=logdir/'desktop.log',level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s',encoding='utf-8')
        if sys.stdout is None:sys.stdout=(logdir/'runtime.stdout.log').open('a',encoding='utf-8',buffering=1)
        if sys.stderr is None:sys.stderr=(logdir/'runtime.stderr.log').open('a',encoding='utf-8',buffering=1)
        self.lock=claim_instance(self.directory)

    def notify(self,message):
        logging.info(message);self.events.put(('message',message))

    def status(self,state,message):
        atomic_json(self.status_path,{'state':state,'message':message,'url':self.url,'data_ready':self.data_ready,'data_error':self.data_error,
            'pid':os.getpid(),'project':str(self.project) if self.project else None,'version':APP_VERSION,'release':APP_LABEL,'tdx_url':os.environ.get('CHANLUN_TDX_BASE_URL'),
            'frozen':bool(getattr(sys,'frozen',False))})

    def start(self):
        try:
            migrate_previous_data(self.directory,self.notify)
            self.project,self.build=prepare_project(self.directory,self.notify)
            os.environ['CHANLUN_WORKBENCH_ROOT']=str(self.project)
            os.environ['CHANLUN_DESKTOP_MODE']='1'
            # Never let frozen imports depend on the caller's Desktop or a
            # PyInstaller extraction directory when resolving mutable state.
            os.chdir(self.project)
            self.status('starting','正在启动工作台')
            self.notify('正在启动工作台…')
            from workbench import server
            server.seed();port=available_port(self.args.web_port or 8791)
            self.url=f'http://127.0.0.1:{port}'
            from http.server import ThreadingHTTPServer
            self.server=ThreadingHTTPServer(('127.0.0.1',port),server.Handler)
            self.http_thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.http_thread.start()
            threading.Thread(target=server.scheduler,daemon=True).start()
            from workbench.monitor import launch as launch_monitor
            launch_monitor()
            if getattr(self.args,'offline',False):
                self.events.put(('workbench',self.url));self.events.put(('offline','离线模式：已保存行情与历史结果可直接计算。'))
                self.status('offline_ready','离线工作台已就绪，可计算已保存行情');return
            self.events.put(('workbench',self.url));self.notify('工作台已就绪，正在连接TDX行情服务…')
            self.status('starting_data','工作台已就绪，正在连接TDX行情服务')
            # Dedicated TDX endpoint avoids accidentally using an unrelated service.
            tdx_port=available_port(self.args.tdx_port or 18080)
            os.environ['CHANLUN_TDX_BASE_URL']=f'http://127.0.0.1:{tdx_port}'
            self.retry_data()
            threading.Thread(target=self.monitor_data,daemon=True).start()
        except Exception as exc:
            logging.exception('Desktop startup failed');self.status('failed',str(exc));self.events.put(('failed',str(exc)))

    def retry_data(self):
        try:
            from workbench.services import ensure_tdx
            self.events.put(('message','正在启动并验证TDX行情服务…'))
            result=ensure_tdx(timeout=90,force=True);self.data_ready=True;self.data_error=None
            self.status('ready','工作台及TDX行情服务正常');self.events.put(('ready',result));self.notify('工作台及行情服务均已就绪。')
        except Exception as exc:
            self.data_ready=False;self.data_error=str(exc);logging.exception('TDX startup failed')
            self.status('offline','历史示例可查看，行情连接暂不可用');self.events.put(('offline',str(exc)))

    def monitor_data(self):
        while not self.stopping.wait(15):
            try:
                from workbench.services import probe
                probe()
            except Exception:
                if self.stopping.is_set():break
                self.events.put(('message','行情连接中断，正在自动重连…'))
                self.retry_data()
            else:
                if self.data_ready:self.status('ready','工作台及TDX行情服务正常')

    def close(self):
        self.stopping.set()
        from workbench.monitor import STOP as MONITOR_STOP
        MONITOR_STOP.set()
        running_workers=False
        if self.project:
            try:
                from workbench.store import connect
                with connect() as db:
                    db.execute("UPDATE jobs SET state='pausing' WHERE state IN ('running','starting')")
                    running_workers=bool(db.execute("SELECT 1 FROM jobs WHERE state='pausing' AND pid IS NOT NULL LIMIT 1").fetchone())
                from workbench.server import STOP
                STOP.set()
            except Exception:logging.exception('Unable to update shutdown state')
        if self.server:self.server.shutdown();self.server.server_close()
        if not running_workers:
            try:
                from workbench.services import OWNED_PROCESS
                if OWNED_PROCESS and OWNED_PROCESS.poll() is None:
                    OWNED_PROCESS.terminate();OWNED_PROCESS.wait(timeout=10)
            except Exception:logging.exception('TDX service shutdown failed')
        # Workers own already-dispatched stock writes. Leave their service alive
        # until a later app run can resume or review the saved job state.
        self.status('closed','程序窗口已关闭；已派发股票保留结果')
        if self.lock:self.lock.close()


def run_gui(controller):
    import tkinter as tk
    from tkinter import ttk,messagebox
    root=tk.Tk();root.title(APP_TITLE);root.geometry('660x420');root.minsize(600,390);root.configure(bg='#f3f5f8')
    root.option_add('*Font',('Microsoft YaHei UI',10))
    frame=tk.Frame(root,bg='#ffffff',padx=30,pady=25);frame.pack(fill='both',expand=True,padx=18,pady=18)
    tk.Label(frame,text=APP_TITLE,font=('Microsoft YaHei UI',21,'bold'),fg='#203e58',bg='#ffffff').pack(anchor='w')
    tk.Label(frame,text=f'Windows {APP_VERSION} · 完整结构 / 多周期确认 / 任务回放',fg='#73899d',bg='#ffffff').pack(anchor='w',pady=(5,23))
    wb=tk.StringVar(value='工作台：正在启动…');tdx=tk.StringVar(value='行情服务：等待启动')
    tk.Label(frame,textvariable=wb,fg='#294f70',bg='#ffffff').pack(anchor='w',pady=4)
    tk.Label(frame,textvariable=tdx,fg='#74889a',bg='#ffffff').pack(anchor='w',pady=4)
    msg=tk.StringVar(value='正在准备本地运行环境…');tk.Label(frame,textvariable=msg,wraplength=560,justify='left',fg='#7b8fa0',bg='#ffffff').pack(anchor='w',pady=(15,8))
    progress=ttk.Progressbar(frame,mode='indeterminate');progress.pack(fill='x',pady=(5,18));progress.start(12)
    buttons=tk.Frame(frame,bg='#ffffff');buttons.pack(fill='x')
    opened=[False]
    def open_workbench():
        if controller.url:webbrowser.open(controller.url+'/?symbol=sz300949')
    launch=tk.Button(buttons,text='打开工作台',command=open_workbench,state='disabled',bg='#294f70',fg='#ffffff',relief='flat',padx=22,pady=7);launch.pack(side='left')
    retry=tk.Button(buttons,text='重试行情连接',command=lambda:threading.Thread(target=controller.retry_data,daemon=True).start(),state='disabled',relief='flat',bg='#edf3f7',fg='#365e7f',padx=15,pady=7);retry.pack(side='left',padx=9)
    tk.Button(buttons,text='数据与日志',command=lambda:os.startfile(str(controller.directory)),relief='flat',bg='#edf3f7',fg='#365e7f',padx=10,pady=7).pack(side='left')
    tk.Label(frame,text='历史数据自动保存到用户目录；报价联网获取。关闭网页不会停止任务。',fg='#8c9ba7',bg='#ffffff',font=('Microsoft YaHei UI',9)).pack(anchor='w',pady=(18,0))
    def poll():
        try:
            while True:
                kind,value=controller.events.get_nowait()
                if kind=='message':msg.set(value)
                if kind=='workbench':
                    wb.set('工作台：已启动  '+value);launch.configure(state='normal')
                if kind in ('ready','offline','failed'):
                    progress.stop();retry.configure(state='normal' if controller.url else 'disabled')
                if kind=='ready':
                    tdx.set('行情服务：已连接，真实报价校验通过');msg.set('已准备就绪，可在工作台创建全市场或单股任务。')
                    if not opened[0] and not controller.args.no_browser:opened[0]=True;open_workbench()
                elif kind=='offline':
                    tdx.set('行情服务：离线模式，可计算本地缓存' if controller.args.offline else '行情服务：暂不可用，可点击重试');msg.set(str(value)[-420:])
                    if not opened[0] and not controller.args.no_browser:opened[0]=True;open_workbench()
                elif kind=='failed':wb.set('工作台：启动失败');msg.set(str(value))
        except queue.Empty:pass
        root.after(150,poll)
    def close_window():
        root.withdraw();controller.close();root.destroy()
    root.protocol('WM_DELETE_WINDOW',close_window);root.after(100,poll)
    if controller.args.stop_after:root.after(controller.args.stop_after*1000,close_window)
    threading.Thread(target=controller.start,daemon=True).start()
    root.mainloop()


def main():
    multiprocessing.freeze_support()
    if len(sys.argv)>2 and sys.argv[1]=='--backtest-worker':
        from workbench.backtests import worker
        worker(sys.argv[2]);return
    if len(sys.argv)>2 and sys.argv[1]=='--worker':
        from workbench.jobs import run
        run(sys.argv[2]);return
    parser=argparse.ArgumentParser();parser.add_argument('--headless',action='store_true');parser.add_argument('--no-browser',action='store_true');parser.add_argument('--offline',action='store_true');parser.add_argument('--web-port',type=int);parser.add_argument('--tdx-port',type=int);parser.add_argument('--stop-after',type=int)
    args=parser.parse_args();controller=DesktopController(args)
    if not controller.lock:
        try:
            action=existing_instance_action(controller)
        except Exception as exc:
            action='old_version_close_required';logging.exception('Unable to hand off old desktop instance')
        if action=='reused':return
        if action!='upgraded':
            text=('旧版仍有扫描或回测任务运行，请等待完成后关闭旧版窗口，再启动4.0.2；已保存任务不会丢失。'
                  if action=='busy_old_version' else '旧版窗口仍在运行，请先关闭旧版窗口，再启动4.0.2；本次没有打开旧版页面。')
            (controller.directory/'upgrade-required.txt').write_text(text,encoding='utf-8')
            logging.warning(text)
            if not args.headless:
                from tkinter import messagebox
                messagebox.showinfo('V4 4.0.2升级提示',text)
            return
    if args.headless:
        controller.start()
        if args.stop_after:
            time.sleep(args.stop_after);controller.close()
        else:
            while True:time.sleep(1)
    else:run_gui(controller)


if __name__=='__main__':
    try:main()
    except Exception:
        try:
            directory=app_directory();directory.mkdir(parents=True,exist_ok=True)
            (directory/'startup-error.txt').write_text(traceback.format_exc(),encoding='utf-8')
        except Exception:pass
        if '--headless' not in sys.argv and '--worker' not in sys.argv and '--multiprocessing-fork' not in sys.argv:
            from tkinter import messagebox
            messagebox.showerror('工作台启动失败','请查看用户数据目录中的startup-error.txt。\n'+str(sys.exc_info()[1]))
