"""Isolated core integration for fast local PDF evidence; no Zotero or live DB."""
from __future__ import annotations

import copy
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))

from audit_knowledge_integrity import audit_database, fast_pdf_passage_status
from knowledge_quality import initialize_quality, input_binding, write_record
from knowledge_system_core import (apply_human_review_policy, build_current_state, database_status,
                                   get_chunk, initialize_database, search_database, sync_database, validate_database)
from test_knowledge_system import source, chunk
from v2_core import sha256_file


class FastPDFCoreIntegrationTests(unittest.TestCase):
    def test_fast_raw_excerpt_boundary_is_not_changed_by_later_hyphenation(self):
        quote = 'The brightness lumi-'
        texts = {1: ['The brightness lumi-\n\nnosity was measured at 37 s cadence.']}
        self.assertEqual(fast_pdf_passage_status(quote, [1], texts), 'raw_exact')

    def test_fast_numbered_excerpts_are_checked_at_each_page_not_as_full_page_join(self):
        quote = 'PDF PAGE 2\nThe fixed forcing is 300 km/s.\nPDF PAGE 5\nNo observed flow is established.'
        texts = {2: ['Heading.\nThe fixed forcing is 300 km/s.\nUnquoted trailing context.'],
                 5: ['Other heading.\nNo observed flow is established.\nLimitations.']}
        self.assertEqual(fast_pdf_passage_status(quote, [2, 5], texts), 'raw_exact')
        self.assertEqual(fast_pdf_passage_status(quote, [2, 4], texts), '')
        self.assertEqual(fast_pdf_passage_status(quote, [2, 5], {2: texts[2], 5: ['Different page.']}), '')

    def test_fast_whitespace_contract_does_not_normalize_numbers_signs_or_negation(self):
        text = 'The model speed is -300 km/s.\nNo observational confirmation.'
        quote = 'PDF PAGE 1\nThe model speed is -300\nkm/s. No observational confirmation.'
        self.assertEqual(fast_pdf_passage_status(quote, [1], {1: [text]}), 'whitespace_equivalent')
        for changed in (quote.replace('-300', '300'), quote.replace('-300', '-301'),
                        quote.replace('No observational', 'An observational'), quote.replace('km/s.', 'km/s,')):
            with self.subTest(changed=changed):
                self.assertEqual(fast_pdf_passage_status(changed, [1], {1: [text]}), '')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'knowledge.sqlite3'
        self.config = {'schema_version': '1.0', 'database_path': str(self.db),
                       'review_dir': str(self.root / 'review'), 'baseline': {'validation_mode': 'dynamic'},
                       'fast_pdf_model_globs': [str(self.root / '02_machine_evidence_pack.json')]}
        pdf = self.root / 'paper.pdf'
        pdf.write_bytes(b'%PDF fixture; tools patched, no science acceptance inferred')
        digest = sha256_file(pdf)
        self.source = source('pdf:' + digest)
        self.source.update(zotero_item_key='', zotero_attachment_key='', doi='',
                           source_path=str(self.root), source_pdf_path=str(pdf), source_pdf_sha256=digest,
                           updated_at='2026-10-23T00:00:00Z', provenance_json=json.dumps({
                               'work_id': 'pdf:' + digest, 'ingestion_mode': 'fast_pdf_two_round',
                               'coverage_status': 'partial', 'coverage_gaps': ['Figure 2 unverified'],
                               'review_provenance': {'producer_id': 'producer-A', 'reviewer_id': 'reviewer-B'}}))
        self.chunk = chunk(self.source['source_id'])
        self.chunk.update(chunk_id='fast:' + digest + ':I001', item_id='I001',
                          evidence_json=json.dumps({'pdf_page_index': 1, 'section': 'Results',
                                                    'quote': self.chunk['text']}))
        model = {key: self.source[key] for key in ['zotero_item_key', 'zotero_attachment_key', 'doi', 'source_pdf_sha256']}
        model.update(schema_version='fast_pdf_pack_v1', status='draft', human_check_status='NOT HUMAN CHECKED')
        model_path = self.root / '02_machine_evidence_pack.json'
        model_path.write_text(json.dumps(model))
        rag = self.root / '02_rag_knowledge.md'
        rag.write_text('<!-- chunk_id: ' + self.chunk['chunk_id'] + ' -->\n')
        self.source.update(model_sha256=sha256_file(model_path), rag_sha256=sha256_file(rag))
        self.adapter = types.ModuleType('fast_pdf_intake')
        self.adapter.discover_fast_pdf_sources = lambda _config: ([copy.deepcopy(self.source)], [copy.deepcopy(self.chunk)], [])
        self.adapter.validate_final_pack = lambda _path: (copy.deepcopy(self.source), [copy.deepcopy(self.chunk)])

    def discovery(self):
        stack = ExitStack()
        stack.enter_context(patch.dict(sys.modules, {'fast_pdf_intake': self.adapter}))
        stack.enter_context(patch('knowledge_system_core.discover_paper_sources', return_value=([], [], [])))
        stack.enter_context(patch('knowledge_system_core.discover_additional_sources', return_value=([], [], [])))
        return stack

    def seed(self):
        with self.discovery():
            return sync_database(self.config)

    def review(self, state='verified'):
        with sqlite3.connect(self.db) as connection:
            write_record(connection, 'chunk_quality', self.chunk['chunk_id'], {
                'state': state, 'semantic_checked': True,
                'inputs': input_binding(self.source | self.chunk | {'source_authority': self.source['authority']}),
                'review': {'method': 'original_source_semantic_review', 'support': 'supported',
                           'producer_id': 'producer-A', 'reviewer_id': 'reviewer-B',
                           'source_passage': self.chunk['text'], 'source_pages': [1],
                           'checks': {key: True for key in ('whole_assertion', 'conditions_scope', 'numbers_units',
                                                          'attribution', 'epistemic_layer', 'compound_assertion')}}})

    def test_lazy_discovery_survives_sync_and_noop_without_zotero_identity(self):
        with self.discovery():
            state = build_current_state(self.config)
            self.assertEqual(state['fast_pdf_source_count'], 1)
            self.assertEqual(state['sources'][0]['zotero_item_key'], '')
            first = sync_database(self.config)
            second = sync_database(self.config, if_stale=True)
        self.assertEqual(first['validation']['fast_pdf_source_count'], 1)
        self.assertTrue(second['no_op'])
        status = database_status(self.db)
        self.assertEqual(status['fast_pdf_source_count'], 1)
        self.assertEqual(status['paper_work_identity_count'], 1)
        self.assertEqual(status['promotion_count'], 0)

    def test_local_citation_and_support_use_same_quality_gate(self):
        self.seed()
        item = get_chunk(self.db, self.chunk['chunk_id'])
        self.assertFalse(item['supports_claim'])
        self.assertFalse(item['human_reviewed'])
        self.assertEqual(item['work_id'], self.source['source_id'])
        self.assertEqual(item['citation']['local_pdf_identity'], self.source['source_id'])
        self.assertEqual(item['citation']['source_pdf_sha256'], self.source['source_pdf_sha256'])
        self.assertEqual(item['coverage_gaps'], ['Figure 2 unverified'])
        self.assertEqual(item['ingestion_mode'], 'fast_pdf_two_round')
        self.review()
        item = get_chunk(self.db, self.chunk['chunk_id'])
        self.assertTrue(item['supports_claim'])
        self.assertFalse(item['human_reviewed'])
        results = search_database(self.db, 'flux transfer event discriminators')['results']
        self.assertEqual(results[0]['chunk_id'], self.chunk['chunk_id'])
        with sqlite3.connect(self.db) as connection:
            before_quality = list(connection.execute('SELECT * FROM quality_history ORDER BY event_id'))
        with self.discovery():
            sync_database(self.config)
        self.assertTrue(get_chunk(self.db, self.chunk['chunk_id'])['supports_claim'])
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(list(connection.execute('SELECT * FROM quality_history ORDER BY event_id')), before_quality)
        self.review('quarantined')
        self.assertFalse(get_chunk(self.db, self.chunk['chunk_id'])['supports_claim'])
        self.assertEqual(search_database(self.db, 'flux transfer event discriminators')['results'], [])

    def test_historical_human_attestation_never_applies_to_fast_source(self):
        row = copy.deepcopy(self.source)
        row['updated_at'] = '2026-01-01T00:00:00Z'
        policy = {'enabled': True, 'mode': 'user_attested_default', 'attested_at': '2026-08-27T00:00:00Z',
                  'attested_by': 'user', 'review_scope': 'historical canonical library',
                  'canonical_generated_at_lte': '2026-08-27T00:00:00Z'}
        self.assertEqual(apply_human_review_policy({'human_review_policy': policy}, [row]), 0)
        self.assertEqual(row['status'], 'draft')

    def test_exact_legacy_baseline_does_not_count_additional_fast_sources(self):
        self.seed()
        with sqlite3.connect(self.db) as connection:
            result = validate_database(connection, {'baseline': {'processed_parent_count': 0, 'paper_chunk_count': 0}})
        self.assertEqual(result['paper_source_count'], 1)
        self.assertEqual(result['fast_pdf_source_count'], 1)

    def test_duplicate_sha_cannot_become_independent_canonical_and_fast_evidence(self):
        legacy = copy.deepcopy(self.source)
        legacy['source_id'] = 'zotero:REALPARENT'
        with self.discovery(), patch('knowledge_system_core.discover_paper_sources', return_value=([legacy], [], [])):
            with self.assertRaisesRegex(RuntimeError, 'duplicates an existing paper source SHA'):
                build_current_state(self.config)

    def test_unresolved_doi_version_collision_blocks_discovery(self):
        self.source['doi'] = '10.test/example'
        legacy = copy.deepcopy(self.source)
        legacy.update(source_id='zotero:REALPARENT', source_pdf_sha256='f' * 64,
                      doi='https://doi.org/10.TEST/EXAMPLE')
        with self.discovery(), patch('knowledge_system_core.discover_paper_sources', return_value=([legacy], [], [])):
            with self.assertRaisesRegex(RuntimeError, 'unresolved DOI/version collision'):
                build_current_state(self.config)

    def test_audit_validates_fast_projection_and_detects_invalid_pack(self):
        self.seed()
        with patch.dict(sys.modules, {'fast_pdf_intake': self.adapter}), \
                patch('audit_knowledge_integrity.pdf_pages', return_value=(1, '')), \
                patch('audit_knowledge_integrity.fresh_page_text', return_value=self.chunk['text']):
            report = audit_database(self.db)
            self.assertEqual(report['coverage']['fast_pdf_final_packs_validated'], 1)
            self.assertFalse(any(f['severity'] == 'error' for f in report['findings']), report['findings'])
            self.adapter.validate_final_pack = lambda _path: (_ for _ in ()).throw(ValueError('review binding drift'))
            report = audit_database(self.db)
            self.assertIn('fast_pdf_final_pack_invalid', {f['code'] for f in report['findings']})

    def test_audit_does_not_waive_same_doi_different_pdf_identity(self):
        self.seed()
        with sqlite3.connect(self.db) as connection:
            # A second active source with the same DOI is still an error; a
            # local PDF identity never authorizes a broad duplicate exception.
            connection.execute("UPDATE sources SET doi='10.test/same'")
            columns = [row[1] for row in connection.execute('PRAGMA table_info(sources)')]
            row = list(connection.execute('SELECT * FROM sources').fetchone())
            row[columns.index('source_id')] = 'pdf:' + 'f' * 64
            row[columns.index('source_pdf_sha256')] = 'f' * 64
            connection.execute('INSERT INTO sources VALUES (' + ','.join('?' for _ in row) + ')', row)
        with patch.dict(sys.modules, {'fast_pdf_intake': self.adapter}), \
                patch('audit_knowledge_integrity.pdf_pages', return_value=(1, '')), \
                patch('audit_knowledge_integrity.fresh_page_text', return_value=self.chunk['text']):
            report = audit_database(self.db)
        self.assertTrue(any(f['code'] == 'active_duplicate_identity' and f['identity_type'] == 'doi'
                            and f['severity'] == 'error' for f in report['findings']))

    def test_real_adapter_cached_pdf_subset_sync_and_audit(self):
        """Use the actual new adapter and extraction against a synthetic PDF.

        The semantic decisions here are explicit test fixtures, not model work
        or human acceptance. Real pilot review is measured separately.
        """
        from reportlab.pdfgen import canvas
        import fast_pdf_intake as intake
        text = ('Synthetic fixture: a model speed is 300 km/s under fixed forcing. '
                'This is a simulation result, not an observation. '
                'Three independent discriminators are required for event identification.')
        pdf = self.root / 'synthetic-source.pdf'
        from reportlab.lib.utils import simpleSplit
        document = canvas.Canvas(str(pdf), pagesize=(595, 842))
        document.setFont('Helvetica', 12)
        for index, line in enumerate(simpleSplit(text, 'Helvetica', 12, 510)):
            document.drawString(40, 802 - index * 16, line)
        document.save()
        with sqlite3.connect(self.db) as connection:
            initialize_database(connection)
            initialize_quality(connection)
        inputs = self.root / 'inputs.json'
        inputs.write_text(json.dumps([{'path': str(pdf), 'project_profile': 'sxi_cusp'}]))
        batch_dir, store = self.root / 'batch', self.root / 'store'
        intake.prepare([], inputs, batch_dir, self.db, storage_root=store,
                       model_settings={'schema': 'literature-model-settings/1',
                                       'analysis': {'model': 'gpt-6.1-sol', 'reasoning_effort': 'medium'},
                                       'review': {'model': 'gpt-6.1-sol', 'reasoning_effort': 'high'}})
        batch = intake.read_json(batch_dir / 'batch.json')
        self.assertEqual(batch['jobs'][0]['state'], 'prepared', batch['jobs'][0])
        directory = Path(batch['jobs'][0]['job_dir'])
        intake.record_dispatch(directory, 'analysis', {'task_id': 'fixture-producer', 'host_tool': 'synthetic-fixture'})
        intake.record_dispatch(directory, 'review', {'task_id': 'fixture-reviewer', 'host_tool': 'synthetic-fixture'})
        descriptor = intake.read_json(directory / 'source.json')
        extraction = intake.read_json(descriptor['extraction_manifest_path'])
        original = Path(extraction['normal_text_path']).read_text().split('\f')[0].strip()
        items = [{'id': 'I00' + str(i), 'kind': 'claim', 'text_zh': statement,
                  'text': statement, 'epistemic_status': 'simulation_result',
                  'evidence': {'pdf_page_indices': [1], 'section': 'Synthetic fixture', 'quote': original},
                  'usable_for': ['integration acceptance only'], 'cannot_support': 'No observational confirmation.'}
                 for i, statement in enumerate(('The synthetic model speed is 300 km/s.',
                                                'A pending assertion.', 'A rejected assertion.'), 1)]
        analysis = {'schema': 'fast-pdf-analysis/1', 'source_pdf_sha256': descriptor['pdf_sha256'],
                    'producer': {'task_id': 'fixture-producer', 'model': 'gpt-6.1-sol', 'reasoning_effort': 'medium'},
                    'paper': {'title': 'Synthetic model speed integration fixture', 'authors': ['Test'],
                              'year': '2026', 'doi': '', 'paper_type': 'simulation'},
                    'coverage': {'pages_read': [1], 'sections': [{'id': 'S1', 'pages': [1], 'status': 'covered'}],
                                 'figures_tables': [], 'equations': []}, 'items': items}
        intake.write_once(directory / 'analysis.json', analysis)
        decisions = []
        for item, state, support in zip(items, ['verified', 'needs_review', 'quarantined'],
                                        ['supported', 'unresolved', 'unsupported']):
            decisions.append({'item_id': item['id'], 'state': state, 'support': support,
                              'checks': {key: state == 'verified' for key in intake.CHECKS},
                              'check_details': {key: 'Synthetic integration fixture decision.' for key in intake.CHECKS},
                              'rationale_zh': '仅测试通过项筛选及隔离边界，不是真实科学复核。',
                              'source_pages': [1], 'source_passage': 'PDF PAGE 1\n' + original})
        review = {'schema': 'fast-pdf-review/1', 'source_pdf_sha256': descriptor['pdf_sha256'],
                  'analysis_sha256': intake.sha(directory / 'analysis.json'),
                  'reviewer': {'task_id': 'fixture-reviewer', 'model': 'gpt-6.1-sol', 'reasoning_effort': 'high'},
                  'independent_inventory': {'pages_read': [1], 'sections': ['Synthetic fixture'],
                                            'figures_tables': [], 'core_results': ['Model speed 300 km/s']},
                  'decisions': decisions, 'gaps': [], 'coverage_status': 'partial'}
        intake.write_once(directory / 'review.json', review)
        model_path = intake.materialize_job(directory, store)
        expected_source, expected_chunks = intake.validate_final_pack(model_path)
        self.assertEqual(len(expected_chunks), 1)
        receipt = intake.publish(batch_dir, self.db, store, confirmed=True)
        self.assertEqual(receipt['status'], 'pass')
        self.assertEqual(receipt['new_chunks'], 1)
        self.config['fast_pdf_model_globs'] = [str(store / 'published/*/*/02_machine_evidence_pack.json')]
        # Real adapter, only unrelated canonical/Zotero discovery is isolated.
        with patch('knowledge_system_core.discover_paper_sources', return_value=([], [], [])), \
                patch('knowledge_system_core.discover_additional_sources', return_value=([], [], [])):
            other_config = {**self.config, 'database_path': str(self.root / 'other.sqlite3')}
            preview = sync_database(other_config, db_override=self.db, dry_run=True)
            self.assertEqual(preview['unchanged_count'], 1)
            self.assertEqual(preview['source_count'], 1)
            sync_database(self.config)
            model = intake.read_json(model_path)
            record = intake.projected_quality(expected_source, expected_chunks[0], analysis, review, extraction, model)
            with sqlite3.connect(self.db) as connection:
                write_record(connection, 'chunk_quality', expected_chunks[0]['chunk_id'], record)
            self.assertTrue(get_chunk(self.db, expected_chunks[0]['chunk_id'])['supports_claim'])
            self.assertIsNone(get_chunk(self.db, 'pdf:' + descriptor['pdf_sha256'] + ':I002'))
            self.assertIsNone(get_chunk(self.db, 'pdf:' + descriptor['pdf_sha256'] + ':I003'))
            sync_database(self.config)
            self.assertTrue(get_chunk(self.db, expected_chunks[0]['chunk_id'])['supports_claim'])
        report = audit_database(self.db)
        self.assertEqual(report['status'], 'pass', report['findings'])
        self.assertEqual(report['coverage']['paper_quotes_match_cached_pdf_bound_extraction'], 1)
        self.assertEqual(report['coverage']['fast_pdf_final_packs_validated'], 1)
        # Mutating a frozen original analysis cannot survive discovery even if
        # the final model and RAG still have their former file hashes.
        analysis['items'][0]['text_zh'] = 'Changed after review.'
        (directory / 'analysis.json').write_text(json.dumps(analysis))
        with self.assertRaisesRegex(ValueError, 'analysis changed after review'):
            intake.validate_final_pack(model_path)


if __name__ == '__main__':
    unittest.main()
