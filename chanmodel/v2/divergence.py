"""Structure-gated force comparisons; indicator evidence never invents a trend."""
from __future__ import annotations

import math

from .recursive import center_relation,validate_units


def force(unit,bars,histogram):
    begin,end=unit['start'],unit['end']
    if not 0<=begin<end<len(bars):raise ValueError('force unit outside data bounds')
    sign=1 if unit['direction']=='up' else -1
    area=math.fsum(max(0.0,sign*h['histogram']) for h in histogram[begin:end+1])
    amplitude=abs(unit['end_price']-unit['start_price'])
    return {'unit_id':unit['id'],'direction':unit['direction'],'rank':unit['rank'],
            'range':[begin,end],'macd_area':area,'price_amplitude':amplitude,
            'price_speed_per_bar':amplitude/(end-begin),'force_definition':'same-direction MACD area; amplitude/speed are corroborating measurements'}


def compare_divergences(units,centers,bars,histogram,rank,as_of,*,zero_axis_fraction=0.1):
    if not 0<=zero_axis_fraction<=1:raise ValueError('zero-axis fraction must be in [0,1]')
    validate_units(units,rank-1,as_of);comparisons=[]
    for ci,center in enumerate(centers):
        entering=center['seed_indices'][0]-1
        if entering<0:continue
        a=units[entering];fa=force(a,bars,histogram)
        last_center=centers[ci-1] if ci else None
        for attempt in center['attempts']:
            c=units[attempt['leave_index']]
            if c['direction']!=a['direction']:continue
            fc=force(c,bars,histogram)
            direction=c['direction']
            trend=last_center is not None and center_relation(last_center,center)==direction
            first_complete_center=last_center is not None and last_center['seed_end']<=a['start']
            same_rank=a['rank']==c['rank']==rank-1
            outside=attempt['relation']=='outside'
            extreme=c['low']<a['low'] if direction=='down' else c['high']>a['high']
            weak=fa['macd_area']>0 and fc['macd_area']<fa['macd_area']
            # Lesson 24 calls for the center to pull DIF/DEA near zero. A numeric
            # tolerance is an explicit research convention, never an original
            # universal threshold or a confirmation on its own.
            scale=max((abs(h['dif'])+abs(h['dea']) for h in histogram[a['start']:a['end']+1]),default=0)
            reset=min((abs(h['dif'])+abs(h['dea']) for h in histogram[center['start']:center['end']+1]),default=math.inf)
            reset_ok=scale>0 and reset<=scale*zero_axis_fraction
            gates={'same_child_rank':same_rank,'children_complete':True,'same_direction':True,
                   'two_separated_same_rank_centers':trend,'prior_center_precedes_comparison_a':first_complete_center,
                   'new_extreme':extreme,'completed_leave_and_nonreturn':outside,
                   'macd_force_weaker':weak,'center_macd_near_zero':reset_ok}
            structural=all(gates[k] for k in ['same_child_rank','children_complete','same_direction','two_separated_same_rank_centers','prior_center_precedes_comparison_a','new_extreme','completed_leave_and_nonreturn'])
            status='trend_divergence_supported_candidate' if structural and weak and reset_ok else 'trend_force_weakening_observed' if structural and weak else 'consolidation_force_weakening' if not trend and weak else 'not_supported'
            comparisons.append({'id':f"d{rank}:{center['id']}:{a['id']}:{c['id']}",'rank':rank,'a':fa,'c':fc,
                                'center_id':center['id'],'prior_center_id':last_center['id'] if last_center else None,
                                'state':status,'gates':gates,'blocked_gates':[k for k,v in gates.items() if not v],
                                'available_at':max(a['completed_at'],c['completed_at'],center['available_at'],attempt['available_at'],
                                                   last_center['available_at'] if last_center else 0),
                                'terminal_range':[c['start'],c['end']], 'anchor':c['end'],'side':'buy' if direction=='down' else 'sell',
                                'price':c['end_price'],'invalidation':c['low'] if direction=='down' else c['high'],
                                'structural_context_passed':structural,'lower_level_turn_required':True,
                                'zero_axis_fraction':zero_axis_fraction,'zero_axis_scale':scale,'zero_axis_min_distance':reset,
                                'indicators_are_auxiliary':True,'definition_sources':[24,27,37],
                                'universal_original_divergence_proven':False})
    return comparisons


def invalidated_by(point, bar):
    value = bar.low if point['side']=='buy' else bar.high
    boundary = point['invalidation']
    operator = point.get('invalidation_operator', 'le' if point['side']=='buy' else 'ge')
    if operator not in {'le','lt','ge','gt'}:raise ValueError('unknown invalidation operator')
    return {'le':value<=boundary,'lt':value<boundary,'ge':value>=boundary,'gt':value>boundary}[operator]


def make_signals(units,centers,comparisons,bars,rank,histogram=None,*,second_policy='original_101',third_boundary='strict_outside'):
    if second_policy not in {'original_101','conservative'}:raise ValueError('unknown second_policy')
    if third_boundary not in {'strict_outside','not_break'}:raise ValueError('unknown third_boundary')
    signals=[]

    def add(kind,side,unit,center,at,limit,**evidence):
        anchor=unit['end']
        inclusive = not (kind.startswith('third_') and third_boundary=='not_break')
        operator = ('le' if inclusive else 'lt') if side=='buy' else ('ge' if inclusive else 'gt')
        broken=any(invalidated_by({'side':side,'invalidation':limit,'invalidation_operator':operator},b) for b in bars[anchor+1:])
        point={'id':f"b{rank}:{kind}:{center['id']}:{anchor}",'class':kind,'side':side,'rank':rank,
               'anchor':anchor,'price':unit['end_price'],'terminal_range':[unit['start'],unit['end']],
               'center_id':center['id'],'zd':center['zd'],'zg':center['zg'],'available_at':at,
               'invalidation':limit,'invalidation_operator':operator,
               'state':'invalidated' if broken else 'awaiting_lower_confirmation',
               'structure_scope':'base_boundary' if rank==1 else 'recursive_completed_types',
               'center_definition_predicate_passed':center['certificate']['passed'],
               'lower_level_trigger':'missing','execution_allowed':False,
               'second_policy':second_policy,'third_boundary':third_boundary,**evidence}
        signals.append(point);return point

    for center in centers:
        departure=center['departure']
        if third_boundary=='not_break':
            departure=next((a for a in center['attempts'] if a['relation'] in {'outside','touch'}),None)
        if departure:
            ret=units[departure['return_index']]
            add('third_'+departure['side'],departure['side'],ret,center,departure['available_at'],departure['boundary'],
                leave_id=departure['leave_id'],return_id=departure['return_id'],
                first_qualifying_return=True,reference=[18,20],equality_policy=third_boundary)
    centers_by_id={c['id']:c for c in centers}
    for comparison in comparisons:
        if comparison['state']!='trend_divergence_supported_candidate':continue
        center=centers_by_id[comparison['center_id']];side=comparison['side']
        unit_index=next(i for i,u in enumerate(units) if u['id']==comparison['c']['unit_id'])
        unit=units[unit_index]
        first=add('first_'+side,side,unit,center,comparison['available_at'],comparison['invalidation'],
                  comparison_id=comparison['id'],reference=[17,24,27,37])
        if unit_index+2>=len(units):continue
        rebound,ret=units[unit_index+1:unit_index+3]
        if ret['direction']!=unit['direction'] or rebound['direction']==unit['direction']:continue
        survived=ret['low']>unit['low'] if side=='buy' else ret['high']<unit['high']
        # Weak/equal retests require their own force comparison; they are not
        # accepted merely by enabling a named "101" flag.
        weak_retest=None
        if histogram is not None and second_policy=='original_101':
            a_force,c_force=force(unit,bars,histogram),force(ret,bars,histogram)
            if a_force['rank']==c_force['rank'] and a_force['macd_area']>0 and c_force['macd_area']<a_force['macd_area']:
                weak_retest={'id':f"retest:{unit['id']}:{ret['id']}",'kind':'first_post_turn_retest_force_comparison',
                             'a':a_force,'c':c_force,'completed_return':True,'force_is_auxiliary':True}
        if not survived and weak_retest is None:continue
        add('second_'+side,side,ret,center,max(first['available_at'],ret['completed_at'],rebound['completed_at']),
            ret['low'] if side=='buy' else ret['high'],first_signal=first['id'],first_retest=True,
            retest_mode='held_first_extreme' if survived else 'weak_retest_101',weak_retest_evidence=weak_retest,
            reference=[17,53,101])
    return signals
