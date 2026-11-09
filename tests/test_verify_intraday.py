from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

from chanmodel.engine import multilevel
from chanmodel.model import Config
from chanmodel.tdx_data import CN,cache_history,normalize_daily,sha256
from chanmodel.tdx_intraday import cache_intraday,normalize_intraday
from tools.verify_intraday_run import (csv_bars,compare_recomputed,sample_symbols,
                                       unavailable_evidence,verify_source,verify_stock,canonical_times)

NOW=datetime(2026,11,6,2,3,45,tzinfo=CN)


def fixture(root):
    sources={};datasets={};periods={}
    for level,period,clock in [('day','day','15:00'),('30m','minute30','10:00'),('5m','minute5','09:35')]:
        rows=[{'Time':f'2026-11-0{day}T{clock}:00+08:00','Open':10000,'High':12000,'Low':9000,'Close':11000,'Volume':10,'Amount':1000000} for day in range(2,6)]
        data={'list':rows,'count':len(rows),'meta':{'source':'tdx','type':period}}
        raw=json.dumps({'code':0,'data':data}).encode()
        receipt={'response_sha256':sha256(raw),'received_at':NOW.isoformat(),'http_status':200}
        if level=='day':
            bars,normalized,quality=normalize_daily(data,NOW)
            meta=cache_history(root,'sz000001','tdx',normalized,raw,receipt,quality)
        else:
            bars,normalized,quality=normalize_intraday(data,NOW,period)
            meta=cache_intraday(root,'sz000001',period,normalized,raw,receipt,quality)
        sources[level]=meta;datasets[level]=bars
    report=multilevel(datasets,('day','30m','5m'),NOW.isoformat(),Config())
    for level,bars in datasets.items():
        periods[level]={'bars':len(bars),'first_close':bars[0].closed_at,'last_close':bars[-1].closed_at,
                        'strokes':len(report['levels'][level]['strokes']),'segments':len(report['levels'][level]['segments']),
                        'centers':len(report['levels'][level]['centers']),'state':report['levels'][level]['current_state'],
                        'active_candidate_count':sum(point['status']=='candidate' for point in report['levels'][level]['signal_candidates'])}
    result={'symbol':'sz000001','status':'success','engine_sha256':'fixture-version','as_of':NOW.isoformat(),
            'adjustment':'unadjusted','execution_allowed':False,'sources':sources,'periods':periods,
            'interval_nesting_audits':report['interval_nesting_audits'],'nested_evidence_count':0,
            'daily_candidates_without_30m_history':0}
    return result,datasets


class IntradayVerificationTests(unittest.TestCase):
    def test_audit_timestamp_offsets_compare_as_equal_instants(self):
        a={'coverage':{'start':'2026-11-05T15:00:00+08:00','status':'covered'}}
        b={'coverage':{'start':'2026-11-05T07:00:00+00:00','status':'covered'}}
        self.assertEqual(canonical_times(a),canonical_times(b))

    def test_all_source_hashes_counts_and_session_grids_pass_fixture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);result,datasets=fixture(root)
            for level in ['day','30m','5m']:
                observed,bars,_,_=verify_source(root,'sz000001',level,NOW,result['sources'][level],result['periods'][level],True)
                self.assertEqual(observed['closed_rows'],4)
                self.assertEqual(bars,datasets[level])

    def test_corrupt_compressed_csv_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);result,_=fixture(root)
            Path(result['sources']['5m']['csv_path']).write_bytes(b'corrupted')
            with self.assertRaisesRegex(ValueError,'CSV compressed storage'):
                verify_source(root,'sz000001','5m',NOW,result['sources']['5m'],result['periods']['5m'])

    def test_result_recorded_input_must_match_current_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);result,_=fixture(root)
            result['sources']['day']['csv_sha256']='different'
            with self.assertRaisesRegex(ValueError,'recorded by stock'):
                verify_source(root,'sz000001','day',NOW,result['sources']['day'],result['periods']['day'])

    def test_timestamp_grid_open_flag_and_ohlc_rejected(self):
        header='closed_at,open,high,low,close,volume,is_closed\n'
        cases=['2026-11-05T12:00:00+08:00,10,12,9,11,100,true\n',
               '2026-11-05T10:00:00+08:00,10,12,9,11,100,false\n',
               '2026-11-05T10:00:00+08:00,10,8,9,11,100,true\n']
        for row in cases:
            with self.subTest(row=row):
                with self.assertRaises(ValueError):csv_bars((header+row).encode(),'30m',NOW)

    def test_historical_daily_saturday_is_not_rejected_by_modern_calendar(self):
        raw=b'closed_at,open,high,low,close,volume,is_closed\n1991-04-06T15:00:00+08:00,10,12,9,11,100,true\n'
        metrics,_=csv_bars(raw,'day',NOW)
        self.assertEqual(metrics['closed_rows'],1)

    def test_sample_recomputation_matches_saved_period_summaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            result,datasets=fixture(Path(temporary))
            comparison=compare_recomputed(result,datasets,Config().__dict__,NOW.isoformat())
            self.assertTrue(all(comparison.values()))
            result['periods']['5m']['strokes']=99
            self.assertFalse(compare_recomputed(result,datasets,Config().__dict__,NOW.isoformat())['period_summaries_match'])

    def test_version_difference_is_explicit_but_integrity_is_still_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);result,_=fixture(root/'cache');(root/'stocks').mkdir()
            (root/'stocks/sz000001.json').write_text(json.dumps(result),encoding='utf-8')
            task={'code':'sz000001','run_dir':str(root),'cache_dir':str(root/'cache'),'result':result,
                  'checkpoint_status':'success','checkpoint_engine':'fixture-version','sampled':True,
                  'run_engine':'fixture-version','current_engine':'current-version','as_of':NOW.isoformat(),'config':Config().__dict__}
            output=verify_stock(task)
            self.assertEqual(output['version_status'],'different_version')
            self.assertEqual(output['status'],'verified')
            self.assertTrue(output['warnings']);self.assertFalse(output['errors'])

    def test_stock_file_disagreement_is_integrity_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);result,_=fixture(root/'cache');(root/'stocks').mkdir()
            (root/'stocks/sz000001.json').write_text('{}',encoding='utf-8')
            task={'code':'sz000001','run_dir':str(root),'cache_dir':str(root/'cache'),'result':result,
                  'checkpoint_status':'success','checkpoint_engine':'fixture-version','sampled':False,
                  'run_engine':'fixture-version','current_engine':'fixture-version','as_of':NOW.isoformat(),'config':Config().__dict__}
            self.assertEqual(verify_stock(task)['status'],'integrity_error')

    def test_count_zero_null_is_evidence_of_unavailability(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);data={'list':None,'count':0,'meta':{'source':'tdx','type':'day'}}
            raw=json.dumps({'code':0,'data':data}).encode();receipt={'response_sha256':sha256(raw),'received_at':NOW.isoformat()}
            bars,rows,q=normalize_daily(data,NOW)
            cache_history(root,'sh688827','tdx',rows,raw,receipt,q)
            evidence=unavailable_evidence(root,'sh688827',{'status':'failed'})
            self.assertEqual(evidence[0]['count'],0)
            self.assertTrue(evidence[0]['list_was_null'])
            self.assertEqual(bars,[])

    def test_stratified_sampling_includes_all_available_exchanges(self):
        data={f'{exchange}{i:06d}':{'status':'success'} for exchange in ['sh','sz','bj'] for i in range(10)}
        sampled=sample_symbols(data,20)
        self.assertEqual(len(sampled),20)
        self.assertEqual({code[:2] for code in sampled},{'sh','sz','bj'})


if __name__=='__main__':unittest.main()
