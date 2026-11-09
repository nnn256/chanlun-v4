import unittest

from chanmodel.evaluate import evaluate_events
from chanmodel.model import Candle, Config, parse_time
from chanmodel.engine import ReplayEngine


class EvaluationTests(unittest.TestCase):
    def test_entry_uses_next_bar_open_not_old_anchor(self):
        bars=[Candle(i,price,price+1,price-1,price) for i,price in enumerate([10,20,40,44,48])]
        event={'kind':'signal','event':'appeared','id':'candidate','observed_at':1,
               'value':{'status':'candidate','signal_class':'first_buy_candidate','side':'buy','anchor':0,'definition_mode':'signal_proxy'}}
        result=evaluate_events(bars,[event],(2,),0)
        row=result['labels'][0]
        self.assertEqual((row['entry_index'],row['entry_price'],row['exit_index']),(2,40,3))
        self.assertAlmostEqual(row['gross_directional_return'],0.1)

    def test_revisions_do_not_count_same_id_twice_and_tail_is_missing(self):
        bars=[Candle(i,10,11,9,10) for i in range(4)]
        event={'kind':'signal','id':'candidate','observed_at':2,
               'value':{'status':'candidate','signal_class':'third_sell_candidate','side':'sell','definition_mode':'signal_proxy'}}
        result=evaluate_events(bars,[event,event],(3,),20)
        self.assertEqual(len(result['labels']),1)
        self.assertEqual(result['labels'][0]['status'],'insufficient_future_for_label')

    def test_invalidated_candidate_and_zero_volume_entry_are_not_fills(self):
        bars=[Candle(i,10,11,9,10,volume=0) for i in range(6)]
        point={'signal_class':'first_buy_candidate','side':'buy','definition_mode':'signal_proxy'}
        events=[{'kind':'signal','id':'old','observed_at':1,'value':{**point,'status':'invalidated_in_known_delay'}},
                {'kind':'signal','id':'new','observed_at':1,'value':{**point,'status':'candidate'}}]
        result=evaluate_events(bars,events,(2,),20)
        self.assertEqual(len(result['labels']),1)
        self.assertEqual(result['labels'][0]['status'],'zero_volume_entry')

    def test_rejected_update_does_not_corrupt_previous_state(self):
        engine=ReplayEngine()
        engine.update(Candle(0,10,11,9,10,'2026-01-01T01:00:00Z'))
        with self.assertRaises(ValueError):engine.update(Candle(1,10,11,9,10,'2026-01-01T00:00:00Z'))
        self.assertEqual(len(engine.bars),1)

    def test_naive_time_has_explicit_fixed_offset(self):
        value,assumed=parse_time('2026-01-01T09:30:00')
        self.assertTrue(assumed)
        self.assertEqual(value,'2026-01-01T01:30:00+00:00')

    def test_nonfinite_cost_is_rejected(self):
        with self.assertRaises(ValueError):evaluate_events([],[],(2,),float('nan'))


if __name__=='__main__':unittest.main()
