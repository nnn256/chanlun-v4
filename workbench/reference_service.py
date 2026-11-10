"""Recompute user chart objects with the pinned engine; preserve old artifacts."""
from collections import OrderedDict
import copy
from dataclasses import asdict
import gzip
import hashlib
import json
import threading

from chanmodel.reference_engine import analyze_reference,PROFILE,COMMIT,SETTINGS,review_geometry
from chanmodel.kline_audit import candles_from_panel,audit_panel
from chanmodel.signal_chart import attach_chart_annotations
from chanmodel.tdx_data import atomic_bytes
from .store import STATE

CACHE=OrderedDict();LOCK=threading.RLock()


def reference_report(symbol,period,bars,cutoff,adjustment='unadjusted',source='saved_raw_ohlcv'):
    payload={'adapter_version':'native-context-review-v6','symbol':symbol,'period':period,'source':source,'adjustment':adjustment,'cutoff':cutoff,'commit':COMMIT,'settings':SETTINGS,
             'bars':[[b.pos,b.closed_at,b.open,b.high,b.low,b.close,b.volume,b.closed] for b in bars]}
    key=hashlib.sha256(json.dumps(payload,allow_nan=False,separators=(',',':')).encode()).hexdigest()
    path=STATE/'reference-rebuild'/COMMIT[:12]/(key+'.json.gz')
    with LOCK:
        if key in CACHE:
            CACHE.move_to_end(key);return review_geometry(copy.deepcopy(CACHE[key]))
    if path.exists():
        value=json.loads(gzip.decompress(path.read_bytes()))
        if value.get('engine_commit')!=COMMIT or value.get('input_sha256')!=key:raise ValueError('reference rebuild cache identity mismatch')
    else:
        value=analyze_reference(symbol,period,bars,cutoff,key,source,adjustment)
        atomic_bytes(path,gzip.compress(json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode(),compresslevel=3,mtime=0))
    with LOCK:
        CACHE[key]=value
        while len(CACHE)>8:CACHE.popitem(last=False)
    return review_geometry(copy.deepcopy(value))


def rebuild_bundle(bundle):
    symbol=bundle['summary']['symbol'];chart=bundle['chart'];cutoff=chart.get('as_of',bundle['summary']['as_of'])
    evidence=bundle.setdefault('evidence',{});new_reports={}
    for panel in chart['panels']:
        if panel.get('kind')=='line':continue
        audit=audit_panel(panel);panel['kline_audit']=audit
        if audit['state']=='invalid':
            panel.update(strokes=[],segments=[],centers=[],signals=[],all_signals=[],structure_layers=[]);continue
        previous={'strokes':len(panel.get('strokes',[])),'segments':len(panel.get('segments',[])),
                  'signals':len(panel.get('all_signals',panel.get('signals',[])))}
        bars=candles_from_panel(panel)
        report=reference_report(symbol,panel['period'],bars,cutoff,bundle['summary'].get('adjustment','unadjusted'))
        new_reports[panel['period']]=report
        panel.update(merged=report['base']['merged'],fractals=report['base']['fractals'],
                     strokes=report['base']['strokes'],segments=report['base']['segments'],higher_segments=report['base']['higher_segments'],
                     structure_layers=report['hierarchy']['levels'],all_signals=report['signals'],
                     centers=report['hierarchy']['levels'][0]['centers'],
                     signals=[s for s in report['signals'] if s['rank']==1],
                     complete_evidence={'divergence_comparisons':[]},engine=PROFILE,
                     engine_provenance=report['engine_provenance'],native_scope_names={'1':'笔结构','2':'线段结构'},
                     reference_comparison={'saved':previous,'rebuilt':{'strokes':len(report['base']['strokes']),
                                            'segments':len(report['base']['segments']),'signals':len(report['signals'])},
                                           'semantics':'independently recomputed from all original candles; original task bytes unchanged'},
                     geometry_review=report['geometry_review'],
                     note='固定引擎 · 笔/线段分别标注 · 橙色待复核不作确认')
        for period_summary in [bundle['summary'].get('periods',{}).get(panel['period'])]:
            if period_summary is not None:
                period_summary['active_candidates']=[{**s,'anchor_time':s['event_time'],
                            'available_time':s['available_time']} for s in report['signals'] if s['state']!='invalidated']
    evidence['reference_reports']=new_reports
    bundle['summary'].update(display_engine=PROFILE,display_engine_commit=COMMIT,
                             result_text='已按固定特征序列引擎从完整原始K线重建；原任务与旧回测保持其原版本')
    chart.update(display_engine=PROFILE,engine_commit=COMMIT,
                 approximation_loss=['native stroke/segment scope is not calendar timeframe or universal theoretical rank',
                                     'engine local structure completion is not an independently proven multi-timeframe execution trigger'])
    attach_chart_annotations(chart)
    return bundle
