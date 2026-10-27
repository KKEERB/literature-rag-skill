import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from portable_config import create
from knowledge_system_core import connect_database, database_path


class PortableConfigTests(unittest.TestCase):
    def test_fresh_workspace_and_identical_reopen(self):
        with tempfile.TemporaryDirectory(prefix='rag 空格 ') as tmp:
            root = (Path(tmp) / 'data').resolve()
            config = create(root)
            self.assertEqual(config, create(root))
            self.assertEqual(config['database'], str(root / 'knowledge.sqlite3'))
            record = json.loads((root / 'config/knowledge_system.json').read_text(encoding='utf-8'))
            self.assertFalse(record['human_review_policy']['enabled'])
            self.assertEqual(record['additional_sources'], [])
            self.assertTrue(record['fast_pdf_model_globs'][0].startswith(str(root)))

    def test_changed_existing_file_does_not_create_other_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'config'
            config.mkdir()
            existing = config / 'workspace.json'
            existing.write_text('{"user": "data"}', encoding='utf-8')
            with self.assertRaises(FileExistsError):
                create(root)
            self.assertFalse((config / 'knowledge_system.json').exists())
            self.assertEqual(existing.read_text(encoding='utf-8'), '{"user": "data"}')

    def test_read_only_database_path_escapes_url_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '知识库 #?.sqlite3'
            with sqlite3.connect(path) as connection:
                connection.execute('CREATE TABLE sentinel (value TEXT)')
                connection.execute("INSERT INTO sentinel VALUES ('ok')")
            with connect_database(path, read_only=True) as connection:
                self.assertEqual(connection.execute('SELECT value FROM sentinel').fetchone()[0], 'ok')
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("INSERT INTO sentinel VALUES ('blocked')")

    def test_existing_database_or_store_cannot_be_inherited(self):
        for name in ('knowledge.sqlite3', 'fast_pdf'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                existing = root / name
                existing.mkdir() if name == 'fast_pdf' else existing.write_bytes(b'old')
                with self.assertRaises(FileExistsError):
                    create(root)
                self.assertFalse((root / 'config/workspace.json').exists())

    def test_database_symlink_is_rejected_for_config_and_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            actual, alias = root / 'real.db', root / 'alias.db'
            actual.write_bytes(b'')
            try:
                alias.symlink_to(actual)
            except OSError:
                self.skipTest('symlinks unavailable')
            for override in (None, alias):
                with self.subTest(override=override), self.assertRaises(ValueError):
                    database_path({'database_path': str(alias)}, override)

    def test_local_pdf_route_does_not_contact_zotero(self):
        """An empty workspace can index and select a PDF without any network call.

        The model and review records are engineering fixtures, not human review.
        """
        import fast_pdf_intake as fast
        import knowledge_system_core as core
        import literature_learning as learning
        import onboarding_doctor
        import portable_deep_read as deep
        if __package__:
            from .test_fast_pdf_intake import fixture
        else:
            from test_fast_pdf_intake import fixture

        with tempfile.TemporaryDirectory(prefix='local pdf no zotero ') as tmp:
            root = Path(tmp).resolve()
            env = dict(os.environ)
            env.pop('ZOTERO_STORAGE_DIR', None)
            env['LITERATURE_RAG_HOME'] = str(root)
            attempts = []
            def block_network(_socket, address):
                attempts.append(address)
                raise AssertionError('network access is forbidden in this local-only test')
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(socket.socket, 'connect', block_network), \
                    patch.object(socket.socket, 'connect_ex', block_network):
                diagnosis = onboarding_doctor.diagnose()
                self.assertNotIn('zotero_local_api', diagnosis)
                workspace = create(root)
                self.assertNotIn('zotero_storage', workspace)
                self.assertFalse((root / 'config/zotero.json').exists())
                config = json.loads(Path(workspace['knowledge_config']).read_text(encoding='utf-8'))
                self.assertEqual(core.sync_database(config)['validation']['integrity'], 'ok')
                fixture_root = root / 'fixture'
                fixture_root.mkdir()
                synthetic = fixture(fixture_root)
                prepared = fast.prepare([fixture_root / 'source.pdf'], None, root / 'batch',
                                        Path(workspace['database']), root / 'fast_pdf',
                                        project_profile='magnetosphere_ionosphere',
                                        model_settings=fast.read_json(synthetic['job_dir'] / 'source.json')['model_settings'])
                job = Path(prepared['jobs'][0]['job_dir'])
                for name in ('analysis.json', 'review.json', 'analysis-dispatch-record.json', 'review-dispatch-record.json'):
                    shutil.copyfile(synthetic['job_dir'] / name, job / name)
                self.assertEqual(fast.publish(root / 'batch', confirmed=True)['status'], 'pass')
                results = core.search_database(Path(workspace['database']), '20')['results']
                self.assertTrue(results)
                source_id = synthetic['source']['source_id']
                selected = learning.select(Path(workspace['database']), source_id, root / 'selected')
                self.assertFalse(selected['state']['whole_paper_accepted'])
                stage = deep.prepare(root / 'selected', root)
                self.assertEqual(stage['status'], 'prepared')
                self.assertFalse(stage['model_executed'])
                self.assertEqual(attempts, [])


if __name__ == '__main__':
    unittest.main()
