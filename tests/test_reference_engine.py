from dataclasses import replace
import copy
import hashlib
import json
from pathlib import Path
import unittest

from chanmodel.reference_engine import analyze_reference,run_reference,review_geometry,SETTINGS,COMMIT,VENDOR
from tests.test_recursive_v2 import daily_series


def endpoint_signature(engine):
    return {'strokes':[(b.get_begin_klu().idx,b.get_end_klu().idx,b.is_sure) for b in engine.bi_list],
            'segments':[(b.get_begin_klu().idx,b.get_end_klu().idx,b.is_sure) for b in engine.seg_list]}


class ReferenceEngineContract(unittest.TestCase):
    def test_exact_source_pin_and_mit_license_are_verified(self):
        receipt=json.loads((VENDOR/'provenance.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['commit'],COMMIT)
        for name,h in receipt['files'].items():self.assertEqual(hashlib.sha256((VENDOR/name).read_bytes()).hexdigest(),h,name)
        self.assertIn('MIT License',(VENDOR/'LICENSE').read_text(encoding='utf-8'))

    def test_adapter_preserves_native_vertices_and_structure_flags(self):
        series=daily_series(260);engine,_,_,_,_=run_reference(series.bars,'day')
        result=analyze_reference(series.symbol,'day',series.bars,series.bars[-1].closed_at)
        native=endpoint_signature(engine)
        for key in ['strokes','segments']:
            actual=[(b['start'],b['end'],b['state']=='confirmed') for b in result['base'][key]]
            self.assertEqual(actual,native[key])
        self.assertEqual([(s['start'],s['end']) for s in result['base']['higher_segments']],
                         [(s.get_begin_klu().idx,s.get_end_klu().idx) for s in engine.segseg_list])

    def test_every_signal_uses_native_endpoint_not_an_invented_retroactive_low(self):
        series=daily_series(500);engine,_,_,_,_=run_reference(series.bars,'day')
        expected={(rank,p.klu.idx,t.value,'buy' if p.is_buy else 'sell') for rank,collection in [(1,engine.bs_point_lst),(2,engine.seg_bs_point_lst)] for p in collection.bsp_iter() for t in p.type}
        report=analyze_reference(series.symbol,'day',series.bars,series.bars[-1].closed_at)
        self.assertEqual({(p['rank'],p['anchor'],p['native_type'],p['side']) for p in report['signals']},expected)
        for p in report['signals']:
            raw=series.bars[p['anchor']]
            self.assertEqual(p['price'],raw.low if p['side']=='buy' else raw.high)
            self.assertGreaterEqual(p['available_at'],p['anchor'])
            self.assertEqual(p['engine_commit'],COMMIT)

    def test_prefix_events_are_immutable_when_future_data_is_appended(self):
        series=daily_series(400);cut=series.bars[249].closed_at
        short=analyze_reference(series.symbol,'day',series.bars[:250],cut)
        full=analyze_reference(series.symbol,'day',series.bars,series.bars[-1].closed_at)
        self.assertEqual(short['observed_signal_events'],[e for e in full['observed_signal_events'] if e['observed_index']<250])
        cutoff_result=analyze_reference(series.symbol,'day',series.bars,cut)
        self.assertEqual(short['base'],cutoff_result['base']);self.assertEqual(short['signals'],cutoff_result['signals'])

    def test_uncertain_or_disputed_structure_never_becomes_confirmed_signal(self):
        report={'base':{'fractals':[{'source':0,'merged_index':0},{'source':5,'merged_index':2}],
                        'merged':[{'low':10,'high':20},{'low':5,'high':25},{'low':12,'high':22}],
                        'strokes':[{'id':'bad','start':0,'end':5,'start_price':10,'end_price':22,'direction':'up','state':'confirmed'}],
                        'segments':[]},'signals':[{'terminal_range':[0,5],'state':'structure_complete','rule_audit':{'passed':True,'local_geometry_completed':True}}]}
        reviewed=review_geometry(report)
        self.assertEqual(reviewed['geometry_review']['stroke_issues'],1)
        self.assertEqual(reviewed['signals'][0]['state'],'needs_review')
        self.assertFalse(reviewed['signals'][0]['rule_audit']['passed'])

    def test_raw_input_is_not_rounded_replaced_or_padded_by_adapter(self):
        series=daily_series(120);before=copy.deepcopy(series.bars)
        result=analyze_reference(series.symbol,'day',series.bars,series.bars[-1].closed_at)
        self.assertEqual(series.bars,before);self.assertEqual(result['input_audit']['bars_used'],120)
        merged_components=[i for m in result['base']['merged'] for i in m['components']]
        self.assertEqual(merged_components,list(range(120)))

    def test_final_snapshot_geometry_matches_every_bar_engine_without_backdating_events(self):
        series=daily_series(400)
        final,_,_,events,_=run_reference(series.bars,'day',False)
        prefix,_,_,_,_=run_reference(series.bars,'day',True)
        self.assertEqual(endpoint_signature(final),endpoint_signature(prefix))
        self.assertFalse(events)
        report=analyze_reference(series.symbol,'day',series.bars,series.bars[-1].closed_at,observe=False)
        self.assertEqual(report['observation_mode'],'final_cutoff_snapshot_no_historical_first_seen_claim')


if __name__=='__main__':unittest.main()
