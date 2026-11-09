import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from chanmodel.v2.io import load_series


class ReceiptInputTests(unittest.TestCase):
    def test_verified_compressed_metadata_and_wrong_symbol_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'bars.csv.gz';meta=root/'metadata.json'
            raw=b'closed_at,open,high,low,close,volume,is_closed\n2026-11-05T10:00:00+08:00,10,11,9,10,1000,true\n'
            stored=gzip.compress(raw);path.write_bytes(stored)
            data={'symbol':'sz000001','period':'minute30','source':'tdx','adjustment':'unadjusted',
                  'csv_sha256':hashlib.sha256(raw).hexdigest(),'csv_storage_sha256':hashlib.sha256(stored).hexdigest()}
            meta.write_text(json.dumps(data),encoding='utf-8')
            series,receipt=load_series(path,'sz000001','30m','unadjusted','tdx',meta)
            self.assertEqual(len(series.bars),1)
            self.assertTrue(receipt['provider_identity_receipt_checked'])
            with self.assertRaises(ValueError):load_series(path,'sh600000','30m','unadjusted','tdx',meta)
            with self.assertRaises(ValueError):load_series(path,'sz000001','5m','unadjusted','tdx',meta)
            with self.assertRaises(ValueError):load_series(path,'sz000001','30m','qfq','tdx',meta)
            with self.assertRaises(ValueError):load_series(path,'sz000001','30m','unadjusted','other',meta)
            meta.write_text(json.dumps({**data,'csv_sha256':'bad'}),encoding='utf-8')
            with self.assertRaises(ValueError):load_series(path,'sz000001','30m','unadjusted','tdx',meta)

    def test_no_receipt_remains_declared_and_naive_time_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'bars.csv'
            path.write_text('closed_at,open,high,low,close\n2026-11-05T15:00:00+08:00,10,11,9,10\n',encoding='utf-8')
            _,receipt=load_series(path,'sz000001','day','unadjusted','user_csv')
            self.assertFalse(receipt['provider_identity_receipt_checked'])
            path.write_text('closed_at,open,high,low,close\n2026-11-05T15:00:00,10,11,9,10\n',encoding='utf-8')
            with self.assertRaises(ValueError):load_series(path,'sz000001','day','unadjusted','user_csv')


if __name__=='__main__':unittest.main()
