from http.server import ThreadingHTTPServer
from pathlib import Path
import json,threading,tempfile,unittest,urllib.request,urllib.error,sqlite3,hashlib
from unittest.mock import patch
from public_web import server


class PublicWebTests(unittest.TestCase):
    def test_public_stock_rejects_unbounded_periods_paths_and_wrong_symbols(self):
        for code,period in [('sh600000/../../api/jobs','day'),('sz600000','day'),('http://localhost','day'),('bj920010','all'),('bj920010','1m')]:
            with self.assertRaises(ValueError):server.stock(code,period)

    def test_public_bundle_excludes_task_paths_raw_receipts_and_configuration(self):
        data={'summary':{'symbol':'bj920010','name':'测试','price':8.68,'live':{},'artifact':'C:/private/job.json','job_id':'private'},
              'chart':{'panels':[{'period':'day','bars':[],'source_receipt':{'url':'http://127.0.0.1:18084'},
                     'engine_provenance':{'commit':'abc','source_path':'C:/private/source','license':'MIT'},
                     'config':{'token':'private'}}],'input_sha256':{'day':'hash'},'engine_commit':'abc'}}
        text=json.dumps(server.public_bundle(data,'day'))
        for secret in ['private','127.0.0.1','artifact','source_receipt','token']:self.assertNotIn(secret,text)
        self.assertIn('read_only',text);self.assertIn('MIT',text)

    def test_desktop_bridge_accepts_only_loopback_http_origin(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'status.json'
            for origin in ['https://example.com','http://192.168.1.2:8791','file:///etc/passwd','http://127.0.0.1:8791/private']:
                p.write_text(json.dumps({'url':origin}))
                with self.assertRaises(ValueError):server.desktop_origin(p)
            p.write_text(json.dumps({'url':'http://127.0.0.1:8791'}));self.assertEqual(server.desktop_origin(p),'http://127.0.0.1:8791')

    def test_query_rate_is_bounded(self):
        with patch.object(server,'REQUESTS',{}):
            self.assertTrue(all(server.allowed_rate('test-ip') for _ in range(60)))
            self.assertFalse(server.allowed_rate('test-ip'))
            self.assertTrue(server.allowed_rate('another-ip'))

    def test_scan_reads_a_consistent_saved_snapshot_without_mutating_the_database(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'workbench.sqlite3'
            db=sqlite3.connect(path)
            db.executescript('''CREATE TABLE monitor_runs(state,started,updated,payload);
                CREATE TABLE monitor_signals(symbol,state,last_seen,payload);
                CREATE TABLE monitor_quotes(symbol);CREATE TABLE monitor_structures(symbol);
                CREATE TABLE monitor_alerts(acknowledged);''')
            db.execute('INSERT INTO monitor_runs VALUES(?,?,?,?)',('running','start','2026-11-10T12:00:00+08:00',json.dumps({'done':980,'total':5580})))
            db.execute('INSERT INTO monitor_quotes VALUES(?)',('bj920010',));db.commit();db.close()
            before=hashlib.sha256(path.read_bytes()).hexdigest()
            result=server.read_scan_snapshot(path)
            self.assertEqual(result['runs'][0]['payload']['done'],980)
            self.assertEqual(result['counts']['quotes'],1)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)

    def test_public_http_refuses_task_mutations_and_arbitrary_files(self):
        http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        threading.Thread(target=http.serve_forever,daemon=True).start()
        base='http://127.0.0.1:'+str(http.server_port)
        try:
            for path,method,code in [('/api/jobs','POST',405),('/api/monitor/config','POST',405),('/../desktop_app.py','GET',404),('/api/monitor','GET',404)]:
                req=urllib.request.Request(base+path,method=method)
                with self.assertRaises(urllib.error.HTTPError) as ctx:urllib.request.urlopen(req,timeout=3)
                self.assertEqual(ctx.exception.code,code)
            req=urllib.request.Request(base+'/api/health',headers={'Origin':'https://nnn256.github.io'})
            with urllib.request.urlopen(req,timeout=3) as r:
                self.assertEqual(r.headers['Access-Control-Allow-Origin'],'https://nnn256.github.io')
                self.assertEqual(json.load(r)['service'],'chanlun-public-read-only')
        finally:http.shutdown();http.server_close()


if __name__=='__main__':unittest.main()
