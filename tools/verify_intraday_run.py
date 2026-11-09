#!/usr/bin/env python3
"""Offline integrity and sampled semantic verification of an intraday run.

Does not request live data, alter checkpoints, or replace production outputs.
Full verification checks every cached bar's timestamp/session grid. Sampled
verification re-normalizes archived protocol bytes and recomputes multilevel
summaries. A code-version mismatch is reported rather than treated as proof of
equivalence with the version originally used by the scan.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, time
import csv
import gzip
import io
import json
import math
from pathlib import Path
import sqlite3
import sys
import time as timer

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from chanmodel.engine import multilevel
from chanmodel.model import Candle,Config,time_value
from chanmodel.tdx_data import CN,atomic_json,now_iso,normalize_daily,sha256,symbol
from chanmodel.tdx_intraday import normalize_intraday,valid_session_close
from intraday_scan import fingerprint,range_coverage

LEVELS={'day':'day','30m':'minute30','5m':'minute5'}
CALCULATION_FILES=['model.py','structure.py','segments.py','centers.py','signals.py','engine.py','tdx_intraday.py']


def calculation_fingerprint():
    files=[ROOT/'chanmodel'/name for name in CALCULATION_FILES]+[ROOT/'intraday_scan.py']
    return sha256(b''.join(p.relative_to(ROOT).as_posix().encode()+p.read_bytes() for p in files))


def archived_response(raw_path,metadata):
    compressed=raw_path.read_bytes()
    if sha256(compressed)!=metadata['raw_storage_sha256']:
        raise ValueError('raw storage SHA-256 mismatch')
    raw=gzip.decompress(compressed)
    if sha256(raw)!=metadata['response_sha256']:
        raise ValueError('raw response SHA-256 mismatch')
    envelope=json.loads(raw)
    if not isinstance(envelope,dict) or envelope.get('code')!=0 or not isinstance(envelope.get('data'),dict):
        raise ValueError('archived API response is not successful')
    return raw,envelope['data']


def csv_bars(raw,level,as_of,collect=False):
    """Count/check the entire cached series while holding one decoded CSV."""
    reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig'),newline=''))
    required={'closed_at','open','high','low','close','volume','is_closed'}
    if not required<=set(reader.fieldnames or []):raise ValueError('CSV headers incomplete')
    count=closed=0;first=last=previous=None;bars=[];open_count=0
    for row in reader:
        point=datetime.fromisoformat(row['closed_at'].replace('Z','+00:00'))
        if point.tzinfo is None:raise ValueError('cached closing time has no timezone')
        local=point.astimezone(CN)
        if previous is not None and point<=previous:raise ValueError('CSV closing times not strictly ascending')
        previous=point
        if level=='day':
            # Early Chinese market history contains Saturday sessions. The
            # daily cache declares a close-time convention, not a modern
            # weekday-only trading calendar imposed retrospectively.
            if local.time()!=time(15,0):raise ValueError('daily closing time is not 15:00 CN')
        elif not valid_session_close(point,30 if level=='30m' else 5):
            raise ValueError('intraday close is outside the declared session grid')
        raw_closed=row['is_closed'].lower()
        if raw_closed not in {'true','false','1','0'}:raise ValueError('invalid cached is_closed')
        marked=raw_closed in {'true','1'}
        if marked!=(point<=as_of):raise ValueError('cached is_closed disagrees with run cutoff')
        values=[float(row[field]) for field in ['open','high','low','close','volume']]
        if not all(math.isfinite(value) for value in values):raise ValueError('nonfinite cached OHLCV')
        if min(values[:4])<=0 or values[4]<0 or values[1]<max(values[0],values[2],values[3]) or values[2]>min(values[0],values[3]):
            raise ValueError('invalid cached OHLCV range')
        if marked:
            if open_count:raise ValueError('closed bar appears after an open cached bar')
            if first is None:first=row['closed_at']
            last=row['closed_at'];closed+=1
            if collect:bars.append(Candle(len(bars),values[0],values[1],values[2],values[3],row['closed_at'],values[4],True))
        else:open_count+=1
        count+=1
    return {'csv_rows':count,'closed_rows':closed,'open_rows':open_count,'first_close':first,'last_close':last},bars


def source_paths(cache,code,level):
    period=LEVELS[level];folder=cache/'tdx'/period
    csv_path=folder/'ohlcv'/(f'{code}.csv.gz' if level!='day' else f'{code}.csv')
    return folder/'raw'/f'{code}.json.gz',csv_path,folder/'metadata'/f'{code}.json'


def verify_source(cache,code,level,as_of,stock_source,period_summary,collect=False):
    raw_path,csv_path,meta_path=source_paths(cache,code,level)
    metadata=json.loads(meta_path.read_text(encoding='utf-8'))
    if metadata.get('symbol')!=code or metadata.get('source')!='tdx' or metadata.get('adjustment')!='unadjusted':
        raise ValueError(f'{level}: metadata source/symbol/adjustment mismatch')
    expected_type=LEVELS[level]
    if metadata.get('provider_meta',{}).get('type') not in {None,expected_type}:
        raise ValueError(f'{level}: provider metadata period mismatch')
    raw,envelope_data=archived_response(raw_path,metadata)
    storage=csv_path.read_bytes()
    if level=='day':
        csv_raw=storage
    else:
        if sha256(storage)!=metadata['csv_storage_sha256']:
            raise ValueError(f'{level}: CSV compressed storage SHA-256 mismatch')
        csv_raw=gzip.decompress(storage)
    if sha256(csv_raw)!=metadata['csv_sha256']:
        raise ValueError(f'{level}: uncompressed CSV SHA-256 mismatch')
    # Daily cache metadata can be re-emitted by an equivalent resume. Require
    # actual input identity rather than receipt-byte equality of metadata JSON.
    for key in ['response_sha256','csv_sha256','raw_storage_sha256']:
        if stock_source.get(key)!=metadata.get(key):
            raise ValueError(f'{level}: current cache differs from the input recorded by stock result ({key})')
    metrics,bars=csv_bars(csv_raw,level,as_of,collect)
    if metrics['csv_rows']!=metadata['unique_rows'] or metrics['closed_rows']!=metadata['closed_rows'] or metrics['open_rows']!=metadata['open_rows_excluded']:
        raise ValueError(f'{level}: CSV counts differ from metadata')
    raw_rows=envelope_data.get('list',envelope_data.get('List'))
    raw_count=envelope_data.get('count',envelope_data.get('Count'))
    if raw_rows is None and raw_count==0:raw_rows=[]
    if not isinstance(raw_rows,list) or len(raw_rows)!=metadata['raw_rows']:
        raise ValueError(f'{level}: raw row count differs from metadata')
    if raw_count is not None and int(raw_count)!=len(raw_rows):
        raise ValueError(f'{level}: response count differs from raw rows')
    if metrics['closed_rows']!=period_summary['bars']:
        raise ValueError(f'{level}: stock result bar count differs from CSV')
    for key in ['first_close','last_close']:
        actual=metrics[key];expected=period_summary[key]
        if (actual is None)!=(expected is None) or (actual is not None and time_value(actual)!=time_value(expected)):
            raise ValueError(f'{level}: stock {key} differs from cached series')
    return metrics,bars,envelope_data,metadata


def unavailable_evidence(cache,code,result):
    levels=[result.get('empty_period')] if result.get('empty_period') else ['day','minute30','minute5']
    receipts=[]
    for period in levels:
        if period not in {'day','minute30','minute5'}:continue
        folder=cache/'tdx'/period
        meta_path=folder/'metadata'/f'{code}.json';raw_path=folder/'raw'/f'{code}.json.gz'
        if not meta_path.exists() or not raw_path.exists():continue
        metadata=json.loads(meta_path.read_text(encoding='utf-8'))
        raw,data=archived_response(raw_path,metadata)
        rows=data.get('list',data.get('List'));count=data.get('count',data.get('Count'))
        if count==0 and rows in (None,[]):
            receipts.append({'period':period,'count':0,'list_was_null':rows is None,
                             'response_sha256':metadata['response_sha256'],'raw_path':str(raw_path),
                             'received_at':metadata.get('received_at')})
    return receipts


def compare_recomputed(result,datasets,config,cutoff):
    report=multilevel(datasets,('day','30m','5m'),cutoff,Config(**config))
    periods={level:{'bars':len(bars),'first_close':bars[0].closed_at,'last_close':bars[-1].closed_at,
                    'strokes':len(report['levels'][level]['strokes']),'segments':len(report['levels'][level]['segments']),
                    'centers':len(report['levels'][level]['centers']),'state':report['levels'][level]['current_state'],
                    'active_candidate_count':sum(point['status']=='candidate' for point in report['levels'][level]['signal_candidates'])}
             for level,bars in datasets.items()}
    # ISO strings may use UTC or CN offsets, but the actual instants must match.
    expected=json.loads(json.dumps(result['periods']))
    actual=json.loads(json.dumps(periods))
    for values in [expected,actual]:
        for details in values.values():
            for key in ['first_close','last_close']:details[key]=time_value(details[key]).isoformat()
    parents={point['id']:point for point in report['levels']['day']['signal_candidates']}
    confirms={point['id']:point for point in report['levels']['30m']['signal_candidates']}
    for audit in report['interval_nesting_audits']:
        audit['daily_terminal_30m_coverage']=range_coverage(parents[audit['parent_signal']],datasets['day'],datasets['30m'])
        for match in audit['matches']:
            match['confirm_terminal_5m_coverage']=range_coverage(confirms[match['confirm_id']],datasets['30m'],datasets['5m'])
    return {'period_summaries_match':actual==expected,
            'interval_audits_match':canonical_times(report['interval_nesting_audits'])==canonical_times(result['interval_nesting_audits']),
            'nested_count_match':sum(a['nested_candidate_evidence'] for a in report['interval_nesting_audits'])==result['nested_evidence_count'],
            'daily_missing_child_history_count_match':sum(not a['daily_terminal_30m_coverage']['terminal_range_covered'] for a in report['interval_nesting_audits'])==result.get('daily_candidates_without_30m_history',0)}


def canonical_times(value):
    """Compare equal instants across loader-normalized UTC and provider CN ISO."""
    if isinstance(value,dict):return {key:canonical_times(item) for key,item in value.items()}
    if isinstance(value,list):return [canonical_times(item) for item in value]
    if isinstance(value,str) and len(value)>=19 and value[10:11]=='T':
        try:return time_value(value).isoformat()
        except ValueError:pass
    return value


def verify_stock(task):
    code=task['code'];root=Path(task['run_dir']);cache=Path(task['cache_dir']);result=task['result']
    errors=[];warnings=[];entry={'symbol':code,'checkpoint_status':task['checkpoint_status'],
                               'checkpoint_engine_sha256':task['checkpoint_engine'],'sampled':task['sampled']}
    try:
        symbol(code)
        if result.get('symbol')!=code or result.get('status')!=task['checkpoint_status']:
            raise ValueError('checkpoint columns and JSON disagree')
        if result.get('engine_sha256')!=task['checkpoint_engine']:
            raise ValueError('checkpoint fingerprint and result fingerprint disagree')
        file_result=json.loads((root/'stocks'/f'{code}.json').read_text(encoding='utf-8'))
        if file_result!=result:raise ValueError('stock JSON differs from checkpoint JSON')
        current_version=task['checkpoint_engine']==task['current_engine']
        run_version=task['checkpoint_engine']==task['run_engine']
        entry['version_status']='same_version' if current_version and run_version else 'different_version'
        if not current_version:warnings.append('current production-file fingerprint differs from saved result')
        if not run_version:warnings.append('result fingerprint differs from run.json settings')
        if task['checkpoint_status']!='success':
            evidence=unavailable_evidence(cache,code,result)
            if evidence:
                entry.update(status='unavailable',unavailable_evidence=evidence,provider_error=result.get('error'))
            else:
                entry.update(status='failed',provider_error=result.get('error'),attempts=result.get('attempts',[]))
                errors.append('non-success result lacks verified empty-upstream evidence')
        else:
            cutoff=datetime.fromisoformat(task['as_of']);datasets={};raw_data={};metrics={}
            if result.get('as_of')!=task['as_of']:raise ValueError('stock cutoff differs from run cutoff')
            if result.get('adjustment')!='unadjusted' or result.get('execution_allowed') is not False:
                raise ValueError('stock adjustment/execution metadata differs from contract')
            if set(result.get('sources',{}))!=set(LEVELS) or set(result.get('periods',{}))!=set(LEVELS):
                raise ValueError('success result does not have all day/30m/5m inputs')
            for level in LEVELS:
                observed,bars,data,meta=verify_source(cache,code,level,cutoff,result['sources'][level],result['periods'][level],task['sampled'])
                metrics[level]=observed
                if task['sampled']:
                    normalizer=normalize_daily if level=='day' else normalize_intraday
                    normalized,rows,quality=normalizer(data,cutoff,'tdx' if level=='day' else LEVELS[level])
                    if normalized!=bars:
                        raise ValueError(f'{level}: archived protocol normalization differs from cached CSV bars')
                    datasets[level]=bars
            entry.update(status='verified',periods=metrics)
            if task['sampled']:
                comparison=compare_recomputed(result,datasets,task['config'],task['as_of'])
                entry['sample_recomputation']={**comparison,'version_status':entry['version_status']}
                if not all(comparison.values()):
                    if current_version and run_version:errors.append('same-version sampled multilevel recomputation differs from saved summary')
                    else:warnings.append('different-version recomputation differs; version-aligned rerun required before semantic equivalence claim')
    except Exception as exc:
        entry['status']='integrity_error';errors.append(f'{type(exc).__name__}: {exc}')
    entry['errors']=errors;entry['warnings']=warnings
    return entry


def sample_symbols(results,count):
    """Deterministic exchange-stratified sample, spread over sorted symbols."""
    successes=[code for code,item in sorted(results.items()) if item['status']=='success']
    if count<=0:return set()
    selected=[]
    for exchange in ['bj','sh','sz']:
        group=[code for code in successes if code.startswith(exchange)]
        quota=min(len(group),max(1,count//3))
        for i in range(quota):
            selected.append(group[round(i*(len(group)-1)/max(1,quota-1))])
    for code in successes:
        if len(selected)>=count:break
        if code not in selected:selected.append(code)
    return set(selected[:count])


def verify_run(run_dir: Path,cache_dir: Path,sample_count=20,workers=4,output_dir: Path|None=None):
    run_dir=run_dir.resolve();cache_dir=cache_dir.resolve();output_dir=output_dir or run_dir/'verification'
    run=json.loads((run_dir/'run.json').read_text(encoding='utf-8'));settings=run['settings']
    desired={record['symbol']:record for record in run['universe']}
    if len(desired)!=len(run['universe']):raise ValueError('run universe has duplicate symbols')
    db=sqlite3.connect((run_dir/'checkpoint.sqlite3').as_uri()+'?mode=ro',uri=True)
    rows=db.execute('SELECT symbol,status,engine_sha256,result FROM stocks ORDER BY symbol').fetchall();db.close()
    checkpoint={code:{'status':status,'engine':engine,'result':json.loads(raw)} for code,status,engine,raw in rows}
    selected=sample_symbols(checkpoint,sample_count);current_engine=fingerprint();results=[]
    extras=sorted(set(checkpoint)-set(desired));pending=sorted(set(desired)-set(checkpoint))
    tasks=[{'code':code,'run_dir':str(run_dir),'cache_dir':str(cache_dir),'result':item['result'],
            'checkpoint_status':item['status'],'checkpoint_engine':item['engine'],'sampled':code in selected,
            'run_engine':settings['engine_sha256'],'current_engine':current_engine,
            'as_of':settings['as_of'],'config':settings['config']} for code,item in checkpoint.items() if code in desired]
    started=timer.monotonic()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures=[pool.submit(verify_stock,task) for task in tasks]
        for index,future in enumerate(as_completed(futures),1):
            results.append(future.result())
            if index%100==0 or index==len(tasks):
                print(json.dumps({'event':'verify_progress','checked':index,'total_recorded':len(tasks),'errors':sum(bool(x['errors']) for x in results)},ensure_ascii=False),flush=True)
    results.sort(key=lambda r:r['symbol'])
    failures=[result for result in results if result['errors']]
    unavailable=[result for result in results if result['status']=='unavailable']
    versions=sorted({result['checkpoint_engine_sha256'] for result in results})
    version_aligned=versions==[settings['engine_sha256']] and settings['engine_sha256']==current_engine
    same_version_samples=[result for result in results if result.get('sample_recomputation',{}).get('version_status')=='same_version']
    sampled=[result for result in results if result.get('sample_recomputation')]
    summary={'verified_at':now_iso(),'run_dir':str(run_dir),'cache_dir':str(cache_dir),'as_of':settings['as_of'],
             'status':'fail' if failures or extras else 'incomplete' if pending else 'pass' if version_aligned else 'pass_with_version_difference',
             'universe_count':len(desired),'checkpoint_count':len(tasks),'verified_success':sum(r['status']=='verified' for r in results),
             'unavailable_count':len(unavailable),'integrity_or_unexplained_failures':len(failures),'pending_count':len(pending),
             'outside_universe_checkpoint_symbols':extras,'pending_symbols':pending,
             'unavailable':unavailable,'failures':failures,'sample_requested':sample_count,'sample_recomputed':len(sampled),
             'sample_symbols':sorted(selected),'same_version_sample_count':len(same_version_samples),
             'sample_all_current_outputs_match':all(all(v for k,v in r['sample_recomputation'].items() if k!='version_status') for r in sampled),
             'version_alignment':{'run_engine_sha256':settings['engine_sha256'],'current_engine_sha256':current_engine,
                                  'checkpoint_engine_sha256_values':versions,'aligned':version_aligned,
                                  'current_calculation_contract_sha256':calculation_fingerprint(),
                                  'note':'The contract fingerprint excludes unrelated modules, but no historical contract digest was recorded; full-hash differences remain explicit.'},
             'total_verified_bars':{level:sum(r.get('periods',{}).get(level,{}).get('closed_rows',0) for r in results) for level in LEVELS},
             'elapsed_seconds':round(timer.monotonic()-started,3),'network_requests':0,
             'semantics':'all cached sources/hash/CSV OHLCV/session grids checked; archived-protocol normalization and structure recomputation sampled',
             'execution_allowed':False}
    atomic_json(output_dir/'summary.json',summary)
    atomic_json(output_dir/'stocks.json',results)
    text='\n'.join(['# 分钟与多周期离线核验','',f"核验时间：{summary['verified_at']}",f"状态：{summary['status']}",'',
        f"股票池{len(desired)}只；检查点{len(tasks)}只；数据完整性通过{summary['verified_success']}只；上游无数据{len(unavailable)}只；待处理{len(pending)}只；完整性或无法解释失败{len(failures)}只。",'',
        '全部已记录成功股票：逐份检查day/30m/5m原始压缩响应、解压响应、标准化CSV及压缩CSV哈希；检查元数据、检查点与stocks JSON一致；逐行检查时间单调、交易时段、周期网格、截止闭合及OHLCV有效性。',
        f"抽样{len(sampled)}只重新从归档原始协议数据标准化，并重算多周期结构摘要；其中与当前代码指纹完全一致{len(same_version_samples)}只。",'',
        f"版本对齐：{version_aligned}。若不同版本，核验会保留different_version并要求同版本缓存重算，不把新版输出相同误作旧版已证明。",'',
        '无数据条目单列unavailable，要求存在成功API外壳中的count=0和空list原始证据，绝不填充虚构K线。其他失败保留原错误。',
        '本工具离线执行，未请求行情源；不修改生产缓存、扫描结果或检查点。仅生成本目录核验文件。',
        f"逐股机器结果：stocks.json；总结果：summary.json。耗时{summary['elapsed_seconds']}秒。",''])
    output_dir.mkdir(parents=True,exist_ok=True);(output_dir/'VERIFICATION.md').write_text(text,encoding='utf-8')
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir',type=Path,required=True);parser.add_argument('--cache-dir',type=Path,required=True)
    parser.add_argument('--sample',type=int,default=20);parser.add_argument('--sample20',action='store_true')
    parser.add_argument('--workers',type=int,default=4);parser.add_argument('--output-dir',type=Path)
    args=parser.parse_args()
    if args.sample<0 or not 1<=args.workers<=16:parser.error('sample>=0 and workers=1..16 required')
    summary=verify_run(args.run_dir,args.cache_dir,20 if args.sample20 else args.sample,args.workers,args.output_dir)
    print(json.dumps({key:summary[key] for key in ['status','universe_count','verified_success','unavailable_count','pending_count','integrity_or_unexplained_failures','sample_recomputed','elapsed_seconds']},ensure_ascii=False),flush=True)
    return 1 if summary['status']=='fail' else 3 if summary['status']=='incomplete' else 0


if __name__=='__main__':raise SystemExit(main())
