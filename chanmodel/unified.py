"""One complete study pipeline and an append-only observation ledger."""
from __future__ import annotations

import copy
from dataclasses import asdict
from datetime import timedelta
import math

from .model import Candle, Config, time_value
from .complete_study import analyze_complete, SIGNAL_NAMES
from .complete_multilevel import analyze_links, join_triple
from .v2.series import Series, CN, PERIODS, session_closes, derive_series

PROFILE = 'chanlun_unified_complete_study_extrema_v4_2'
DEFAULT_CONFIG = Config(stroke_mode='strict',second_policy='original_101',macd_multiplier=2.0)


def analyze_all(inputs, cutoff, config=None, max_rank=8, roles=None, volume_tolerance=1.0):
    config = config or DEFAULT_CONFIG
    if not inputs:raise ValueError('at least one declared timeframe is required')
    if len({s.symbol for s in inputs.values()})!=1 or len({s.adjustment for s in inputs.values()})!=1:
        raise ValueError('all inputs must share security and adjustment')
    if any(key!=s.period for key,s in inputs.items()):raise ValueError('input keys must equal actual periods')
    if not math.isfinite(volume_tolerance) or volume_tolerance<0:raise ValueError('invalid volume tolerance')
    reports={}
    for period,s in inputs.items():
        reports[period]=analyze_complete(s.symbol,period,s.bars,cutoff,s.input_sha256 or s.prefix(cutoff).digest(),
                                        source=s.source,adjustment=s.adjustment,config=config,max_rank=max_rank)
    links=analyze_links(reports,inputs,cutoff,volume_tolerance=volume_tolerance) if len(inputs)>1 else {
        'signals':[],'interval_nesting':[],'small_to_large':[],'small_to_large_audit':[],
        'pairs':{},'execution_allowed':False,'state':'lower_timeframes_not_supplied'}
    if roles:
        if len(roles)!=3 or len(set(roles))!=3 or any(p not in inputs for p in roles):
            raise ValueError('roles require three distinct supplied timeframes')
        if not PERIODS[roles[0]]>PERIODS[roles[1]]>PERIODS[roles[2]]:
            raise ValueError('trade > confirm > trigger required')
        links['selected_roles']=list(roles)
        links['selected_signals']=join_triple(reports,{p:s.prefix(cutoff) for p,s in inputs.items()},roles,links['pairs'],cutoff)
    else:links['selected_signals']=links['signals']
    classes=[]
    for period,r in reports.items():
        for kind,label in SIGNAL_NAMES.items():
            values=[p for p in r['signals'] if p['class']==kind]
            classes.append({'period':period,'class':kind,'label':label,'count':len(values),
                            'active':sum(p['state']!='invalidated' for p in values),
                            'state':'observed' if values else 'not_observed_under_profile'})
    return {'schema':'chanlun.complete.v3','profile':PROFILE,'as_of':cutoff,
            'symbol':next(iter(inputs.values())).symbol,'config':asdict(config),'max_rank':max_rank,
            'reports':reports,'links':links,'six_class_coverage':classes,
            'input_audits':{p:r['input_audit'] for p,r in reports.items()},
            'definition_mode':'recursive_completed_types_under_declared_profile',
            'structure_completeness':'base_and_recursive_types_force_six_classes_nesting_and_trigger_states',
            'approximation_loss':next(iter(reports.values()))['approximation_loss'],
            'strict_original_equivalence_proven':False,'execution_allowed':False}


def objects(snapshot):
    for period,report in snapshot['reports'].items():
        for key in ['merged','fractals','strokes','segments']:
            for obj in report['base'][key]:yield period,key,obj
        for key in ['feature_sequences','center_evolution','center_monitors','construction_phases','divergence_comparisons','signals']:
            for obj in report[key]:yield period,key,obj
        for obj in report['decompositions']:
            yield period,'types:'+obj['view'],obj
        for layer in report['hierarchy']['levels']:
            for obj in layer['centers']:yield period,'centers',obj
    for key in ['selected_signals','interval_nesting','small_to_large']:
        for obj in snapshot['links'][key]:yield 'multi',key,obj


class CompleteReplay:
    def __init__(self,inputs,config=None,max_rank=8,roles=None,volume_tolerance=1.0):
        self.inputs=inputs;self.config=config or DEFAULT_CONFIG;self.max_rank=max_rank
        self.roles=roles;self.volume_tolerance=volume_tolerance
        self.registry={};self.events=[];self.snapshot=None;self.last=None

    def observe(self,cutoff):
        if self.last is not None and time_value(cutoff)<=time_value(self.last):
            raise ValueError('observation times must strictly increase')
        snapshot=analyze_all(self.inputs,cutoff,self.config,self.max_rank,self.roles,self.volume_tolerance)
        present=set();new=[]
        for period,kind,obj in objects(snapshot):
            ident=(period,kind,obj['id']);present.add(ident);old=self.registry.get(ident)
            body=copy.deepcopy(obj)
            confirmed=obj.get('state') in {'confirmed','complete','structurally_triggered_under_profile','completed_under_profile'}
            if old is None or not old['active'] or old['body']!=body:
                revision=0 if old is None else old['revision']+1
                event='appeared' if old is None else 'reappeared' if not old['active'] else 'revised'
                if old and old['body'].get('state')!='confirmed' and obj.get('state')=='confirmed':event='confirmed'
                record={'active':True,'body':body,'revision':revision,'available_time':cutoff,
                        'first_seen_time':old['first_seen_time'] if old else cutoff,
                        'confirmed_time':cutoff if confirmed else None}
                self.registry[ident]=record
                new.append({'period':period,'kind':kind,'id':obj['id'],'event':event,'revision':revision,
                            'observed_at':cutoff,'value':copy.deepcopy(body)})
            else:record=old
            obj.update(first_seen_time=record['first_seen_time'],version_available_time=record['available_time'],
                       version_confirmed_time=record['confirmed_time'],revision=record['revision'])
        for ident,old in self.registry.items():
            if old['active'] and ident not in present:
                old['active']=False;old['revision']+=1
                new.append({'period':ident[0],'kind':ident[1],'id':ident[2],'event':'withdrawn',
                            'revision':old['revision'],'observed_at':cutoff})
        self.events.extend(new);self.snapshot=snapshot;self.last=cutoff
        snapshot['availability_semantics']='version observation times; structural indices remain separate; prior events append only'
        return snapshot


def replay_schedule(inputs,cutoff,tail_count=0,every=1):
    if tail_count<0 or every<1:raise ValueError('invalid replay window/step')
    clipped={p:s.prefix(cutoff) for p,s in inputs.items()}
    if any(not s.bars for s in clipped.values()):raise ValueError('no shared completed history at cutoff')
    finest=min(clipped,key=lambda p:PERIODS[p])
    ready=max(time_value(s.bars[0].closed_at) for s in clipped.values())
    times=[b.closed_at for b in clipped[finest].bars if time_value(b.closed_at)>=ready]
    if tail_count:times=times[-tail_count:]
    selected=times[::every]
    if times and (not selected or selected[-1]!=times[-1]):selected.append(times[-1])
    return selected,{'finest_period':finest,'requested_tail_bars':tail_count,'step':every,
                     'observations':len(selected),'first':selected[0] if selected else None,
                     'last':selected[-1] if selected else None,
                     'semantics':'each scheduled close uses all earlier input; warm history is retained, no final geometry reuse'}


def demo_inputs(days=32):
    from datetime import date
    current=date(2026,1,5);bars=[]
    while len(bars)<days*48:
        if current.weekday()<5:
            for moment in session_closes(current,'5m'):
                i=len(bars);v=round(100+18*math.sin(i/135)+7*math.sin(i/19)+2*math.sin(i/3.5),6)
                hi,lo=v+.2,v-.2
                if i and i%29==0:
                    hi,lo=bars[-1].high-.03,bars[-1].low+.03;v=(hi+lo)/2
                bars.append(Candle(i,v,hi,lo,v,moment.isoformat(),1000.,True))
        current+=timedelta(days=1)
    five=Series('sz000001','5m','unadjusted',tuple(bars),'synthetic_teaching')
    cut=bars[-1].closed_at;thirty=derive_series(five,'30m',cut);daily=derive_series(thirty,'day',cut)
    return {'day':daily,'30m':thirty,'5m':five}
