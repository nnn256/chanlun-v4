"""Witnessed timeframe alignment, interval nesting and small-to-large studies.

Every accepted link needs matching security/adjustment, a reconciled OHLCV
interval and named structural evidence. Timeframe labels do not certify ranks.
"""
from __future__ import annotations

from datetime import timedelta
from .model import time_value
from .v2.series import PERIODS, CN, reconcile, validate_series
from .v2.engine import structural_confirmations, trigger_after, interval_reconciled, bar_time
from .v2.divergence import invalidated_by


def pair_contracts(series, cutoff, price_tolerance=.005, volume_tolerance=100):
    pairs={}
    for upper in series:
        for lower in series:
            if PERIODS[upper]<=PERIODS[lower] or PERIODS[upper]%PERIODS[lower]:continue
            p=reconcile(series[upper],series[lower],cutoff,price_tolerance=price_tolerance,volume_tolerance=volume_tolerance)
            p['default_one_share']=reconcile(series[upper],series[lower],cutoff,price_tolerance=price_tolerance,volume_tolerance=1)
            pairs[upper+'>'+lower]=p
    return pairs


def period_contains(series,index,instant):
    end=bar_time(series,index);point=instant if hasattr(instant,'tzinfo') else time_value(instant)
    if series.period=='day':return end.astimezone(CN).date()==point.astimezone(CN).date()
    return end-timedelta(minutes=PERIODS[series.period])<point<=end


def lower_comparison(parent, lower_report, upper_series, lower_series, pair, price_tolerance=.005):
    """A nested weakening range is evidence, not standalone turn confirmation."""
    start=bar_time(upper_series,parent['terminal_range'][0]);end=bar_time(upper_series,parent['terminal_range'][1])
    if not interval_reconciled(pair,start,end):return None,'parent_interval_not_reconciled'
    candidates=[]
    for comp in lower_report['divergence_comparisons']:
        if comp['state']=='not_supported' or comp['side']!=parent['side'] or not comp['gates']['macd_force_weaker']:continue
        if not all(comp['gates'][key] for key in ['same_child_rank','children_complete','same_direction','new_extreme']):continue
        begin=bar_time(lower_series,comp['terminal_range'][0]);finish=bar_time(lower_series,comp['terminal_range'][1])
        if not start<=begin<=finish<=end:continue
        if not period_contains(upper_series,parent['anchor'],finish):continue
        if abs(comp['price']-parent['price'])>price_tolerance:continue
        candidates.append(comp)
    if not candidates:return None,'no_matching_completed_lower_force_comparison'
    chosen=min(candidates,key=lambda c:(c['terminal_range'][1]-c['terminal_range'][0],-c['rank']))
    return chosen,None


def interval_nesting(reports,series,chains,pairs):
    results=[]
    for chain in chains:
        if any(p not in series for p in chain):continue
        for root in reports[chain[0]]['divergence_comparisons']:
            if root['state']=='not_supported' or not root['gates']['macd_force_weaker'] or not root['gates']['new_extreme']:continue
            parent=root;period=chain[0]
            nodes=[{'period':period,'comparison_id':root['id'],'rank':root['rank'],
                    'range':root['terminal_range'],'price':root['price'],'side':root['side'],
                    'state':root['state'],'available_time':series[period].bars[root['available_at']].closed_at}]
            stop='lowest_supplied_resolution_reached'
            for lower in chain[1:]:
                child,reason=lower_comparison(parent,reports[lower],series[period],series[lower],pairs[period+'>'+lower])
                if child is None:stop=reason;break
                nodes.append({'period':lower,'comparison_id':child['id'],'rank':child['rank'],'range':child['terminal_range'],
                              'price':child['price'],'side':child['side'],'state':child['state'],
                              'available_time':series[lower].bars[child['available_at']].closed_at})
                parent=child;period=lower
            results.append({'id':'nest:'+':'.join(chain)+':'+root['id'],'chain':list(chain),'root_period':chain[0],
                            'root_anchor':root['anchor'],'side':root['side'],'price':root['price'],'nodes':nodes,
                            'matched_depth':len(nodes),'state':'nested_evidence' if len(nodes)>1 else 'waiting_lower_evidence',
                            'stop_reason':stop,'available_time':max((n['available_time'] for n in nodes),key=time_value),
                            'execution_allowed':False,'unique_exact_turn_proven':False,'sources':[37,43,61,62]})
    return results


def join_triple(reports,series,roles,pairs,cutoff):
    up,low,fine=roles;trade,confirm,trigger=(series[p] for p in roles);out=[]
    for parent in reports[up]['signals']:
        point={'id':'link:'+':'.join(roles)+':'+parent['id'],'parent_signal':parent['id'],
               'class':parent['class'],'rank':parent['rank'],'side':parent['side'],'price':parent['price'],
               'anchor':parent['anchor'],'trade_period':up,'confirm_period':low,'trigger_period':fine,
               'state':'waiting_lower_confirmation','confirmation':None,'trigger':None,'execution_allowed':False}
        if parent['state']=='invalidated':point['state']='invalidated';out.append(point);continue
        anchor=bar_time(trade,parent['anchor'])
        broken=next((x for x in trigger.bars if time_value(x.closed_at)>anchor and invalidated_by(parent,x)),None)
        if broken is not None:
            point.update(state='invalidated',invalidation_time=broken.closed_at,
                         invalidation_evidence={'period':fine,'boundary':parent['invalidation'],'low':broken.low,'high':broken.high})
            out.append(point);continue
        lo=bar_time(trade,parent['terminal_range'][0]);hi=bar_time(trade,parent['terminal_range'][1])
        if not interval_reconciled(pairs[up+'>'+low],lo,hi):
            point['state']='blocked_terminal_history_or_OHLCV';out.append(point);continue
        available=bar_time(trade,parent['available_at'])
        evidence=structural_confirmations(parent,trade,confirm,reports[low],cutoff)
        for proof in evidence:
            a=bar_time(confirm,proof['terminal_range'][0]);b=bar_time(confirm,proof['terminal_range'][1])
            if not interval_reconciled(pairs[low+'>'+fine],a,b):continue
            point.update(confirmation=proof,state='waiting_trigger')
            local=trigger_after(parent,proof,trigger,reports[fine],cutoff,available)
            if local:
                limit=parent['invalidation'];anchor=bar_time(trade,parent['anchor'])
                broken=next((x for x in trigger.bars if time_value(x.closed_at)>anchor and invalidated_by(parent,x)),None)
                if broken:
                    point.update(state='invalidated',invalidation_time=broken.closed_at)
                else:point.update(state='structurally_triggered_under_profile',trigger=local,available_time=local['available_time'])
                break
        out.append(point)
    return out


def small_to_large(reports,series,upper,lower,pair,cutoff,diagnostics=None):
    """Named lower divergence + opposite break + retest, then upper turn review."""
    high,low=series[upper],series[lower];segments=reports[upper]['base']['segments'];result=[]
    for i,segment in enumerate(segments):
        side='buy' if segment['direction']=='down' else 'sell'
        parent={'side':side,'price':segment['end_price'],'anchor':segment['end'],'terminal_range':[segment['start'],segment['end']]}
        child,reason=lower_comparison(parent,reports[lower],high,low,pair)
        if child is None:
            if diagnostics is not None:diagnostics.append({'upper_period':upper,'lower_period':lower,'parent_segment_id':segment['id'],
                'anchor':segment['end'],'state':'not_established','missing_evidence':reason})
            continue
        # Force alone is insufficient: require an independently completed
        # lower movement with opposite-center-break and completed nonreturn.
        proofs=structural_confirmations(parent,high,low,reports[lower],cutoff)
        if not proofs:
            if diagnostics is not None:diagnostics.append({'upper_period':upper,'lower_period':lower,'parent_segment_id':segment['id'],
                'anchor':segment['end'],'state':'not_established','lower_comparison_id':child['id'],
                'missing_evidence':'no_matching_completed_opposite_center_break_and_nonreturn'})
            continue
        proof=proofs[0]
        upper_turn=segments[i+1] if i+1<len(segments) and segments[i+1]['state']=='confirmed' else None
        record={'id':f'small-large:{upper}:{lower}:{segment["id"]}:{child["id"]}',
                'upper_period':upper,'lower_period':lower,'side':side,'price':segment['end_price'],
                'anchor':segment['end'],'parent_segment_id':segment['id'],'lower_comparison_id':child['id'],
                'lower_reversal':proof,'state':'higher_segment_turn_witnessed' if upper_turn else 'candidate_waiting_higher_structure',
                'available_time':max([proof['available_time'],high.bars[upper_turn['basis_at']].closed_at if upper_turn else proof['available_time']],key=time_value),
                'higher_turn_id':upper_turn['id'] if upper_turn else None,'higher_trend_reversal_proven':False,
                'sources':[53,102],'execution_allowed':False}
        result.append(record)
        if upper_turn and i+2<len(segments):
            ret=segments[i+2]
            held=ret['actual_low']>segment['actual_low'] if side=='buy' else ret['actual_high']<segment['actual_high']
            if ret['state']=='confirmed' and held:
                record['second_53']={'class':'second_'+side,'rank':1,'side':side,'anchor':ret['end'],
                                     'price':ret['end_price'],'available_time':max([record['available_time'],high.bars[ret['basis_at']].closed_at],key=time_value),
                                     'first_post_turn_retest':True,'first_same_rank_divergence_required':False,
                                     'state':'small_to_large_second_candidate','execution_allowed':False,
                                     'invalidation':ret['actual_low'] if side=='buy' else ret['actual_high']}
    return result


def analyze_links(reports, series, cutoff, volume_tolerance=100):
    series={p:s.prefix(cutoff) for p,s in series.items()}
    for s in series.values():validate_series(s,cutoff)
    pairs=pair_contracts(series,cutoff,volume_tolerance=volume_tolerance)
    chains=[('day','60m','15m','5m','1m'),('day','30m','5m','1m'),('60m','15m','5m','1m'),('30m','5m','1m'),('15m','5m','1m'),('5m','1m')]
    triples=[('day','60m','15m'),('day','30m','5m'),('60m','15m','5m'),('30m','5m','1m'),('15m','5m','1m')]
    points=[point for roles in triples if all(p in series for p in roles) for point in join_triple(reports,series,roles,pairs,cutoff)]
    small=[];diagnostics=[]
    for upper,lower in [('day','60m'),('day','30m'),('60m','15m'),('30m','5m'),('15m','5m'),('5m','1m')]:
        if upper in series and lower in series:small+=small_to_large(reports,series,upper,lower,pairs[upper+'>'+lower],cutoff,diagnostics)
    return {'profile':'observed_multi_timeframe_evidence_v1','as_of':cutoff,'pairs':pairs,'signals':points,
            'interval_nesting':interval_nesting(reports,series,chains,pairs),'small_to_large':small,
            'small_to_large_audit':diagnostics,
            'roles_are_sampling_timeframes_not_proven_Chan_ranks':True,
            'volume_tolerance_shares':volume_tolerance,'default_tolerance_preserved_separately':1,
            'execution_allowed':False}
