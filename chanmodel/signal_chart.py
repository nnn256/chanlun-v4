"""Traceable chart annotations; drawing never invents or upgrades a signal."""
from __future__ import annotations

from bisect import bisect_left
import copy
from datetime import datetime, timezone, timedelta
import math

CN=timezone(timedelta(hours=8))
LABELS={'first_buy':'一买','second_buy':'二买','third_buy':'三买',
        'first_sell':'一卖','second_sell':'二卖','third_sell':'三卖'}
STATES={'candidate':'待下级确认','waiting_trigger':'待细级触发','triggered':'模型结构触发',
        'structure_complete':'本级结构完成·待多周期',
        'needs_review':'结构分歧·待复核',
        'invalidated':'已失效','withdrawn':'已撤回','blocked':'缺确认数据'}


def instant(value):
    point=datetime.fromisoformat(value.replace('Z','+00:00'))
    if point.tzinfo is None:raise ValueError('chart evidence timestamps require timezone')
    return point


def bar_time(bar):
    if bar.get('closed_at'):return instant(bar['closed_at'])
    if bar.get('date_cn'):return datetime.fromisoformat(bar['date_cn']).replace(tzinfo=CN)
    raise ValueError('chart requires original bar closing time')


def finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def index_ok(index,length):return isinstance(index,int) and not isinstance(index,bool) and 0<=index<length


def build_annotations(panel,links=(),cutoff=None):
    bars=panel.get('bars',[]);n=len(bars);result=[];rejected=[]
    if not n or panel.get('kind')=='line':return {'version':'signal-chart.v1','objects':[],'rejected':[]}
    times=[bar_time(b) for b in bars]
    if any(a>=b for a,b in zip(times,times[1:])):raise ValueError('chart bars must be ordered by close time')
    if any(b.get('source_index',i)!=i for i,b in enumerate(bars)):raise ValueError('signal chart requires complete original indices')
    as_of=instant(cutoff) if cutoff else times[-1]
    signals=panel.get('all_signals',panel.get('signals',[]));by_id={s['id']:s for s in signals}
    centers={c['id']:c for l in panel.get('structure_layers',[]) for c in l.get('centers',[])}
    centers.update({c['id']:c for c in panel.get('centers',[])})
    comparisons={c['id']:c for c in panel.get('complete_evidence',{}).get('divergence_comparisons',[])}
    units={u['id']:u for u in panel.get('signal_units',[])}
    for l in panel.get('structure_layers',[]):units.update({u['id']:u for u in l.get('completed_types',[])})
    for s in panel.get('segments',[]):units.setdefault(f"u0:{s['start']}:{s['end']}",s)

    def evidence_range(label,bounds,ident=None):
        if not isinstance(bounds,(list,tuple)) or len(bounds)!=2:return None
        a,b=bounds
        if not index_ok(a,n) or not index_ok(b,n) or a>b or times[b]>as_of:return None
        return {'label':label,'id':ident,'start':a,'end':b,'start_time':times[a].astimezone(CN).isoformat(),
                'end_time':times[b].astimezone(CN).isoformat(),'low':min(x['low'] for x in bars[a:b+1]),
                'high':max(x['high'] for x in bars[a:b+1])}

    for point in signals:
        ident=point['id'];kind=point.get('class');anchor=point.get('anchor');available=point.get('available_at')
        reason=None
        if kind not in LABELS:reason='unknown_signal_class'
        elif point.get('side')!=('buy' if kind.endswith('_buy') else 'sell'):reason='class_side_mismatch'
        elif not index_ok(anchor,n) or not index_ok(available,n) or available<anchor:reason='invalid_anchor_or_availability'
        elif not finite(point.get('price')) or not finite(point.get('invalidation')):reason='nonfinite_price_or_boundary'
        elif times[available]>as_of or bars[available].get('is_closed') is False:reason='not_available_at_cutoff'
        if reason:
            rejected.append({'id':ident,'reason':reason});continue
        center=centers.get(point.get('center_id'))
        if center and center.get('rank',point['rank'])!=point['rank']:
            rejected.append({'id':ident,'reason':'center_rank_mismatch'});continue
        state='invalidated' if point.get('state')=='invalidated' else 'withdrawn' if point.get('state')=='withdrawn' else 'candidate'
        if point.get('state')=='needs_review':state='needs_review'
        rule_audit=point.get('rule_audit') or {};local_at=rule_audit.get('evidence_available_at')
        local_time=None
        if state=='candidate' and rule_audit.get('local_geometry_completed') and index_ok(local_at,n) and times[local_at]<=as_of:
            state='structure_complete';local_time=times[local_at].astimezone(CN).isoformat()
        candidates=[l for l in links if l.get('parent_signal')==ident and l.get('trade_period')==panel['period']
                    and l.get('side')==point['side'] and l.get('rank')==point['rank']]
        # One valid link is evidence of a trigger; a point already invalidated
        # at this cutoff remains invalidated. A plain "confirmed" string alone
        # is deliberately insufficient to upgrade the picture.
        proof=None;confirmed_time=None;confirmed_index=None;link_state=None
        for link in candidates:
            trigger=link.get('trigger') or {};confirmation=link.get('confirmation') or {}
            if link['state']=='invalidated':state='invalidated';link_state=link['state'];break
            at=trigger.get('available_time');lower_at=confirmation.get('available_time')
            if state not in {'invalidated','withdrawn'} and link['state']=='structurally_triggered_under_profile' and at and lower_at:
                moment=instant(at);lower=instant(lower_at)
                if times[available]<=moment<=as_of and lower<=moment and trigger.get('id') and confirmation.get('id'):
                    state='triggered';confirmed_time=moment.astimezone(CN).isoformat();location=bisect_left(times,moment)
                    confirmed_index=location if location<n and times[location]<=as_of else None
                    proof=copy.deepcopy(link);link_state=link['state'];break
            if state in {'candidate','structure_complete'} and link['state']=='waiting_trigger':state='waiting_trigger';link_state=link['state']
            elif state in {'candidate','structure_complete'} and link['state'].startswith('blocked'):state='blocked';link_state=link['state']
        references=[]
        terminal=evidence_range('末端运动',point.get('terminal_range'),ident)
        if terminal:references.append(terminal)
        comparison=comparisons.get(point.get('comparison_id'))
        if comparison:
            for name in ['a','c']:
                obj=comparison.get(name,{})
                span=evidence_range('比较'+name.upper(),obj.get('range'),obj.get('unit_id'))
                if span:references.append(span)
        for field,label in [('leave_id','离开段'),('return_id','首次回试')]:
            obj=units.get(point.get(field))
            if obj:
                span=evidence_range(label,[obj['start'],obj['end']],obj['id'])
                if span:references.append(span)
        first=by_id.get(point.get('first_signal'))
        first_context=None
        if first and index_ok(first.get('anchor'),n) and first.get('rank')==point['rank']:
            first_context={'id':first['id'],'anchor':first['anchor'],'price':first['price'],'label':LABELS[first['class']]}
        if point.get('engine') and index_ok(point.get('relate_bsp1_anchor'),n):
            first_anchor=point['relate_bsp1_anchor']
            first_context={'id':'native-first:'+str(first_anchor),'anchor':first_anchor,
                           'price':bars[first_anchor]['low' if point['side']=='buy' else 'high'],
                           'label':'原生一类'+('买' if point['side']=='buy' else '卖')}
        marker={'id':panel['period']+':'+ident,'signal_id':ident,'period':panel['period'],'rank':point['rank'],
                'class':kind,'label':point.get('label') if point.get('engine') else LABELS[kind],
                'native_type':point.get('native_type'),'native_engine':point.get('engine'),
                'first_seen_time':point.get('first_seen_time'),'side':point['side'],'state':state,'state_label':STATES[state],
                'anchor':anchor,'anchor_time':times[anchor].astimezone(CN).isoformat(),'price':point['price'],
                'available_at':available,'available_time':times[available].astimezone(CN).isoformat(),'available_price':bars[available]['close'],
                'delay_bars':available-anchor,'confirmed_time':confirmed_time,'confirmed_index':confirmed_index,
                'local_completed_time':local_time,
                'confirmed_price':bars[confirmed_index]['close'] if confirmed_index is not None else None,
                'invalidation':point['invalidation'],'invalidation_operator':point.get('invalidation_operator','le' if point['side']=='buy' else 'ge'),
                'center':{k:center[k] for k in ['id','rank','start','end','zd','zg'] if k in center} if center else None,
                'references':references,'first_context':first_context,'comparison':copy.deepcopy(comparison),
                'link_state':link_state,'confirmation_evidence':proof,'original_signal':copy.deepcopy(point),
                'definition_mode':point.get('definition_mode','declared_structural_profile'),
                'availability_semantics':'structural evidence index; first observed time requires causal event replay',
                'decomposition_end':point.get('decomposition_end',anchor),
                'rule_audit':copy.deepcopy(point.get('rule_audit')),
                'anchor_correction':copy.deepcopy(point.get('saved_original_point')),
                'invalidated_at':point.get('invalidated_at'),
                'invalidated_time':times[point['invalidated_at']].astimezone(CN).isoformat() if index_ok(point.get('invalidated_at'),n) else None,
                'invalidated_price':(bars[point['invalidated_at']]['low'] if point['side']=='buy' else bars[point['invalidated_at']]['high']) if index_ok(point.get('invalidated_at'),n) else None,
                'execution_allowed':False}
        result.append(marker)
    return {'version':'signal-chart.v1','objects':sorted(result,key=lambda x:(x['anchor'],x['rank'],x['class'])),
            'rejected':rejected,'semantics':'endpoint/evidence/trigger positions are distinct; plot does not change signal rules'}


def attach_chart_annotations(chart,multiframe=None):
    multi=multiframe or {};links=multi.get('signals',[])
    if isinstance(multi.get('complete_links'),dict):links=multi['complete_links'].get('selected_signals',multi['complete_links'].get('signals',links))
    for panel in chart.get('panels',[]):
        if panel.get('kline_audit',{}).get('state')=='invalid':
            panel['signal_chart']={'version':'signal-chart.v1','objects':[],'rejected':[{'id':'raw-data','reason':'invalid_raw_kline'}]}
            continue
        panel['signal_chart']=build_annotations(panel,links,chart.get('as_of'))
    return chart
