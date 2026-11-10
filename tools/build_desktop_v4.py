"""Build V4 from current source, with hashed resources and isolated sample state."""
from __future__ import annotations

from datetime import datetime,timezone,timedelta
import hashlib
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from zipfile import ZipFile,ZIP_DEFLATED

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from chanmodel.release import APP_VERSION,APP_LABEL
from chanmodel.reference_engine import PROFILE

OUT=Path(os.environ.get('CHANLUN_V4_BUILD_DIR',ROOT.parent/'artifacts/chanlun-v4-windows-20261110'))
PYTHON=ROOT/'artifacts/exe-build-20261108/build-env/Scripts/python.exe'


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def portable_value(value):
    if isinstance(value,dict):return {k:portable_value(v) for k,v in value.items()}
    if isinstance(value,list):return [portable_value(v) for v in value]
    if isinstance(value,str):
        normalized=value.replace('\\','/')
        if (len(normalized)>3 and normalized[1:3]==':/') or normalized.startswith('/Users/') or normalized.startswith('/home/'):
            for folder in ['market_data/','runtime/','runs/']:
                if '/'+folder in normalized:return folder+normalized.split('/'+folder,1)[1]
            return '<local_path_omitted>'
    return value


def source_entries(root=ROOT):
    entries={}
    for folder in ['chanmodel','workbench','tools','third_party/chanpy']:
        for p in (root/folder).rglob('*'):
            if p.is_file() and '__pycache__' not in p.parts and (p.suffix in {'.py','.html','.js','.css','.json','.md'} or p.name=='LICENSE'):
                entries[p.relative_to(root).as_posix()]=p
    for name in ['run_complete.py','run_market_backtest.py','run_v2.py','run.py','history_replay.py','market_scan.py','intraday_scan.py',
                 '完整算法V3说明.md','V4扫描提醒与全市场回测.md','V4买卖画图说明.md','V4原始K线与买卖点核验说明.md','V4固定引擎与显示重构说明.md','V4实时K线说明.md']:
        entries[name]=root/name
    assets=root/'runs/aoya-v2-chart-20261107/assets'
    for p in assets.iterdir():
        if p.is_file():entries[p.relative_to(root).as_posix()]=p
    entries['runs/tdx-all-a-20261106/universe.json']=Path(os.environ.get('CHANLUN_V4_UNIVERSE',str(root/'runs/tdx-all-a-20261106/universe.json')))
    entries['runtime/tdx-api-revised.exe']=root/'runtime/tdx-api-revised.exe'
    for symbol in ['sz300949','sz000001','sh600519','bj920002','sh600531','sh600000','sz000002','sh600419','sz000529','bj920010']:
        for period in ['day','minute30','minute5']:
            suffix='.csv' if period=='day' else '.csv.gz'
            paths=[f'market_data/tdx/{period}/ohlcv/{symbol}{suffix}',f'market_data/tdx/{period}/metadata/{symbol}.json']
            cache_root=Path(os.environ.get('CHANLUN_V4_CACHE_ROOT',str(root)))
            if all((cache_root/name).is_file() for name in paths):
                for name in paths:entries[name]=cache_root/name
    return entries


SEED_SCRIPT=r'''
import json
from pathlib import Path
from workbench.server import seed
from workbench.store import connect,publish_result,ROOT
from workbench.compute import process_stock
from chanmodel.tdx_data import now_iso
from chanmodel.release import APP_LABEL
seed();results=[]
for symbol in ['sz300949','sz000001','sh600000','bj920002','sh600419','sz000529','bj920010']:
    with connect() as db:record=dict(db.execute('SELECT * FROM universe WHERE symbol=?',(symbol,)).fetchone())
    result=process_stock(record,{'mode':'day','refresh':False,'cutoff':'2026-11-09T15:00:00+08:00','volume_tolerance':1},'bundled-v4')
    assert result['state']=='success',result
    assert result['summary']['algorithm']==APP_LABEL
    with connect() as db:publish_result(db,symbol,'day','bundled-v4',now_iso(),json.dumps(result['summary'],ensure_ascii=False),result['artifact'])
    results.append({'symbol':symbol,'algorithm':result['summary']['algorithm'],'bars':result['summary']['periods']['day']['bars'],'artifact':result['artifact']})
(ROOT/'seed-validation.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
import os,shutil
source=os.environ.get('CHANLUN_V4_BACKTEST')
if source:
    source=Path(source);target=ROOT/'runtime/workbench/backtests/bundled-all-market';target.mkdir(parents=True,exist_ok=True)
    for name in ['summary.json','stocks.csv']:
        shutil.copy2(source/name,target/name)
    summary=json.loads((target/'summary.json').read_text(encoding='utf-8'))
    value={'id':'bundled-all-market','created':'2026-11-10T03:00:00+08:00','cutoff':'2026-11-10T02:10:00+08:00',
           'state':'complete','pid':None,'total':summary['total'],'output':'runtime/workbench/backtests/bundled-all-market'}
    with connect() as db:db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('backtest:bundled-all-market',json.dumps(value)))
print(json.dumps(results,ensure_ascii=False))
'''


def make_seed():
    project=OUT/'seed-project'
    if project.exists():raise ValueError('seed staging already exists; use a fresh build directory')
    project.mkdir(parents=True)
    entries=source_entries()
    for name,source in entries.items():
        if not source.is_file():raise FileNotFoundError(source)
        dest=project/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,dest)
    env={**os.environ,'CHANLUN_WORKBENCH_ROOT':str(project),'PYTHONIOENCODING':'utf-8'}
    p=subprocess.run([str(PYTHON),'-c',SEED_SCRIPT],cwd=project,env=env,capture_output=True,encoding='utf-8',errors='replace')
    (OUT/'seed.stdout.txt').write_text(p.stdout,encoding='utf-8');(OUT/'seed.stderr.txt').write_text(p.stderr,encoding='utf-8')
    if p.returncode:raise RuntimeError('V4 sample computation failed; see seed.stderr.txt')
    for path in project.rglob('*'):
        if path.suffix=='.json':
            value=json.loads(path.read_text(encoding='utf-8'))
            path.write_text(json.dumps(portable_value(value),ensure_ascii=False,indent=2),encoding='utf-8')
        elif path.name.endswith('.json.gz'):
            value=json.loads(gzip.decompress(path.read_bytes()))
            path.write_bytes(gzip.compress(json.dumps(portable_value(value),ensure_ascii=False,allow_nan=False,separators=(',',':')).encode(),mtime=0))
    resources=OUT/'resources';resources.mkdir(exist_ok=True)
    files={p.relative_to(project).as_posix():p for p in project.rglob('*') if p.is_file() and '__pycache__' not in p.parts
           and not p.name.endswith(('-wal','-shm'))}
    archive=resources/'project-seed.zip'
    with ZipFile(archive,'w',ZIP_DEFLATED,compresslevel=6) as z:
        for name,path in sorted(files.items()):z.write(path,name)
    with ZipFile(archive) as z:assert z.testzip() is None
    code={name:digest(p) for name,p in files.items() if name.startswith(('chanmodel/','workbench/','tools/')) or p.suffix=='.py'}
    build={'version':APP_VERSION,'release':APP_LABEL,'algorithm_profile':PROFILE,
           'created_at':datetime.now(timezone(timedelta(hours=8))).isoformat(),
           'payload_sha256':digest(archive),'seed_files':len(files),'code_sha256':code,
           'architecture':'Windows x64','requires_system_python':False,'execution_allowed':False,
           'sample_as_of':'2026-11-09T15:00:00+08:00'}
    (resources/'build-info.json').write_text(json.dumps(build,ensure_ascii=False,indent=2),encoding='utf-8')
    shutil.copy2(ROOT/'artifacts/exe-build-20261108/resources/app.ico',resources/'app.ico')
    return build


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    if not PYTHON.is_file():raise FileNotFoundError('the existing pinned build environment is required')
    build=make_seed();resources=OUT/'resources'
    version_file=resources/'windows-version.txt'
    version_template="""VSVersionInfo(
 ffi=FixedFileInfo(filevers=(4,0,0,0),prodvers=(4,0,0,0),mask=0x3f,flags=0x0,OS=0x40004,fileType=0x1,subtype=0x0,date=(0,0)),
 kids=[StringFileInfo([StringTable('080404b0',[
  StringStruct('FileDescription','缠论完整算法工作台 V4'),
  StringStruct('FileVersion','4.0.0'),StringStruct('ProductName','Chanlun Workbench V4'),
  StringStruct('ProductVersion','4.0.0'),StringStruct('OriginalFilename','ChanlunWorkbenchV4.exe')
 ])]),VarFileInfo([VarStruct('Translation',[2052,1200])])])"""
    version_tuple=','.join(APP_VERSION.split('.')+['0'])
    version_template=version_template.replace('(4,0,0,0)','('+version_tuple+')').replace('4.0.0',APP_VERSION)
    version_file.write_text(version_template,encoding='utf-8')
    print('V4 seed verified; compiling executable',flush=True)
    arguments=[str(PYTHON),'-m','PyInstaller','--noconfirm','--clean','--onefile','--windowed',
               '--name','ChanlunWorkbenchV4','--distpath',str(OUT/'dist'),'--workpath',str(OUT/'pyinstaller-work'),
               '--specpath',str(OUT),'--paths',str(ROOT),'--paths',str(ROOT/'third_party/chanpy'),'--icon',str(resources/'app.ico'),'--version-file',str(version_file),
               '--add-data',str(resources/'project-seed.zip')+';.',
               '--add-data',str(resources/'build-info.json')+';.',
               '--collect-submodules','chanmodel','--collect-submodules','workbench',
               '--collect-submodules','Bi','--collect-submodules','Seg','--collect-submodules','ZS',
               '--collect-submodules','KLine','--collect-submodules','Common','--collect-submodules','Combiner',
               '--collect-submodules','BuySellPoint','--collect-submodules','Math','--collect-submodules','ChanModel',
               '--hidden-import','ChanConfig',
               '--hidden-import','tools.capture_full_stock','--hidden-import','tkinter',
               '--hidden-import','tkinter.ttk','--hidden-import','tkinter.messagebox',str(ROOT/'desktop_app.py')]
    vendor=ROOT/'third_party/chanpy'
    vendor_dir=OUT/'resources/vendor-runtime';vendor_dir.mkdir(exist_ok=True)
    shutil.copytree(vendor,vendor_dir/'chanpy',dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
    arguments[arguments.index(str(ROOT/'desktop_app.py')):arguments.index(str(ROOT/'desktop_app.py'))]=['--add-data',str(vendor)+';third_party/chanpy']
    (OUT/'build-command.json').write_text(json.dumps(arguments,ensure_ascii=False,indent=2),encoding='utf-8')
    with (OUT/'build.stdout.txt').open('w',encoding='utf-8') as stdout,(OUT/'build.stderr.txt').open('w',encoding='utf-8') as stderr:
        p=subprocess.run(arguments,cwd=ROOT,stdout=stdout,stderr=stderr)
    if p.returncode:raise RuntimeError('PyInstaller failed; inspect build.stderr.txt')
    exe=OUT/'dist/ChanlunWorkbenchV4.exe'
    receipt={**build,'exe':str(exe),'bytes':exe.stat().st_size,'sha256':digest(exe)}
    (OUT/'build-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:receipt[k] for k in ['version','release','exe','bytes','sha256','seed_files']},ensure_ascii=False))


if __name__=='__main__':main()
