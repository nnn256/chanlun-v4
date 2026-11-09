"""Full, source-traceable study layers under one explicit decomposition profile.

The output contains calculated objects and missing evidence separately.  Layer
availability never implies that an event occurred or that a theory was proved.
"""
from __future__ import annotations

from dataclasses import replace
import copy
import math

from .model import Config, time_value
from .study_profile import analyze_period, CONFIG
from .structure import merge_bars, fractals_at, strokes_at
from .segments import build_segments, append_feature, feature
from .signals import attach_ranges
from .v2.recursive import base_units, build_hierarchy, audit_hierarchy, center_relation
from .v2.divergence import compare_divergences, make_signals

PROFILE = 'source_declared_complete_study_layers_v1'
SIGNAL_NAMES = {'first_buy':'一买','second_buy':'二买','third_buy':'三买',
                'first_sell':'一卖','second_sell':'二卖','third_sell':'三卖'}


def event_time(bars, index):
    return bars[min(max(0, index), len(bars)-1)].closed_at if bars else None


def feature_layers(strokes, segments, bars):
    results=[]
    for seg in segments:
        stop=seg['confirm_move'] if seg['confirm_move'] is not None else len(strokes)-1
        stack=[];raw=[]
        for i in range(seg['start_move']+1, min(stop+1, len(strokes)), 2):
            item=feature(strokes[i], i)
            raw.append({**item, 'stroke_id':strokes[i]['id'], 'available_at':strokes[i]['basis_at']})
            append_feature(stack, item, seg['direction'])
        results.append({'id':'features:'+seg['id'], 'segment_id':seg['id'], 'direction':seg['direction'],
                        'start':seg['start'], 'end':seg['end'], 'raw':raw, 'standardized':stack,
                        'confirmation':copy.deepcopy(seg.get('confirmation')),
                        'pending_hypotheses':copy.deepcopy(seg.get('pending_hypotheses', [])),
                        'available_at':seg['basis_at'], 'available_time':event_time(bars, seg['basis_at']),
                        'rule_sources':[67,71,78,81], 'state':seg['state']})
    return results


def center_evolution(centers, children, parent_centers=()):
    events=[];by_id={u['id']:u for u in children}
    for c in centers:
        events.append({'id':'formed:'+c['id'],'kind':'formed','center_id':c['id'],'rank':c['rank'],
                       'anchor':c['seed_end'],'available_at':c['formed_at'],'zd':c['zd'],'zg':c['zg']})
        for uid in c['member_ids'][3:]:
            u=by_id[uid]
            events.append({'id':'extend:'+c['id']+':'+uid,'kind':'extension','center_id':c['id'],
                           'rank':c['rank'],'anchor':u['end'],'available_at':max(c['formed_at'],u['completed_at']),
                           'component':uid,'zd':c['zd'],'zg':c['zg']})
        for j,a in enumerate(c['attempts']):
            u=by_id[a['return_id']]
            events.append({'id':f'return:{c["id"]}:{j}','kind':'nonreturn' if a['relation']=='outside' else 'return',
                           'center_id':c['id'],'rank':c['rank'],'anchor':u['end'],'available_at':a['available_at'],
                           'side':a['side'],'relation':a['relation'],'boundary':a['boundary'],'extreme':a['return_extreme'],
                           'leave_id':a['leave_id'],'return_id':a['return_id']})
        if len(c['member_ids'])>=9:
            ninth=by_id[c['member_ids'][8]]
            # Count alone is a warning. Promotion needs an actual next-rank
            # center with completed child-type evidence and enclosing interval.
            witness=next((p for p in parent_centers if p['rank']==c['rank']+1 and
                          p['start']<=c['start'] and p['end']>=ninth['end'] and
                          p['certificate']['sublevel_types_verified']),None)
            events.append({'id':'nine:'+c['id'],'kind':'nine_component_upgrade','center_id':c['id'],'rank':c['rank'],
                           'anchor':ninth['end'],'available_at':max(c['formed_at'],ninth['completed_at'],witness['formed_at'] if witness else 0),
                           'component_count':len(c['member_ids']), 'state':'parent_center_witnessed' if witness else 'awaiting_parent_type_overlap',
                           'parent_center_id':witness['id'] if witness else None,'count_alone_is_confirmation':False})
    for a,b in zip(centers,centers[1:]):
        relation=center_relation(a,b)
        core_overlap=max(a['zd'],b['zd'])<min(a['zg'],b['zg'])
        outer_overlap=max(a['dd'],b['dd'])<=min(a['gg'],b['gg'])
        events.append({'id':'relation:'+a['id']+':'+b['id'], 'kind':'newborn' if relation in {'up','down'} else 'expansion',
                       'rank':a['rank'],'left':a['id'],'right':b['id'],'anchor':b['seed_end'],
                       'available_at':max(a['available_at'],b['formed_at']),
                       'relation':relation,'core_overlap':core_overlap,'outer_overlap':outer_overlap,
                       'state':'separated_same_rank_centers' if relation in {'up','down'} else 'higher_rank_reconstruction_required'})
    return events


def center_monitor(center, children):
    """Lesson 92: Zn is the midpoint of a named completed sublevel range."""
    lookup={x['id']:x for x in children};z=(center['zd']+center['zg'])/2
    points=[]
    for i,uid in enumerate(center['member_ids']):
        u=lookup[uid];zn=(u['low']+u['high'])/2
        points.append({'unit_id':uid,'anchor':u['end'],'available_at':max(center['formed_at'],u['completed_at']),
                       'zn':zn,'z':z,'delta':zn-z,'state':'stronger' if zn>z else 'weaker' if zn<z else 'balanced',
                       'outside_core':zn>center['zg'] or zn<center['zd'],'requires_rank_review':i>=8})
    return {'id':'monitor:'+center['id'],'center_id':center['id'],'rank':center['rank'],
            'start':center['start'],'end':center['end'],'a':center['zd'],'b':center['zg'],'z':z,'points':points,
            'does_not_confirm_third_point':True,'definition_sources':[92]}


def construction_phases(signals):
    """Lesson 108's first-point -> same-center third-point process, both sides."""
    phases=[]
    for first in signals:
        if not first['class'].startswith('first_'):continue
        later=sorted((p for p in signals if p['center_id']==first['center_id'] and p['rank']==first['rank'] and
                      p['class'].startswith('third_') and p['available_at']>first['available_at']),key=lambda p:p['available_at'])
        finish=later[0] if later else None
        state=('completed_under_profile' if finish['side']==first['side'] else 'failed_opposite_departure') if finish else 'under_construction'
        if first['state']=='invalidated' and not finish:state='first_extreme_invalidated'
        phases.append({'id':'construction:'+first['id'],'rank':first['rank'],'kind':'bottom' if first['side']=='buy' else 'top',
                       'start':first['anchor'],'end':finish['anchor'] if finish else None,
                       'available_at':max(first['available_at'],finish['available_at'] if finish else 0),
                       'first_id':first['id'],'third_id':finish['id'] if finish else None,
                       'center_id':first['center_id'],'state':state,'definition_sources':[108]})
    return phases


def enrich_comparisons(comparisons, histogram):
    for comp in comparisons:
        for role in ['a','c']:
            obj=comp[role];start,end=obj['range'];values=histogram[start:end+1]
            obj['dif_extreme']=max((abs(h['dif']) for h in values),default=0)
            obj['duration_bars']=end-start+1
        area=comp['a']['macd_area'];comp['force_ratio']=comp['c']['macd_area']/area if area else None
        comp['dif_ratio']=comp['c']['dif_extreme']/comp['a']['dif_extreme'] if comp['a']['dif_extreme'] else None
        comp['force_ratio_is_auxiliary']=True
    return comparisons


def analyze_complete(symbol,period,bars,cutoff,input_sha256,*,source='tdx',adjustment='unadjusted',config=None,max_rank=8):
    config=config or replace(CONFIG,second_policy='original_101')
    base=analyze_period(symbol,period,bars,cutoff,input_sha256,source,adjustment=adjustment,config=config)
    selected=[b for b in bars if b.closed and time_value(b.closed_at)<=time_value(cutoff)]
    selected=[replace(b,**{k:round(getattr(b,k),config.price_decimals) for k in ['open','high','low','close']}) for b in selected]
    units=base_units(base['base']['segments'],len(selected)-1)
    hierarchy=build_hierarchy(units,max_rank,len(selected)-1)
    audit=audit_hierarchy(hierarchy,len(selected)-1)
    if audit['errors']:raise ValueError('Full hierarchy predicate audit failed')
    all_comps=[];all_signals=[];evolution=[];monitors=[];decompositions=[];children=units
    for i,layer in enumerate(hierarchy['levels']):
        rank=layer['rank'];centers=layer['centers']
        comps=enrich_comparisons(compare_divergences(children,centers,selected,base['macd'],rank,len(selected)-1),base['macd'])
        points=make_signals(children,centers,comps,selected,rank,base['macd'],
                            second_policy=config.second_policy,third_boundary=config.third_boundary)
        for point in points:
            point.update(label=SIGNAL_NAMES[point['class']], event_time=event_time(selected,point['anchor']),
                         available_time=event_time(selected,point['available_at']),
                         policy='original_101_force_evidence' if point.get('retest_mode')=='weak_retest_101' else 'standard_structure',
                         definition_mode='structure_proxy' if rank==1 else 'declared_recursive_profile',
                         lower_level_trigger='missing',confirmed_at=None,execution_allowed=False)
        layer.update(comparisons=comps,signals=points)
        all_comps+=comps;all_signals+=points
        higher=hierarchy['levels'][i+1]['centers'] if i+1<len(hierarchy['levels']) else []
        evolution+=center_evolution(centers,children,higher)
        monitors.extend(center_monitor(c,children) for c in centers)
        decompositions.extend({**copy.deepcopy(u),'view':'same_level_partition'} for u in layer['completed_types'])
        decompositions.extend({**copy.deepcopy(u),'view':'opposite_center_break'} for u in layer['movement_views']['completed_types'])
        for pending in layer['movement_views']['pending']:
            decompositions.append({**copy.deepcopy(pending),'id':f'pending:{rank}:{pending["start"]}',
                                   'kind':'transition','view':'movement_pending','available_at':len(selected)-1})
        children=layer['completed_types']
    loose,unused=strokes_at(base['base']['fractals'],replace(config,stroke_mode='loose'))
    phases=construction_phases(all_signals)
    base.update(profile=PROFILE, hierarchy=hierarchy, certificate_audit=audit,divergence_comparisons=all_comps,
                signals=all_signals, feature_sequences=feature_layers(base['base']['strokes'],base['base']['segments'],selected),
                center_evolution=evolution,center_monitors=monitors,construction_phases=phases,
                decompositions=decompositions,loose_stroke_comparison=loose,
                scope='all computable layers under a declared profile; no fabricated missing events',
                layer_catalog=[
                    {'key':'merged','name':'包含与合并K线','count':len(base['base']['merged'])},
                    {'key':'fractals','name':'全部顶底分型','count':len(base['base']['fractals'])},
                    {'key':'strokes','name':'严格笔','count':len(base['base']['strokes'])},
                    {'key':'loose_strokes','name':'宽松笔口径对照','count':len(loose)},
                    {'key':'segments','name':'线段与候选','count':len(base['base']['segments'])},
                    {'key':'features','name':'特征序列与两种确认','count':len(base['base']['segments'])},
                    {'key':'centers','name':'各秩中枢核心与波动范围','count':sum(len(x['centers']) for x in hierarchy['levels'])},
                    {'key':'evolution','name':'延伸新生扩展与升级核验','count':len(evolution)},
                    {'key':'types','name':'同级分解与趋势盘整过渡','count':len(decompositions)},
                    {'key':'divergences','name':'趋势与盘整力度比较','count':len(all_comps)},
                    {'key':'signals','name':'六类点及101回试','count':len(all_signals)},
                    {'key':'monitor','name':'92课中枢震荡监视器','count':sum(len(x['points']) for x in monitors)},
                    {'key':'construction','name':'108课顶底构造','count':len(phases)}])
    return base


def trace_segment_revisions(bars, start_index=0):
    """Causal prefix replay. Changes are witnessed, never inferred from final lines."""
    usable=[b for b in bars if b.closed];registry={};events=[];previous_tail=None
    for index in range(max(0,start_index-1),len(usable)):
        prefix=usable[:index+1];merged=merge_bars(prefix,CONFIG)
        fractals=fractals_at(merged);strokes,_=strokes_at(fractals,CONFIG);segments=build_segments(strokes)
        present={x['id']:x for x in segments}
        if index>=start_index:
            for ident,value in present.items():
                old=registry.get(ident)
                if old==value:continue
                event='appeared' if old is None else 'confirmed' if old['state']!='confirmed' and value['state']=='confirmed' else 'revised'
                events.append({'id':ident,'event':event,'anchor':value['end'],'price':value['end_price'],
                               'observed_index':index,'observed_at':usable[index].closed_at,
                               'basis_at':value['basis_at'],'state':value['state']})
            for ident,old in registry.items():
                if ident not in present:
                    events.append({'id':ident,'event':'withdrawn','anchor':old['end'],'price':old['end_price'],
                                   'observed_index':index,'observed_at':usable[index].closed_at,'state':old['state']})
            tail=segments[-1] if segments else None
            if previous_tail and tail and previous_tail['start']==tail['start'] and previous_tail['end']!=tail['end']:
                had_candidate=bool(previous_tail.get('pending_hypotheses'))
                events.append({'id':tail['id'],'event':'candidate_cancelled_and_recovered' if had_candidate else 'endpoint_extended',
                               'anchor':previous_tail['end'],'price':previous_tail['end_price'],
                               'replacement_anchor':tail['end'],'replacement_price':tail['end_price'],
                               'observed_index':index,'observed_at':usable[index].closed_at,
                               'rebuild_features':had_candidate,'previous_hypotheses':previous_tail.get('pending_hypotheses',[])})
        registry=copy.deepcopy(present);previous_tail=copy.deepcopy(segments[-1]) if segments else None
    return {'start_index':start_index,'end_index':len(usable)-1,'prefixes_evaluated':max(0,len(usable)-start_index),
            'schedule':'each closed bar in declared window; every evaluation uses all preceding source data',
            'events':events,'future_data_reused':False}
