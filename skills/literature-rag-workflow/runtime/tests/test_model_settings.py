"""Settings and metadata tests use fake models, not actual model execution."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

if __package__:
    from .test_fast_pdf_intake import SETTINGS, fast, fixture
else:
    from test_fast_pdf_intake import SETTINGS, fast, fixture
import model_settings


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_initial_settings_require_both_rounds_and_partial_update(self):
        path = self.root / 'profile' / 'model_settings.json'
        with self.assertRaisesRegex(ValueError, 'requires model and reasoning_effort'):
            model_settings.save(path, {'analysis': SETTINGS['analysis']})
        self.assertFalse(path.exists())
        model_settings.save(path, {'analysis': SETTINGS['analysis'], 'review': SETTINGS['review']})
        changed = model_settings.save(path, {'review': {'reasoning_effort': 'xhigh'}})
        self.assertEqual(changed['analysis'], SETTINGS['analysis'])
        self.assertEqual(changed['review'], {'model': 'gpt-6.1-sol', 'reasoning_effort': 'xhigh'})
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'reasoning effort'):
            model_settings.save(path, {'review': {'reasoning_effort': 'bad effort'}})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(model_settings.load(path), changed)
        # Syntactically valid future host values must not be rejected based on a
        # static list; account and combination support is a host-side check.
        updated = model_settings.save(path, {'review': {'reasoning_effort': 'future_effort'}})
        self.assertEqual(updated['review']['reasoning_effort'], 'future_effort')

    def test_requires_choice_and_legacy_batch_resumes(self):
        pdf = self.root / 'source.pdf'
        pdf.write_bytes(b'placeholder')
        database = self.root / 'old.sqlite3'
        batch = self.root / 'old'
        with self.assertRaisesRegex(ValueError, 'model settings required'):
            fast.prepare([pdf], None, self.root / 'new', database, project_profile='example')
        config = {'inputs': [{'path': str(pdf.resolve()), 'project_profile': 'example'}],
                  'database': str(database.resolve()), 'storage_root': str((self.root / 'fast_pdf').resolve()),
                  'max_pages': 40}
        batch.mkdir()
        fast.write_once(batch / 'batch.json', {**config, 'schema': 'fast-pdf-batch/1',
                                               'prepare_binding': fast.object_sha(config), 'jobs': [],
                                               'models': {'analysis': ['gpt-6.1-sol', 'medium'],
                                                          'review': ['gpt-6.1-sol', 'high']}})
        result = fast.prepare([pdf], None, batch, database, project_profile='example')
        self.assertEqual(result['execution_model_identity'], 'NOT_VERIFIED')
        with self.assertRaisesRegex(ValueError, 'legacy batch'):
            fast.prepare([pdf], None, batch, database, project_profile='example', model_settings=SETTINGS)
        with self.assertRaisesRegex(ValueError, 'database override differs'):
            fast.publish(batch, database=self.root / 'different.sqlite3')

    def test_requested_settings_frozen_and_checked_with_no_claim_of_attestation(self):
        selected = {'schema': model_settings.SCHEMA,
                    'analysis': {'model': 'fixture-analysis', 'reasoning_effort': 'low'},
                    'review': {'model': 'fixture-review', 'reasoning_effort': 'future_effort'}}
        info = fixture(self.root, selected)
        job = info['job_dir']
        batch = fast.read_json(info['batch_dir'] / 'batch.json')
        self.assertEqual(batch['model_settings'], selected)
        self.assertEqual(fast.read_json(job / 'producer-task.json')['model'], 'fixture-analysis')
        self.assertEqual(fast.read_json(job / 'reviewer-task.json')['reasoning_effort'], 'future_effort')
        self.assertEqual(fast.batch_status(info['batch_dir'])['execution_model_identity'], 'NOT_VERIFIED')
        fast.validate_job(job)
        fast.prepare([], info['manifest'], info['batch_dir'], info['database'], info['storage'])
        changed = {**selected, 'review': {'model': 'other', 'reasoning_effort': 'high'}}
        with self.assertRaisesRegex(ValueError, 'model settings differ'):
            fast.prepare([], info['manifest'], info['batch_dir'], info['database'], info['storage'], model_settings=changed)
        review_path = job / 'review.json'
        review = fast.read_json(review_path)
        review['reviewer']['model'] = 'not-the-requested-model'
        review_path.write_text(json.dumps(review), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'output declaration differs'):
            fast.validate_job(job)
        self.assertEqual(batch['model_settings'], selected)

    def test_tampered_task_model_is_not_accepted(self):
        info = fixture(self.root)
        task_path = info['job_dir'] / 'producer-task.json'
        task = fast.read_json(task_path)
        task['model'] = 'different-model'
        task_path.write_text(json.dumps(task), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'immutable task binding changed'):
            fast.validate_job(info['job_dir'])

    def test_batch_and_source_cannot_disagree_when_summary_is_edited(self):
        info = fixture(self.root)
        batch_path = info['batch_dir'] / 'batch.json'
        batch = fast.read_json(batch_path)
        batch['model_settings']['review']['model'] = 'another-model'
        batch['models']['review'][0] = 'another-model'
        frozen = {k: batch[k] for k in ('inputs', 'database', 'storage_root', 'max_pages', 'model_settings')}
        batch['prepare_binding'] = fast.object_sha(frozen)
        batch_path.write_text(json.dumps(batch), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'source input/model settings differ'):
            fast.batch_status(info['batch_dir'])
        with self.assertRaisesRegex(ValueError, 'source input/model settings differ'):
            fast.publish(info['batch_dir'])

    def test_configured_batch_cannot_be_downgraded_to_legacy(self):
        info = fixture(self.root)
        batch_path = info['batch_dir'] / 'batch.json'
        batch = fast.read_json(batch_path)
        del batch['model_settings']
        legacy = {k: batch[k] for k in ('inputs', 'database', 'storage_root', 'max_pages')}
        batch['prepare_binding'] = fast.object_sha(legacy)
        batch_path.write_text(json.dumps(batch), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'cannot be treated as a legacy batch'):
            fast.batch_status(info['batch_dir'])
        with self.assertRaisesRegex(ValueError, 'cannot be treated as a legacy batch'):
            fast.publish(info['batch_dir'])

    def test_task_requirements_are_bound_before_dispatch(self):
        info = fixture(self.root)
        task_path = info['job_dir'] / 'reviewer-task.json'
        task = fast.read_json(task_path)
        task['requirements'] = []
        task_path.write_text(json.dumps(task), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'immutable task binding changed'):
            fast.batch_status(info['batch_dir'])
        with self.assertRaisesRegex(ValueError, 'immutable task binding changed'):
            fast.record_dispatch(info['job_dir'], 'review', {'host_tool': 'fixture', 'task_id': 'review'})

    def test_expected_source_hash_checks_before_reuse_or_extraction(self):
        info = fixture(self.root)
        pdf = self.root / 'source.pdf'
        def prepare_expected(name, expected):
            manifest = self.root / (name + '.json')
            manifest.write_text(json.dumps([{'path': str(pdf), 'project_profile': 'magnetosphere_ionosphere',
                                             'expected_pdf_sha256': expected}]), encoding='utf-8')
            return fast.prepare([], manifest, self.root / name, info['database'], info['storage'], model_settings=SETTINGS)
        good = prepare_expected('correct', fast.sha(pdf))
        self.assertEqual(good['counts'], {'awaiting_analysis': 1})
        wrong = prepare_expected('wrong', '0' * 64)
        self.assertEqual(wrong['counts'], {'blocked': 1})
        self.assertIn('expected_pdf_sha256 does not match', wrong['jobs'][0]['reason'])
        malformed = prepare_expected('bad-format', 'not-a-hash')
        self.assertEqual(malformed['counts'], {'blocked': 1})
        self.assertIn('invalid expected_pdf_sha256', malformed['jobs'][0]['reason'])

    def test_default_db_follows_user_workspace_environment(self):
        workspace = self.root / '独立 workspace'
        script_dir = Path(fast.__file__).parent
        env = dict(os.environ, LITERATURE_RAG_HOME=str(workspace), PYTHONPATH=str(script_dir))
        result = subprocess.run([sys.executable, '-c', 'import fast_pdf_intake; print(fast_pdf_intake.DEFAULT_DB)'],
                                env=env, capture_output=True, text=True, encoding='utf-8', check=True)
        self.assertEqual(Path(result.stdout.strip()), workspace / 'knowledge.sqlite3')

    def test_host_metadata_is_separate_and_does_not_attest_served_model(self):
        info = fixture(self.root, dispatch=False)
        metadata = {'host_tool': 'fixture', 'task_id': 'synthetic-producer',
                    'observed_model': 'gpt-6.1-sol'}
        record = fast.record_dispatch(info['job_dir'], 'analysis', metadata)
        self.assertEqual(record['underlying_model_identity'], 'NOT_VERIFIED')
        status = fast.batch_status(info['batch_dir'])['jobs'][0]
        self.assertEqual(status['analysis_dispatch_record']['caller_supplied_host_metadata'], metadata)
        self.assertFalse(status['analysis_output_declaration_is_execution_attestation'])
        fast.record_dispatch(info['job_dir'], 'review', {'task_id': 'synthetic-reviewer', 'host_tool': 'fixture'})
        fast.validate_job(info['job_dir'])
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            fast.record_dispatch(info['job_dir'], 'review', {'host_tool': 'fixture',
                'task_id': 'synthetic-reviewer', 'observed_model': 'other'})
        with self.assertRaisesRegex(ValueError, 'immutable artifact'):
            fast.record_dispatch(info['job_dir'], 'analysis', {'host_tool': 'fixture',
                'task_id': 'different-task'})

    def test_both_dispatch_records_required_for_configured_batch(self):
        info = fixture(self.root, dispatch=False)
        status = fast.batch_status(info['batch_dir'])
        self.assertEqual(status['counts'], {'dispatch_record_missing': 1})
        self.assertEqual(status['jobs'][0]['missing_dispatch_records'], ['analysis', 'review'])
        with self.assertRaisesRegex(ValueError, 'analysis host dispatch record required'):
            fast.validate_job(info['job_dir'])
        with self.assertRaisesRegex(ValueError, 'analysis host dispatch record required'):
            fast.publish(info['batch_dir'], confirmed=True)
        with self.assertRaisesRegex(ValueError, 'task_id and host_tool'):
            fast.record_dispatch(info['job_dir'], 'analysis', {'task_id': 'synthetic-producer'})
        fast.record_dispatch(info['job_dir'], 'analysis', {'task_id': 'synthetic-producer', 'host_tool': 'fixture'})
        with self.assertRaisesRegex(ValueError, 'review host dispatch record required'):
            fast.publish(info['batch_dir'])
        fast.record_dispatch(info['job_dir'], 'review', {'task_id': 'wrong-id', 'host_tool': 'fixture'})
        with self.assertRaisesRegex(ValueError, 'output task id differs'):
            fast.publish(info['batch_dir'], confirmed=True)

    def test_malformed_dispatch_record_cannot_satisfy_gate(self):
        info = fixture(self.root)
        path = info['job_dir'] / 'review-dispatch-record.json'
        record = fast.read_json(path)
        del record['caller_supplied_host_metadata']['host_tool']
        path.write_text(json.dumps(record), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'invalid host dispatch metadata'):
            fast.validate_job(info['job_dir'])


if __name__ == '__main__':
    unittest.main()
