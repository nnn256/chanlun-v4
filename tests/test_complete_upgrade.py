from dataclasses import replace
from datetime import datetime
import unittest

from chanmodel.model import Candle, Config
from chanmodel.v2.divergence import compare_divergences, make_signals
from chanmodel.v2.recursive import centers_from_units
from chanmodel.v2.engine import trigger_after
from chanmodel.v2.series import derive_series
from chanmodel.complete_study import analyze_complete
from chanmodel.complete_multilevel import join_triple
from tests.test_recursive_v2 import units, minute_series, daily_series
from chanmodel.unified import analyze_all, CompleteReplay, replay_schedule
from chanmodel.v2.divergence import invalidated_by
from run_complete import compact, export
import copy
import json
from pathlib import Path
import tempfile
import gzip
import base64


class CompletionGaps(unittest.TestCase):
    def test_conservative_policy_cannot_admit_weak_101_second(self):
        source = units([30,20,28,21,26,14,18,12,17,10,13,9])
        bars = [Candle(i,20,31,0,20) for i in range(125)]
        hist = [{'histogram':-2 if 40<=i<=50 else -.4 if 80<=i<=90 else -.01,
                 'dif':10 if i<51 else 0,'dea':0} for i in range(125)]
        centers = centers_from_units(source,1,125)
        comps = compare_divergences(source,centers,bars,hist,1,125)
        conservative = make_signals(source,centers,comps,bars,1,hist,second_policy='conservative')
        original = make_signals(source,centers,comps,bars,1,hist,second_policy='original_101')
        self.assertFalse(any(s.get('retest_mode')=='weak_retest_101' for s in conservative))
        self.assertTrue(any(s.get('retest_mode')=='weak_retest_101' for s in original))

    def test_ended_historical_stroke_is_not_a_new_live_trigger(self):
        fine = minute_series(1)
        proof = {'available_time':fine.bars[10].closed_at,'return_end_time':fine.bars[8].closed_at}
        def stroke(start,end,basis):
            return {'id':str(start),'start':start,'end':end,'basis_at':basis,'state':'confirmed',
                    'direction':'up','confirmation_basis':'next-down','end_price':101}
        report = {'base':{'strokes':[stroke(8,9,15),stroke(12,16,20)]}}
        result = trigger_after({'side':'buy','invalidation':90},proof,fine,report,
                               fine.bars[-1].closed_at,datetime.fromisoformat(fine.bars[11].closed_at))
        self.assertEqual(result['stroke_id'],'12')

    def test_fine_price_invalidation_applies_while_waiting_for_confirmation(self):
        fine = minute_series(2);cut = fine.bars[-1].closed_at
        confirm = derive_series(fine,'30m',cut);trade = derive_series(confirm,'day',cut)
        parent = {'id':'parent','class':'third_buy','rank':1,'side':'buy','price':100,'anchor':0,
                  'state':'awaiting_lower_confirmation','terminal_range':[0,0],'available_at':0,'invalidation':1000}
        reports = {'day':{'signals':[parent]},'30m':{},'5m':{}}
        pair = {'comparisons':[],'gaps':[],'matched_start':None,'matched_end':None}
        result = join_triple(reports,{'day':trade,'30m':confirm,'5m':fine},('day','30m','5m'),
                             {'day>30m':pair,'30m>5m':pair},cut)
        self.assertEqual(result[0]['state'],'invalidated')
        self.assertIn('invalidation_time',result[0])

    def test_full_study_retains_identity_adjustment_and_configuration(self):
        source = daily_series(90)
        config = Config(stroke_mode='strict',second_policy='conservative',macd_multiplier=2)
        report = analyze_complete(source.symbol,'day',source.bars,source.bars[-1].closed_at,'receipt',
                                  source='caller_csv',adjustment='qfq',config=config,max_rank=2)
        self.assertEqual(report['input_audit']['source'],'caller_csv')
        self.assertEqual(report['input_audit']['adjustment'],'qfq')
        self.assertEqual(report['config']['second_policy'],'conservative')
        self.assertTrue(all(layer['rank']<=2 for layer in report['hierarchy']['levels']))

    def test_all_six_classes_have_working_structural_positive_paths(self):
        values=[30,20,28,21,26,14,18,12,17,10,13,9]
        kinds=set()
        for mirror in [False,True]:
            source=units([40-x for x in values] if mirror else values)
            bars=[Candle(i,20,40,0,20) for i in range(125)]
            sign=1 if mirror else -1
            hist=[{'histogram':sign*(2 if 40<=i<=50 else .4 if 80<=i<=90 else .01),
                   'dif':10 if i<51 else 0,'dea':0} for i in range(125)]
            centers=centers_from_units(source,1,125)
            comps=compare_divergences(source,centers,bars,hist,1,125)
            signals=make_signals(source,centers,comps,bars,1,hist)
            kinds.update(s['class'] for s in signals)
        self.assertEqual(kinds,{'first_buy','second_buy','third_buy','first_sell','second_sell','third_sell'})

    def test_third_touch_policy_changes_signal_boundary_without_changing_center(self):
        source=units([8,2,7,3,12,7]);centers=centers_from_units(source,1,70)
        bars=[Candle(i,6,6.5,5.5,6) for i in range(70)]
        strict=make_signals(source,centers,[],bars,1)
        relaxed=make_signals(source,centers,[],bars,1,third_boundary='not_break')
        self.assertFalse(strict);self.assertEqual(relaxed[0]['class'],'third_buy')
        self.assertEqual(relaxed[0]['invalidation_operator'],'lt')
        touch=Candle(0,7,8,7,7)
        self.assertFalse(invalidated_by(relaxed[0],touch))
        self.assertTrue(invalidated_by({**relaxed[0],'invalidation_operator':'le'},touch))


class UnifiedPipeline(unittest.TestCase):
    def test_complete_snapshot_equals_cutoff_prefix(self):
        s=daily_series(200);cut=s.bars[149].closed_at
        a=analyze_all({'day':s},cut,max_rank=3)
        b=analyze_all({'day':replace(s,bars=s.bars[:150])},cut,max_rank=3)
        self.assertEqual(a,b)
        self.assertEqual(len(a['six_class_coverage']),6)
        self.assertEqual(a['reports']['day']['certificate_audit']['errors'],[])

    def test_full_layer_event_prefix_is_immutable_and_caller_cannot_mutate_it(self):
        s=daily_series(160);times=[b.closed_at for b in s.bars[119:130]]
        a=CompleteReplay({'day':s},max_rank=3)
        b=CompleteReplay({'day':replace(s,bars=s.bars[:130])},max_rank=3)
        for t in times[:6]:a.observe(t)
        prefix=copy.deepcopy(a.events)
        for t in times[6:]:a.observe(t)
        self.assertEqual(prefix,a.events[:len(prefix)])
        for t in times:b.observe(t)
        self.assertEqual(a.events,b.events)
        old=copy.deepcopy(a.events)
        a.snapshot['reports']['day']['base']['merged'].clear()
        self.assertEqual(old,a.events)
        with self.assertRaises(ValueError):a.observe(times[-1])

    def test_mismatched_security_and_identity_alias_are_rejected(self):
        s=daily_series(50);cut=s.bars[-1].closed_at
        for inputs in [{'day':s,'5m':replace(s,symbol='sh600000')},{'fake':s}]:
            with self.assertRaises(ValueError):analyze_all(inputs,cut)

    def test_schedule_retains_all_warm_history(self):
        s=daily_series(120)
        times,schedule=replay_schedule({'day':s},s.bars[-1].closed_at,10,3)
        self.assertEqual(times[-1],s.bars[-1].closed_at)
        self.assertEqual(schedule['first'],s.bars[-10].closed_at)
        engine=CompleteReplay({'day':s},max_rank=2)
        report=engine.observe(times[0])
        self.assertEqual(report['reports']['day']['input_audit']['bars_used'],111)

    def test_export_offline_payload_matches_complete_snapshot_and_preserves_inputs(self):
        s=daily_series(80);cut=s.bars[-1].closed_at;r=analyze_all({'day':s},cut,max_rank=2)
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'out';export(out,{'day':s},r,[],[],{'observations':1},{})
            page=(out/'完整算法复习.html').read_text(encoding='utf-8')
            encoded=page.split('<script id="packed" type="application/octet-stream">',1)[1].split('</script>',1)[0]
            bundle=json.loads(gzip.decompress(base64.b64decode(encoded)))
            self.assertEqual(bundle['frames'][0]['reports']['day']['hierarchy'],r['reports']['day']['hierarchy'])
            self.assertEqual(bundle['bars']['day'][-1]['close'],s.bars[-1].close)
            before=(out/'report.json').read_bytes()
            with self.assertRaises(ValueError):export(out,{'day':s},r,[],[],{}, {})
            self.assertEqual(before,(out/'report.json').read_bytes())


if __name__=='__main__':unittest.main()
