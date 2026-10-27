import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from apply_knowledge_reviews import CHECKS, import_reviews
from knowledge_quality import availability, canonical, digest, input_binding, write_record, record_intake_quality
from knowledge_system_core import initialize_database, insert_source_and_chunks, get_chunk, insert_row, CHUNK_COLUMNS
from review_knowledge_quality import apply, prepare
from test_knowledge_system import source, chunk


class QualityTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'test.sqlite3'
        self.s, self.c = source(), chunk()
        for filename, field in (('source.pdf', 'source_pdf_sha256'),
                                ('02_machine_evidence_pack.json', 'model_sha256'),
                                ('02_rag_knowledge.md', 'rag_sha256')):
            path = self.root / filename
            path.write_text('synthetic fixture ' + filename)
            self.s[field] = digest(path)
        self.s.update(source_path=str(self.root), source_pdf_path=str(self.root / 'source.pdf'))
        self.row = self.s | self.c
        with sqlite3.connect(self.db) as con:
            initialize_database(con)
            insert_source_and_chunks(con, self.s, [self.c])
        self.baseline = {'state': 'unchecked', 'inputs': input_binding(self.row),
                         'mechanical_checked': True, 'source_id': self.s['source_id']}
        self.plan = {'database': str(self.db.resolve()),
                     'source_records': {self.s['source_id']: {'state': 'unchecked', 'source_availability': availability(self.s)}},
                     'chunk_records': {self.c['chunk_id']: self.baseline}}
        self.plan_path = self.root / 'quality_plan.json'
        self.plan_path.write_text(json.dumps(self.plan))
        self.lock = self.root / 'workflow.lock'
        self.fresh = b'An original source passage.\n\f'
        self.record = self.baseline | {'chunk_id': self.c['chunk_id'], 'state': 'verified',
            'semantic_checked': True, 'review': {'method': 'original_source_semantic_review',
                'support': 'supported', 'producer_id': 'original-producer', 'reviewer_id': 'independent-reviewer',
                'rationale_zh': 'This is a synthetic test receipt.', 'reviewer_attestation': 'fixture',
                'source_pdf_path': self.s['source_pdf_path'], 'source_pages': [1],
                'source_passage': 'PDF PAGE 1\nAn original source passage.\n',
                'fresh_text_sha256': hashlib.sha256(self.fresh).hexdigest(),
                'checks': {key: True for key in CHECKS}, 'check_details': {key: 'fixture check' for key in CHECKS}}}
        self.decisions = self.root / 'decisions.jsonl'
        self.decisions.write_text(canonical(self.record) + '\n')

    def test_baseline_write_is_idempotent_and_preserves_source_status(self):
        apply(self.db, self.plan_path, self.lock)
        apply(self.db, self.plan_path, self.lock)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute('SELECT count(*) FROM quality_history').fetchone()[0], 2)
            self.assertEqual(con.execute('SELECT status FROM sources').fetchone()[0], self.s['status'])

    def test_concurrent_user_content_change_blocks_write(self):
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE chunks SET text='user edited content'")
        with self.assertRaisesRegex(ValueError, 'changed since baseline'):
            apply(self.db, self.plan_path, self.lock)
        with sqlite3.connect(self.db) as con:
            self.assertIsNone(con.execute("SELECT name FROM sqlite_master WHERE name='chunk_quality'").fetchone())

    def test_interrupted_transaction_can_be_retried(self):
        with patch('review_knowledge_quality.write_record', side_effect=RuntimeError('simulated interruption')):
            with self.assertRaisesRegex(RuntimeError, 'interruption'):
                apply(self.db, self.plan_path, self.lock)
        result = apply(self.db, self.plan_path, self.lock)
        self.assertTrue(result['postcommit_readback'])

    def test_new_semantic_review_cannot_be_overwritten_by_stale_baseline(self):
        apply(self.db, self.plan_path, self.lock)
        with patch('apply_knowledge_reviews.subprocess.run') as extract:
            extract.return_value.stdout = self.fresh
            result = import_reviews(self.db, self.decisions, self.root / 'import', confirmed=True, lock_path=self.lock)
        self.assertEqual(result['status'], 'pass')
        self.assertTrue(get_chunk(self.db, self.c['chunk_id'])['supports_claim'])
        with self.assertRaisesRegex(ValueError, 'newer quality decision'):
            apply(self.db, self.plan_path, self.lock)
        self.assertTrue(get_chunk(self.db, self.c['chunk_id'])['supports_claim'])

    def test_fabricated_or_old_passage_cannot_be_imported(self):
        apply(self.db, self.plan_path, self.lock)
        self.record['review']['source_passage'] = 'PDF PAGE 1\nInvented text.\n'
        self.decisions.write_text(canonical(self.record) + '\n')
        with patch('apply_knowledge_reviews.subprocess.run') as extract:
            extract.return_value.stdout = self.fresh
            with self.assertRaisesRegex(ValueError, 'absent from bound'):
                import_reviews(self.db, self.decisions, self.root / 'import', confirmed=True, lock_path=self.lock)
        self.assertFalse(get_chunk(self.db, self.c['chunk_id'])['supports_claim'])

    def test_local_quarantine_does_not_block_other_verified_atoms(self):
        apply(self.db, self.plan_path, self.lock)
        other = dict(self.c, chunk_id=self.c['chunk_id'] + ':bad-figure')
        with sqlite3.connect(self.db) as con:
            insert_row(con, 'chunks', CHUNK_COLUMNS, other)
        bad = json.loads(canonical(self.record))
        bad.update(chunk_id=other['chunk_id'], inputs=input_binding(self.s | other), state='quarantined')
        bad['review']['support'] = 'unsupported'
        bad['review']['checks']['whole_assertion'] = False
        self.decisions.write_text(canonical(self.record) + '\n' + canonical(bad) + '\n')
        with patch('apply_knowledge_reviews.subprocess.run') as extract:
            extract.return_value.stdout = self.fresh
            import_reviews(self.db, self.decisions, self.root / 'import', confirmed=True, lock_path=self.lock)
        self.assertTrue(get_chunk(self.db, self.c['chunk_id'])['supports_claim'])
        self.assertFalse(get_chunk(self.db, other['chunk_id'])['supports_claim'])
        with sqlite3.connect(self.db) as con:
            summary = json.loads(con.execute('SELECT record_json FROM source_quality').fetchone()[0])
        self.assertEqual(summary['state_scope'], 'atomic_summary')
        self.assertEqual(summary['atomic_state_counts']['quarantined'], 1)
        # A disappearing original still blocks the verified atom immediately.
        Path(self.s['source_pdf_path']).unlink()
        self.assertFalse(get_chunk(self.db, self.c['chunk_id'])['supports_claim'])

    def test_explicit_and_legacy_whole_source_quarantines_survive_atomic_import(self):
        apply(self.db, self.plan_path, self.lock)
        for scope in ('source', None):
            with self.subTest(scope=scope):
                record = {'state': 'quarantined', 'reason_zh': 'synthetic identity concern'}
                if scope:
                    record['state_scope'] = scope
                with sqlite3.connect(self.db) as con:
                    write_record(con, 'source_quality', self.s['source_id'], record)
                with patch('apply_knowledge_reviews.subprocess.run') as extract:
                    extract.return_value.stdout = self.fresh
                    import_reviews(self.db, self.decisions, self.root / ('import-' + str(scope)),
                                   confirmed=True, lock_path=self.lock)
                self.assertFalse(get_chunk(self.db, self.c['chunk_id'])['supports_claim'])
                with sqlite3.connect(self.db) as con:
                    current = json.loads(con.execute('SELECT record_json FROM source_quality').fetchone()[0])
                self.assertEqual(current['state'], 'quarantined')
                self.assertNotEqual(current.get('state_scope'), 'atomic_summary')

    def test_mechanical_refresh_keeps_atomic_quarantine_local(self):
        apply(self.db, self.plan_path, self.lock)
        other = dict(self.c, chunk_id=self.c['chunk_id'] + ':bad-figure')
        inputs = {k: self.s[k] for k in ('source_pdf_sha256', 'model_sha256', 'rag_sha256')}
        bad = self.baseline | {'state': 'quarantined', 'semantic_checked': True,
                              'inputs': input_binding(self.s | other)}
        with sqlite3.connect(self.db) as con:
            insert_row(con, 'chunks', CHUNK_COLUMNS, other)
            write_record(con, 'chunk_quality', self.c['chunk_id'], self.record)
            write_record(con, 'chunk_quality', other['chunk_id'], bad)
            write_record(con, 'source_quality', self.s['source_id'],
                         {'state': 'quarantined', 'state_scope': 'atomic_summary', 'inputs': inputs,
                          'atomic_state_counts': {'verified': 1, 'quarantined': 1}})
        with patch('review_knowledge_quality.audit_database', return_value={'findings': []}):
            prepare(self.db, self.root / 'refresh-local')
        plan = json.loads((self.root / 'refresh-local/quality_plan.json').read_text())
        self.assertEqual(plan['source_records'][self.s['source_id']]['state_scope'], 'atomic_summary')
        self.assertEqual(plan['chunk_records'][self.c['chunk_id']]['state'], 'verified')
        self.assertEqual(plan['chunk_records'][other['chunk_id']]['state'], 'quarantined')
        apply(self.db, self.root / 'refresh-local/quality_plan.json', self.lock)
        self.assertTrue(get_chunk(self.db, self.c['chunk_id'])['supports_claim'])
        self.assertFalse(get_chunk(self.db, other['chunk_id'])['supports_claim'])

    def test_mechanical_refresh_preserves_scientific_time_and_complete_binding_metadata(self):
        apply(self.db, self.plan_path, self.lock)
        original = self.record | {'observed_at': '2026-01-01T12:34:56+00:00',
                                 'technical_binding': {'binder_id': 'different-binder',
                                                       'original_review_path': '/fixture/review.json'},
                                 'original_science_receipt_sha256': 'fixture-exact-receipt'}
        with sqlite3.connect(self.db) as con:
            write_record(con, 'chunk_quality', self.c['chunk_id'], original)
        output = self.root / 'refresh-science-time'
        with patch('review_knowledge_quality.audit_database', return_value={'findings': []}):
            prepare(self.db, output)
        prepared = json.loads((output / 'quality_plan.json').read_text())['chunk_records'][self.c['chunk_id']]
        for key, value in original.items():
            if key not in ('mechanical_findings', 'checker_version'):
                self.assertEqual(prepared[key], value, key)
        self.assertNotEqual(prepared['mechanical_observed_at'], original['observed_at'])
        apply(self.db, output / 'quality_plan.json', self.lock)
        with sqlite3.connect(self.db) as con:
            actual = json.loads(con.execute('SELECT record_json FROM chunk_quality').fetchone()[0])
            self.assertEqual(actual, prepared)
            old_history = [json.loads(raw) for raw, in con.execute('SELECT record_json FROM quality_history')]
            self.assertIn(original, old_history)
        self.assertTrue(get_chunk(self.db, self.c['chunk_id'])['supports_claim'])

    def test_refresh_does_not_invent_a_missing_semantic_observation_time(self):
        apply(self.db, self.plan_path, self.lock)
        with sqlite3.connect(self.db) as con:
            write_record(con, 'chunk_quality', self.c['chunk_id'], self.record)
        output = self.root / 'refresh-missing-science-time'
        with patch('review_knowledge_quality.audit_database', return_value={'findings': []}):
            prepare(self.db, output)
        prepared = json.loads((output / 'quality_plan.json').read_text())['chunk_records'][self.c['chunk_id']]
        self.assertNotIn('observed_at', prepared)
        self.assertIn('mechanical_observed_at', prepared)
        self.assertEqual(prepared['review'], self.record['review'])

    def test_mechanical_refresh_preserves_whole_source_reason_and_new_missing_source(self):
        apply(self.db, self.plan_path, self.lock)
        inputs = {k: self.s[k] for k in ('source_pdf_sha256', 'model_sha256', 'rag_sha256')}
        for scope in ('source', None, 'atomic_summary'):
            with self.subTest(scope=scope):
                prior = {'state': 'quarantined', 'inputs': inputs, 'reason_zh': 'original identity concern'}
                if scope:
                    prior['state_scope'] = scope
                with sqlite3.connect(self.db) as con:
                    write_record(con, 'chunk_quality', self.c['chunk_id'], self.record)
                    write_record(con, 'source_quality', self.s['source_id'], prior)
                if scope == 'atomic_summary':
                    Path(self.s['source_pdf_path']).unlink()
                output = self.root / ('refresh-' + str(scope))
                with patch('review_knowledge_quality.audit_database', return_value={'findings': []}):
                    prepare(self.db, output)
                plan = json.loads((output / 'quality_plan.json').read_text())
                source_record = plan['source_records'][self.s['source_id']]
                self.assertEqual(source_record['state'], 'quarantined')
                self.assertNotEqual(source_record.get('state_scope'), 'atomic_summary')
                if scope != 'atomic_summary':
                    self.assertEqual(source_record['reason_zh'], prior['reason_zh'])
                self.assertEqual(plan['chunk_records'][self.c['chunk_id']]['state'], 'quarantined')

    def test_unbound_or_changed_whole_quarantine_survives_refresh_and_intake(self):
        apply(self.db, self.plan_path, self.lock)
        for scope in ('source', None):
            for old_inputs in (None, {'model_sha256': 'old-quarantine-binding'}):
                with self.subTest(scope=scope, inputs=old_inputs):
                    prior = {'state': 'quarantined', 'reason_zh': 'unresolved original identity',
                             'revision_relation': {'unrelated': 'stale fixture relation'}}
                    if scope:
                        prior['state_scope'] = scope
                    if old_inputs:
                        prior['inputs'] = old_inputs
                    with sqlite3.connect(self.db) as con:
                        write_record(con, 'chunk_quality', self.c['chunk_id'], self.record)
                        write_record(con, 'source_quality', self.s['source_id'], prior)
                        record_intake_quality(con, self.s, [self.c])
                        intake = json.loads(con.execute('SELECT record_json FROM source_quality').fetchone()[0])
                    self.assertEqual(intake['state'], 'quarantined')
                    self.assertEqual(intake['reason_zh'], prior['reason_zh'])
                    self.assertNotIn('revision_relation', intake)
                    self.assertFalse(get_chunk(self.db, self.c['chunk_id'])['supports_claim'])
                    # Also check the original unbound record directly, rather
                    # than only testing the already repaired intake result.
                    with sqlite3.connect(self.db) as con:
                        write_record(con, 'source_quality', self.s['source_id'], prior)
                    output = self.root / ('unbound-' + str(scope) + '-' + str(bool(old_inputs)))
                    with patch('review_knowledge_quality.audit_database', return_value={'findings': []}):
                        prepare(self.db, output)
                    plan = json.loads((output / 'quality_plan.json').read_text())
                    sr = plan['source_records'][self.s['source_id']]
                    self.assertEqual(sr['state'], 'quarantined')
                    self.assertEqual(sr['reason_zh'], prior['reason_zh'])
                    self.assertNotIn('revision_relation', sr)
                    self.assertEqual(plan['chunk_records'][self.c['chunk_id']]['state'], 'quarantined')


if __name__ == '__main__':
    unittest.main()
