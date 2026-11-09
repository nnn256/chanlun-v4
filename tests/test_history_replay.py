import copy
from datetime import datetime,timedelta,timezone
import math
import random
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chanmodel.engine import analyze,object_body
from chanmodel.model import Candle,Config
from chanmodel.structure import merge_bars,fractals_at,strokes_at
from history_replay import SignalOnlyReplay,replay_daily,open_checkpoint,save_checkpoint,label_groups,normalized


def bars(count):
    start=datetime(2020,1,1,15,tzinfo=timezone(timedelta(hours=8)))
    return [Candle(i,c,c+.2,c-.2,c,(start+timedelta(days=i)).isoformat(),1000) for i in range(count)
            for c in [100+30*math.sin(i/40)+9*math.sin(i/10)+3*math.sin(i/2.5)]]


class HistoryReplayTests(unittest.TestCase):
    def test_gate_signal_snapshots_match_original_every_prefix(self):
        data=bars(360);engine=SignalOnlyReplay()
        for stop,bar in enumerate(data,1):
            actual=engine.update(bar)
            expected=[object_body(p) for p in analyze(data[:stop],Config(),'day')['signal_candidates']]
            self.assertEqual(actual,expected,f'prefix={stop}')
        self.assertGreater(engine.reused_structure_prefixes,0)

    def test_gate_without_reference_call_keeps_dynamic_signal_invalidation(self):
        data=bars(700)
        engine=SignalOnlyReplay()
        for stop,bar in enumerate(data,1):
            actual=engine.update(bar)
            expected=[object_body(p) for p in analyze(data[:stop],Config(),'day')['signal_candidates']]
            self.assertEqual(actual,expected,f'prefix={stop}')
        self.assertTrue(engine.events)

    def test_caller_cannot_mutate_past_event_values(self):
        data=bars(700);engine=SignalOnlyReplay()
        for bar in data[:600]:engine.update(bar)
        past=copy.deepcopy(engine.events)
        for bar in data[600:]:engine.update(bar)
        self.assertEqual(past,engine.events[:len(past)])

    def test_incremental_preprocessing_matches_random_reference(self):
        rng=random.Random(728);data=[];price=100
        for i in range(250):
            previous=price;price+=rng.uniform(-2,2)
            data.append(Candle(i,previous,max(previous,price)+rng.random()*3,min(previous,price)-rng.random()*3,price))
        engine=SignalOnlyReplay()
        for stop,bar in enumerate(data,1):
            engine.update(bar)
            expected=merge_bars([normalized(b,Config()) for b in data[:stop]],Config())
            self.assertEqual(engine.merged,expected)
            self.assertEqual(engine.fractals,fractals_at(expected))
            self.assertEqual(engine.last_strokes,strokes_at(fractals_at(expected),Config())[0])

    def test_signal_only_events_match_reference_route(self):
        data=bars(420)
        fast,events,_=replay_daily(data,gated=True)
        slow,expected,_=replay_daily(data,gated=False)
        self.assertEqual(events,expected)
        self.assertLess(fast.full_analyses,slow.full_analyses)

    def test_future_append_does_not_rewrite_old_events(self):
        data=bars(420)
        _,short,_=replay_daily(data[:300])
        _,long,_=replay_daily(data)
        self.assertEqual(short,[e for e in long if e['observed_at']<300])

    def test_window_uses_full_prior_history_and_excludes_baseline_entries(self):
        data=bars(420);start=data[300].closed_at[:10]
        fast,events,entries=replay_daily(data,start_date=start)
        slow,reference,reference_entries=replay_daily(data,start_date=start,gated=False)
        self.assertEqual(events,reference)
        self.assertEqual(entries,reference_entries)
        self.assertEqual(fast.warmup_bars,300)
        self.assertEqual(fast.visited_prefixes,120)
        self.assertEqual(len(fast.bars),420)
        self.assertTrue(all(e['observed_at']>=300 for e in events))

    def test_open_or_out_of_order_bars_rejected(self):
        engine=SignalOnlyReplay()
        with self.assertRaises(ValueError):engine.update(Candle(1,10,11,9,10))
        with self.assertRaises(ValueError):engine.update(Candle(0,10,11,9,10,closed=False))

    def test_checkpoint_preserves_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            db=open_checkpoint(Path(temporary)/'checkpoint.sqlite3')
            result={'symbol':'bj920002','status':'success','fingerprint':'abc','input_sha256':'123','visited_prefixes':591}
            save_checkpoint(db,result)
            row=db.execute('SELECT symbol,status,fingerprint,input_sha256,result_json FROM history_stocks').fetchone()
            self.assertEqual(row[:4],('bj920002','success','abc','123'))
            db.close()

    def test_aggregate_labels_keeps_missing_outcomes(self):
        rows=[{'signal_class':'first_buy_candidate','horizon_bars':5,'status':'labelled','gross_directional_return':.1,'hypothetical_return_after_constant_cost':.098},
              {'signal_class':'first_buy_candidate','horizon_bars':5,'status':'insufficient_future_for_label'}]
        group=label_groups(rows)[0]
        self.assertEqual((group['observations'],group['labelled'],group['positive']),(2,1,1))
        self.assertEqual(group['excluded'],{'insufficient_future_for_label':1})


if __name__=='__main__':unittest.main()
