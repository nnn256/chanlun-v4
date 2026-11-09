from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench import store,monitor
from chanmodel.market_backtest import aggregate,backtest_one
from chanmodel.causal_portfolio import simulate
from chanmodel.model import Candle
from chanmodel.v2.series import CN
from tests.test_causal_backtest import sample
from chanmodel.tdx_data import atomic_bytes
from workbench import server


class MonitorStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.db=self.root/'state.sqlite3';store.initialize(self.db)
        self.patch=patch.object(monitor,'connect',lambda:store.connect(self.db));self.patch.start()
        with store.connect(self.db) as db:db.execute("INSERT INTO universe(symbol,code,name,exchange,active) VALUES('sz000001','000001','平安银行','sz',1)")
    def tearDown(self):self.patch.stop();self.tmp.cleanup()
    def value(self,price=10,state='awaiting_lower_confirmation'):
        point={'id':'p1','signal_id':'day:p1','class':'third_buy','state':state,'period':'day','side':'buy',
               'price':price,'anchor':10,'invalidation':9,'invalidation_operator':'le','available_time':'2026-11-09T15:00:00+08:00','age_bars':0}
        return {'symbol':'sz000001','state':'calculated','as_of':'2026-11-10T10:00:00+08:00','signals':[point]}
    def test_baseline_suppressed_changes_alert_once(self):
        self.assertEqual(monitor.record_structure(self.value()),[])
        self.assertEqual(monitor.record_structure(self.value()),[])
        self.assertEqual(len(monitor.record_structure(self.value(11))),1)
        self.assertEqual(monitor.record_structure(self.value(11)),[])
        self.assertEqual(monitor.overview()['counts']['unread'],1)
    def test_failed_history_refresh_never_revokes_good_structure(self):
        monitor.record_structure(self.value());monitor.record_structure({'symbol':'sz000001','as_of':'x','state':'failed','signals':[]})
        self.assertEqual(len(monitor.overview()['signals']),1)
    def test_quote_boundary_alert_never_upgrades_signal_and_deduplicates(self):
        monitor.record_structure(self.value());quote={'symbol':'sz000001','quote_status':'observed','price':8.9,'received_at':'2026-11-10T10:01:00+08:00'}
        self.assertEqual(len(monitor.quote_invalidations([quote])),1)
        self.assertEqual(monitor.quote_invalidations([quote]),[])
        result=monitor.overview();self.assertEqual(result['signals'][0]['state'],'awaiting_lower_confirmation')
        self.assertTrue(result['alerts'][0]['payload']['closed_bar_confirmation_required'])
    def test_disappeared_signal_withdraws_once(self):
        monitor.record_structure(self.value());empty={**self.value(),'signals':[]};monitor.record_structure(empty);monitor.record_structure(empty)
        result=monitor.overview();self.assertFalse(result['signals']);self.assertEqual(len(result['alerts']),1)
    def test_validate_config_and_persisted_reopen(self):
        for values in [{'quote_interval':1},{'scope':'unknown'},{'symbols':['unlisted']},{'workers':99},{'scope':'selected','symbols':[]}]:
            with self.assertRaises(ValueError):monitor.configure(values)
        monitor.configure({'enabled':True,'scope':'selected','symbols':['sz000001']})
        self.assertTrue(monitor.config()['enabled'])
    def test_closed_hours_and_lunch_do_not_claim_trading(self):
        for hour,minute in [(2,0),(12,0),(16,0)]:
            self.assertEqual(monitor.session_state(datetime(2026,11,10,hour,minute,tzinfo=CN))['state'],'outside_session')
    def test_empty_signal_baseline_does_not_invent_alerts(self):
        monitor.record_structure({**self.value(),'signals':[]});self.assertEqual(monitor.overview()['counts']['unread'],0)
    def test_source_replay_rejects_today_candidate_as_past_fill(self):
        from chanmodel.causal_replay import StreamingStructure,SignalLedger
        bars=sample(120,5)
        def replay(values):
            stream=StreamingStructure();ledger=SignalLedger('day')
            for b in values:
                changed=stream.append(b);ledger.invalidate(b)
                if changed:ledger.observe(stream.snapshot(),b,stream.bars)
            return ledger.events
        self.assertEqual(replay(bars[:90]),[e for e in replay(bars) if e['observed_index']<90])


class MarketBacktestTests(unittest.TestCase):
    def test_windows_progress_sharing_denial_is_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'summary.json';path.write_bytes(b'old')
            original=Path.replace;calls=[]
            def replace_once(source,target):
                calls.append(1)
                if len(calls)==1:raise PermissionError('sharing conflict')
                return original(source,target)
            with patch.object(Path,'replace',replace_once),patch('chanmodel.tdx_data.time_module.sleep'):
                atomic_bytes(path,b'new')
            self.assertEqual(path.read_bytes(),b'new');self.assertEqual(len(calls),2)
    def test_failed_and_zero_trade_accounts_remain_in_coverage(self):
        row={'state':'complete','bars':10,'confirmations':0,'metrics':{'total_return':0,'completed_trades':0,'total_fees':0,'max_drawdown':0}}
        r=aggregate([row,{'symbol':'bad','state':'failed'}],3)
        self.assertFalse(r['coverage_complete']);self.assertEqual(r['zero_trade_securities'],1);self.assertEqual(r['failed_symbols'],['bad'])
    def test_daily_open_uses_china_date_for_utc_input(self):
        times=['2026-11-02T07:00:00+00:00','2026-11-03T07:00:00+00:00','2026-11-04T07:00:00+00:00']
        bars=[Candle(i,10,10.2,9.8,10,t,1000000) for i,t in enumerate(times)]
        events=[{'observed_index':0,'signal_id':'p','event':'confirmed','side':'buy','rank':2,'class':'third_buy','invalidation':1}]
        r=simulate(bars,bars,events,{t[:10]:10 for t in times},period='day',symbol='sh600000')
        fill=next(o for o in r['orders'] if o['state']=='filled');self.assertIn('T09:30:00+08:00',fill['time']);self.assertEqual(fill['bar_index'],1)
    def test_board_limits_not_one_20_percent_rule_for_all(self):
        bars=[Candle(i,12 if i==1 else 10,12 if i==1 else 10,12 if i==1 else 10,12 if i==1 else 10,f'2026-11-0{i+2}T15:00:00+08:00',1000000) for i in range(3)]
        daily={f'2026-10-{d:02d}':10 for d in range(20,30)};daily.update({b.closed_at[:10]:b.close for b in bars})
        events=[{'observed_index':0,'signal_id':'p','event':'confirmed','side':'buy','rank':2,'class':'third_buy','invalidation':1}]
        main=simulate(bars,bars,events,daily,period='day',symbol='sh600000')
        beijing=simulate(bars,bars,events,daily,period='day',symbol='bj920002')
        self.assertTrue(any(o.get('reason')=='open_at_upper_limit' for o in main['orders']))
        self.assertTrue(any(o['state']=='filled' and o['bar_index']==1 for o in beijing['orders']))


if __name__=='__main__':unittest.main()
