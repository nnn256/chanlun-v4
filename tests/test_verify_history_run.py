import importlib.util
from datetime import datetime,timedelta,timezone
from pathlib import Path
import unittest
import tempfile
import gzip
import hashlib
import json

from chanmodel.model import Candle

PATH=Path(__file__).resolve().parents[1]/'tools/verify_history_run.py'
SPEC=importlib.util.spec_from_file_location('verify_history_run',PATH)
VERIFY=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


def bars(count=6):
    start=datetime(2020,1,1,15,tzinfo=timezone(timedelta(hours=8)))
    return [Candle(i,10+i,11+i,9+i,10+i,(start+timedelta(days=i)).isoformat(),1000) for i in range(count)]


def event(data):
    return {'sequence':0,'kind':'signal','id':'point','event':'appeared','observed_at':1,
            'observed_time':data[1].closed_at,'available_at':1,'input_revision':0,'revision':0,'first_candidate_at':1,
            'value':{'id':'point','status':'candidate','signal_class':'first_buy_candidate','side':'buy',
                     'anchor':0,'basis_at':1,'terminal_range':[0,0],'definition_mode':'signal_proxy','execution_allowed':False}}


class HistoryVerificationTests(unittest.TestCase):
    def test_empty_source_requires_zero_count_and_matching_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);daily=root/'daily';cache=root/'cache';code='sh688827'
            (daily/'stocks').mkdir(parents=True)
            (daily/'responses').mkdir()
            meta=cache/'tdx/day/metadata';meta.mkdir(parents=True)
            (daily/'stocks'/f'{code}.json').write_text(json.dumps({'status':'empty_history','quality':{'closed_rows':0}}),encoding='utf-8')
            for rows in [None, []]:
                raw=json.dumps({'code':0,'data':{'count':0,'list':rows}}).encode()
                (daily/'responses'/f'{code}.json.gz').write_bytes(gzip.compress(raw))
                (meta/f'{code}.json').write_text(json.dumps({'response_sha256':hashlib.sha256(raw).hexdigest()}),encoding='utf-8')
                evidence=VERIFY.no_history_evidence(code,daily,cache,'tdx')
                self.assertTrue(evidence['verified'])
                self.assertEqual(evidence['raw_rows'],0)
            raw=json.dumps({'code':0,'data':{'count':1,'list':None}}).encode()
            (daily/'responses'/f'{code}.json.gz').write_bytes(gzip.compress(raw))
            (meta/f'{code}.json').write_text(json.dumps({'response_sha256':hashlib.sha256(raw).hexdigest()}),encoding='utf-8')
            self.assertFalse(VERIFY.no_history_evidence(code,daily,cache,'tdx')['verified'])
            raw=json.dumps({'code':0,'data':{'count':0,'list':None}}).encode()
            (daily/'responses'/f'{code}.json.gz').write_bytes(gzip.compress(raw))
            self.assertFalse(VERIFY.no_history_evidence(code,daily,cache,'tdx')['verified'])

    def test_correct_time_and_next_bar_labels_pass(self):
        data=bars();point=event(data)
        errors,entries=VERIFY.check_events([point],data)
        self.assertEqual(errors,[])
        labels=[VERIFY.expected_label(data,point,h,20) for h in [2,20]]
        self.assertEqual(VERIFY.check_labels(labels,entries,data,[2,20],20),[])
        self.assertEqual(labels[0]['entry_index'],2)
        self.assertEqual(labels[0]['exit_index'],3)

    def test_backdated_event_and_wrong_timestamp_rejected(self):
        data=bars();point=event(data);point['available_at']=0;point['observed_time']=data[0].closed_at
        errors,_=VERIFY.check_events([point],data)
        self.assertTrue(any('backdated' in error for error in errors))
        self.assertTrue(any('CSV close' in error for error in errors))

    def test_future_anchor_rejected(self):
        data=bars();point=event(data);point['value']['anchor']=4
        errors,_=VERIFY.check_events([point],data)
        self.assertTrue(any('future anchor' in error for error in errors))

    def test_duplicate_label_and_wrong_cost_rejected(self):
        data=bars();point=event(data);label=VERIFY.expected_label(data,point,2,20)
        label['hypothetical_return_after_constant_cost']+=.1
        errors=VERIFY.check_labels([label,label],[point],data,[2],20)
        self.assertTrue(any('duplicate' in error for error in errors))
        self.assertTrue(any('constant_cost' in error for error in errors))

    def test_ledger_withdrawal_removes_object_only_after_observation(self):
        data=bars();point=event(data)
        withdrawn={'id':'point','event':'withdrawn','observed_at':4}
        self.assertEqual(VERIFY.ledger_at([point,withdrawn],3),{'point':point['value']})
        self.assertEqual(VERIFY.ledger_at([point,withdrawn],4),{})

    def test_sample_includes_three_exchange_length_extremes(self):
        records=[{'symbol':f'{exchange}{i:06d}','exchange':exchange,'history_bars':100+i,'candidate_first_seen_count':i%2}
                 for exchange in ['sh','sz','bj'] for i in range(10)]
        selected=VERIFY.select_sample(records,20)
        self.assertEqual({r['exchange'] for r in selected},{'sh','sz','bj'})
        for exchange in ['sh','sz','bj']:
            self.assertTrue(any(r['symbol']==exchange+'000000' for r in selected))
            self.assertTrue(any(r['symbol']==exchange+'000009' for r in selected))


if __name__=='__main__':unittest.main()
