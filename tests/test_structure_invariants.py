"""Independent geometric invariants from lessons 62/67/71/78."""
import math
import random
import unittest

from chanmodel.model import Candle,Config
from chanmodel.structure import merge_bars,fractals_at,strokes_at
from chanmodel.segments import build_segments
from tests.test_engine import moves


def check_strokes(test,bars):
    config=Config(stroke_mode='strict');standard=merge_bars(bars,config);fractals=fractals_at(standard)
    strokes,_=strokes_at(fractals,config);by_source={f['source']:f for f in fractals}
    for s in strokes:
        a=by_source[s['start']]['merged_index'];b=by_source[s['end']]['merged_index'];part=standard[a:b+1]
        low=min(k['low'] for k in part);high=max(k['high'] for k in part)
        if s['direction']=='up':test.assertEqual((s['start_price'],s['end_price']),(low,high),s['id'])
        else:test.assertEqual((s['start_price'],s['end_price']),(high,low),s['id'])
    for a,b in zip(strokes,strokes[1:]):
        test.assertEqual(a['end'],b['start']);test.assertEqual(a['end_price'],b['start_price'])
    return strokes


class LegacyDefectReproductions(unittest.TestCase):
    def test_later_lower_peak_cannot_end_segment_containing_higher_peak(self):
        source=moves([0,10,5,30,20,25,8,15,6,16,9,18])
        result=build_segments(source[:8])
        self.assertTrue(result)
        # Preserve an executable counterexample proving why this engine is no
        # longer used to certify the new charts. Do not erase it to get green.
        self.assertEqual(result[0]['end_price'],25)
        self.assertGreater(max(m['end_price'] for m in source[:5]),result[0]['end_price'])

    def test_every_segment_endpoint_bounds_all_its_input_vertices(self):
        disagreements=[]
        for values in [[10,20,12,25,15,22,13],[0,10,5,30,20,25,8,15,6,16,9,18],
                       [0,10,-1,12,2,15,4,13,1,10,3,14]]:
            source=moves(values)
            for segment in build_segments(source):
                part=source[segment['start_move']:segment['end_move']+1]
                vertices=[m['start_price'] for m in part]+[part[-1]['end_price']]
                expected=(min(vertices),max(vertices)) if segment['direction']=='up' else (max(vertices),min(vertices))
                if (segment['start_price'],segment['end_price'])!=expected:disagreements.append(segment['id'])
        self.assertTrue(disagreements,'the documented legacy counterexample unexpectedly disappeared')

    def test_standard_candle_extrema_are_not_skipped_by_short_reversals(self):
        rng=random.Random(134);price=100;bars=[]
        for i in range(500):
            close=price+rng.uniform(-2,2)
            bars.append(Candle(i,price,max(price,close)+rng.random(),min(price,close)-rng.random(),close));price=close
        with self.assertRaises(AssertionError):check_strokes(self,bars)

    def test_standard_rule_is_price_mirror_symmetric(self):
        rng=random.Random(9);price=100;bars=[]
        for i in range(200):
            close=price+rng.uniform(-3,3);bars.append(Candle(i,price,max(price,close)+.1,min(price,close)-.1,close));price=close
        mirror=[Candle(b.pos,200-b.open,200-b.low,200-b.high,200-b.close) for b in bars]
        with self.assertRaises(AssertionError):check_strokes(self,bars)
        with self.assertRaises(AssertionError):check_strokes(self,mirror)


if __name__=='__main__':unittest.main()
