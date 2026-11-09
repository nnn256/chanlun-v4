"""V2 composed engine: recursive types, structural gates and causal triggers."""
from __future__ import annotations

import copy
from datetime import datetime,timedelta

from ..model import Config,time_value
from ..structure import merge_bars,fractals_at,strokes_at
from ..segments import build_segments
from ..signals import macd,attach_ranges
from .series import Series,validate_series,validate_multiframe,CN,PERIODS
from .recursive import base_units,build_hierarchy,audit_hierarchy
from .divergence import compare_divergences,make_signals,invalidated_by


def analyze(series:Series,cutoff:str,config=None,max_rank=3):
    config=config or Config();input_audit=validate_series(series,cutoff);selected=series.prefix(cutoff)
    bars=list(selected.bars)
    # Normalizing numerical precision is identical to v1's declared boundary.
    from ..model import Candle
    bars=[Candle(b.pos,*(round(v,config.price_decimals) for v in [b.open,b.high,b.low,b.close]),b.closed_at,b.volume,True) for b in bars]
    merged=merge_bars(bars,config);fractals=fractals_at(merged);strokes,rejected=strokes_at(fractals,config)
    histogram=macd(bars,config);segments=build_segments(strokes,0);attach_ranges(segments,bars,histogram)
    units=base_units(segments,len(bars)-1);hierarchy=build_hierarchy(units,max_rank,len(bars)-1)
    certificate_audit=audit_hierarchy(hierarchy,len(bars)-1)
    if certificate_audit['errors']:raise ValueError('internal recursive certificates failed: '+str(certificate_audit['errors'][:3]))
    all_comparisons=[];all_signals=[];children=units
    for layer in hierarchy['levels']:
        comparisons=compare_divergences(children,layer['centers'],bars,histogram,layer['rank'],len(bars)-1)
        signals=make_signals(children,layer['centers'],comparisons,bars,layer['rank'],histogram,
                             second_policy=config.second_policy,third_boundary=config.third_boundary)
        layer['comparisons']=comparisons;layer['signals']=signals
        all_comparisons.extend(comparisons);all_signals.extend(signals);children=layer['completed_types']
    return {'schema':'chanlun.recursive.v2','symbol':series.symbol,'period':series.period,'adjustment':series.adjustment,
            'as_of':cutoff,'last_bar_at':bars[-1].closed_at,'input_audit':input_audit,
            'base':{'merged':merged,'fractals':fractals,'strokes':strokes,'stroke_rejections':rejected,'segments':segments},
            'hierarchy':hierarchy,'certificate_audit':certificate_audit,'divergence_comparisons':all_comparisons,'signals':all_signals,
            'definition_mode':'recursive_completed_types_under_same_level_decomposition',
            'strict_original_equivalence_proven':False,'execution_allowed':False,
            'remaining_scope':['base segment boundaries inherit the published tool convention',
                               'same-level cuts complete partition units, not market reversals; opposite-center-break confirms turns conservatively',
                               'MACD force and numeric zero-axis tolerance remain auxiliary research conventions']}


def bar_time(series,index):
    return time_value(series.bars[index].closed_at)


def interval_reconciled(pair,start,end):
    overlapping=[x for x in pair['comparisons'] if start<=time_value(x['parent_close'])<=end]
    gaps=[x for x in pair['gaps'] if start<=time_value(x['parent_close'])<=end]
    covered=pair.get('matched_start') is not None and time_value(pair['matched_start'])<=start and time_value(pair['matched_end'])>=end
    return covered and bool(overlapping) and not gaps and all(x['state']=='matched' for x in overlapping)


def structural_confirmations(parent,trade,confirm,confirm_report,cutoff,price_tolerance=0.005):
    """Find opposite-center-break proofs inside/after the terminal movement.

    This selects a witnessed lower movement reversal, not another similarly
    named buy/sell candidate. Endpoints may precede completion; available time
    is carried independently.
    """
    lo=bar_time(trade,parent['terminal_range'][0]);anchor=bar_time(trade,parent['anchor'])
    desired='up' if parent['side']=='buy' else 'down';old_direction='down' if desired=='up' else 'up'
    matches=[]
    centers={c['id']:c for layer in confirm_report['hierarchy']['levels'] for c in layer['centers']+layer['movement_views']['centers']}
    units={u['id']:u for u in confirm_report['hierarchy']['base_units']}
    for layer in confirm_report['hierarchy']['levels']:
        units.update({u['id']:u for u in layer['completed_types']})
    for layer in confirm_report['hierarchy']['levels']:
        for unit in layer['movement_views']['completed_types']:
            proof=unit['completion']
            if unit['direction']!=old_direction or proof['opposite_side']!=parent['side']:continue
            center=centers[proof['evidence_center_id']]
            pivot=bar_time(confirm,unit['end']);center_begin=bar_time(confirm,center['start'])
            at=bar_time(confirm,unit['completed_at']);ret=units[proof['return_id']]
            # Daily endpoints are close timestamps, so the terminal date can
            # contain an earlier intraday extreme. Cover the last parent bar's
            # session rather than incorrectly demanding exact timestamp equality.
            anchor_day=anchor.astimezone(CN).date()
            pivot_day=pivot.astimezone(CN).date()
            in_anchor_bar=pivot_day==anchor_day if trade.period=='day' else anchor-timedelta(minutes=PERIODS[trade.period])<pivot<=anchor
            pivot_allowed=lo<=pivot<=anchor and in_anchor_bar
            price_matches=abs(unit['end_price']-parent['price'])<=price_tolerance
            if not pivot_allowed or not price_matches or center_begin<lo or at>time_value(cutoff):continue
            matches.append({'id':f"confirm:{confirm.period}:{unit['id']}",'unit_id':unit['id'],'center_id':center['id'],
                            'side':parent['side'],'pivot_time':pivot.isoformat(),'pivot_price':unit['end_price'],
                            'parent_pivot_price':parent['price'],'price_match_tolerance':price_tolerance,
                            'available_time':at.isoformat(),'return_end_time':bar_time(confirm,ret['end']).isoformat(),
                            'return_extreme':proof['return_extreme'],'boundary':proof['boundary'],
                            'terminal_range':[center['start'],ret['end']],
                            'rule':'completed_lower_movement_with_opposite_center_break_and_nonreturn',
                            'evidence':copy.deepcopy(proof)})
    return sorted(matches,key=lambda x:(time_value(x['available_time']),x['unit_id']))


def trigger_after(parent,confirmation,trigger,report,cutoff,parent_available):
    direction='up' if parent['side']=='buy' else 'down'
    not_before=max(time_value(confirmation['available_time']),parent_available)
    after_return=time_value(confirmation['return_end_time'])
    for stroke in report['base']['strokes']:
        if stroke['state']!='confirmed' or stroke['direction']!=direction:continue
        start=bar_time(trigger,stroke['start']);end=bar_time(trigger,stroke['end']);at=bar_time(trigger,stroke['basis_at'])
        # A new local reversal after the confirming return is a separate
        # trigger. A historical up-stroke already finished before confirmation
        # cannot retrospectively serve as a live trigger.
        if start<after_return or end<not_before or at<not_before or at>time_value(cutoff):continue
        return {'id':f"trigger:{trigger.period}:{stroke['id']}",'stroke_id':stroke['id'],'direction':direction,
                'start_time':start.isoformat(),'end_time':end.isoformat(),'available_time':at.isoformat(),
                'invalidation':parent['invalidation'],'rule':'new_confirmed_opposite_stroke_after_lower_nonreturn',
                'confirmation_basis':stroke['confirmation_basis'],'price':stroke['end_price']}
    return None


def multiframe(series:dict[str,Series],roles,cutoff,config=None,max_rank=3,*,price_tolerance=0.005,volume_tolerance=1.0):
    data_audit=validate_multiframe(series,roles,cutoff,price_tolerance=price_tolerance,volume_tolerance=volume_tolerance)
    if data_audit['state']=='blocked':
        return {'schema':'chanlun.multiframe.v2','as_of':cutoff,'state':'blocked_data_contract',
                'data_contract':data_audit,'signals':[],'execution_allowed':False}
    clipped={role:series[role].prefix(cutoff) for role in roles}
    reports={role:analyze(clipped[role],cutoff,config,max_rank) for role in roles}
    trade_role,confirm_role,trigger_role=roles
    trade,confirm,trigger=(clipped[role] for role in roles)
    outputs=[]
    for parent in reports[trade_role]['signals']:
        point={'parent_signal':parent['id'],'class':parent['class'],'rank':parent['rank'],'side':parent['side'],
               'trade_period':trade.period,'confirm_period':confirm.period,'trigger_period':trigger.period,
               'state':'waiting_lower_confirmation','parent':parent,'confirmation':None,'trigger':None,
               'strict_original_equivalence_proven':False,'execution_allowed':False}
        if parent['state']=='invalidated':
            point['state']='invalidated';outputs.append(point);continue
        anchor_time=bar_time(trade,parent['anchor'])
        invalidating=next((b for b in trigger.bars if time_value(b.closed_at)>anchor_time and invalidated_by(parent,b)),None)
        if invalidating is not None:
            point.update(state='invalidated',invalidation_evidence={'period':trigger.period,'closed_at':invalidating.closed_at,
                          'low':invalidating.low,'high':invalidating.high,'boundary':parent['invalidation']})
            outputs.append(point);continue
        start=bar_time(trade,parent['terminal_range'][0]);end=bar_time(trade,parent['terminal_range'][1])
        if not interval_reconciled(data_audit['pairs'][0],start,end):
            point['state']='blocked_terminal_history';outputs.append(point);continue
        confirmations=structural_confirmations(parent,trade,confirm,reports[confirm_role],cutoff,price_tolerance)
        for evidence in confirmations:
            cstart=bar_time(confirm,evidence['terminal_range'][0]);cend=bar_time(confirm,evidence['terminal_range'][1])
            if not interval_reconciled(data_audit['pairs'][1],cstart,cend):continue
            point['confirmation']=evidence;point['state']='waiting_trigger'
            trigger_evidence=trigger_after(parent,evidence,trigger,reports[trigger_role],cutoff,bar_time(trade,parent['available_at']))
            if trigger_evidence:
                point['trigger']=trigger_evidence;point['state']='structurally_triggered_under_profile'
                point['available_time']=trigger_evidence['available_time'];break
        outputs.append(point)
    return {'schema':'chanlun.multiframe.v2','as_of':cutoff,'state':'calculated','data_contract':data_audit,
            'levels':reports,'signals':outputs,'execution_allowed':False,
            'trigger_semantics':'independent completed lower movement reversal followed by a new confirmed finer stroke; not same-side label coincidence'}


def replay(series:Series,config=None,max_rank=3):
    """Reference version ledger: prefix recomputation, no historical backfill."""
    registry={};events=[];snapshot=None
    for index,bar in enumerate(series.bars):
        if not bar.closed:break
        clipped=Series(series.symbol,series.period,series.adjustment,series.bars[:index+1],series.source,series.input_sha256)
        snapshot=analyze(clipped,bar.closed_at,config,max_rank)
        objects=[]
        for layer in snapshot['hierarchy']['levels']:
            objects += [('type',u) for u in layer['completed_types']]
            objects += [('center',c) for c in layer['centers']]
        objects += [('signal',s) for s in snapshot['signals']]
        present=set()
        for kind,obj in objects:
            key=(kind,obj['id']);present.add(key);previous=registry.get(key)
            if previous is None or not previous['active'] or previous['value']!=obj:
                event='appeared' if previous is None else 'reappeared' if not previous['active'] else 'revised'
                revision=0 if previous is None else previous['revision']+1
                events.append({'kind':kind,'id':obj['id'],'event':event,'revision':revision,
                               'observed_index':index,'observed_at':bar.closed_at,'value':copy.deepcopy(obj)})
                registry[key]={'active':True,'revision':revision,'value':copy.deepcopy(obj)}
        for key,previous in registry.items():
            if previous['active'] and key not in present:
                previous['active']=False;previous['revision']+=1
                events.append({'kind':key[0],'id':key[1],'event':'withdrawn','revision':previous['revision'],
                               'observed_index':index,'observed_at':bar.closed_at})
    return snapshot,events


def replay_multiframe(series,roles,decision_times,config=None,max_rank=3,**tolerances):
    """Auditable multi-timeframe trigger-state ledger at supplied fine-bar closes.

    A caller choosing a subset must report that schedule. Each decision still
    receives all history up to its cutoff; final future structures are not
    reused. Newly observed versions keep the observation time, even if their
    structural evidence points to an earlier price bar.
    """
    fine=series[roles[2]];allowed={time_value(b.closed_at) for b in fine.bars if b.closed}
    last=None;registry={};events=[];snapshot=None
    for cutoff in decision_times:
        moment=time_value(cutoff)
        if moment not in allowed or last is not None and moment<=last:
            raise ValueError('decision times must be increasing closed finest bars')
        last=moment;snapshot=multiframe(series,roles,cutoff,config,max_rank,**tolerances)
        present=set()
        for point in snapshot['signals']:
            ident=point['parent_signal'];present.add(ident)
            body={'state':point['state'],'confirmation':point['confirmation'],'trigger':point['trigger'],
                  'invalidation_evidence':point.get('invalidation_evidence'),'available_time':point.get('available_time')}
            previous=registry.get(ident)
            if previous is None or not previous['active'] or previous['value']!=body:
                rev=0 if previous is None else previous['revision']+1
                events.append({'id':ident,'event':'first_observed' if previous is None else 'state_changed',
                               'revision':rev,'observed_at':cutoff,'state':point['state'],'value':copy.deepcopy(body)})
                registry[ident]={'active':True,'revision':rev,'value':copy.deepcopy(body)}
        for ident,previous in registry.items():
            if previous['active'] and ident not in present:
                previous['active']=False;previous['revision']+=1
                events.append({'id':ident,'event':'withdrawn','revision':previous['revision'],'observed_at':cutoff,
                               'reason':'candidate absent or data contract blocked at this cutoff'})
    return snapshot,events
