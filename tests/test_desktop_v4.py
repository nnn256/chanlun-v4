import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import desktop_app
from chanmodel.release import APP_VERSION,APP_LABEL

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('build_desktop_v4',ROOT/'tools/build_desktop_v4.py')
builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)


class DesktopV4Tests(unittest.TestCase):
    def test_public_seed_paths_are_portable_and_hashes_remain_intact(self):
        value={'file':'C:/Users/fixture/project/market_data/tdx/day/abc.csv',
               'raw_path':'C:\\Users\\fixture\\private\\unknown.json','csv_sha256':'0123',
               'relative':'runtime/results/example.json'}
        result=builder.portable_value(value)
        self.assertEqual(result['file'],'market_data/tdx/day/abc.csv')
        self.assertEqual(result['raw_path'],'<local_path_omitted>')
        self.assertEqual(result['csv_sha256'],'0123')
        self.assertEqual(result['relative'],value['relative'])
    def resources(self,folder):
        folder.mkdir()
        code=b"VERSION='4.0.0'\n";page=b'<title>V4</title>'
        archive=folder/'project-seed.zip'
        with ZipFile(archive,'w') as z:
            z.writestr('chanmodel/release.py',code)
            z.writestr('workbench/web/index.html',page)
            z.writestr('runtime/workbench/workbench.sqlite3',b'bundled-example-db')
        build={'version':APP_VERSION,'release':APP_LABEL,'payload_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
               'code_sha256':{'chanmodel/release.py':hashlib.sha256(code).hexdigest(),
                              'workbench/web/index.html':hashlib.sha256(page).hexdigest()}}
        (folder/'build-info.json').write_text(json.dumps(build),encoding='utf-8')
        return build

    def test_distribution_is_v4_and_every_live_algorithm_file_is_selected(self):
        entries=builder.source_entries()
        self.assertEqual((APP_VERSION,APP_LABEL),('4.0.0','V4'))
        for p in (ROOT/'chanmodel').rglob('*.py'):
            if '__pycache__' not in p.parts:self.assertEqual(entries[p.relative_to(ROOT).as_posix()],p)
        self.assertIn('chanmodel/unified.py',entries)
        self.assertIn('run_complete.py',entries)
        self.assertIn('tools/complete_study_view.html',entries)
        self.assertIn('runtime/tdx-api-revised.exe',entries)
        self.assertNotIn('runtime/workbench/workbench.sqlite3',entries)

    def test_code_repair_preserves_existing_database_and_job_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.resources(root/'resources');data=root/'user-data';data.mkdir()
            with patch.object(desktop_app,'resource_directory',return_value=root/'resources'):
                project,build=desktop_app.prepare_project(data,lambda _:None)
                db=project/'runtime/workbench/workbench.sqlite3';db.write_bytes(b'user-jobs-must-stay')
                result=project/'runtime/workbench/jobs/existing/result.json';result.parent.mkdir(parents=True);result.write_text('saved')
                (project/'chanmodel/release.py').write_text('stale code',encoding='utf-8')
                desktop_app.prepare_project(data,lambda _:None)
            self.assertEqual(db.read_bytes(),b'user-jobs-must-stay')
            self.assertEqual(result.read_text(),'saved')
            self.assertEqual(hashlib.sha256((project/'chanmodel/release.py').read_bytes()).hexdigest(),build['code_sha256']['chanmodel/release.py'])

    def test_corrupted_embedded_archive_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.resources(root/'resources');data=root/'data';data.mkdir()
            archive=root/'resources/project-seed.zip'
            archive.write_bytes(archive.read_bytes()+b'corrupt')
            with patch.object(desktop_app,'resource_directory',return_value=root/'resources'):
                with self.assertRaisesRegex(ValueError,'资源校验失败'):desktop_app.prepare_project(data,lambda _:None)

    def test_incomplete_seed_cannot_claim_successful_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.resources(root/'resources');data=root/'data';data.mkdir()
            info=root/'resources/build-info.json';build=json.loads(info.read_text(encoding='utf-8'))
            build['code_sha256']['chanmodel/missing.py']='0'*64;info.write_text(json.dumps(build),encoding='utf-8')
            with patch.object(desktop_app,'resource_directory',return_value=root/'resources'):
                with self.assertRaisesRegex(ValueError,'不一致'):desktop_app.prepare_project(data,lambda _:None)


if __name__=='__main__':unittest.main()
