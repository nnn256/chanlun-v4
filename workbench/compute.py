"""Reuse V2 unchanged; keep full history, observed dates, and quality contracts."""
from collections import Counter
from datetime import datetime
import gzip
import json
from pathlib import Path
import time

from chanmodel.tdx_data import CN, TdxClient, atomic_bytes, cache_history, normalize_daily
from chanmodel.tdx_intraday import fetch_intraday, normalize_intraday, cache_intraday
from chanmodel.v2.io import load_cache_series
from chanmodel.v2.engine import analyze, multiframe, replay
from chanmodel.v2.series import validate_multiframe
from chanmodel.unified import analyze_all,CompleteReplay,DEFAULT_CONFIG
from chanmodel.release import APP_LABEL
from .store import ROOT, STATE

NAMES = {'first_buy':'一买候选','second_buy':'二买候选','third_buy':'三买候选',
         'first_sell':'一卖候选','second_sell':'二卖候选','third_sell':'三卖候选'}


def local(value):
    return datetime.fromisoformat(value).astimezone(CN).strftime('%Y-%m-%d %H:%M')


def contract_brief(contract):
    if not contract:
        return None
    return {'state': contract['state'], 'pairs': [
        {**{k:p.get(k) for k in ('parent_period','child_period','state','price_tolerance','volume_tolerance',
            'matched_parent_bars','mismatched_parent_bars','incomplete_parent_bars','matched_start','matched_end')},
         'max_volume_difference': max((abs(c.get('volume_difference') or 0) for c in p['comparisons']), default=0),
         'max_price_difference': max((abs(v) for c in p['comparisons'] for v in c.get('price_differences',{}).values()), default=0)}
        for p in contract['pairs']]}


def make_chart(record, inputs, reports, cutoff, combined=None, default_contract=None):
    panels, period_summary = [], {}
    for period, report in reports.items():
        bars = inputs[period].prefix(cutoff).bars
        keep = {'day':180,'30m':280,'5m':420}[period]
        start = 0
        layer = report['hierarchy']['levels'][0]
        center = layer['centers'][-1] if layer['centers'] else None
        tail = report['base']['segments'][-1] if report['base']['segments'] else None
        points = [{'time':int(datetime.fromisoformat(b.closed_at).timestamp())+8*3600,
                   'source_index':b.pos, 'date_cn':local(b.closed_at), 'open':b.open,
                   'high':b.high,'low':b.low,'close':b.close,'volume':b.volume} for b in bars[start:]]
        candidates = []
        for s in sorted(report['signals'], key=lambda x:x['anchor'], reverse=True):
            if s['state'] == 'invalidated':
                continue
            candidates.append({**{k:s[k] for k in ('id','class','rank','state','price','invalidation','zd','zg')},
                               'label':NAMES.get(s['class'],s['class']), 'anchor_time':local(bars[s['anchor']].closed_at),
                               'available_time':local(bars[s['available_at']].closed_at)})
        note = ('向上' if tail['direction']=='up' else '向下') + ('待确认' if tail['state']=='provisional' else '已确认') + f" · {tail['start_price']:.2f} → {tail['end_price']:.2f}" if tail else '尚未形成线段'
        panels.append({'period':period,'visible_start':start,'visible_bars':len(points),'input_bars':len(bars),'default_visible':keep,'history_complete_in_chart':True,
            'bars':points,'strokes':[s for s in report['base']['strokes'] if s['end']>=start],
            'segments':[s for s in report['base']['segments'] if s['end']>=start],
            'centers':[c for c in layer['centers'] if c['end']>=start],
            'signals':[{**s,'label':NAMES.get(s['class'],s['class'])} for s in report['signals'] if s['rank']==1 and s['state']!='invalidated' and s['anchor']>=start],
            'note':note,'latest_center':center,
            'merged':report['base']['merged'],'fractals':report['base']['fractals'],
            'structure_layers':report['hierarchy']['levels'],
            'all_signals':report['signals'],
            'complete_evidence':{k:report.get(k,[]) for k in ['feature_sequences','center_evolution','center_monitors',
                                                          'construction_phases','divergence_comparisons']}})
        period_summary[period] = {'bars':len(bars),'first_close':local(bars[0].closed_at),
            'last_close':local(bars[-1].closed_at),'latest_center':{k:center[k] for k in ('zd','zg','state','rank')} if center else None,
            'last_segment':{k:tail[k] for k in ('direction','state','start_price','end_price')} if tail else None,
            'certificate_audit':report['certificate_audit'], 'active_candidates':candidates}
    daily = inputs['day'].prefix(cutoff).bars
    last = daily[-1]; previous = daily[-2].close if len(daily)>1 else last.close
    states = dict(Counter(s['state'] for s in combined.get('signals',[]))) if combined else {}
    triggered = states.get('structurally_triggered_under_profile',0) if combined and combined['state']=='calculated' else None
    state = combined['state'] if combined else 'day_only'
    result = '尚未运行三周期校验' if combined is None else '数据校验未通过，触发不成立' if state=='blocked_data_contract' else f'{triggered}个模型研究触发' if triggered else '暂无有效多周期触发'
    summary = {**record,'price':last.close,'previous_close':previous,'change_pct':(last.close-previous)/previous*100,
               'last_date':local(last.closed_at)[:10], 'last_close_time':local(last.closed_at), 'as_of':cutoff,
               'mode':'multi' if combined else 'day','algorithm':APP_LABEL if reports['day'].get('feature_sequences') is not None else 'V2','state':state,'triggered':triggered,
               'active_candidates':len(period_summary['day']['active_candidates']), 'multiframe_states':states,
               'default_contract':contract_brief(default_contract),
               'comparison_contract':contract_brief(combined['data_contract']) if combined else None,
               'periods':period_summary,'result_text':result,'execution_allowed':False,
               'definition_mode':reports['day']['definition_mode'],'strict_original_equivalence_proven':False}
    chart = {**{k:summary[k] for k in ('name','symbol','price','change_pct','last_close_time','result_text','execution_allowed','definition_mode')},
             'code':record['code'],'change':last.close-previous,'source':'TDX','adjustment':'不复权','panels':panels,
             'input_sha256':{p:s.input_sha256 for p,s in inputs.items()},'as_of':cutoff,
             'approximation_loss':reports['day'].get('approximation_loss',reports['day'].get('remaining_scope',[])),'strict_original_equivalence_proven':False,
             'snapshot_semantics':'final-cutoff structure; anchors may precede availability'}
    return chart, summary


def process_stock(record, settings, job_id):
    started = time.monotonic()
    code = record['symbol']; mode = settings['mode']; cutoff = settings['cutoff']
    folder = STATE / 'jobs' / job_id
    cache = folder / 'cache' if settings['refresh'] else ROOT / 'market_data'
    periods = ('day','30m','5m') if mode=='multi' else ('day',)
    inputs, receipts = {}, {}
    from .progress import reporter
    progress=reporter(job_id,code,record['name'])
    try:
        progress('starting','开始处理该股')
        if mode=='full':
            from .full_compute import process_full
            return process_full(record,settings,job_id)
        for period in periods:
            progress('kline',f'正在{"获取" if settings["refresh"] else "读取"}{period}全部历史')
            if settings['refresh']:
                client = TdxClient(timeout=60)
                raw_period = {'day':'day','30m':'minute30','5m':'minute5'}[period]
                data,receipt,raw=client.request_retry('/api/kline-all/tdx',{'code':code,'type':raw_period},
                    on_retry=lambda info:progress('retry',f'行情请求中断，正在重试{info["next_attempt"]}/{info["max_attempts"]}',**info))
                atomic_bytes(folder/'responses'/f'{code}-{period}.json.gz',gzip.compress(raw,mtime=0))
                bars, rows, quality = normalize_daily(data,datetime.fromisoformat(cutoff)) if period=='day' else normalize_intraday(data,datetime.fromisoformat(cutoff),raw_period)
                quality['cache_run_as_of'] = cutoff
                if not bars:
                    raise ValueError(f'{period}: provider returned no closed bars')
                if period=='day':
                    cache_history(cache,code,'tdx',rows,raw,receipt,quality)
                else:
                    cache_intraday(cache,code,raw_period,rows,raw,receipt,quality)
            inputs[period], receipts[period] = load_cache_series(cache,code,period)
        combined, default = None, None
        progress('analysis','行情读取完成，正在计算走势结构和数据校验')
        complete=analyze_all(inputs,cutoff,roles=('day','30m','5m') if mode=='multi' else None,
                             volume_tolerance=settings['volume_tolerance'])
        reports=complete['reports']
        if mode=='multi':
            default = validate_multiframe(inputs,('day','30m','5m'),cutoff)
            audit=validate_multiframe(inputs,('day','30m','5m'),cutoff,volume_tolerance=settings['volume_tolerance'])
            combined={'state':'calculated' if audit['state']=='verified' else 'blocked_data_contract',
                      'data_contract':audit,'levels':reports,
                      'signals':complete['links']['selected_signals'] if audit['state']=='verified' else [],
                      'complete_links':complete['links'],'execution_allowed':False}
        chart, summary = make_chart(record,inputs,reports,cutoff,combined,default)
        if mode=='replay':
            progress('replay',f'正在从最早日期逐日回放，共{len(inputs["day"].bars):,}根K线；长历史会耗时较久')
            engine=CompleteReplay(inputs)
            for bar in inputs['day'].prefix(cutoff).bars:engine.observe(bar.closed_at)
            final=engine.snapshot['reports']['day'];events=engine.events
            def body(value):
                if isinstance(value,dict):return {k:body(v) for k,v in value.items() if k not in {'first_seen_time','version_available_time','version_confirmed_time','revision'}}
                if isinstance(value,list):return [body(v) for v in value]
                return value
            if body(final['hierarchy'])!=body(reports['day']['hierarchy']) or body(final['signals'])!=body(reports['day']['signals']):
                raise ValueError('daily replay differs from current snapshot')
            atomic_bytes(folder/'events'/f'{code}.jsonl.gz',gzip.compress(''.join(json.dumps(e,ensure_ascii=False)+'\n' for e in events).encode(),mtime=0))
            summary['replay'] = {'bars':len(inputs['day'].prefix(cutoff).bars),'events':len(events),'final_matches_snapshot':True}
        summary.update(mode=mode,elapsed_seconds=round(time.monotonic()-started,3),volume_tolerance=settings['volume_tolerance'])
        summary['algorithm']=APP_LABEL;summary['profile']=complete['profile']
        artifact = folder/'results'/f'{code}.json.gz'
        bundle = {'summary':summary,'chart':chart,'evidence':{'reports':reports,'default_contract':default,
                  'multiframe':{k:v for k,v in combined.items() if k!='levels'} if combined else None,'source_receipts':receipts}}
        atomic_bytes(artifact,gzip.compress(json.dumps(bundle,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode(),compresslevel=3,mtime=0))
        progress('finished','结果已保存，可以查看图表')
        return {'state':'success','summary':summary,'artifact':str(artifact.relative_to(STATE))}
    except Exception as exc:
        progress('failed',f'{type(exc).__name__}: {exc}')
        return {'state':'failed','error':f'{type(exc).__name__}: {exc}','elapsed_seconds':round(time.monotonic()-started,3)}
