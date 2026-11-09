"""Regress the user's old-instance and invisible-default-marker report."""
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import desktop_app
from chanmodel.release import APP_VERSION


class InstanceUpgradeTests(unittest.TestCase):
    def controller(self,root):
        status=root/'desktop-status.json'
        status.write_text(json.dumps({'url':'http://127.0.0.1:8791','pid':1234}),encoding='utf-8')
        return SimpleNamespace(directory=root,status_path=status,args=SimpleNamespace(no_browser=True),lock=None,notify=lambda _:None)

    def responses(self,root,version='4.0.0',jobs=(),backtests=(),busy=False):
        return {'/api/health':{'service':'chanlun-workbench-v1','database':str(root/'project/runtime/workbench/workbench.sqlite3'),
                               'pid':1234,'version':version,'features':['marker_visibility_v2'] if version==APP_VERSION else []},
                '/api/overview':{'jobs':list(jobs)},'/api/backtests':{'runs':list(backtests)},'/api/monitor':{'busy':busy}}

    def test_old_running_app_is_not_silently_reopened(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);c=self.controller(root);answers=self.responses(root)
            with patch.object(desktop_app,'local_json',side_effect=lambda url,**k:answers['/'+url.split('/',3)[3]]),\
                 patch.object(desktop_app,'request_window_close',return_value=True) as close,\
                 patch.object(desktop_app,'claim_instance',return_value='new-lock'),\
                 patch.object(desktop_app.webbrowser,'open') as browser:
                self.assertEqual(desktop_app.existing_instance_action(c),'upgraded')
            close.assert_called_once_with(1234);browser.assert_not_called();self.assertEqual(c.lock,'new-lock')

    def test_running_workbench_backtest_or_scan_is_preserved(self):
        for changes in [{'jobs':[{'state':'running'}]},{'jobs':[{'state':'queued'}]},
                        {'backtests':[{'state':'running'}]},{'busy':True}]:
            with self.subTest(changes=changes),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);answers=self.responses(root,**changes)
                with patch.object(desktop_app,'local_json',side_effect=lambda url,**k:answers['/'+url.split('/',3)[3]]),\
                     patch.object(desktop_app,'request_window_close') as close:
                    self.assertEqual(desktop_app.existing_instance_action(self.controller(root)),'busy_old_version')
                    close.assert_not_called()

    def test_same_version_with_required_feature_reuses_existing_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);answers=self.responses(root,version=APP_VERSION)
            with patch.object(desktop_app,'local_json',side_effect=lambda url,**k:answers['/'+url.split('/',3)[3]]),\
                 patch.object(desktop_app,'request_window_close') as close:
                self.assertEqual(desktop_app.existing_instance_action(self.controller(root)),'reused');close.assert_not_called()

    def test_unrelated_local_server_is_never_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);answers=self.responses(root);answers['/api/health']['database']=str(root/'different/db')
            with patch.object(desktop_app,'local_json',side_effect=lambda url,**k:answers['/'+url.split('/',3)[3]]),\
                 patch.object(desktop_app,'request_window_close') as close:
                with self.assertRaises(ValueError):desktop_app.existing_instance_action(self.controller(root))
                close.assert_not_called()


@unittest.skipUnless(shutil.which('node'),'geometry regression requires optional Node')
class DefaultVisibilityTests(unittest.TestCase):
    def test_default_contains_invalid_history_and_focuses_latest_real_marker(self):
        path=Path(__file__).resolve().parents[1]/'workbench/web/signal-chart.js'
        code='''require(process.argv[1]);const items=[{id:'old',period:'day',rank:1,side:'buy',state:'invalidated',anchor:50,available_at:60},{id:'new',period:'day',rank:1,side:'sell',state:'invalidated',anchor:500,available_at:530}];const a=SignalChart.presentation(items);const b=SignalChart.presentation(items,{showInvalid:false});process.stdout.write(JSON.stringify({a,b,range:SignalChart.markerRange(a.latest,600)}));'''
        p=subprocess.run(['node','-e',code,str(path)],capture_output=True,text=True,encoding='utf-8',check=True)
        v=json.loads(p.stdout);self.assertEqual(v['a']['total'],2);self.assertEqual(len(v['a']['items']),2)
        self.assertEqual(v['a']['latest']['id'],'new');self.assertEqual(v['a']['active'],0)
        self.assertEqual(v['b']['items'],[]);self.assertLess(v['range']['from'],500);self.assertGreater(v['range']['to'],530)

    def test_current_rank_is_empty_but_other_rank_is_reported(self):
        path=Path(__file__).resolve().parents[1]/'workbench/web/signal-chart.js'
        code='''require(process.argv[1]);process.stdout.write(JSON.stringify(SignalChart.presentation([{id:'p',period:'day',rank:2,side:'buy',state:'candidate',anchor:100,available_at:110}],{rank:1})));'''
        p=subprocess.run(['node','-e',code,str(path)],capture_output=True,text=True,encoding='utf-8',check=True)
        v=json.loads(p.stdout);self.assertEqual(v['total'],0);self.assertEqual(v['ranks'],[2]);self.assertIsNone(v['latest'])


if __name__=='__main__':unittest.main()
