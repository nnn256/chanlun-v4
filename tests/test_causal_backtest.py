from dataclasses import replace
from datetime import datetime,timedelta
import copy
import math
import random
import unittest

from chanmodel.causal_replay import StreamingStructure,SignalLedger,source_rule_audit,abcd_objects,CONFIG
from chanmodel.causal_portfolio import simulate,CostModel
from chanmodel.model import Candle
from chanmodel.structure import merge_bars,fractals_at,strokes_at
from chanmodel.segments import build_segments
from chanmodel.v2.series import CN
from chanmodel.v2.recursive import centers_from_units
from tests.test_recursive_v2 import units


def sample(n=130,seed=4):
    rng=random.Random(seed);out=[];p=100.;date=datetime(2026,1,5,15,tzinfo=CN)
    while len(out)<n:
        if date.weekday()<5:
            c=p+rng.uniform(-4,4);out.append(Candle(len(out),p,max(p,c)+rng.random(),min(p,c)-rng.random(),c,date.isoformat(),100000));p=c
        date+=timedelta(days=1)
    return out


def third_report(rank=2):
    child=units([0,10,5,12,8,22,18,24],rank=rank-1)
    center=centers_from_units(child,rank,100)[0];dep=center['departure'];ret=child[dep['return_index']]
    point={'id':'third','class':'third_buy','side':'buy','rank':rank,'anchor':ret['end'],'price':ret['end_price'],
           'terminal_range':[ret['start'],ret['end']],'center_id':center['id'],'available_at':dep['available_at'],
           'invalidation':center['zg'],'state':'awaiting_lower_confirmation','equality_policy':'strict_outside'}
    lower={'rank':rank-1,'completed_types':child,'centers':[],'comparisons':[]}
    upper={'rank':rank,'completed_types':[],'centers':[center],'comparisons':[]}
    report={'hierarchy':{'base_units':child if rank==1 else [],'levels':([lower] if rank>1 else [])+[upper]},
            'signals':[point],'divergence_comparisons':[],'as_of_index':100}
    return point,report


class CausalReplayTests(unittest.TestCase):
    def test_stream_equals_batch_for_every_prefix_and_extreme_containment(self):
        for seed in [1,7,13]:
            bars=sample(160,seed);stream=StreamingStructure()
            for i,b in enumerate(bars):
                stream.append(b);batch=merge_bars(bars[:i+1],CONFIG);f=fractals_at(batch);strokes,_=strokes_at(f,CONFIG)
                self.assertEqual(stream.merged,batch)
                self.assertEqual(stream.fractals,f)
                self.assertEqual(stream.strokes,strokes)
                segments=build_segments(strokes)
                for a,c in zip(stream.segments,segments):
                    self.assertEqual({k:a[k] for k in c},c)
                self.assertEqual(len(stream.segments),len(segments))

    def test_future_tail_does_not_change_signal_event_prefix(self):
        def replay(bars):
            s=StreamingStructure();ledger=SignalLedger('day')
            for b in bars:
                changed=s.append(b);ledger.invalidate(b)
                if changed:ledger.observe(s.snapshot(),b,s.bars)
            return ledger.events
        b=sample(240,21);a=replay(b[:170]);full=replay(b)
        self.assertEqual(a,[x for x in full if x['observed_index']<170])

    def test_strict_child_type_gate_rejects_renamed_base_segments(self):
        p,r=third_report(1);audit=source_rule_audit(p,r)
        self.assertFalse(audit['passed']);self.assertIn('completed_child_types',audit['missing'])

    def test_third_confirm_requires_completed_return_and_opposite_boundary(self):
        p,r=third_report();self.assertTrue(source_rule_audit(p,r)['passed'])
        cut=copy.deepcopy(r);cut['hierarchy']['levels'][0]['completed_types']=cut['hierarchy']['levels'][0]['completed_types'][:-1]
        self.assertFalse(source_rule_audit(p,cut)['passed'])

    def test_touching_core_cannot_be_confirmed(self):
        p,r=third_report();child=r['hierarchy']['levels'][0]['completed_types'];ret=next(u for u in child if u['end']==p['anchor'])
        ret['low']=r['hierarchy']['levels'][1]['centers'][0]['zg']
        self.assertIn('first_return_strictly_outside',source_rule_audit(p,r)['missing'])

    def test_decomposition_endpoint_cannot_replace_actual_price_extreme(self):
        p,r=third_report();p['price']+=.25
        self.assertIn('price_equals_actual_terminal_extreme',source_rule_audit(p,r)['missing'])

    def test_confirmation_history_survives_later_invalidation(self):
        p,r=third_report();bars=[Candle(i,20,21,19,20,(datetime(2026,1,1,tzinfo=CN)+timedelta(days=i)).isoformat(),100000) for i in range(102)]
        ledger=SignalLedger('day');ledger.observe(r,bars[100],bars)
        self.assertEqual(len(ledger.confirmations),1)
        bad=replace(bars[101],low=1);ledger.invalidate(bad)
        self.assertEqual(ledger.records[p['id']]['state'],'invalidated')
        self.assertEqual(len(ledger.confirmations),1);self.assertEqual(ledger.events[-1]['event'],'invalidated')

    def test_confirmation_is_withdrawn_if_completion_evidence_disappears(self):
        p,r=third_report();bars=[Candle(i,20,21,19,20,(datetime(2026,1,1,tzinfo=CN)+timedelta(days=i)).isoformat(),100000) for i in range(102)]
        ledger=SignalLedger('day');ledger.observe(r,bars[100],bars)
        r['hierarchy']['levels'][0]['completed_types']=r['hierarchy']['levels'][0]['completed_types'][:-1]
        r['as_of_index']=101;ledger.observe(r,bars[101],bars)
        self.assertEqual(ledger.events[-1]['event'],'withdrawn')
        self.assertEqual(ledger.records[p['id']]['state'],'withdrawn')
        self.assertEqual(len(ledger.confirmations),1)

    def test_original_abcd_missing_d_is_explicit_not_fabricated(self):
        stream=StreamingStructure()
        for b in sample(200):stream.append(b)
        report=stream.snapshot();groups=abcd_objects(report)
        for g in groups:
            if g['mode']=='four_consecutive_same_rank':
                self.assertEqual([x['label'] for x in g['letters']],list('abcd'[:len(g['letters'])]))


class PortfolioTests(unittest.TestCase):
    def bars(self):
        times=['2026-11-02T09:35:00+08:00','2026-11-02T09:40:00+08:00','2026-11-02T09:45:00+08:00',
               '2026-11-03T09:35:00+08:00','2026-11-03T09:40:00+08:00']
        return [Candle(i,10+i,11+i,9+i,10+i,t,1000000) for i,t in enumerate(times)]

    def event(self,i,side='buy',event='confirmed'):
        return {'observed_index':i,'observed_at':self.bars()[i].closed_at,'signal_id':'s','event':event,'side':side,
                'rank':2,'class':'third_'+side,'invalidation':1}

    def test_next_bar_fill_and_T1(self):
        bars=self.bars();events=[self.event(0),self.event(1,'sell')]
        r=simulate(bars,bars,events,{'2026-11-02':12,'2026-11-03':14})
        buy=next(x for x in r['orders'] if x['state']=='filled' and x['side']=='buy')
        sell=next(x for x in r['orders'] if x['state']=='filled' and x['side']=='sell')
        self.assertEqual(buy['bar_index'],1);self.assertEqual(sell['bar_index'],3)
        self.assertGreater(buy['price'],bars[1].open)
        self.assertTrue(any(x.get('reason')=='T_plus_1_locked' for x in r['orders']))

    def test_fee_minimum_and_insufficient_future_not_forced_exit(self):
        bars=self.bars();r=simulate(bars,bars,[self.event(3)],{'2026-11-02':12,'2026-11-03':14})
        self.assertEqual(r['metrics']['completed_trades'],0);self.assertIsNotNone(r['open_position'])
        self.assertGreaterEqual(r['metrics']['total_fees'],5)

    def test_forward_split_preserves_holdings_economic_value(self):
        times=['2026-05-28T10:00:00+08:00','2026-05-28T10:30:00+08:00','2026-05-29T10:00:00+08:00']
        prices=[14.5,14.5,10];raw=[Candle(i,p,p,p,p,t,1000000) for i,(p,t) in enumerate(zip(prices,times))]
        adj=[replace(b,open=14.5,high=14.5,low=14.5,close=14.5) for b in raw]
        e={'observed_index':0,'observed_at':times[0],'signal_id':'s','event':'confirmed','side':'buy','rank':2,'class':'third_buy','invalidation':1}
        action={'ex_date':'2026-05-29','share_multiplier':1.45,'cash_per_share':0}
        r=simulate(raw,adj,[e],{'2026-05-28':14.5,'2026-05-29':10},[action],CostModel(slippage_bps=0))
        self.assertAlmostEqual(r['equity'][1]['equity'],r['equity'][2]['equity'])
        self.assertAlmostEqual(r['equity'][2]['shares'],r['equity'][1]['shares']*1.45)

    def test_upper_limit_blocks_entry_and_does_not_fill_retroactively(self):
        bars=self.bars();bars[1]=replace(bars[1],open=12,high=12,low=12,close=12)
        daily={f'2026-10-{d:02d}':10 for d in range(20,30)};daily.update({'2026-11-02':12,'2026-11-03':14})
        r=simulate(bars,bars,[self.event(0)],daily)
        self.assertTrue(any(x.get('reason')=='open_at_upper_limit' for x in r['orders']))
        self.assertFalse(any(x['state']=='filled' and x['bar_index']==1 for x in r['orders']))

    def test_buy_is_cancelled_if_next_open_gaps_through_invalidation(self):
        bars=self.bars();event=self.event(0);event['invalidation']=9.5
        bars[1]=replace(bars[1],open=9,high=12,low=8,close=11)
        r=simulate(bars,bars,[event],{'2026-11-02':12,'2026-11-03':14})
        self.assertTrue(any(x.get('reason')=='opening_gap_invalidates_buy' for x in r['orders']))
        self.assertFalse(any(x['state']=='filled' for x in r['orders']))


if __name__=='__main__':unittest.main()
