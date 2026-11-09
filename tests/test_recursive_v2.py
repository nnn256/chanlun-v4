import copy
from dataclasses import replace
from datetime import datetime,timedelta
import math
import unittest

from chanmodel.model import Candle,Config
from chanmodel.v2.series import Series,CN,derive_series,validate_multiframe,validate_series,reconcile,session_closes
from chanmodel.v2.recursive import centers_from_units,partition_types,build_hierarchy,validate_units,PROFILE,CUT_PROFILE,decompose_same_level,audit_hierarchy,base_units
from chanmodel.v2.divergence import compare_divergences,make_signals
from chanmodel.v2.engine import analyze,multiframe,replay,trigger_after,structural_confirmations,interval_reconciled


def units(vertices,rank=0):
    result=[]
    for i,(a,b) in enumerate(zip(vertices,vertices[1:])):
        result.append({'id':f'u{rank}:{i*10}:{(i+1)*10}','rank':rank,'kind':'base_segment' if rank==0 else 'consolidation',
                       'direction':'up' if b>a else 'down','state':'complete','start':i*10,'end':(i+1)*10,
                       'start_price':a,'end_price':b,'low':min(a,b),'high':max(a,b),
                       'completed_at':(i+1)*10+2,'available_at':(i+1)*10+2,
                       'components':[f's{i}'] if rank==0 else [f'lower{i}-{k}' for k in range(3)],
                       'center_ids':[] if rank==0 else [f'lowercenter{i}'],
                       'completion':{'rule':'base_segment_confirmed' if rank==0 else PROFILE,'available_at':(i+1)*10+2}})
    return result


def minute_series(days=8):
    start=datetime(2026,1,5,tzinfo=CN).date();result=[];date=start
    while len(result)<days*48:
        if date.weekday()<5:
            for point in session_closes(date,'5m'):
                i=len(result);c=100+12*math.sin(i/40)+5*math.sin(i/9)
                result.append(Candle(i,c,c+.3,c-.3,c,point.isoformat(),100))
        date+=timedelta(days=1)
    return Series('sz000001','5m','unadjusted',tuple(result),'synthetic-test')


def daily_series(count):
    date=datetime(2025,1,2,tzinfo=CN).date();bars=[]
    while len(bars)<count:
        if date.weekday()<5:
            i=len(bars);c=100+20*math.sin(i/35)+8*math.sin(i/9)+2*math.sin(i/2)
            bars.append(Candle(i,c,c+.2,c-.2,c,datetime.combine(date,datetime.min.time(),CN).replace(hour=15).isoformat(),1000))
        date+=timedelta(days=1)
    return Series('sz000001','day','unadjusted',tuple(bars),'synthetic-test')


class RecursiveTypeTests(unittest.TestCase):
    def test_delayed_predecessor_confirmation_propagates_to_successor(self):
        segments=[{'id':'s0','state':'confirmed','basis_at':100,'start':0,'end':30,'start_price':1,'end_price':10,
                   'actual_low':1,'actual_high':10,'direction':'up','confirmation':{'path':'feature_gap'}},
                  {'id':'s1','state':'confirmed','basis_at':70,'start':30,'end':60,'start_price':10,'end_price':2,
                   'actual_low':2,'actual_high':10,'direction':'down','confirmation':{'path':'feature_no_gap'}}]
        result=base_units(segments,100)
        self.assertEqual([x['completed_at'] for x in result],[100,100])
        self.assertEqual(result[1]['completion']['raw_basis_at'],70)

    def test_higher_center_rejects_segment_renamed_as_type(self):
        data=units([0,10,5,12],rank=1)
        data[0]['kind']='base_segment'
        with self.assertRaises(ValueError):centers_from_units(data,2,100)

    def test_center_rejects_mixed_ranks_incomplete_future_or_gap(self):
        for mutate in [lambda x:x[1].update(rank=2),lambda x:x[1].update(state='developing'),
                       lambda x:x[1].update(completed_at=1000),lambda x:x[1].update(start=11)]:
            data=units([0,10,5,12],rank=1);mutate(data)
            with self.assertRaises(ValueError):centers_from_units(data,2,100)

    def test_three_completed_child_types_form_recursive_center(self):
        data=units([10,18,12,20],rank=1)
        c=centers_from_units(data,2,40)[0]
        self.assertEqual((c['zd'],c['zg']),(12,18))
        self.assertTrue(c['certificate']['sublevel_types_verified'])
        self.assertEqual(c['formed_at'],32)

    def test_three_children_alone_do_not_complete_parent(self):
        level=partition_types(units([0,10,5,12]),1,40)
        self.assertEqual(level['completed_types'],[])
        self.assertEqual(level['pending'][0]['state'],'developing')

    def test_opposite_break_requires_complete_nonreturn(self):
        source=units([0,10,5,12,2,3,1])
        short=partition_types(source[:4],1,100)
        self.assertFalse(short['completed_types'])
        result=partition_types(source,1,100)['completed_types'][0]
        self.assertEqual((result['kind'],result['end'],result['completed_at']),('consolidation',30,52))
        self.assertEqual(result['completion']['opposite_side'],'sell')
        self.assertEqual(result['components'],[x['id'] for x in source[:3]])

    def test_two_separated_centers_make_completed_trend(self):
        source=units([0,10,5,12,8,22,18,24,20,30,15,17])
        level=partition_types(source,1,120);trend=level['completed_types'][0]
        self.assertEqual(trend['kind'],'trend')
        self.assertEqual((trend['end_price'],trend['completed_at']),(30,112))
        self.assertEqual(len(trend['center_ids']),2)
        self.assertTrue(trend['trend_certificate']['outer_ranges_strictly_separated'])

    def test_touch_returns_and_core_does_not_drift(self):
        c=centers_from_units(units([8,2,7,3,12,7]),1,60)[0]
        self.assertEqual((c['zd'],c['zg']),(3,7))
        self.assertEqual(c['attempts'][0]['relation'],'touch')
        self.assertIsNone(c['departure'])

    def test_mirror_has_same_completion_indices(self):
        v=[0,10,5,12,8,22,18,24,20,30,15,17]
        a=partition_types(units(v),1,120)['completed_types'][0]
        b=partition_types(units([-x for x in v]),1,120)['completed_types'][0]
        self.assertEqual((a['start'],a['end'],a['completed_at']),(b['start'],b['end'],b['completed_at']))
        self.assertEqual(a['end_price'],-b['end_price'])

    def test_pending_tail_does_not_feed_upper_rank(self):
        graph=build_hierarchy(units([0,10,5]),3,30)
        self.assertEqual(len(graph['levels']),1)
        self.assertFalse(graph['levels'][0]['completed_types'])

    def test_partition_cut_is_completed_type_but_never_turn_confirmation(self):
        result=decompose_same_level(units([0,10,5,12]),1,40)
        self.assertEqual(len(result['types']),1)
        self.assertEqual(result['types'][0]['completion']['rule'],CUT_PROFILE)
        self.assertFalse(result['types'][0]['completion']['is_turn_confirmation'])

    def test_expanding_consolidation_orientation_is_not_its_net_displacement(self):
        result=decompose_same_level(units([10,0,15,11,20,12]),1,60)
        self.assertEqual(len(result['types']),1)
        self.assertEqual(result['types'][0]['direction'],'down')
        self.assertEqual(result['types'][0]['net_direction'],'up')

    def test_recursion_consumes_real_completed_types_and_propagates_times(self):
        source=units([0,10,5,12,4,9,2,11,5,13,3,8,1])
        graph=build_hierarchy(source,3,130)
        self.assertGreaterEqual(len(graph['levels']),2)
        lower={x['id']:x for x in graph['levels'][0]['completed_types']}
        for upper in graph['levels'][1]['completed_types']:
            self.assertEqual(upper['completed_at'],max(lower[key]['completed_at'] for key in upper['components']))
            self.assertTrue(all(lower[key]['kind']=='consolidation' for key in upper['components']))

    def test_certificate_booleans_cannot_hide_wrong_ranges_or_future_completion(self):
        graph=build_hierarchy(units([0,10,5,12,4,9,2,11,5,13]),2,100)
        self.assertEqual(audit_hierarchy(graph,100)['state'],'passed')
        corrupted=copy.deepcopy(graph)
        corrupted['levels'][0]['partition_centers'][0]['zg']+=1
        self.assertEqual(audit_hierarchy(corrupted,100)['state'],'failed')
        corrupted=copy.deepcopy(graph);corrupted['levels'][0]['completed_types'][0]['completed_at']-=10
        self.assertEqual(audit_hierarchy(corrupted,100)['state'],'failed')


class DataContractTests(unittest.TestCase):
    def test_actual_aggregation_matches_every_complete_parent_bar(self):
        five=minute_series();cut=five.bars[-1].closed_at
        thirty=derive_series(five,'30m',cut);day=derive_series(thirty,'day',cut)
        result=validate_multiframe({'t':day,'c':thirty,'f':five},('t','c','f'),cut)
        self.assertEqual(result['state'],'verified')
        self.assertEqual([p['matched_parent_bars'] for p in result['pairs']],[8,64])

    def test_daily_relabelled_as_minutes_is_rejected(self):
        d=daily_series(30);cut=d.bars[-1].closed_at
        with self.assertRaises(ValueError):validate_multiframe({'d':d,'m':replace(d,period='30m'),'f':replace(d,period='5m')},('d','m','f'),cut)

    def test_legacy_duplicate_input_bug_is_also_blocked(self):
        from chanmodel.engine import multilevel
        d=daily_series(30)
        with self.assertRaises(ValueError):multilevel({'day':list(d.bars),'30m':list(d.bars),'5m':list(d.bars)},('day','30m','5m'),d.bars[-1].closed_at)

    def test_missing_parent_with_complete_child_bucket_is_recorded(self):
        f=minute_series();cut=f.bars[-1].closed_at;c=derive_series(f,'30m',cut)
        c=replace(c,bars=tuple(replace(b,pos=i) for i,b in enumerate(c.bars[:5]+c.bars[6:])))
        result=reconcile(c,f,cut)
        self.assertTrue(any(x.get('reason')=='complete_child_bucket_has_no_parent_observation' for x in result['gaps']))

    def test_security_adjustment_and_reversed_periods_rejected(self):
        f=minute_series();cut=f.bars[-1].closed_at;c=derive_series(f,'30m',cut);d=derive_series(c,'day',cut)
        for changed in [replace(c,symbol='sh600000'),replace(c,adjustment='qfq')]:
            with self.assertRaises(ValueError):validate_multiframe({'d':d,'c':changed,'f':f},('d','c','f'),cut)
        with self.assertRaises(ValueError):validate_multiframe({'d':f,'c':c,'f':d},('d','c','f'),cut)

    def test_valid_looking_wrong_ohlc_blocks_all_triggers(self):
        f=minute_series();cut=f.bars[-1].closed_at;c=derive_series(f,'30m',cut);d=derive_series(c,'day',cut)
        changed=replace(d.bars[2],high=d.bars[2].high+1)
        d=replace(d,bars=d.bars[:2]+(changed,)+d.bars[3:])
        result=multiframe({'d':d,'c':c,'f':f},('d','c','f'),cut)
        self.assertEqual(result['state'],'blocked_data_contract')
        self.assertEqual(result['signals'],[])

    def test_missing_volume_is_not_verified_as_zero(self):
        f=minute_series();cut=f.bars[-1].closed_at;c=derive_series(f,'30m',cut)
        changed=replace(c,bars=tuple(replace(b,volume=None) for b in c.bars))
        result=reconcile(changed,f,cut)
        self.assertEqual(result['state'],'mismatch')
        self.assertEqual(result['mismatches'][0]['state'],'missing_volume')

    def test_partial_child_window_is_not_full_coverage(self):
        f=minute_series();cut=f.bars[-1].closed_at;c=derive_series(f,'30m',cut)
        partial=replace(f,bars=tuple(replace(b,pos=i) for i,b in enumerate(f.bars[3:])))
        result=reconcile(c,partial,cut)
        self.assertEqual(result['state'],'verified_with_gaps')
        self.assertFalse(interval_reconciled(result,datetime.fromisoformat(c.bars[0].closed_at),datetime.fromisoformat(c.bars[1].closed_at)))

    def test_future_parent_and_children_are_excluded(self):
        f=minute_series();cut=f.bars[100].closed_at;c=derive_series(f,'30m',f.bars[-1].closed_at);d=derive_series(c,'day',f.bars[-1].closed_at)
        a=validate_multiframe({'d':d,'c':c,'f':f},('d','c','f'),cut)
        b=validate_multiframe({'d':d.prefix(cut),'c':c.prefix(cut),'f':f.prefix(cut)},('d','c','f'),cut)
        self.assertEqual(a,b)


class DivergenceTests(unittest.TestCase):
    def test_weak_second_retest_has_its_own_named_comparison(self):
        source=units([30,20,28,21,26,14,18,12,17,10,13,9])
        bars=[Candle(i,20,31,0,20) for i in range(125)]
        hist=[{'histogram':-2 if 40<=i<=50 else -.4 if 80<=i<=90 else -.01,'dif':10 if i<51 else 0,'dea':0} for i in range(125)]
        cs=centers_from_units(source,1,125);comparisons=compare_divergences(source,cs,bars,hist,1,125)
        signals=make_signals(source,cs,comparisons,bars,1,hist)
        weak=[s for s in signals if s.get('retest_mode')=='weak_retest_101']
        self.assertEqual(len(weak),1)
        self.assertEqual(weak[0]['weak_retest_evidence']['a']['unit_id'],source[8]['id'])
        self.assertEqual(weak[0]['weak_retest_evidence']['c']['unit_id'],source[10]['id'])

    def test_indicator_weakness_without_two_centers_is_not_trend_divergence(self):
        source=units([30,20,28,21,26,14,18,12,17,10,13,11])
        bars=[Candle(i,20,31,0,20) for i in range(125)]
        hist=[{'histogram':-1 if i<60 else -.1,'dif':0,'dea':0} for i in range(125)]
        centers=centers_from_units(source,1,125)
        comparisons=compare_divergences(source,centers[1:],bars,hist,1,125)
        self.assertTrue(comparisons)
        self.assertFalse(any(c['structural_context_passed'] for c in comparisons))

    def test_trend_and_named_weaker_completed_leave_pass_explicit_gates(self):
        source=units([30,20,28,21,26,14,18,12,17,10,13,11])
        bars=[Candle(i,20,31,0,20) for i in range(125)]
        hist=[{'histogram':-2 if 40<=i<=50 else -.1,'dif':10 if i<51 else 0,'dea':0} for i in range(125)]
        cs=centers_from_units(source,1,125);comparisons=compare_divergences(source,cs,bars,hist,1,125)
        passed=[c for c in comparisons if c['state']=='trend_divergence_supported_candidate']
        self.assertTrue(passed)
        self.assertEqual(passed[0]['a']['unit_id'],source[4]['id'])
        self.assertEqual(passed[0]['c']['unit_id'],source[8]['id'])
        self.assertTrue(passed[0]['lower_level_turn_required'])


class TriggerAndReplayTests(unittest.TestCase):
    def test_actual_lower_center_break_yields_confirmation_then_new_fine_stroke(self):
        fine=minute_series(10);cut=fine.bars[-1].closed_at
        confirm=derive_series(fine,'30m',cut);trade=derive_series(confirm,'day',cut)
        lower=units([30,20,25,18,28,27])
        for unit in lower:
            for key in ['start','end','completed_at','available_at']:unit[key]+=8
            unit['id']=f"u0:{unit['start']}:{unit['end']}"
        view=partition_types(lower,1,70)
        self.assertTrue(view['completed_types'])
        lower_report={'hierarchy':{'base_units':lower,'levels':[{'centers':view['centers'],'completed_types':[],'movement_views':view}]}}
        parent={'side':'buy','terminal_range':[0,4],'anchor':4,'price':18,'invalidation':17}
        evidence=structural_confirmations(parent,trade,confirm,lower_report,cut)
        self.assertTrue(evidence)
        at=datetime.fromisoformat(evidence[0]['available_time'])
        index=next(i for i,b in enumerate(fine.bars) if datetime.fromisoformat(b.closed_at)>at)
        finer_report={'base':{'strokes':[{'id':'new-up','direction':'up','start':index,'end':index+4,'basis_at':index+8,
                       'state':'confirmed','confirmation_basis':'next-down','end_price':101}]}}
        trigger=trigger_after(parent,evidence[0],fine,finer_report,cut,datetime.fromisoformat(trade.bars[4].closed_at))
        self.assertIsNotNone(trigger)
        self.assertGreater(datetime.fromisoformat(trigger['available_time']),at)
        wrong_pivot={**parent,'price':19}
        self.assertFalse(structural_confirmations(wrong_pivot,trade,confirm,lower_report,cut))

    def test_trigger_must_follow_confirmation_and_be_completed(self):
        f=minute_series(1);parent={'side':'buy','invalidation':90}
        confirmation={'available_time':f.bars[10].closed_at,'return_end_time':f.bars[8].closed_at}
        def stroke(start,end,basis,state='confirmed'):
            return {'id':str(start),'start':start,'end':end,'basis_at':basis,'direction':'up','state':state,'confirmation_basis':'next-down','end_price':101}
        report={'base':{'strokes':[stroke(1,4,6),stroke(8,13,15,'provisional'),stroke(10,17,20)]}}
        evidence=trigger_after(parent,confirmation,f,report,f.bars[-1].closed_at,datetime.fromisoformat(f.bars[11].closed_at))
        self.assertEqual(evidence['stroke_id'],'10')
        earlier=trigger_after(parent,confirmation,f,report,f.bars[18].closed_at,datetime.fromisoformat(f.bars[11].closed_at))
        self.assertIsNone(earlier)

    def test_future_append_cannot_rewrite_reference_event_prefix(self):
        s=daily_series(230)
        _,small=replay(replace(s,bars=s.bars[:170]))
        _,large=replay(s)
        self.assertEqual(small,[e for e in large if e['observed_index']<170])

    def test_higher_type_graph_contains_types_not_segment_inputs(self):
        report=analyze(daily_series(500),daily_series(500).bars[-1].closed_at)
        for level in report['hierarchy']['levels'][1:]:
            self.assertTrue(all(x.startswith('u'+str(level['rank']-1)+':') for x in level['input_ids']))
        self.assertFalse(report['execution_allowed'])


if __name__=='__main__':unittest.main()
