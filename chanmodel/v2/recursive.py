"""Recursive completed movement types, centers, and conservative completion.

L0 is an explicitly chosen segment boundary (lesson 63). Higher centers admit
only completed L(n-1) movement types. A movement is conservatively completed
after the latest center breaks against its direction and a completed opposite
return stays outside. This is a declared delayed completion profile, not a
claim that all original decomposition conventions have one implementation.
"""
from __future__ import annotations

import copy
import math

PROFILE='opposite_center_break_completion_v1'
CUT_PROFILE='same_level_completed_center_cut_v1'


def validate_units(units,rank,as_of):
    ids=set();previous=None
    for unit in units:
        if unit['id'] in ids:raise ValueError('duplicate movement identity')
        ids.add(unit['id'])
        if unit['rank']!=rank or unit['state']!='complete':raise ValueError('centers accept completed units of one exact child rank')
        if rank==0 and unit['kind']!='base_segment':raise ValueError('L0 must be the declared base-segment boundary')
        if rank>0 and unit['kind'] not in {'consolidation','trend'}:raise ValueError('higher centers require movement TYPES, not segments or rectangles')
        if not unit.get('completion') or unit['completion'].get('rule') not in {'base_segment_confirmed',PROFILE,CUT_PROFILE}:
            raise ValueError('missing/unknown completion certificate')
        if not unit['start']<unit['end']<=unit['completed_at']<=as_of:raise ValueError('movement completion uses future or invalid indices')
        if not all(math.isfinite(unit[k]) for k in ['start_price','end_price','low','high']) or unit['low']>unit['high']:
            raise ValueError('invalid movement range')
        if not unit['low']<=min(unit['start_price'],unit['end_price'])<=max(unit['start_price'],unit['end_price'])<=unit['high']:
            raise ValueError('movement range does not include endpoints')
        if unit['direction'] not in {'up','down'}:
            raise ValueError('direction inconsistent with endpoints')
        if unit['kind']!='consolidation' and (((unit['end_price']>unit['start_price']) != (unit['direction']=='up')) or unit['end_price']==unit['start_price']):
            raise ValueError('direction inconsistent with endpoints')
        if rank>0 and (not unit.get('center_ids') or len(unit.get('components',[]))<3):
            raise ValueError('completed movement type lacks its center/components')
        if previous and (unit['start']!=previous['end'] or unit['start_price']!=previous['end_price'] or unit['direction']==previous['direction']):
            raise ValueError('recursive child units must form a contiguous alternating partition')
        if previous and unit['completed_at']<previous['completed_at']:
            raise ValueError('a partition successor cannot be available before its predecessor boundary')
        previous=unit


def base_units(segments,as_of):
    units=[]
    for segment in segments:
        if segment['state']!='confirmed':break
        if segment['basis_at']>as_of:raise ValueError('segment basis is in the future')
        if not segment.get('confirmation'):raise ValueError('base segment missing confirmation path')
        known_at=max(segment['basis_at'],units[-1]['completed_at'] if units else 0)
        units.append({'id':f"u0:{segment['start']}:{segment['end']}",'rank':0,'kind':'base_segment',
                      'direction':segment['direction'],'start':segment['start'],'end':segment['end'],
                      'start_price':segment['start_price'],'end_price':segment['end_price'],
                      'low':segment['actual_low'],'high':segment['actual_high'],
                      'state':'complete','completed_at':known_at,'available_at':known_at,
                      'components':[segment['id']],'center_ids':[],
                      'completion':{'rule':'base_segment_confirmed','path':segment['confirmation']['path'],
                                    'evidence_id':segment['id'],'raw_basis_at':segment['basis_at'],
                                    'predecessor_boundary_available_at':units[-1]['completed_at'] if units else None,
                                    'available_at':known_at},
                      'boundary_assumption':'confirmed base segments are irreducible units at the selected resolution',
                      'definition_sources':[63,67,71,78,81]})
    validate_units(units,0,as_of)
    return units


def centers_from_units(units,parent_rank,as_of):
    """Fixed core; explicit member, leave and first-return certificates."""
    validate_units(units,parent_rank-1,as_of)
    centers=[];cursor=0
    while cursor+2<len(units):
        seed=units[cursor:cursor+3]
        zd=max(x['low'] for x in seed);zg=min(x['high'] for x in seed)
        if zd>=zg:
            cursor+=1;continue
        members=list(range(cursor,cursor+3));attempts=[];leave=None;departure=None;j=cursor+3
        while j<len(units):
            unit=units[j]
            if leave is None:
                exceeds=unit['high']>zg if unit['direction']=='up' else unit['low']<zd
                if exceeds:leave=j
                else:members.append(j)
                j+=1;continue
            outgoing=units[leave];up=outgoing['direction']=='up';value=unit['low'] if up else unit['high']
            relation='outside' if (value>zg if up else value<zd) else 'touch' if value==(zg if up else zd) else 'crossed' if (value<zd if up else value>zg) else 'inside'
            attempt={'leave_index':leave,'return_index':j,'leave_id':outgoing['id'],'return_id':unit['id'],
                     'side':'buy' if up else 'sell','relation':relation,'boundary':zg if up else zd,
                     'return_extreme':value,'available_at':max(x['completed_at'] for x in seed+[outgoing,unit]),
                     'return_completed':True,'return_rank':parent_rank-1,'equality_policy':'touch_is_return'}
            attempts.append(attempt)
            if relation=='outside':departure=attempt;break
            if relation=='crossed':members.append(leave);leave=j
            else:members.extend([leave,j]);leave=None
            j+=1
        selected=[units[k] for k in members]
        ident=f"c{parent_rank}:{seed[0]['start']}:{seed[2]['end']}"
        centers.append({'id':ident,'rank':parent_rank,'child_rank':parent_rank-1,
                        'state':'broken' if departure else 'leaving' if leave is not None else 'extended' if len(members)>3 else 'formed',
                        'start':seed[0]['start'],'seed_end':seed[-1]['end'],'end':selected[-1]['end'],
                        'zd':zd,'zg':zg,'dd':min(x['low'] for x in selected),'gg':max(x['high'] for x in selected),
                        'seed_ids':[x['id'] for x in seed],'seed_indices':list(range(cursor,cursor+3)),
                        'member_ids':[x['id'] for x in selected],'member_indices':members,
                        'formed_at':max(x['completed_at'] for x in seed),
                        'available_at':max([x['completed_at'] for x in selected]+([departure['available_at']] if departure else [])),
                        'attempts':attempts,'departure':departure,
                        'certificate':{'predicate':'three_contiguous_completed_sublevel_movement_ranges_overlap',
                                       'passed':True,'sublevel_types_verified':parent_rank>1,
                                       'base_boundary_used':parent_rank==1,'definition_sources':[17,18,20,63]},
                        'pending_leave_id':units[leave]['id'] if leave is not None and not departure else None})
        if not departure:break
        cursor=departure['return_index']
    return centers


def center_relation(a,b):
    if a['rank']!=b['rank']:raise ValueError('center comparison requires equal structural rank')
    if b['dd']>a['gg']:return 'up'
    if b['gg']<a['dd']:return 'down'
    if b['zd']>a['zg'] or b['zg']<a['zd']:return 'expansion'
    return 'overlap'


def shifted_center(center,offset):
    result=copy.deepcopy(center)
    result['seed_indices']=[k+offset for k in center['seed_indices']]
    result['member_indices']=[k+offset for k in center['member_indices']]
    for attempt in result['attempts']:
        attempt['leave_index']+=offset;attempt['return_index']+=offset
    # deepcopy preserves departure's alias to the matching attempt. Do not
    # apply the offset to it twice.
    return result


def partition_types(units,parent_rank,as_of):
    """Emit only witnessed complete types; unresolved tails never feed upward."""
    validate_units(units,parent_rank-1,as_of)
    completed=[];pending=[];centers=[];cursor=0
    while cursor+2<len(units):
        tail=units[cursor:];local=centers_from_units(tail,parent_rank,as_of)
        if not local:break
        # An initial unidentifiable prefix is exposed, never silently skipped.
        if local[0]['seed_indices'][0]!=0:
            pending.append({'rank':parent_rank,'state':'unresolved_prefix','start':tail[0]['start'],
                            'end':tail[local[0]['seed_indices'][0]-1]['end'],'reason':'first three children do not establish a center at this partition boundary'})
            if not completed and cursor==0:
                cursor=local[0]['seed_indices'][0]
                continue
            centers.extend(shifted_center(c,cursor) for c in local);break
        direction=tail[0]['direction'];accepted=[];finish=None;expansion=False
        for center in local:
            if accepted:
                relation=center_relation(accepted[-1],center)
                if relation!=direction:
                    expansion=True
                    pending.append({'rank':parent_rank,'state':'expansion_pending' if relation in {'expansion','overlap'} else 'direction_change_pending','start':tail[0]['start'],'end':center['end'],
                                    'center_ids':[c['id'] for c in accepted]+[center['id']],
                                    'center_relation':relation,
                                    'reason':'outer ranges do not prove continued same-direction trend; cannot certify this type'})
                    break
            accepted.append(center)
            departure=center['departure']
            against=departure and ((direction=='up' and departure['side']=='sell') or (direction=='down' and departure['side']=='buy'))
            if not against:continue
            last_before=departure['leave_index']-1
            eligible=[i for i in range(2,last_before+1,2) if tail[i]['end']>=center['seed_end']]
            if not eligible:continue
            endpoint=(max if direction=='up' else min)(eligible,key=lambda i:tail[i]['end_price'])
            end_price=tail[endpoint]['end_price'];start_price=tail[0]['start_price']
            if (end_price<=start_price if direction=='up' else end_price>=start_price):continue
            # No center used to prove the type may extend beyond its endpoint.
            if any(c['end']>tail[endpoint]['end'] for c in accepted):continue
            finish=(endpoint,departure);break
        if expansion:
            centers.extend(shifted_center(c,cursor) for c in local);break
        if finish is None:
            centers.extend(shifted_center(c,cursor) for c in local)
            pending.append({'rank':parent_rank,'state':'developing','start':tail[0]['start'],'end':tail[-1]['end'],
                            'direction':direction,'center_ids':[c['id'] for c in accepted],
                            'reason':'no completed opposite-side center break and return certificate'})
            break
        endpoint,proof=finish;group=tail[:endpoint+1]
        at=max([u['completed_at'] for u in group]+[proof['available_at']])
        ident=f"view{parent_rank}:{group[0]['start']}:{group[-1]['end']}"
        kind='trend' if len(accepted)>=2 else 'consolidation'
        unit={'id':ident,'rank':parent_rank,'kind':kind,'direction':direction,'state':'complete',
              'start':group[0]['start'],'end':group[-1]['end'],'start_price':group[0]['start_price'],'end_price':group[-1]['end_price'],
              'low':min(u['low'] for u in group),'high':max(u['high'] for u in group),
              'components':[u['id'] for u in group],'center_ids':[c['id'] for c in accepted],
              'completed_at':at,'available_at':at,
              'completion':{'rule':PROFILE,'evidence_center_id':accepted[-1]['id'],
                            'opposite_side':proof['side'],'leave_id':proof['leave_id'],'return_id':proof['return_id'],
                            'leave_index':proof['leave_index']+cursor,'return_index':proof['return_index']+cursor,
                            'return_completed':True,'boundary':proof['boundary'],'return_extreme':proof['return_extreme'],
                            'available_at':at,'source_rules':[18,20]},
              'trend_certificate':{'same_rank_centers':True,'outer_ranges_strictly_separated':kind=='trend',
                                   'ordered_center_ids':[c['id'] for c in accepted]},
              'definition_sources':[17,18,20,63], 'completion_profile_is_conservative':True}
        completed.append(unit)
        # Store local center indices in the canonical child sequence.
        for c in accepted:
            centers.append(shifted_center(c,cursor))
        cursor+=endpoint+1
    if cursor<len(units) and not pending:
        pending.append({'rank':parent_rank,'state':'insufficient_children','start':units[cursor]['start'],'end':units[-1]['end'],
                        'child_count':len(units)-cursor})
    validate_units(completed,parent_rank,as_of)
    return {'rank':parent_rank,'completed_types':completed,'pending':pending,
            'centers':centers,'completion_profile':PROFILE,'all_children_assigned':cursor==len(units)}


def decompose_same_level(units,parent_rank,as_of):
    """Lesson-38 same-level partition: complete a one-center consolidation.

    All children are completed types (except the declared L0 boundary). Cuts
    require a genuine center, alternating continuity and a consistent endpoint.
    Three arbitrary bars/strokes cannot enter this recursion. A completed cut
    is explicitly NOT a reversal or proof that price will change direction.
    """
    validate_units(units,parent_rank-1,as_of)
    result=[];certified_centers=[];cursor=0;unassigned=[]
    while cursor+2<len(units):
        found=None
        for seed_start in range(cursor,len(units)-2):
            seed=units[seed_start:seed_start+3]
            zd=max(x['low'] for x in seed);zg=min(x['high'] for x in seed)
            if zd>=zg:continue
            endpoint=seed_start+2
            if (endpoint-cursor)%2: endpoint+=1
            if endpoint>=len(units):break
            found=(seed_start,endpoint,zd,zg);break
        if found is None:
            if not result:
                unassigned.append(units[cursor]['id']);cursor+=1;continue
            break
        seed_start,endpoint,zd,zg=found;seed=units[seed_start:seed_start+3];group=units[cursor:endpoint+1]
        center_id=f"pc{parent_rank}:{seed[0]['start']}:{seed[-1]['end']}"
        center={'id':center_id,'rank':parent_rank,'child_rank':parent_rank-1,'state':'partition_cut',
                'start':seed[0]['start'],'seed_end':seed[-1]['end'],'end':group[-1]['end'],
                'zd':zd,'zg':zg,'dd':min(x['low'] for x in group[seed_start-cursor:]),'gg':max(x['high'] for x in group[seed_start-cursor:]),
                'seed_ids':[x['id'] for x in seed],'seed_indices':list(range(seed_start,seed_start+3)),
                'member_ids':[x['id'] for x in group[seed_start-cursor:]],'member_indices':list(range(seed_start,endpoint+1)),
                'formed_at':max(x['completed_at'] for x in seed),'available_at':max(x['completed_at'] for x in group),
                'certificate':{'predicate':'three_contiguous_completed_sublevel_movement_ranges_overlap','passed':True,
                               'sublevel_types_verified':parent_rank>1,'base_boundary_used':parent_rank==1,'definition_sources':[17,18,38,63]},
                'attempts':[],'departure':None,'pending_leave_id':None}
        certified_centers.append(center)
        result.append({'id':f"u{parent_rank}:{group[0]['start']}:{group[-1]['end']}",'rank':parent_rank,
                       'kind':'consolidation','direction':group[0]['direction'],'state':'complete',
                       'start':group[0]['start'],'end':group[-1]['end'],'start_price':group[0]['start_price'],'end_price':group[-1]['end_price'],
                       'net_direction':'up' if group[-1]['end_price']>group[0]['start_price'] else 'down' if group[-1]['end_price']<group[0]['start_price'] else 'flat',
                       'direction_semantics':'first_and_last_child_orientation; a consolidation can have opposite or flat net displacement',
                       'low':min(x['low'] for x in group),'high':max(x['high'] for x in group),
                       'components':[x['id'] for x in group],'center_ids':[center_id],
                       'completed_at':max(x['completed_at'] for x in group),'available_at':max(x['completed_at'] for x in group),
                       'completion':{'rule':CUT_PROFILE,'center_id':center_id,'available_at':max(x['completed_at'] for x in group),
                                     'children_completed':True,'source_rules':[18,38,63],'is_turn_confirmation':False},
                       'definition_sources':[17,18,38,63],'decomposition_mode':'same_level_partition'} )
        cursor=endpoint+1
    validate_units(result,parent_rank,as_of)
    return {'types':result,'centers':certified_centers,'unassigned_prefix_ids':unassigned,
            'pending_tail_ids':[x['id'] for x in units[cursor:]],'profile':CUT_PROFILE}


def build_hierarchy(base,max_rank,as_of):
    if not 1<=max_rank<=8:raise ValueError('max_rank must be 1..8')
    levels=[];children=base;edges=[]
    for rank in range(1,max_rank+1):
        partition=decompose_same_level(children,rank,as_of)
        views=partition_types(children,rank,as_of)
        centers=centers_from_units(children,rank,as_of)
        level={'rank':rank,'completed_types':partition['types'],'centers':centers,
               'partition_centers':partition['centers'],'input_ids':[u['id'] for u in children],
               'pending':{'unassigned_prefix_ids':partition['unassigned_prefix_ids'],'pending_tail_ids':partition['pending_tail_ids']},
               'movement_views':views,'completion_profile':CUT_PROFILE}
        levels.append(level)
        for unit in level['completed_types']:
            edges.extend({'parent':unit['id'],'child':child,'relation':'movement_component'} for child in unit['components'])
        for center in level['centers']+level['partition_centers']:
            edges.extend({'parent':center['id'],'child':child,'relation':'center_seed'} for child in center['seed_ids'])
        children=level['completed_types']
        if len(children)<3:break
    return {'base_units':base,'levels':levels,'edges':edges,
            'profile':CUT_PROFILE,'turn_confirmation_profile':PROFILE,'period_labels_are_not_rank_labels':True,
            'strict_structure_scope':'center input type/rank/completion/overlap predicates; movement completion follows the declared conservative profile',
            'universal_original_equivalence_proven':False}


def audit_hierarchy(hierarchy,as_of):
    """Recompute certificate predicates from referenced objects, not booleans."""
    arena={u['id']:u for u in hierarchy['base_units']};errors=[];centers_checked=types_checked=0
    for layer in hierarchy['levels']:
        rank=layer['rank'];parents={c['id']:c for c in layer['partition_centers']}
        for center in layer['centers']+layer['partition_centers']:
            try:
                seed=[arena[key] for key in center['seed_ids']]
                validate_units(seed,rank-1,as_of)
                if len(seed)!=3 or center['zd']!=max(u['low'] for u in seed) or center['zg']!=min(u['high'] for u in seed) or center['zd']>=center['zg']:
                    raise ValueError('center core is not the actual three-child intersection')
                if center['formed_at']!=max(u['completed_at'] for u in seed):raise ValueError('center formation is backdated')
                centers_checked+=1
            except (KeyError,ValueError) as exc:errors.append({'id':center['id'],'error':str(exc)})
        for unit in layer['completed_types']:
            try:
                children=[arena[key] for key in unit['components']]
                validate_units(children,rank-1,as_of)
                center=parents[unit['center_ids'][0]]
                if not set(center['seed_ids'])<=set(unit['components']):raise ValueError('type center contains foreign children')
                if unit['start']!=children[0]['start'] or unit['end']!=children[-1]['end']:raise ValueError('type endpoints do not cover component partition')
                if unit['low']!=min(u['low'] for u in children) or unit['high']!=max(u['high'] for u in children):raise ValueError('type range differs from actual component ranges')
                if unit['completed_at']!=max(u['completed_at'] for u in children):raise ValueError('partition completion uses wrong knowledge time')
                if unit['completion']['rule']!=CUT_PROFILE or unit['completion'].get('is_turn_confirmation') is not False:raise ValueError('partition cut must not claim a reversal')
                types_checked+=1
            except (KeyError,ValueError) as exc:errors.append({'id':unit['id'],'error':str(exc)})
            arena[unit['id']]=unit
    return {'state':'passed' if not errors else 'failed','centers_checked':centers_checked,'completed_types_checked':types_checked,
            'errors':errors,'scope':'referenced-child/rank/continuity/completion/range predicates under the declared decomposition profile'}
