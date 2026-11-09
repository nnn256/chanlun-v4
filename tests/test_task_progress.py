import json
from contextlib import closing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chanmodel.tdx_data import TdxClient,TdxError
from workbench import progress
import desktop_app


class TaskProgressTests(unittest.TestCase):
    def test_timeout_retried_and_receipt_preserves_attempts(self):
        callback=[]
        with patch.object(TdxClient,'request',side_effect=[TdxError('获取K线失败: 超时'),({'list':[]},{'url':'fixture'},b'{}')]),patch('chanmodel.tdx_data.time_module.sleep'):
            data,receipt,raw=TdxClient().request_retry('/api/kline-all/tdx',on_retry=callback.append)
        self.assertEqual(receipt['request_attempts'],2)
        self.assertEqual(len(receipt['transient_failures_before_success']),1)
        self.assertEqual(callback[0]['next_attempt'],2)

    def test_invalid_response_not_retried_as_network_failure(self):
        with patch.object(TdxClient,'request',side_effect=TdxError('response is not JSON')) as request:
            with self.assertRaises(TdxError):TdxClient().request_retry('/api/quote')
            self.assertEqual(request.call_count,1)

    def test_retries_remain_bounded_when_upstream_unavailable(self):
        with patch.object(TdxClient,'request',side_effect=TimeoutError('timeout')) as request,patch('chanmodel.tdx_data.time_module.sleep'):
            with self.assertRaises(TimeoutError):TdxClient().request_retry('/api/quote',attempts=3)
            self.assertEqual(request.call_count,3)

    def test_per_stock_progress_survives_a_reader_reopen(self):
        with tempfile.TemporaryDirectory() as d,patch.object(progress,'STATE',Path(d)):
            report=progress.reporter('fixture-job','sz002149','西部材料')
            report('timeshare','正在采集历史分时：100/4484个交易日',completed=100,total=4484,unit='交易日')
            value=progress.read_progress('fixture-job','sz002149')
            self.assertEqual(value['completed'],100)
            self.assertEqual(value['name'],'西部材料')
            self.assertTrue(value['updated_at'])
            report('analysis','正在计算1分钟结构')
            self.assertEqual(progress.read_progress('fixture-job','sz002149')['stage'],'analysis')

    def test_app_data_root_stable_across_packaged_appdata_environment(self):
        with patch.dict(os.environ,{'LOCALAPPDATA':'C:/temporary-packaged-cache'}):
            expected=Path.home()/'Documents/ChanlunWorkbenchData'
            actual=desktop_app.app_directory()
            if 'CHANLUN_APP_DATA_DIR' not in os.environ:self.assertEqual(actual,expected.resolve())

    def test_loopback_tdx_client_ignores_inherited_dead_proxy(self):
        import urllib.request
        with patch('urllib.request.getproxies',return_value={'http':'http://127.0.0.1:7890'}):
            client=TdxClient('http://127.0.0.1:18080')
        proxies=[h for h in client._local_opener.handlers if isinstance(h,urllib.request.ProxyHandler)]
        self.assertFalse(any(p.proxies for p in proxies))
        self.assertIsNone(TdxClient('https://market.example')._local_opener)

    def test_migration_preserves_existing_job_database(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);legacy=root/'legacy/ChanlunWorkbench';dbpath=legacy/'project/runtime/workbench/workbench.sqlite3';dbpath.parent.mkdir(parents=True)
            with closing(sqlite3.connect(dbpath)) as db:
                db.execute('CREATE TABLE jobs(id TEXT)');db.execute("INSERT INTO jobs VALUES('existing-job')")
                db.commit()
            (legacy/'project/fixture-history.txt').write_text('original history')
            target=root/'Documents/ChanlunWorkbenchData';target.mkdir(parents=True)
            with patch.dict(os.environ,{'LOCALAPPDATA':str(root/'legacy')}),patch.object(desktop_app.Path,'home',return_value=root):
                desktop_app.migrate_previous_data(target,lambda message:None)
            with closing(sqlite3.connect(target/'project/runtime/workbench/workbench.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT id FROM jobs').fetchone()[0],'existing-job')
            self.assertEqual((target/'project/fixture-history.txt').read_text(),'original history')


if __name__=='__main__':unittest.main()
