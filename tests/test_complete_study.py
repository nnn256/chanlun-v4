from datetime import datetime, timedelta
import unittest

from chanmodel.complete_study import center_monitor, center_evolution, construction_phases, analyze_complete, trace_segment_revisions
from chanmodel.complete_multilevel import lower_comparison, period_contains, small_to_large
from chanmodel.model import Candle, time_value
from chanmodel.v2.series import Series, CN, derive_series, reconcile
from tests.test_recursive_v2 import daily_series, units
from chanmodel.v2.recursive import centers_from_units


class CompleteStudyTests(unittest.TestCase):
    def test_92_monitor_uses_completed_unit_ranges_not_close_average(self):
        child=units([10,18,12,20])
        center=centers_from_units(child,1,40)[0]
        mon=center_monitor(center,child)
        self.assertEqual((mon['a'],mon['b'],mon['z']),(12,18,15))
        self.assertEqual([p['zn'] for p in mon['points']],[14,15,16])
        self.assertTrue(mon['does_not_confirm_third_point'])

    def test_nine_components_do_not_invent_parent_center(self):
        child=units([0,10,5,12,6,11,7,13,6,12,5])
        center=centers_from_units(child,1,120)[0]
        self.assertGreaterEqual(len(center['member_ids']),9)
        event=next(x for x in center_evolution([center],child) if x['kind']=='nine_component_upgrade')
        self.assertEqual(event['state'],'awaiting_parent_type_overlap')
        self.assertFalse(event['count_alone_is_confirmation'])

    def test_evolution_never_drifts_original_core(self):
        child=units([8,2,7,3,12,7])
        center=centers_from_units(child,1,70)[0]
        formed=center_evolution([center],child)[0]
        self.assertEqual((formed['zd'],formed['zg']),(3,7))
        self.assertIsNone(center['departure'])

    def test_108_opposite_third_is_construction_failure(self):
        base={'rank':1,'center_id':'c','side':'buy','state':'awaiting_lower_confirmation'}
        points=[dict(base,id='first',**{'class':'first_buy'},anchor=5,available_at=7),
                dict(base,id='third',**{'class':'third_sell'},side='sell',anchor=9,available_at=11)]
        p=construction_phases(points)[0]
        self.assertEqual(p['state'],'failed_opposite_departure')
        self.assertEqual(p['third_id'],'third')

    def test_108_third_must_reference_same_center_rank_and_later_time(self):
        first={'id':'f','class':'first_buy','rank':1,'center_id':'a','side':'buy','state':'awaiting_lower_confirmation','anchor':5,'available_at':7}
        unrelated=dict(first,id='x',**{'class':'third_buy'},center_id='b',anchor=9,available_at=11)
        self.assertEqual(construction_phases([first,unrelated])[0]['state'],'under_construction')

    def test_full_layers_are_prefix_causal(self):
        src=daily_series(170);cut=src.bars[119].closed_at
        a=analyze_complete(src.symbol,'day',src.bars,cut,'fixture')
        b=analyze_complete(src.symbol,'day',src.bars[:120],cut,'fixture')
        for key in ['hierarchy','signals','feature_sequences','center_monitors','center_evolution','decompositions']:
            self.assertEqual(a[key],b[key],key)
        self.assertFalse(a['execution_allowed'])
        self.assertEqual(a['certificate_audit']['errors'],[])

    def test_all_six_classes_remain_distinct_even_when_absent(self):
        src=daily_series(120);r=analyze_complete(src.symbol,'day',src.bars,src.bars[-1].closed_at,'fixture')
        self.assertTrue(all(p['class'] in {'first_buy','second_buy','third_buy','first_sell','second_sell','third_sell'} for p in r['signals']))
        self.assertTrue(all(p['execution_allowed'] is False for p in r['signals']))
        self.assertEqual(len({x['key'] for x in r['layer_catalog']}),13)

    def test_replay_declares_window_and_does_not_backfill_observation(self):
        bars=daily_series(90).bars;r=trace_segment_revisions(bars,70)
        self.assertEqual(r['prefixes_evaluated'],20)
        self.assertFalse(r['future_data_reused'])
        self.assertTrue(all(x['anchor']<=x['observed_index'] for x in r['events']))

    def test_day_membership_uses_china_date_not_host_timezone(self):
        b=Candle(0,10,10,10,10,'2026-11-06T15:00:00+08:00',100)
        series=Series('sz300949','day','unadjusted',(b,),'fixture')
        self.assertTrue(period_contains(series,0,'2026-11-06T01:25:00+00:00'))
        self.assertTrue(period_contains(series,0,time_value('2026-11-06T01:25:00+00:00')))
        self.assertFalse(period_contains(series,0,'2026-11-05T15:25:00+00:00'))

    def test_nested_comparison_rejects_unreconciled_interval(self):
        b=Candle(0,10,11,9,10,'2026-11-06T15:00:00+08:00',100)
        s=Series('sz300949','day','unadjusted',(b,),'fixture')
        parent={'terminal_range':[0,0],'anchor':0,'side':'buy','price':9}
        pair={'comparisons':[],'gaps':[],'matched_start':None,'matched_end':None}
        value,reason=lower_comparison(parent,{'divergence_comparisons':[]},s,s,pair)
        self.assertIsNone(value);self.assertEqual(reason,'parent_interval_not_reconciled')

    def nested_fixture(self):
        date=datetime(2026,11,6,9,30,tzinfo=CN)
        bars=tuple(Candle(i,10+i*.01,10+i*.01+.05,10+i*.01-.05,10+i*.01,
                          (date+timedelta(minutes=i+1)).isoformat(),100) for i in range(60))
        low=Series('sz300949','1m','unadjusted',bars,'fixture');high=derive_series(low,'5m',bars[-1].closed_at)
        pair=reconcile(high,low,bars[-1].closed_at)
        parent={'side':'sell','price':high.bars[2].high,'anchor':2,'terminal_range':[0,2]}
        comp={'id':'cmp','side':'sell','state':'consolidation_force_weakening','rank':1,'price':parent['price'],
              'terminal_range':[6,14],'anchor':14,'available_at':18,
              'gates':{key:True for key in ['macd_force_weaker','same_child_rank','children_complete','same_direction','new_extreme']}}
        return high,low,pair,parent,comp

    def test_nested_positive_requires_matching_interval_price_and_evidence(self):
        high,low,pair,parent,comp=self.nested_fixture()
        value,reason=lower_comparison(parent,{'divergence_comparisons':[comp]},high,low,pair)
        self.assertEqual(value['id'],'cmp');self.assertIsNone(reason)

    def test_nested_rejects_price_mismatch_and_unsupported_comparison(self):
        high,low,pair,parent,comp=self.nested_fixture()
        for change in [{'price':parent['price']+.1},{'state':'not_supported'},{'side':'buy'}]:
            value,reason=lower_comparison(parent,{'divergence_comparisons':[{**comp,**change}]},high,low,pair)
            self.assertIsNone(value)

    def test_small_to_large_force_alone_cannot_establish_reversal(self):
        high,low,pair,parent,comp=self.nested_fixture()
        segment={'id':'seg','direction':'up','start':0,'end':2,'end_price':parent['price']}
        reports={'5m':{'base':{'segments':[segment]}},'1m':{'divergence_comparisons':[comp],
                 'hierarchy':{'base_units':[],'levels':[]}}}
        diagnostics=[]
        found=small_to_large(reports,{'5m':high,'1m':low},'5m','1m',pair,low.bars[-1].closed_at,diagnostics)
        self.assertEqual(found,[])
        self.assertEqual(diagnostics[0]['missing_evidence'],'no_matching_completed_opposite_center_break_and_nonreturn')


if __name__=='__main__':unittest.main()
