from datetime import datetime
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from chanmodel.tdx_data import CN, TdxError, sha256
from chanmodel.tdx_intraday import (cache_intraday,fetch_intraday,load_intraday_cache,
                                    normalize_intraday,valid_session_close)
from chanmodel.model import Candle
from intraday_scan import range_coverage


def row(time='2026-11-05T10:00:00+08:00',**changes):
    result={'Time':time,'Open':10000,'High':12000,'Low':9000,'Close':11000,'Volume':123,'Amount':12345000}
    result.update(changes);return result


def payload(rows,period='minute30'):
    return {'count':len(rows),'list':rows,'meta':{'source':'tdx','type':period}}


NOW=datetime(2026,11,6,2,tzinfo=CN)


class IntradayTests(unittest.TestCase):
    def test_empty_upstream_null_list_is_not_an_invented_bar(self):
        bars,rows,quality=normalize_intraday({'count':0,'list':None,'meta':{'source':'tdx','type':'minute5'}},NOW,'minute5')
        self.assertEqual((bars,rows),([],[]))
        self.assertEqual(quality['closed_rows'],0)

    def test_missing_child_history_is_distinct_from_no_nested_signal(self):
        parent=[Candle(i,10,11,9,10,f'2024-01-0{i+1}T15:00:00+08:00') for i in range(3)]
        child=[Candle(0,10,11,9,10,'2024-01-02T10:00:00+08:00'),Candle(1,10,11,9,10,'2024-01-03T15:00:00+08:00')]
        audit=range_coverage({'terminal_range':[0,2]},parent,child)
        self.assertFalse(audit['terminal_range_covered'])
        self.assertEqual(audit['status'],'insufficient_child_history')

    def test_explicit_unadjusted_route_and_supported_periods(self):
        client=Mock();client.request.return_value=({}, {}, b'')
        fetch_intraday(client,'sz000001','minute30')
        client.request.assert_called_once_with('/api/kline-all/tdx',{'code':'sz000001','type':'minute30'})
        with self.assertRaises(ValueError):fetch_intraday(client,'sz000001','day')

    def test_prices_volume_amount_and_timezone(self):
        bars,rows,quality=normalize_intraday(payload([row()]),NOW,'minute30')
        self.assertEqual((bars[0].open,bars[0].high,bars[0].low,bars[0].close,bars[0].volume),(10,12,9,11,12300))
        self.assertEqual(rows[0]['amount_yuan'],12345)
        self.assertEqual(bars[0].closed_at,'2026-11-05T10:00:00+08:00')
        self.assertEqual(quality['adjustment'],'unadjusted')
        self.assertFalse(quality['session_gaps_padded'])

    def test_lunch_gap_is_not_filled_and_utc_is_converted(self):
        data=payload([row('2026-11-05T03:30:00Z'),row('2026-11-05T05:30:00Z')])
        bars,rows,quality=normalize_intraday(data,NOW,'minute30')
        self.assertEqual(len(bars),2)
        self.assertEqual([b.closed_at for b in bars],['2026-11-05T11:30:00+08:00','2026-11-05T13:30:00+08:00'])

    def test_current_period_is_excluded_until_closing_time(self):
        cutoff=datetime(2026,11,5,9,59,tzinfo=CN)
        bars,rows,q=normalize_intraday(payload([row()]),cutoff,'minute30')
        self.assertEqual(bars,[]);self.assertEqual(q['open_rows_excluded'],1);self.assertFalse(rows[0]['is_closed'])

    def test_5m_and_30m_have_separate_session_grids(self):
        for value in ['2026-11-05T09:35:00+08:00','2026-11-05T13:05:00+08:00','2026-11-05T15:00:00+08:00']:
            self.assertTrue(valid_session_close(datetime.fromisoformat(value),5))
        for value in ['2026-11-05T09:30:00+08:00','2026-11-05T12:00:00+08:00','2026-11-05T13:00:00+08:00','2026-11-05T15:05:00+08:00']:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):normalize_intraday(payload([row(value)],'minute5'),NOW,'minute5')
        with self.assertRaises(ValueError):normalize_intraday(payload([row('2026-11-05T09:35:00+08:00')]),NOW,'minute30')

    def test_unsorted_duplicate_and_conflicting_values(self):
        r=row();later=row('2026-11-05T10:30:00+08:00')
        bars,rows,q=normalize_intraday(payload([later,r,r]),NOW,'minute30')
        self.assertEqual([b.pos for b in bars],[0,1]);self.assertEqual(q['input_order'],'reordered');self.assertEqual(q['identical_duplicates_removed'],1)
        with self.assertRaises(ValueError):normalize_intraday(payload([r,row(Close=10500)]),NOW,'minute30')

    def test_invalid_future_no_timezone_and_bad_prices_fail(self):
        for bad in [row('2026-11-09T10:00:00+08:00'),row('2026-11-05T10:00:00'),row(High=8000),row(Volume=-1),row(Close=0),row(Open=float('nan')),row(Amount=-1)]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):normalize_intraday(payload([bad]),NOW,'minute30')

    def test_count_source_period_and_list_are_checked(self):
        for bad in [{'count':2,'list':[row()]},payload([row()],'minute5'),{'list':[row()],'meta':{'source':'ths'}},{'list':None}]:
            with self.subTest(bad=bad):
                with self.assertRaises(TdxError):normalize_intraday(bad,NOW,'minute30')

    def test_compressed_cache_hashes_are_verified_and_cutoff_reapplied(self):
        data=payload([row()]);raw=json.dumps({'code':0,'data':data}).encode();receipt={'response_sha256':sha256(raw),'received_at':NOW.isoformat()}
        bars,rows,q=normalize_intraday(data,NOW,'minute30');q['cache_run_as_of']=NOW.isoformat()
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            meta=cache_intraday(folder,'sz000001','minute30',rows,raw,receipt,q)
            self.assertEqual(gzip.decompress(Path(meta['raw_path']).read_bytes()),raw)
            self.assertEqual(sha256(gzip.decompress(Path(meta['csv_path']).read_bytes())),meta['csv_sha256'])
            cutoff=datetime(2026,11,5,9,59,tzinfo=CN)
            loaded,_,quality,_,_=load_intraday_cache(folder,'sz000001','minute30',cutoff)
            self.assertEqual(loaded,[]);self.assertEqual(quality['open_rows_excluded'],1)
            Path(meta['raw_path']).write_bytes(b'broken')
            with self.assertRaises(ValueError):load_intraday_cache(folder,'sz000001','minute30',NOW)


if __name__=='__main__':unittest.main()
