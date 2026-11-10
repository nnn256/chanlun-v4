"""Pinned MIT chan.py adapter, raw OHLC provenance and observed signal ledger.

The upstream algorithm and its options are named in every output. No claim of
universal original-Chan equivalence is inferred from agreement with this engine.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime,timezone,timedelta
import hashlib
import json
from pathlib import Path
import sys
import threading

VENDOR=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parents[1]))/'third_party/chanpy'
if str(VENDOR) not in sys.path:sys.path.insert(0,str(VENDOR))
from ChanConfig import CChanConfig
from Common.CEnum import DATA_FIELD,KL_TYPE,FX_TYPE
from Common.CTime import CTime
from KLine.KLine_List import CKLine_List
from KLine.KLine_Unit import CKLine_Unit

from .model import time_value

PROFILE='chanpy_feature_sequence_strict_pinned_v1'
COMMIT='f8ca3b5e44d7166e2abd756a8e7fa816c79faebe'
SETTINGS={'trigger_step':True,'bi_strict':True,'bi_fx_check':'strict',
          'bi_end_is_peak':True,'bi_allow_sub_peak':False,'gap_as_kl':False,
          'seg_algo':'chan','left_seg_method':'peak','zs_algo':'normal','zs_combine':True,
          'one_bi_zs':False,'divergence_rate':1.0,'min_zs_cnt':1,'bsp3_follow_1':False,
          'strict_bsp3':True,'bs_type':'1,1p,2,2s,3a,3b','print_warning':False}
CN=timezone(timedelta(hours=8))
_HASH_LOCK=threading.Lock();_CHECKED=False


def verify_engine():
    global _CHECKED
    if _CHECKED:return
    with _HASH_LOCK:
        receipt=json.loads((VENDOR/'provenance.json').read_text(encoding='utf-8'))
        if receipt['commit']!=COMMIT:raise ValueError('reference engine commit mismatch')
        for name,digest in receipt['files'].items():
            if hashlib.sha256((VENDOR/name).read_bytes()).hexdigest()!=digest:
                raise ValueError('reference engine source hash mismatch: '+name)
        _CHECKED=True


def local_time(bars,index):return time_value(bars[index].closed_at).astimezone(CN).isoformat()


def native_objects(engine):
    for rank,collection in [(1,engine.bs_point_lst),(2,engine.seg_bs_point_lst)]:
        for point in collection.bsp_iter():
            yield rank,point


def run_reference(bars,period='day',observe=True,as_of=None):
    verify_engine()
    period_map={'1m':KL_TYPE.K_1M,'5m':KL_TYPE.K_5M,'15m':KL_TYPE.K_15M,'30m':KL_TYPE.K_30M,
                '60m':KL_TYPE.K_60M,'day':KL_TYPE.K_DAY,'week':KL_TYPE.K_WEEK,'month':KL_TYPE.K_MON,
                'quarter':KL_TYPE.K_QUARTER,'year':KL_TYPE.K_YEAR,'5d':KL_TYPE.K_DAY}
    if not bars:raise ValueError('reference engine requires observed raw candles')
    cutoff=time_value(as_of) if as_of else time_value(bars[-1].closed_at)
    closed=[b for b in bars if b.closed and time_value(b.closed_at)<=cutoff]
    if not closed:raise ValueError('no closed OHLC at cutoff')
    if any(b.pos!=i for i,b in enumerate(closed)):raise ValueError('raw indices not a closed prefix')
    options={**SETTINGS,'trigger_step':observe}
    engine=CKLine_List(period_map.get(period,KL_TYPE.K_DAY),CChanConfig(options))
    previous=None;registry={};events=[];structural_first={}
    for index,bar in enumerate(closed):
        d=time_value(bar.closed_at).astimezone(CN)
        k=CKLine_Unit({DATA_FIELD.FIELD_TIME:CTime(d.year,d.month,d.day,d.hour,d.minute,auto=False),
                      DATA_FIELD.FIELD_OPEN:bar.open,DATA_FIELD.FIELD_HIGH:bar.high,
                      DATA_FIELD.FIELD_LOW:bar.low,DATA_FIELD.FIELD_CLOSE:bar.close,
                      DATA_FIELD.FIELD_VOLUME:bar.volume or 0})
        k.set_idx(index)
        if previous:k.set_pre_klu(previous)
        engine.add_single_klu(k);previous=k
        if not observe:continue
        present=set()
        for rank,p in native_objects(engine):
            for kind in p.type:
                ident=f'cp:{rank}:{kind.value}:{"buy" if p.is_buy else "sell"}:{p.klu.idx}'
                present.add(ident);sure=bool(p.bi.is_sure)
                signature=json.dumps({'begin':p.bi.get_begin_klu().idx,'end':p.klu.idx,'sure':sure,
                                      'first_context':p.relate_bsp1.klu.idx if p.relate_bsp1 else None,
                                      'features':dict(p.features.items())},sort_keys=True,allow_nan=False)
                old=registry.get(ident)
                if old is None or not old['active'] or old.get('signature')!=signature:
                    event='appeared' if old is None else 'reappeared' if not old['active'] else 'structure_completed' if sure and not old['sure'] else 'confirmation_withdrawn' if old['sure'] and not sure else 'revised'
                    record={'first_seen':old['first_seen'] if old else index,'observed':index,
                            'sure':sure,'active':True,'confirmed':index if sure else None,'signature':signature}
                    registry[ident]=record
                    events.append({'id':ident,'event':event,'observed_index':index,'observed_at':bar.closed_at,
                                   'anchor':p.klu.idx,'native_type':kind.value,'rank':rank,'side':'buy' if p.is_buy else 'sell',
                                   'state':'structure_complete' if sure else 'candidate'})
        for ident,old in registry.items():
            if old['active'] and ident not in present:
                old['active']=False;old['observed']=index
                events.append({'id':ident,'event':'withdrawn','observed_index':index,'observed_at':bar.closed_at})
        for label,collection in [('stroke',engine.bi_list),('segment',engine.seg_list),('segment2',engine.segseg_list)]:
            for obj in collection:
                if obj.is_sure:
                    structural_first.setdefault((label,obj.get_begin_klu().idx,obj.get_end_klu().idx),index)
    if not observe:engine.cal_seg_and_zs()
    return engine,closed,registry,events,structural_first


def make_report(engine,bars,registry,events,structural_first,symbol,period,cutoff,source,adjustment,input_sha256):
    def move(obj,kind):
        a,b=obj.get_begin_klu().idx,obj.get_end_klu().idx
        known=structural_first.get((kind,a,b),len(bars)-1)
        return {'id':f'cp:{kind}:{a}:{b}','start':a,'end':b,'direction':'up' if obj.is_up() else 'down',
                'start_price':obj.get_begin_val(),'end_price':obj.get_end_val(),'low':obj._low(),'high':obj._high(),
                'state':'confirmed' if obj.is_sure else 'provisional','basis_at':known,'available_at':known,
                'basis_closed':True,'engine':PROFILE,'native_index':obj.idx,'reason':getattr(obj,'reason',None),
                'confirmed_time':local_time(bars,known) if obj.is_sure else None}
    strokes=[move(s,'stroke') for s in engine.bi_list]
    segments=[move(s,'segment') for s in engine.seg_list]
    higher=[move(s,'segment2') for s in engine.segseg_list]
    centers=[];layers=[]
    center_lookup={}
    for rank,collection in [(1,engine.zs_list),(2,engine.segzs_list)]:
        values=[]
        for z in collection:
            ident=f'cp:c{rank}:{z.begin.idx}:{z.end.idx}'
            c={'id':ident,'rank':rank,'start':z.begin.idx,'end':z.end.idx,'zd':z.low,'zg':z.high,
               'dd':z.peak_low,'gg':z.peak_high,'state':'formed' if z.is_sure else 'forming',
               'basis_at':len(bars)-1,'components':[f'cp:{"stroke" if rank==1 else "segment"}:{m.get_begin_klu().idx}:{m.get_end_klu().idx}' for m in z.bi_lst],
               'native_is_sure':z.is_sure,'member_count':len(z.bi_lst),'engine':PROFILE}
            values.append(c);centers.append(c);center_lookup[id(z)]=c
        completed=[{**m,'rank':rank,'kind':'native_segment','state':'complete','completed_at':m['basis_at']} for m in (segments if rank==1 else higher) if m['state']=='confirmed']
        layers.append({'rank':rank,'centers':values,'completed_types':completed,'comparisons':[],'signals':[],
                       'input_kind':'native_stroke' if rank==1 else 'native_segment','completion_profile':PROFILE,
                       'definition_mode':'pinned_native_feature_sequence'})
    labels={'1':('first','一类'),'1p':('first','盘整一类'),'2':('second','二类'),
            '2s':('second','二类延伸'),'3a':('third','三类A'),'3b':('third','三类B')}
    signals=[]
    for rank,p in native_objects(engine):
        side='buy' if p.is_buy else 'sell';anchor=p.klu.idx;price=p.klu.low if p.is_buy else p.klu.high
        parent=p.bi.parent_seg
        for native in p.type:
            near=None;center_proof=None
            collection=engine.zs_list if rank==1 else engine.segzs_list
            if native.value=='3a':
                # Upstream 3a is the first pullback immediately after this
                # named center's outgoing line; do not substitute a nearby box.
                candidates=[z for z in collection if z.bi_out and z.bi_out.idx+1==p.bi.idx]
                if len(candidates)==1:near=center_lookup.get(id(candidates[0]));center_proof='native_zs_bi_out_plus_one'
            elif native.value=='3b' and p.relate_bsp1:
                first_parent=p.relate_bsp1.bi.parent_seg
                z=first_parent.get_final_multi_bi_zs() if first_parent else None
                if z is not None:near=center_lookup.get(id(z));center_proof='related_first_point_segment_final_center'
            elif native.value=='1' and parent:
                z=parent.get_final_multi_bi_zs()
                if z is not None:near=center_lookup.get(id(z));center_proof='native_terminal_segment_final_center'
            prefix,label=labels[native.value];ident=f'cp:{rank}:{native.value}:{side}:{anchor}'
            record=registry.get(ident,{'first_seen':len(bars)-1,'observed':len(bars)-1,'confirmed':None})
            at=record['observed'];own_sure=bool(p.bi.is_sure)
            # Invalidation is a parent center boundary only when it is the
            # explicit third-point reference; otherwise the signal pivot.
            boundary=price
            operator='lt' if p.is_buy else 'gt'
            broken=next((b for b in bars[at+1:] if (b.low<boundary if p.is_buy else b.high>boundary)),None)
            state='invalidated' if broken else 'structure_complete' if own_sure else 'awaiting_lower_confirmation'
            start=p.bi.get_begin_klu().idx
            signal={'id':ident,'class':prefix+'_'+side,'label':label+('买' if p.is_buy else '卖'),
                    'native_type':native.value,'side':side,'rank':rank,'anchor':anchor,'price':price,
                    'terminal_range':[start,anchor],'available_at':at,'available_time':local_time(bars,at),
                    'first_seen_time':local_time(bars,record['first_seen']),
                    'confirmed_at':record['confirmed'],'confirmed_time':local_time(bars,record['confirmed']) if record['confirmed'] is not None else None,
                    'event_time':local_time(bars,anchor),'invalidation':boundary,'invalidation_operator':operator,
                    'invalidated_at':broken.pos if broken else None,'state':state,'native_is_sure':own_sure,
                    'invalidation_semantics':'native signal pivot broken after its observed availability; context center is not silently reused as stop',
                    'center_id':near['id'] if near else None,'zd':near['zd'] if near else None,'zg':near['zg'] if near else None,
                    'center_relation_proof':center_proof or 'not_uniquely_mapped; no nearby-center substitution',
                    'relate_bsp1_anchor':p.relate_bsp1.klu.idx if p.relate_bsp1 else None,
                    'features':dict(p.features.items()),'engine':PROFILE,'engine_commit':COMMIT,
                    'definition_mode':'pinned_native_rules','structure_scope':'native_stroke_points' if rank==1 else 'native_segment_points',
                    'lower_level_trigger':'not_proven','execution_allowed':False,
                    'rule_audit':{'passed':own_sure,'local_geometry_completed':own_sure,'evidence_available_at':record['confirmed'] or at,
                                  'missing':[] if own_sure else ['native_stroke_or_segment_is_provisional'],
                                  'scope':'native sure structure; cross-timeframe trigger not asserted'}}
            signals.append(signal);layers[rank-1]['signals'].append(signal)
    merged=[];fractals=[]
    for k in engine.lst:
        raw=k.lst;item={'id':f'cp:k:{raw[0].idx}:{raw[-1].idx}','index':k.idx,'start':raw[0].idx,'end':raw[-1].idx,
                       'high':k.high,'low':k.low,'components':[x.idx for x in raw],'closed':True}
        merged.append(item)
        if k.fx!=FX_TYPE.UNKNOWN:
            top=k.fx==FX_TYPE.TOP;pivot=k.get_high_peak_klu() if top else k.get_low_peak_klu()
            fractals.append({'id':f'cp:f:{pivot.idx}','kind':'top' if top else 'bottom','source':pivot.idx,
                             'price':k.high if top else k.low,'merged_index':k.idx})
    report={'schema':'chanlun.reference.v1','profile':PROFILE,'symbol':symbol,'period':period,'adjustment':adjustment,
            'source':source,'as_of':cutoff,'last_bar_at':bars[-1].closed_at,'config':SETTINGS,
            'input_sha256':input_sha256,'input_audit':{'state':'raw_observed_prefix','bars_used':len(bars)},
            'base':{'merged':merged,'fractals':fractals,'strokes':strokes,'segments':segments,'stroke_rejections':[]},
            'hierarchy':{'levels':layers,'base_units':[],'edges':[],'profile':PROFILE},'signals':signals,
            'divergence_comparisons':[],'feature_sequences':[],'center_evolution':[],'center_monitors':[],
            'construction_phases':[],'decompositions':[],'observed_signal_events':events,
            'certificate_audit':{'state':'native_engine','errors':[],'scope':'native engine assertions and adapter provenance'},
            'definition_mode':'pinned_native_feature_sequence','engine_commit':COMMIT,
            'approximation_loss':['Upstream pinned rule options are explicit; universal original-Chan equivalence is not proved.',
                                  'Native is_sure is local structure certainty, not a multi-timeframe trading trigger.',
                                  'Historical signals may be revised; observed event ledger records first-seen and withdrawn times.'],
            'strict_original_equivalence_proven':False,'execution_allowed':False,'signal_anchor_profile':'native_line_endpoint',
            'engine_provenance':{'url':'https://github.com/Vespa314/chan.py','commit':COMMIT,'license':'MIT'},
            'snapshot_semantics':'native engine computed every raw prefix; version observation times are not endpoint times'}
    return review_geometry(report)


def review_geometry(report):
    """Surface disagreements with an independent strict-extrema interpretation.

    This is a review criterion, not a proof that the upstream interpretation is
    universally wrong. Unreviewed parent structures cannot certify a signal.
    """
    fractals={f['source']:f for f in report['base']['fractals']};issues=[];bad_strokes=set();bad_segments=set()
    for s in report['base']['strokes']:
        reasons=[]
        if s['state']=='confirmed' and s['start'] in fractals and s['end'] in fractals:
            a,z=fractals[s['start']]['merged_index'],fractals[s['end']]['merged_index']
            part=report['base']['merged'][a:z+1]
            expected=(min(x['low'] for x in part),max(x['high'] for x in part))
            actual=(s['start_price'],s['end_price']) if s['direction']=='up' else (s['end_price'],s['start_price'])
            if actual!=expected:reasons.append('standard_interval_extrema_disagreement')
        s['review_issues']=reasons
        if reasons:bad_strokes.add(s['id']);issues.append({'id':s['id'],'kind':'stroke','reasons':reasons})
    for s in report['base']['segments']:
        part=[m for m in report['base']['strokes'] if s['start']<=m['start'] and m['end']<=s['end']]
        reasons=[]
        if s['state']=='confirmed' and part:
            vertices=[m['start_price'] for m in part]+[part[-1]['end_price']]
            actual=(s['start_price'],s['end_price']) if s['direction']=='up' else (s['end_price'],s['start_price'])
            if actual!=(min(vertices),max(vertices)):reasons.append('input_vertex_extrema_disagreement')
            if any(m['id'] in bad_strokes for m in part):reasons.append('contains_stroke_requiring_review')
        s['review_issues']=reasons
        if reasons:bad_segments.add(s['id']);issues.append({'id':s['id'],'kind':'segment','reasons':reasons})
    bad_centers=set()
    for layer in report.get('hierarchy',{}).get('levels',[]):
        bad_children=bad_strokes if layer['rank']==1 else bad_segments
        for center in layer['centers']:
            if any(component in bad_children for component in center.get('components',[])):
                center['review_issues']=['contains_component_requiring_review'];bad_centers.add(center['id'])
            else:center['review_issues']=[]
    for p in report['signals']:
        dependencies=[];start,end=p['terminal_range']
        for s in report['base']['strokes']:
            if s['start']<=end and s['end']>=start and s['id'] in bad_strokes:dependencies.append(s['id'])
        for s in report['base']['segments']:
            if s['start']<=end and s['end']>=start and s['id'] in bad_segments:dependencies.append(s['id'])
        if p.get('center_id') in bad_centers:dependencies.append(p['center_id'])
        if p.get('native_type','').startswith('3') and not p.get('center_id'):
            dependencies.append('native_center_relation_not_uniquely_mapped')
        p['review_dependencies']=dependencies
        if dependencies and p['state']!='invalidated':
            p['state']='needs_review';p['rule_audit']['passed']=False;p['rule_audit']['local_geometry_completed']=False
            p['rule_audit']['missing']=['independent_geometry_review_required']
    report['geometry_review']={'profile':'independent_strict_extrema_review_v1','issues':issues,
                               'stroke_issues':len(bad_strokes),'segment_issues':len(bad_segments),
                               'semantics':'strict-extrema interpretation disagreement; not an automatic original-theory verdict'}
    return report


def analyze_reference(symbol,period,bars,cutoff,input_sha256='',source='tdx',adjustment='unadjusted',*,observe=True):
    engine,selected,registry,events,first=run_reference(bars,period,observe,cutoff)
    report=make_report(engine,selected,registry,events,first,symbol,period,cutoff,source,adjustment,input_sha256)
    report['observation_mode']='every_closed_bar_prefix' if observe else 'final_cutoff_snapshot_no_historical_first_seen_claim'
    return report
