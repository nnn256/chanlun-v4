import copy
from dataclasses import replace
from datetime import datetime,timedelta
import gzip
import json
from pathlib import Path
import tempfile
import unittest

from chanmodel.model import Candle
from chanmodel.signal_anchor import align_signal
from chanmodel.kline_audit import audit_panel,repair_signal_anchors,verify_source
from chanmodel.tdx_data import normalize_daily,cache_history,sha256
from chanmodel.v2.io import load_cache_series
from chanmodel.unified import analyze_all
from tests.test_recursive_v2 import daily_series
from chanmodel.causal_replay import StreamingStructure,CONFIG
from chanmodel.v2.engine import analyze as analyze_v2


class SignalAccuracy(unittest.TestCase):
    def bars(self):
        start=datetime.fromisoformat('2026-01-05T15:00:00+08:00')
        values=[10,9,7.36,8,7.57,9,10]
        return [Candle(i,v,v+.2,v-.2,v,(start+timedelta(days=i)).isoformat(),1000) for i,v in enumerate(values)]
    def point(self):
        return {'id':'stable-id','class':'second_buy','side':'buy','rank':1,'anchor':4,'price':7.57,
                'terminal_range':[1,4],'available_at':6,'invalidation':7.0,'state':'awaiting_lower_confirmation'}
    def test_partition_end_is_not_raw_price_extreme(self):
        bars=self.bars();point=self.point();align_signal(point,bars)
        self.assertEqual(point['anchor'],2);self.assertAlmostEqual(point['price'],7.16)
        self.assertEqual(point['decomposition_end'],4);self.assertEqual(point['decomposition_end_price'],7.57)
        self.assertEqual(point['available_at'],6);self.assertEqual(point['id'],'stable-id')
        self.assertAlmostEqual(point['invalidation'],7.16)
    def test_sell_mirror_and_equal_extreme_earliest_source(self):
        bars=self.bars();point=self.point();point.update(side='sell',**{'class':'second_sell'})
        mirrored=[replace(b,open=20-b.open,high=20-b.low,low=20-b.high,close=20-b.close) for b in bars]
        align_signal(point,mirrored);self.assertEqual(point['anchor'],2);self.assertAlmostEqual(point['price'],12.84)
        original=self.point();bars[3]=replace(bars[3],low=bars[2].low)
        align_signal(original,bars);self.assertEqual(original['anchor'],2)
    def test_third_center_boundary_is_not_replaced_by_pivot_price(self):
        point=self.point();point.update(**{'class':'third_buy'},invalidation=3.83)
        align_signal(point,self.bars());self.assertEqual(point['invalidation'],3.83)
    def test_new_extreme_after_terminal_cannot_move_historical_pivot(self):
        bars=self.bars();p=self.point();align_signal(p,bars);q=copy.deepcopy(p)
        bars.append(Candle(7,5,6,1,5,'2026-01-12T15:00:00+08:00',1000))
        align_signal(q,bars);self.assertEqual(q['anchor'],p['anchor']);self.assertEqual(q['price'],p['price'])
        self.assertEqual(q['state'],'invalidated');self.assertEqual(q['invalidated_at'],7)
    def test_read_time_old_result_repair_preserves_original_and_never_changes_raw_bars(self):
        bars=self.bars();panel={'period':'day','input_bars':len(bars),'bars':[{'source_index':b.pos,'closed_at':b.closed_at,
                    'open':b.open,'high':b.high,'low':b.low,'close':b.close,'volume':b.volume} for b in bars],
                    'all_signals':[self.point()]}
        original=copy.deepcopy(panel['bars']);bundle={'chart':{'panels':[panel]}}
        repair_signal_anchors(bundle)
        self.assertEqual(panel['bars'],original);self.assertEqual(panel['all_signals'][0]['anchor'],2)
        self.assertEqual(panel['all_signals'][0]['saved_original_point']['anchor'],4)
        self.assertEqual(len(panel['anchor_corrections']),1)
    def test_full_snapshot_and_prefix_share_extreme_mapping(self):
        s=daily_series(220);cut=s.bars[169].closed_at
        a=analyze_all({'day':s},cut);b=analyze_all({'day':replace(s,bars=s.bars[:170])},cut)
        self.assertEqual(a['reports']['day']['signals'],b['reports']['day']['signals'])
    def test_stream_and_snapshot_use_one_raw_pivot_algorithm(self):
        series=daily_series(350);stream=StreamingStructure(CONFIG)
        for bar in series.bars:stream.append(bar)
        incremental=stream.snapshot()['signals']
        batch=analyze_v2(series,series.bars[-1].closed_at,CONFIG,max_rank=8)['signals']
        keys=['id','anchor','price','available_at','invalidation','state','decomposition_end','extreme_evidence']
        self.assertEqual([{k:s[k] for k in keys} for s in incremental],[{k:s[k] for k in keys} for s in batch])


class CompleteRawKlineAudit(unittest.TestCase):
    def fixture(self,tmp):
        rows=[{'Time':f'2026-11-0{day}T15:00:00+08:00','Open':10000,'High':11000+day,'Low':9000,
               'Close':10500,'Volume':100+day,'Amount':100000} for day in [2,3,4]]
        data={'list':rows,'count':3,'meta':{'source':'tdx','type':'day'}};raw=json.dumps({'code':0,'data':data}).encode()
        cutoff=datetime.fromisoformat('2026-11-04T15:00:00+08:00')
        bars,normalized,quality=normalize_daily(data,cutoff)
        receipt={'response_sha256':sha256(raw),'received_at':cutoff.isoformat()}
        meta=cache_history(Path(tmp)/'market_data','sz000001','tdx',normalized,raw,receipt,quality)
        series,source=load_cache_series(Path(tmp)/'market_data','sz000001','day')
        panel={'period':'day','input_bars':3,'bars':[{'source_index':b.pos,'closed_at':b.closed_at,'open':b.open,
                'high':b.high,'low':b.low,'close':b.close,'volume':b.volume,'is_closed':True} for b in bars]}
        return panel,source,cutoff.isoformat()
    def test_every_raw_value_matches_csv_and_original_provider_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            panel,receipt,cut=self.fixture(tmp);audit=verify_source(panel,receipt,'sz000001',tmp,cut)
            self.assertEqual(audit['source_state'],'matched');self.assertEqual(audit['compared_rows'],3)
            self.assertEqual(audit['provider_response_state'],'hash_and_ohlcv_matched')
            self.assertFalse(audit['listing_inception_proven'])
    def test_truncated_ohlc_duplicate_time_and_source_changed_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            panel,receipt,cut=self.fixture(tmp)
            cropped=copy.deepcopy(panel);cropped['bars'].pop();self.assertEqual(audit_panel(cropped)['state'],'invalid')
            duplicate=copy.deepcopy(panel);duplicate['bars'][1]['closed_at']=duplicate['bars'][0]['closed_at']
            self.assertEqual(audit_panel(duplicate)['state'],'invalid')
            bad=copy.deepcopy(panel);bad['bars'][1]['high']=10.9
            self.assertEqual(verify_source(bad,receipt,'sz000001',tmp,cut)['source_state'],'mismatch')
            path=Path(receipt['file']);path.write_bytes(path.read_bytes()+b'\n')
            self.assertEqual(verify_source(panel,receipt,'sz000001',tmp,cut)['source_state'],'mismatch')
    def test_missing_source_does_not_claim_original_accuracy(self):
        with tempfile.TemporaryDirectory() as tmp:
            panel,_,cut=self.fixture(tmp);value=verify_source(panel,{},'sz000001',tmp,cut)
            self.assertEqual(value['source_state'],'source_unavailable');self.assertEqual(value['state'],'raw_validated')


if __name__=='__main__':unittest.main()
