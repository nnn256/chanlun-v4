"""Every-bar, prefix-causal Chan signal replay with explicit original-rule gates.

Only append operations enter the live state. Incremental merging/fractals are
checked against the existing batch definitions; strokes and segments are
recomputed on changed inputs. Higher structures rebuild only when completed
base units change. Price invalidation is still checked on every closed bar.
"""
from __future__ import annotations
from dataclasses import replace
import copy
import hashlib
import json
from .model import Candle, Config
from .structure import contains, local_direction, merge_pair, fractals_at, strokes_at
from .segments import build_segments
from .signals import attach_ranges
from .v2.recursive import base_units, build_hierarchy, audit_hierarchy
from .v2.divergence import compare_divergences, make_signals,invalidated_by

CONFIG=Config(stroke_mode='strict',macd_multiplier=2.0,second_policy='conservative')
PROFILE='original_clause_gated_causal_confirmation_v1'


def sign(obj):
    return hashlib.sha256(json.dumps(obj,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


class StreamingStructure:
    def __init__(self,config=CONFIG):
        self.config=config;self.bars=[];self.merged=[];self.fractals=[];self.strokes=[];self.segments=[];self.histogram=[]
        self.fast=self.slow=None;self.dea=0.;self.stroke_sig=None;self.completed_sig=None;self.range_cache={}
        self.structure_rebuilds=0;self.stroke_rebuilds=0;self.segment_rebuilds=0

    def append(self,bar):
        if bar.pos!=len(self.bars) or not bar.closed:raise ValueError('Append one next closed bar only')
        if self.bars and bar.closed_at<=self.bars[-1].closed_at:raise ValueError('Time must strictly increase')
        self.bars.append(bar);i=bar.pos;c=self.config
        if self.fast is None:self.fast=self.slow=bar.close
        self.fast+=2/(c.macd_fast+1)*(bar.close-self.fast);self.slow+=2/(c.macd_slow+1)*(bar.close-self.slow)
        dif=self.fast-self.slow;self.dea+=2/(c.macd_signal+1)*(dif-self.dea)
        self.histogram.append({'dif':dif,'dea':self.dea,'histogram':c.macd_multiplier*(dif-self.dea)})
        current={'start':i,'end':i,'basis_at':i,'high':round(bar.high,c.price_decimals),'low':round(bar.low,c.price_decimals),
                 'high_source':i,'low_source':i,'open':bar.open,'close':bar.close,'closed':True,'components':[i]}
        while self.merged and contains(self.merged[-1],current):
            left=self.merged.pop();direction=local_direction(self.merged[-1] if self.merged else None,left,current)
            current=merge_pair(left,current,direction)
        changed=len(self.merged);current.update(index=changed,id=f'k:{current["start"]}:{current["end"]}')
        self.merged.append(current)
        # Only windows touching the changed suffix can differ.
        first_center=max(1,changed-1)
        prior=self.fractals
        keep=len(prior)
        while keep and prior[keep-1]['merged_index']>=first_center:keep-=1
        new=fractals_at(self.merged[max(0,first_center-1):])
        offset=max(0,first_center-1)
        for f in new:f['merged_index']+=offset
        updated=prior[:keep]+new
        if updated==prior:return False
        self.fractals=updated;self.stroke_rebuilds+=1
        strokes,_=strokes_at(updated,c)
        signature=tuple((s['start'],s['end'],s['start_price'],s['end_price'],s['basis_at'],s['state']) for s in strokes)
        self.strokes=strokes
        if signature==self.stroke_sig:return False
        self.stroke_sig=signature;self.segment_rebuilds+=1
        self.segments=build_segments(strokes)
        for segment in self.segments:
            key=(segment['start'],segment['end'])
            if key not in self.range_cache:
                one=copy.deepcopy(segment);attach_ranges([one],self.bars,self.histogram)
                one['actual_low']=round(one['actual_low'],c.price_decimals)
                one['actual_high']=round(one['actual_high'],c.price_decimals)
                self.range_cache[key]={k:one[k] for k in ['actual_low','actual_high','actual_low_source','actual_high_source','macd_area','amplitude','bars_count','slope']}
            segment.update(self.range_cache[key])
        completed=tuple((s['id'],s['basis_at'],s['actual_low'],s['actual_high'],s['confirmation']['path']) for s in self.segments if s['state']=='confirmed')
        if completed==self.completed_sig:return False
        self.completed_sig=completed;self.structure_rebuilds+=1;return True

    def snapshot(self,max_rank=8):
        if not self.bars:raise ValueError('No source bars')
        now=len(self.bars)-1;units=base_units(self.segments,now);hierarchy=build_hierarchy(units,max_rank,now)
        audit=audit_hierarchy(hierarchy,now)
        if audit['errors']:raise ValueError('Recursive certificate error')
        points=[];comparisons=[];children=units
        for layer in hierarchy['levels']:
            comp=compare_divergences(children,layer['centers'],self.bars,self.histogram,layer['rank'],now)
            signals=make_signals(children,layer['centers'],comp,self.bars,layer['rank'],self.histogram,
                                 second_policy=self.config.second_policy,third_boundary=self.config.third_boundary)
            # Lesson 53 distinguishes a type's decomposition endpoint from
            # its internal price extreme. Buy/sell anchors use the actual
            # extremum; completion remains at the later witnessed boundary.
            for point in signals:
                start,end=point['terminal_range'];part=self.bars[start:end+1]
                pivot=(min if point['side']=='buy' else max)(part,key=lambda b:b.low if point['side']=='buy' else b.high)
                old_anchor=point['anchor'];point['decomposition_end']=old_anchor
                point['anchor']=pivot.pos;point['price']=pivot.low if point['side']=='buy' else pivot.high
                point['identity_semantics']='stable decomposition identity; actual price anchor stored separately'
                point['extreme_evidence']={'range':[start,end],'source_index':pivot.pos,'price':point['price'],
                                           'kind':'lowest_low' if point['side']=='buy' else 'highest_high','references':[21,53]}
                if not point['class'].startswith('third_'):point['invalidation']=point['price']
                broken=any(b.low<=point['invalidation'] if point['side']=='buy' else b.high>=point['invalidation'] for b in self.bars[pivot.pos+1:])
                point['state']='invalidated' if broken else 'awaiting_lower_confirmation'
            layer.update(comparisons=comp,signals=signals)
            comparisons+=comp;points+=signals;children=layer['completed_types']
        return {'base':{'merged':self.merged,'fractals':self.fractals,'strokes':self.strokes,'segments':self.segments},
                'hierarchy':hierarchy,'signals':points,'divergence_comparisons':comparisons,
                'certificate_audit':audit,'as_of_index':now,'as_of':self.bars[-1].closed_at}


def source_rule_audit(point,report):
    """Recompute gates from named objects; never upgrade a label alone.

    'Confirmed' uses the declared same-level partition and base convention.
    strict_original_equivalence_proven remains false even when these clauses pass.
    """
    rank=point['rank'];layers=report['hierarchy']['levels'];layer=next(x for x in layers if x['rank']==rank)
    children=report['hierarchy']['base_units'] if rank==1 else next(x for x in layers if x['rank']==rank-1)['completed_types']
    lookup={u['id']:u for u in children};center=next(c for c in layer['centers'] if c['id']==point['center_id'])
    terminal=next((u for u in children if [u['start'],u['end']]==point['terminal_range']),None)
    terminal_index=children.index(terminal) if terminal else -1
    opposite=children[terminal_index+1] if terminal is not None and terminal_index+1<len(children) else None
    seed=[lookup[x] for x in center['seed_ids']]
    gates={'center_positive_overlap':max(u['low'] for u in seed)<min(u['high'] for u in seed),
           'three_contiguous_children':all(a['end']==b['start'] and a['end_price']==b['start_price'] for a,b in zip(seed,seed[1:])),
           'same_child_rank':all(u['rank']==rank-1 for u in seed),
           'completed_child_types':rank>1 and all(u['kind'] in {'trend','consolidation'} and u['state']=='complete' for u in seed),
           'terminal_is_completed_type':terminal is not None and rank>1 and terminal['state']=='complete' and terminal['kind'] in {'trend','consolidation'},
           'price_equals_actual_terminal_extreme':terminal is not None and abs(point['price']-terminal['low' if point['side']=='buy' else 'high'])<=1e-7,
           'terminal_direction_matches_side':terminal is not None and terminal['direction']==('down' if point['side']=='buy' else 'up'),
           'opposite_child_boundary_confirmed':opposite is not None and opposite['direction']!=terminal['direction'] and opposite['state']=='complete',
           'signal_not_invalidated_at_observation':point['state']!='invalidated',
           'all_evidence_within_prefix':point['available_at']<=report['as_of_index'] and center['available_at']<=report['as_of_index']}
    extreme=point.get('extreme_evidence')
    if extreme:
        gates['price_anchor_is_terminal_extreme']=extreme['range']==point['terminal_range'] and extreme['source_index']==point['anchor'] and extreme['price']==point['price']
        gates['anchor_inside_completed_terminal']=point['terminal_range'][0]<=point['anchor']<=point['terminal_range'][1]
    refs=[17,18,20,21,38,63];extra={}
    if point['class'].startswith('third_'):
        dep=center['departure'];leave=lookup.get(dep['leave_id']) if dep else None;ret=lookup.get(dep['return_id']) if dep else None
        gates.update(completed_departure_and_first_return=bool(dep and dep['return_completed'] and ret and ret['id']==terminal['id']),
                     same_rank_leave_return=bool(leave and ret and leave['rank']==ret['rank']==rank-1),
                     first_return_strictly_outside=bool(ret and (ret['low']>center['zg'] if point['side']=='buy' else ret['high']<center['zd'])),
                     equality_policy_explicit=point.get('equality_policy')=='strict_outside')
        if dep:extra={'leave_id':dep['leave_id'],'return_id':dep['return_id']}
    elif point['class'].startswith('first_'):
        comp=next(c for c in report['divergence_comparisons'] if c['id']==point['comparison_id'])
        gates.update({'divergence_'+k:v for k,v in comp['gates'].items()})
        # The formal 'c' must contain its own lower-level center organization.
        internal=next((l for l in layers if l['rank']==rank-1),None)
        within=[c for c in internal['centers'] if terminal and terminal['start']<=c['start'] and c['end']<=terminal['end']] if internal else []
        gates['terminal_sublevel_context_present']=len(within)>=1
        refs+=[24,27,37];extra={'comparison_id':comp['id'],'sublevel_center_ids':[c['id'] for c in within]}
    else:
        first=next((s for s in report['signals'] if s['id']==point.get('first_signal')),None)
        gates['prior_first_context_present']=first is not None
        gates['first_post_turn_retest']=point.get('first_retest') is True
        gates['conservative_extreme_held']=point.get('retest_mode')=='held_first_extreme'
        gates['first_source_context_passed']=bool(first and source_rule_audit(first,report)['structural_context_passed'])
        refs+=[53,101]
    structural={k:v for k,v in gates.items() if k!='signal_not_invalidated_at_observation'}
    at=max(point['available_at'],center['formed_at'],terminal['completed_at'] if terminal else 0,opposite['completed_at'] if opposite else 0)
    return {'profile':PROFILE,'passed':all(gates.values()),'structural_context_passed':all(structural.values()),
            'gates':gates,'missing':[k for k,v in gates.items() if not v],
            'evidence_available_at':at,'terminal_type':terminal['id'] if terminal else None,
            'opposite_boundary_type':opposite['id'] if opposite else None,'references':sorted(set(refs)),**extra,
            'strict_original_equivalence_proven':False,
            'remaining_assumption':'completed types follow declared lesson-38 partition; L0 inherits segment conventions'}


def abcd_objects(report):
    """Both requested conventions. d is an explicit continuation label, not a theorem."""
    output=[];units=report['hierarchy']['base_units']
    for rank in range(0,len(report['hierarchy']['levels'])+1):
        if rank:
            layer=next((l for l in report['hierarchy']['levels'] if l['rank']==rank),None)
            if not layer:break
            units=layer['completed_types']
        for start in range(0,len(units),4):
            group=units[start:start+4]
            if not group:continue
            output.append({'id':f'abcd:L{rank}:{group[0]["start"]}','mode':'four_consecutive_same_rank',
                           'rank':rank,'complete':len(group)==4,'letters':[dict(label='abcd'[j],**copy.deepcopy(u)) for j,u in enumerate(group)],
                           'start':group[0]['start'],'end':group[-1]['end'],'available_at':max(u['completed_at'] for u in group),
                           'label_semantics':'a/b/c/d are four successive objects at one rank; no standalone ABCD trade pattern assumed'})
    children=report['hierarchy']['base_units']
    for layer in report['hierarchy']['levels']:
        by_id={u['id']:u for u in children};centers={c['id']:c for c in layer['centers']}
        for cmp in layer['comparisons']:
            B=centers[cmp['center_id']];A=centers.get(cmp['prior_center_id']);b=by_id[cmp['a']['unit_id']];c=by_id[cmp['c']['unit_id']]
            ci=children.index(c);d=children[ci+1] if ci+1<len(children) else None
            ai=A['seed_indices'][0]-1 if A else -1;a=children[ai] if ai>=0 else None
            letters=[]
            for label,obj,kind in [('a',a,'move'),('A',A,'center'),('b',b,'move'),('B',B,'center'),('c',c,'move'),('d',d,'continuation')]:
                if obj:letters.append({'label':label,'object_kind':kind,**copy.deepcopy(obj)})
            output.append({'id':'original:'+cmp['id'],'mode':'a+A+b+B+c_then_d','rank':layer['rank'],
                           'letters':letters,'start':min(x['start'] for x in letters),'end':max(x['end'] for x in letters),
                           'available_at':max([cmp['available_at']]+[u['completed_at'] for u in [a,b,c,d] if u]),
                           'comparison_id':cmp['id'],'state':cmp['state'],'sources':[24,33,37],
                           'missing_labels':[x for x in ['a','A','b','B','c','d'] if x not in [y['label'] for y in letters]],
                           'label_semantics':'original uses a+A+b+B+c; A/B are centers; d labels the observed next move only'})
        children=layer['completed_types']
    return output


class SignalLedger:
    def __init__(self,period):
        self.period=period;self.records={};self.events=[];self.confirmations=[];self.last_report=None

    def invalidate(self,bar):
        events=[]
        for rec in self.records.values():
            if rec['state'] not in {'candidate','confirmed'}:continue
            point=rec['point'];limit=point['invalidation']
            if bar.pos<=point['anchor']:continue
            if (bar.low<=limit if point['side']=='buy' else bar.high>=limit):
                rec['state']='invalidated';rec['invalidated_at']=bar.closed_at
                events.append(self.emit(rec,'invalidated',bar,{'boundary':limit,'bar_low':bar.low,'bar_high':bar.high}))
        return events

    def emit(self,rec,event,bar,extra=None):
        item={'sequence':len(self.events),'period':self.period,'signal_id':rec['id'],'event':event,
              'observed_index':bar.pos,'observed_at':bar.closed_at,'class':rec['point']['class'],
              'rank':rec['point']['rank'],'side':rec['point']['side'],'anchor':rec['point']['anchor'],
              'anchor_time':rec['point'].get('event_time'),'price':rec['point']['price'],
              'invalidation':rec['point']['invalidation'],'confirmed_at':rec.get('confirmed_at'),**(extra or {})}
        self.events.append(item);return item

    def observe(self,report,bar,bars):
        self.last_report=report;present=set();changes=[]
        for p in report['signals']:
            p=copy.deepcopy(p);p['event_time']=bars[p['anchor']].closed_at
            ident=p['id'];present.add(ident);audit=source_rule_audit(p,report)
            if ident not in self.records:
                rec={'id':ident,'point':p,'first_seen_at':bar.closed_at,'state':'candidate','confirmed_at':None,'audit':audit}
                self.records[ident]=rec;changes.append(self.emit(rec,'candidate_observed',bar,{'missing':audit['missing']}))
            rec=self.records[ident]
            old_point=rec['point'];was_confirmed=rec['state']=='confirmed'
            geometry_changed=any(old_point.get(k)!=p.get(k) for k in ['anchor','price','terminal_range','invalidation'])
            rec['point']=p;rec['audit']=audit
            if was_confirmed and p['state']!='invalidated' and (geometry_changed or not audit['structural_context_passed']):
                rec['state']='withdrawn';rec['withdrawn_at']=bar.closed_at
                changes.append(self.emit(rec,'withdrawn',bar,{'reason':'confirmed_geometry_changed' if geometry_changed else 'original_clause_no_longer_passes'}))
                continue
            if rec['state']=='withdrawn' and p['state']!='invalidated':
                rec['state']='candidate';changes.append(self.emit(rec,'reappeared',bar))
            if p['state']=='invalidated':
                if rec['state']!='invalidated':rec['state']='invalidated';rec['invalidated_at']=bar.closed_at;changes.append(self.emit(rec,'invalidated',bar))
                continue
            if audit['passed'] and rec['state']=='candidate':
                rec['state']='confirmed'
                if rec['confirmed_at'] is None:
                    rec['confirmed_at']=bar.closed_at;rec['confirmed_index']=bar.pos
                    confirmed=self.emit(rec,'confirmed',bar,{'rule_audit':audit,'point':p,
                        'definition_mode':'source_clause_confirmed_under_declared_base','strict_original_equivalence_proven':False,'execution_allowed':False})
                    self.confirmations.append(confirmed);changes.append(confirmed)
        for ident,rec in self.records.items():
            if ident not in present and rec['state'] in {'candidate','confirmed'}:
                rec['state']='withdrawn';rec['withdrawn_at']=bar.closed_at;changes.append(self.emit(rec,'withdrawn',bar))
        return changes
