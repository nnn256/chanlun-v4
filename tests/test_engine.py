import copy
import csv
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import tempfile
import unittest

from chanmodel.centers import build_centers, classify
from chanmodel.engine import ReplayEngine, analyze, collections, multilevel, replay
from chanmodel.model import Candle, Config, load_csv
from chanmodel.segments import build_segments, characteristic_candidate, feature, append_feature
from chanmodel.signals import macd, structural_signals
from chanmodel.structure import fractals_at, merge_bars, stroke_check


def moves(vertices, forces=None):
    result = []
    for i, (a, b) in enumerate(zip(vertices, vertices[1:])):
        result.append({"id": f"p:{i}", "direction": "up" if b > a else "down", "start": 10*i, "end": 10*(i+1),
                       "start_price": a, "end_price": b, "low": min(a,b), "high": max(a,b),
                       "actual_low": min(a,b), "actual_high": max(a,b), "state": "confirmed", "basis_closed": True,
                       "basis_at": 10*(i+1)+3, "macd_area": forces[i] if forces else 10,
                       "amplitude": abs(b-a), "slope": abs(b-a)/10})
    return result


def candles(values):
    return [Candle(i, value, value+0.2, value-0.2, value) for i,value in enumerate(values)]


def wave(count):
    return candles([100+18*math.sin(i/38)+6*math.sin(i/10)+2*math.sin(i/2.5) for i in range(count)])


class DocumentedRules(unittest.TestCase):
    def test_containment_is_not_partial_overlap(self):
        bars=[Candle(0,14,18,10,14),Candle(1,16,20,12,16)]
        self.assertEqual(len(merge_bars(bars,Config())),2)

    def test_initial_direction_uses_midpoint_before_close(self):
        bars=[Candle(0,12,18,10,18),Candle(1,14,17,13,13)]
        merged=merge_bars(bars,Config())
        self.assertEqual((merged[0]['low'],merged[0]['high']),(13,18))

    def test_direction_source_and_equal_price_earliest_origin(self):
        bars=[Candle(0,10,15,5,10),Candle(1,14,18,10,14),Candle(2,15,18,12,15),Candle(3,13,16,11,13)]
        merged=merge_bars(bars,Config())
        self.assertEqual((merged[1]['low'],merged[1]['high']),(12,18))
        self.assertEqual((merged[1]['high_source'],merged[1]['low_source']),(1,2))
        top=fractals_at(merged)[0]
        self.assertEqual((top['kind'],top['source'],top['basis_at']),('top',1,3))

    def test_fractal_checks_both_high_and_low(self):
        standard=[{'id':str(i),'start':i,'end':i,'high_source':i,'low_source':i,'high':h,'low':l,'closed':True} for i,(l,h) in enumerate([(10,14),(5,18),(11,16)])]
        self.assertEqual(fractals_at(standard),[])

    def test_strict_and_loose_spacing_use_different_indices(self):
        a={'kind':'bottom','merged_index':2,'source':10,'center_high':12}
        b={'kind':'top','merged_index':5,'source':14,'center_high':20}
        self.assertFalse(stroke_check(a,b,'strict')[0])
        self.assertTrue(stroke_check(a,b,'loose')[0])
        b['merged_index']=4
        self.assertEqual(stroke_check(a,b,'loose')[1],'shared_standard_candle')

    def test_pen_price_is_center_high_not_disjoint_ranges(self):
        a={'kind':'bottom','merged_index':2,'source':2,'center_high':200}
        b={'kind':'top','merged_index':6,'source':6,'center_high':220}
        self.assertTrue(stroke_check(a,b,'strict')[0])
        b['center_high']=200
        self.assertFalse(stroke_check(a,b,'strict')[0])

    def test_segment_seed_rejects_zero_width(self):
        self.assertEqual(build_segments(moves([10,18,8,10])),[])

    def test_no_gap_published_example(self):
        result=build_segments(moves([10,20,12,25,15,22,13]))
        first=result[0]
        self.assertEqual((first['start_price'],first['end_price'],first['state']),(10,25,'confirmed'))
        self.assertEqual(first['confirmation']['path'],'feature_no_gap')
        self.assertEqual(first['confirm_move'],5)

    def test_gap_waits_for_second_sequence(self):
        data=moves([0,10,5,30,20,25,15,24,18,27])
        pending=characteristic_candidate(data[:6],0,2)
        self.assertEqual(pending['state'],'pending')
        confirmed=characteristic_candidate(data,0,2)
        self.assertEqual((confirmed['state'],confirmed['confirm_move']),('confirmed',8))
        self.assertEqual(confirmed['second_sequence']['kind'],'bottom')

    def test_gap_candidate_invalidated_by_new_extreme(self):
        candidate=characteristic_candidate(moves([0,10,5,30,20,25,15,31,18,32]),0,2)
        self.assertNotEqual(candidate['state'],'confirmed')

    def test_first_pen_break_needs_later_strict_cross(self):
        data=moves([10,20,14,25,18])
        self.assertEqual(build_segments(data)[0]['state'],'provisional')
        self.assertEqual(build_segments(moves([10,20,14,25,18,22,18]))[0]['state'],'provisional')
        first=build_segments(moves([10,20,14,25,18,22,16]))[0]
        self.assertEqual((first['end_price'],first['state']),(25,'confirmed'))

    def test_later_earlier_endpoint_revises_confirmed_selection(self):
        data=moves([0,10,5,30,20,25,8,15,6,16,9,18])
        early=build_segments(data[:8])[0]
        later=build_segments(data)[0]
        self.assertEqual(early['end_price'],25)
        self.assertEqual(later['end_price'],30)
        self.assertLess(later['end'],early['end'])
        self.assertGreater(later['basis_at'],early['basis_at'])

    def test_price_mirror_of_segment_examples(self):
        values=[0,10,5,30,20,25,8,15,6,16,9,18]
        a=build_segments(moves(values))[0]
        b=build_segments(moves([-x for x in values]))[0]
        self.assertEqual((a['start'],a['end'],a['confirm_move']),(b['start'],b['end'],b['confirm_move']))
        self.assertEqual(a['end_price'],-b['end_price'])

    def test_center_fixed_seed_and_touch_return(self):
        a=build_centers(moves([8,2,7,3,12,7]))[0]
        self.assertEqual((a['zd'],a['zg']),(3,7))
        self.assertEqual(a['attempts'][0]['relation'],'touch')
        self.assertIsNone(a['departure'])
        b=build_centers(moves([8,2,7,3,12,8]))[0]
        self.assertEqual(b['departure']['relation'],'outside')
        self.assertEqual(b['end'],30)  # connector and return are not old members

    def test_center_uses_actual_covered_range(self):
        data=moves([10,18,12,20])
        data[1]['actual_high']=25
        data[0]['actual_low']=8
        c=build_centers(data)[0]
        self.assertEqual((c['zd'],c['zg']),(12,18))
        self.assertEqual(c['gg'],25)
        del data[0]['actual_low']
        with self.assertRaises(ValueError):build_centers(data)

    def test_provisional_segment_cannot_confirm_center(self):
        data=moves([10,18,12,20]);data[-1]['state']='provisional'
        self.assertEqual(build_centers(data),[])

    def test_core_separation_without_outer_separation_is_not_trend(self):
        result=classify([{'id':'a','state':'formed','zd':3,'zg':7,'dd':1,'gg':12},
                         {'id':'b','state':'formed','zd':9,'zg':10,'dd':8,'gg':15}])
        self.assertEqual(result['state'],'higher_level_expansion_candidate')

    def test_third_point_requires_return_and_respects_boundary_policy(self):
        data=moves([8,2,7,3,12,7]); bars=candles([10]*60)
        signals,_=structural_signals(data,build_centers(data),bars,Config(),'synthetic')
        self.assertEqual(signals,[])
        relaxed,_=structural_signals(data,build_centers(data),bars,Config(third_boundary='not_break'),'synthetic')
        self.assertEqual(relaxed[0]['signal_class'],'third_buy_candidate')
        self.assertEqual(relaxed[0]['invalidation_operator'],'lt')

    def test_original_101_weak_second_can_undercut_first(self):
        data=moves([30,20,28,21,26,14,18,12,17,10,16,9],forces=[10,10,10,10,30,10,10,10,10,10,3])
        cs=[{'id':'c0','dd':20,'gg':28,'zd':21,'zg':26,'seed_indices':[0,1,2],'basis_at':33,'attempts':[]},
            {'id':'c1','dd':12,'gg':18,'zd':14,'zg':17,'seed_indices':[5,6,7],'basis_at':83,'attempts':[]}]
        bars=candles([20]*120)
        original,_=structural_signals(data,cs,bars,Config(second_policy='original_101'),'synthetic')
        conservative,_=structural_signals(data,cs,bars,Config(second_policy='conservative'),'synthetic')
        second=[s for s in original if s['signal_class']=='second_buy_candidate']
        self.assertEqual(len(second),1)
        self.assertEqual(second[0]['retest_kind'],'weak_or_equal_retest_101')
        self.assertFalse(any(s['signal_class']=='second_buy_candidate' for s in conservative))

    def test_macd_matches_official_small_numbers(self):
        result=macd(candles([10,12,11,13]),Config(macd_fast=3,macd_slow=5,macd_signal=3))
        self.assertAlmostEqual(result[1]['dif'],1/3)
        self.assertAlmostEqual(result[1]['dea'],1/6)
        self.assertAlmostEqual(result[1]['histogram'],1/6)


class CausalityAndInput(unittest.TestCase):
    def test_prefix_events_do_not_change_when_future_is_appended(self):
        bars=wave(260)
        short, a=replay(bars[:180],Config(max_depth=2),'synthetic')
        long, b=replay(bars,Config(max_depth=2),'synthetic')
        self.assertEqual(a,[event for event in b if event['observed_at']<180])
        self.assertTrue(any(event['kind']=='stroke' for event in b))
        for kind,objects in collections(long):
            for obj in objects:
                self.assertLessEqual(obj['basis_at'],long['as_of'])
                self.assertLessEqual(obj['available_at'],long['as_of'])

    def test_open_bar_can_be_replaced_and_revisions_are_append_only(self):
        engine=ReplayEngine(); engine.update(Candle(0,10,11,9,10,closed=False))
        previous=copy.deepcopy(engine.events)
        engine.update(Candle(0,10,12,9,11,closed=True))
        self.assertEqual(previous,engine.events[:len(previous)])
        self.assertEqual(engine.input_revision,1)
        with self.assertRaises(ValueError):engine.update(Candle(0,10,13,8,11))

    def test_history_correction_is_observed_now(self):
        engine=ReplayEngine()
        for b in candles([10,14,11,9,12]):engine.update(b)
        before=len(engine.events)
        engine.revise(1,Candle(1,10,10.2,9.8,10))
        self.assertTrue(engine.events[before:])
        self.assertTrue(all(e['observed_at']==4 for e in engine.events[before:]))

    def test_multi_timeframe_future_is_not_visible(self):
        start=datetime(2026,1,1,tzinfo=timezone.utc)
        def timed(count,minutes):
            return [Candle(i,b.open,b.high,b.low,b.close,(start+timedelta(minutes=(i+1)*minutes)).isoformat()) for i,b in enumerate(wave(count))]
        full={'30m':timed(20,30),'5m':timed(120,5),'1m':timed(600,1)}
        cut=(start+timedelta(minutes=300)).isoformat()
        shortened={key:[b for b in val if b.closed_at<=cut] for key,val in full.items()}
        a=multilevel(full,('30m','5m','1m'),cut)
        b=multilevel(shortened,('30m','5m','1m'),cut)
        self.assertEqual(a,b)
        self.assertEqual(a['levels']['30m']['input_bars'],10)

    def test_multilevel_rejects_unstated_close_times(self):
        with self.assertRaises(ValueError):multilevel({'a':wave(10),'b':wave(10),'c':wave(10)},('a','b','c'),'2026-01-01T00:00:00Z')

    def test_csv_invalid_ranges_and_duplicate_timestamps(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'bad.csv'
            p.write_text('open,high,low,close\n10,9,8,10\n',encoding='utf-8')
            with self.assertRaises(ValueError):load_csv(p)
            p.write_text('timestamp,open,high,low,close\n2026-01-01T01:00:00Z,10,11,9,10\n2026-01-01T01:00:00Z,10,11,9,10\n',encoding='utf-8')
            with self.assertRaises(ValueError):load_csv(p)

    def test_snapshot_never_claims_unproven_strict_recursion(self):
        report=analyze(wave(260),Config(),'synthetic')
        self.assertEqual(report['mode'],'structure_proxy')
        self.assertFalse(report['execution_allowed'])
        self.assertTrue(report['approximation_loss'])


if __name__=='__main__':unittest.main()
