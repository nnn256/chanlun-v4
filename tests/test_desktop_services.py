import os
import sys
import unittest
from unittest.mock import patch

from chanmodel.tdx_data import TdxClient
from workbench import services


class DesktopServiceTests(unittest.TestCase):
    def test_explicit_endpoint_wins_over_desktop_environment(self):
        with patch.dict(os.environ,{'CHANLUN_TDX_BASE_URL':'http://127.0.0.1:18880'}):
            self.assertEqual(TdxClient().base_url,'http://127.0.0.1:18880')
            self.assertEqual(TdxClient('http://127.0.0.1:18080').base_url,'http://127.0.0.1:18080')

    def test_frozen_worker_uses_exe_dispatch(self):
        with patch.object(sys,'frozen',True,create=True):
            self.assertEqual(services.worker_command('job-test'),[sys.executable,'--worker','job-test'])

    def test_source_worker_keeps_python_module_dispatch(self):
        with patch.object(sys,'frozen',False,create=True):
            self.assertEqual(services.worker_command('job-test'),[sys.executable,'-m','workbench.jobs','--job','job-test'])

    def test_live_probe_returns_observed_receipt(self):
        with patch.object(services.TdxClient,'quotes',return_value=([{'Code':'300949'}],{'received_at':'2026-11-08T00:00:00+08:00'},b'')):
            self.assertTrue(services.probe()['ready'])

    def test_wrong_security_is_not_a_ready_service(self):
        with patch.object(services.TdxClient,'quotes',return_value=([{'Code':'000001'}],{'received_at':'date'},b'')):
            with self.assertRaisesRegex(ValueError,'指定证券'):services.probe()

    def test_refused_connection_to_missing_binary_is_actionable(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as d,patch.object(services,'ROOT',Path(d)),patch.object(services,'probe',side_effect=ConnectionRefusedError('10061')):
            with self.assertRaisesRegex(ValueError,'未找到'):services.ensure_tdx(force=True)


if __name__=='__main__':unittest.main()
