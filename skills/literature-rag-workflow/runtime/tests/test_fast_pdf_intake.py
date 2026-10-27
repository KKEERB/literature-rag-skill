"""Isolated engineering fixtures; no claim of actual model/human science review."""
from __future__ import annotations
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fast_pdf_intake as fast
from knowledge_system_core import initialize_database
from knowledge_quality import initialize_quality

SETTINGS = {'schema': 'literature-model-settings/1',
            'analysis': {'model': 'gpt-6.1-sol', 'reasoning_effort': 'medium'},
            'review': {'model': 'gpt-6.1-sol', 'reasoning_effort': 'high'}}


def synthetic_dispatch(job_dir, analysis_id='synthetic-producer', review_id='synthetic-reviewer'):
    """Synthetic metadata tests bindings only; it is not a real host receipt."""
    for stage, task_id in (('analysis', analysis_id), ('review', review_id)):
        fast.record_dispatch(job_dir, stage, {'task_id': task_id, 'host_tool': 'synthetic-fixture'})


def fixture(directory, settings=SETTINGS, dispatch=True):
    from reportlab.pdfgen import canvas
    directory = Path(directory)
    pdf, database = directory / 'source.pdf', directory / 'knowledge.sqlite3'
    quote = 'The simulation speed is 20 km/s under condition A. This is not an observation.'
    document = canvas.Canvas(str(pdf), pagesize=(595, 842))
    document.setFont('Helvetica', 12)
    for index, line in enumerate(('SYNTHETIC ENGINEERING FIXTURE', quote,
                                  'The counterexample uses condition B and does not establish mechanism identity.')):
        document.drawString(30, 812 - index * 16, line)
    document.save()
    with sqlite3.connect(database) as con:
        initialize_database(con); initialize_quality(con)
    manifest = directory / 'inputs.json'
    manifest.write_text(json.dumps([{'path': str(pdf), 'project_profile': 'magnetosphere_ionosphere'}]))
    batch_dir, storage = directory / 'batch', directory / 'storage'
    result = fast.prepare([], manifest, batch_dir, database, storage, model_settings=settings)
    assert result['counts'] == {'awaiting_analysis': 1}, result
    job_dir = Path(result['jobs'][0]['job_dir'])
    source = fast.read_json(job_dir / 'source.json')
    if dispatch:
        synthetic_dispatch(job_dir)
    analysis = {'schema': 'fast-pdf-analysis/1', 'source_pdf_sha256': source['pdf_sha256'],
        'producer': {'task_id': 'synthetic-producer', **settings['analysis']},
        'paper': {'title': 'Synthetic engineering fixture', 'authors': 'Test', 'year': '2026', 'doi': '', 'paper_type': 'synthetic_fixture'},
        'coverage': {'pages_read': [1], 'sections': [{'id': 'all', 'pages': [1], 'status': 'read'}], 'figures_tables': [], 'equations': []},
        'items': [{'id': 'c1', 'kind': 'claim', 'text_zh': '在条件 A 下，模拟速度为 20 km/s；这不是观测。', 'epistemic_status': 'simulation_result', 'evidence': {'pdf_page_indices': [1], 'quote': quote}, 'usable_for': ['fixture'], 'cannot_support': '不能支持观测确认。'},
                  {'id': 'c2', 'kind': 'limitation', 'text_zh': '模拟结果不能确定机制身份。', 'epistemic_status': 'simulation_result', 'evidence': {'pdf_page_indices': [1], 'quote': 'The counterexample uses condition B and does not establish mechanism identity.'}, 'usable_for': ['fixture'], 'cannot_support': '不能作为机制确认。'}]}
    fast.write_once(job_dir / 'analysis.json', analysis)
    review = {'schema': 'fast-pdf-review/1', 'source_pdf_sha256': source['pdf_sha256'], 'analysis_sha256': fast.sha(job_dir / 'analysis.json'),
        'reviewer': {'task_id': 'synthetic-reviewer', **settings['review']},
        'independent_inventory': {'pages_read': [1], 'sections': ['Whole fixture'], 'figures_tables': [], 'core_results': ['Conditional simulation only']},
        'coverage_status': 'partial', 'gaps': ['Synthetic unresolved counterexample'], 'visual_inspections': [], 'decisions': []}
    for item in analysis['items']:
        passed = item['id'] == 'c1'
        review['decisions'].append({'item_id': item['id'], 'state': 'verified' if passed else 'needs_review', 'checks': {k: passed for k in fast.CHECKS}, 'check_details': {k: 'Synthetic fixture explanation: ' + k for k in fast.CHECKS}, 'rationale_zh': '仅测试原文绑定与筛选。', 'source_pages': [1], 'source_passage': 'PDF PAGE 1\n' + item['evidence']['quote'], 'support': 'supported' if passed else 'unresolved'})
    fast.write_once(job_dir / 'review.json', review)
    return {'database': database, 'batch_dir': batch_dir, 'job_dir': job_dir, 'source': source, 'analysis': analysis, 'review': review, 'storage': storage, 'manifest': manifest}


class FastIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.f = fixture(self.root)

    def mutate_review(self, change):
        path = self.f['job_dir'] / 'review.json'; value = fast.read_json(path); change(value); path.write_text(json.dumps(value))

    def test_source_copy_and_cache_reuse(self):
        s = self.f['source']; self.assertEqual(fast.sha(s['source_pdf_path']), s['pdf_sha256'])
        self.assertTrue(fast.extract_object(self.root / 'source.pdf', self.f['storage'], 40)[2])

    def test_render_selected_page_with_pdfium_and_reuse(self):
        from PIL import Image
        source = self.f['source']
        result = fast.render_pdf_pages(source, [1, 1])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['page'], 1)
        self.assertEqual(result[0]['sha256'], fast.sha(result[0]['path']))
        self.assertEqual(Path(result[0]['path']).read_bytes()[:8], b'\x89PNG\r\n\x1a\n')
        # This fixture has text only. A renderer that silently drops glyphs can
        # still return a valid all-white PNG, so check the text area has pixels.
        with Image.open(result[0]['path']) as image:
            top = image.convert('L').crop((0, 0, image.width, image.height // 5))
            self.assertGreater(sum(top.histogram()[:235]), 150)
        self.assertEqual(result, fast.render_pdf_pages(source, [1]))
        with self.assertRaisesRegex(ValueError, 'invalid render page'):
            fast.render_pdf_pages(source, [2])

    def test_render_fails_closed_on_missing_pdfium_or_source_change(self):
        source = self.f['source']
        target = Path(source['extraction_manifest_path']).parent / 'rendered' / 'page-0001.png'
        with patch.dict(sys.modules, {'pypdfium2': None}):
            with self.assertRaisesRegex(RuntimeError, 'requires pypdfium2'):
                fast.render_pdf_pages(source, [1])
        self.assertFalse(target.exists())
        actual_render = fast._render_page_to_png
        def change_source(*args, **kwargs):
            result = actual_render(*args, **kwargs)
            Path(source['source_pdf_path']).write_bytes(b'changed during render')
            return result
        with patch.object(fast, '_render_page_to_png', side_effect=change_source):
            with self.assertRaisesRegex(ValueError, 'retained PDF binding changed'):
                fast.render_pdf_pages(source, [1])
        self.assertFalse(target.exists())

    def test_preview_does_not_modify_database_or_discover_uncommitted(self):
        before = fast.sha(self.f['database']); self.assertEqual(fast.publish(self.f['batch_dir'])['status'], 'preview'); self.assertEqual(fast.sha(self.f['database']), before)
        sources, chunks, warnings = fast.discover_fast_pdf_sources({'fast_pdf_model_globs': [str(self.f['storage'] / 'published/**/02_machine_evidence_pack.json')]})
        self.assertFalse(sources); self.assertFalse(chunks); self.assertTrue(warnings)

    def test_publish_verified_subset_only_and_noop(self):
        self.assertEqual(fast.publish(self.f['batch_dir'], confirmed=True)['status'], 'pass')
        with sqlite3.connect(self.f['database']) as con:
            self.assertEqual(con.execute('SELECT item_id FROM chunks').fetchall(), [('c1',)])
            self.assertEqual(con.execute('SELECT zotero_item_key,status FROM sources').fetchall(), [('', 'draft')])
        self.assertEqual(fast.publish(self.f['batch_dir'], confirmed=True)['status'], 'noop')

    def test_publish_initializes_absent_quality_tables_atomically(self):
        with sqlite3.connect(self.f['database']) as con:
            for table in ('source_quality', 'chunk_quality', 'quality_history'):
                con.execute(f'DROP TABLE {table}')
        self.assertEqual(fast.publish(self.f['batch_dir'], confirmed=True)['status'], 'pass')
        with sqlite3.connect(self.f['database']) as con:
            self.assertEqual(con.execute('SELECT item_id FROM chunks').fetchall(), [('c1',)])
            self.assertEqual(con.execute('SELECT count(*) FROM chunk_quality').fetchone()[0], 1)
            self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_unrelated_quality_changes_roll_back_even_if_tables_were_absent(self):
        import knowledge_quality
        original = knowledge_quality.write_record
        with sqlite3.connect(self.f['database']) as con:
            for table in ('source_quality', 'chunk_quality', 'quality_history'):
                con.execute(f'DROP TABLE {table}')
        def inject(con, table, key, record):
            original(con, table, key, record)
            if table == 'chunk_quality':
                con.execute("INSERT INTO chunk_quality VALUES (?,?)", ('unrelated', '{}'))
        with patch.object(knowledge_quality, 'write_record', side_effect=inject):
            with self.assertRaisesRegex(ValueError, 'unrelated database rows changed'):
                fast.publish(self.f['batch_dir'], confirmed=True)
        with sqlite3.connect(self.f['database']) as con:
            self.assertEqual(con.execute('SELECT count(*) FROM sources').fetchone()[0], 0)
            self.assertIsNone(con.execute("SELECT name FROM sqlite_master WHERE name='chunk_quality'").fetchone())

    def test_existing_unrelated_quality_mutation_is_rejected_and_rolled_back(self):
        import knowledge_quality
        original = knowledge_quality.write_record
        with sqlite3.connect(self.f['database']) as con:
            con.execute("INSERT INTO chunk_quality VALUES (?,?)", ('unrelated', '{\"before\":true}'))
        def inject(con, table, key, record):
            original(con, table, key, record)
            if table == 'chunk_quality':
                con.execute("UPDATE chunk_quality SET record_json=? WHERE chunk_id='unrelated'", ('{}',))
        with patch.object(knowledge_quality, 'write_record', side_effect=inject):
            with self.assertRaisesRegex(ValueError, 'unrelated database rows changed'):
                fast.publish(self.f['batch_dir'], confirmed=True)
        with sqlite3.connect(self.f['database']) as con:
            self.assertEqual(con.execute('SELECT count(*) FROM sources').fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT record_json FROM chunk_quality WHERE chunk_id='unrelated'").fetchone()[0], '{\"before\":true}')

    def test_analysis_hash_change_blocks(self):
        p = self.f['job_dir'] / 'analysis.json'; p.write_text(p.read_text() + ' ')
        with self.assertRaisesRegex(ValueError, 'analysis changed'): fast.publish(self.f['batch_dir'], confirmed=True)

    def test_explicit_mixed_route_real_publish_and_invalid_route_block(self):
        path = self.f['job_dir'] / 'analysis.json'
        analysis = fast.read_json(path)
        evidence = analysis['items'][0]['evidence']
        evidence.update(text_route='mixed', page_text_routes={'1': 'layout'},
                        quote='PDF PAGE 1\n' + evidence['quote'])
        path.write_text(json.dumps(analysis))
        self.mutate_review(lambda r: r.update(analysis_sha256=fast.sha(path)))
        evidence['page_text_routes'] = {'1': 'normal+layout'}
        path.write_text(json.dumps(analysis))
        self.mutate_review(lambda r: r.update(analysis_sha256=fast.sha(path)))
        with self.assertRaisesRegex(ValueError, 'one reading order per cited page'):
            fast.publish(self.f['batch_dir'], confirmed=True)
        with sqlite3.connect(self.f['database']) as con:
            self.assertEqual(con.execute('SELECT count(*) FROM chunks').fetchone()[0], 0)
        evidence['page_text_routes'] = {'1': 'layout'}
        path.write_text(json.dumps(analysis))
        self.mutate_review(lambda r: r.update(analysis_sha256=fast.sha(path)))
        self.assertEqual(fast.publish(self.f['batch_dir'], confirmed=True)['new_chunks'], 1)
        self.assertEqual(fast.publish(self.f['batch_dir'], confirmed=True)['status'], 'noop')

    def test_real_pdf_mixed_review_excerpts_keep_fresh_route_quality_bindings(self):
        import copy
        from reportlab.pdfgen import canvas
        from knowledge_quality import initialize_quality
        directory=self.root/'route-fixture';directory.mkdir()
        pdf=directory/'columns.pdf'
        document=canvas.Canvas(str(pdf), pagesize=(595,842));document.setFont('Helvetica',12)
        document.drawString(30,812,'FIRSTLEFT alpha');document.drawString(30,796,'SECONDLEFT beta')
        document.drawString(310,812,'FIRSTRIGHT gamma');document.drawString(310,796,'SECONDRIGHT delta')
        document.drawString(30,642,'SYNTHETIC ENGINEERING FIXTURE. These independent source excerpts test reading')
        document.drawString(30,626,'order bindings, not actual model or human scientific review.')
        document.save()
        db=directory/'knowledge.sqlite3'
        with sqlite3.connect(db) as con:initialize_database(con);initialize_quality(con)
        manifest=directory/'inputs.json';manifest.write_text(json.dumps([{'path':str(pdf),'project_profile':'magnetosphere_ionosphere'}]))
        batch=directory/'batch';prepared=fast.prepare([],manifest,batch,db,directory/'storage',model_settings=SETTINGS)
        job=Path(prepared['jobs'][0]['job_dir']);source=fast.read_json(job/'source.json')
        synthetic_dispatch(job)
        extraction=fast.read_json(source['extraction_manifest_path'])
        normal=Path(extraction['normal_text_path']).read_text();layout=Path(extraction['layout_text_path']).read_text()
        q_normal='FIRSTLEFT alpha\nSECONDLEFT beta';q_layout='FIRSTLEFT alpha FIRSTRIGHT gamma'
        squash=lambda value:''.join(value.split())
        self.assertIn(squash(q_normal),squash(normal));self.assertNotIn(squash(q_normal),squash(layout))
        self.assertIn(squash(q_layout),squash(layout));self.assertNotIn(squash(q_layout),squash(normal))
        analysis=copy.deepcopy(self.f['analysis']);analysis['source_pdf_sha256']=source['pdf_sha256'];analysis['items']=analysis['items'][:1]
        analysis['items'][0].update(text_zh='仅用于工程验收的两列独立原文片段；不表示科学接受。',evidence={'pdf_page_indices':[1],'quote':q_normal})
        fast.write_once(job/'analysis.json',analysis)
        review=copy.deepcopy(self.f['review']);review['source_pdf_sha256']=source['pdf_sha256'];review['analysis_sha256']=fast.sha(job/'analysis.json');review['decisions']=review['decisions'][:1]
        review['decisions'][0]['source_passage']='PDF PAGE 1\n'+q_normal+'\nPDF PAGE 1\n'+q_layout
        fast.write_once(job/'review.json',review)
        receipt=fast.publish(batch,confirmed=True)
        self.assertEqual(receipt['new_chunks'],1)
        with sqlite3.connect(db) as con:
            record=json.loads(con.execute('SELECT record_json FROM chunk_quality').fetchone()[0])
        self.assertEqual(record['review']['source_passage_routes'],['normal','layout'])
        self.assertEqual(record['review']['fresh_text_sha256_by_route'],extraction['full_text_sha256'])
        self.assertEqual(record['review']['source_passage'],review['decisions'][0]['source_passage'])
        self.assertEqual(fast.publish(batch,confirmed=True)['status'],'noop')

    def test_status_reports_interrupted_json_without_destroying_staging(self):
        path = self.f['job_dir'] / 'review.json'
        path.write_text('{"schema":')
        status = fast.batch_status(self.f['batch_dir'])
        self.assertEqual(status['counts'], {'interrupted_staging': 1})
        self.assertTrue(status['jobs'][0]['staging_errors'])
        self.assertEqual(path.read_text(), '{"schema":')
        with sqlite3.connect(self.f['database']) as con:
            self.assertEqual(con.execute('SELECT count(*) FROM chunks').fetchone()[0], 0)

    def test_original_change_blocks(self):
        p = self.root / 'source.pdf'; p.write_bytes(p.read_bytes() + b'changed')
        with self.assertRaisesRegex(ValueError, 'original input PDF changed'): fast.publish(self.f['batch_dir'], confirmed=True)

    def test_pending_invisible(self):
        (self.f['job_dir'] / 'review.json').unlink()
        self.assertEqual(fast.publish(self.f['batch_dir'], confirmed=True)['status'], 'nothing_publishable')
        with sqlite3.connect(self.f['database']) as con: self.assertEqual(con.execute('SELECT count(*) FROM chunks').fetchone()[0], 0)

    def test_rejected_bad_first_round_quote_does_not_block_verified_subset(self):
        path = self.f['job_dir'] / 'analysis.json'
        analysis = fast.read_json(path)
        analysis['items'][1]['evidence']['quote'] = 'A noncontiguous or unsupported producer excerpt.'
        path.write_text(json.dumps(analysis))
        self.mutate_review(lambda r: r.update(analysis_sha256=fast.sha(path)))
        receipt = fast.publish(self.f['batch_dir'], confirmed=True)
        self.assertEqual(receipt['new_chunks'], 1)
        with sqlite3.connect(self.f['database']) as con:
            self.assertEqual(con.execute('SELECT item_id FROM chunks').fetchall(), [('c1',)])
            self.assertFalse(con.execute("SELECT 1 FROM chunks_fts WHERE chunk_id LIKE '%:c2'").fetchone())
        model = fast.read_json(Path(receipt['admitted_sources'][0]['model_path']))
        self.assertEqual([item['id'] for item in model['items']], ['c1'])
        self.assertEqual(fast.read_json(path)['items'][1]['evidence']['quote'], 'A noncontiguous or unsupported producer excerpt.')

    def test_verified_bad_first_round_quote_still_blocks(self):
        path = self.f['job_dir'] / 'analysis.json'
        analysis = fast.read_json(path)
        analysis['items'][0]['evidence']['quote'] = 'A noncontiguous or unsupported producer excerpt.'
        path.write_text(json.dumps(analysis))
        self.mutate_review(lambda r: r.update(analysis_sha256=fast.sha(path)))
        with self.assertRaisesRegex(ValueError, 'excerpt absent'):
            fast.publish(self.f['batch_dir'], confirmed=True)

    def test_rejected_bad_quote_cannot_hide_source_drift(self):
        path = self.f['job_dir'] / 'analysis.json'
        analysis = fast.read_json(path)
        analysis['items'][1]['evidence']['quote'] = 'Unsupported producer excerpt.'
        path.write_text(json.dumps(analysis))
        self.mutate_review(lambda r: r.update(analysis_sha256=fast.sha(path)))
        (self.root / 'source.pdf').write_bytes((self.root / 'source.pdf').read_bytes() + b'changed')
        with self.assertRaisesRegex(ValueError, 'original input PDF changed'):
            fast.publish(self.f['batch_dir'], confirmed=True)

    def test_fully_rejected_paper_does_not_block_other_paper(self):
        other_root = self.root / 'other'
        other_root.mkdir()
        other = fixture(other_root)
        manifest = self.root / 'two-paper.json'
        manifest.write_text(json.dumps([{'path': str(self.root / 'source.pdf'), 'project_profile': 'magnetosphere_ionosphere'},
                                        {'path': str(other_root / 'source.pdf'), 'project_profile': 'magnetosphere_ionosphere'}]))
        batch_dir = self.root / 'two-paper-batch'
        status = fast.prepare([], manifest, batch_dir, self.f['database'], self.f['storage'], model_settings=SETTINGS)
        self.assertEqual(status['counts'], {'awaiting_analysis': 2})
        for job, existing, reject in zip(status['jobs'], (self.f, other), (False, True)):
            directory = Path(job['job_dir'])
            synthetic_dispatch(directory)
            analysis = fast.read_json(existing['job_dir'] / 'analysis.json')
            review = fast.read_json(existing['job_dir'] / 'review.json')
            fast.write_once(directory / 'analysis.json', analysis)
            review['analysis_sha256'] = fast.sha(directory / 'analysis.json')
            if reject:
                for decision in review['decisions']:
                    decision.update(state='quarantined', support='unsupported',
                                    checks={key: False for key in fast.CHECKS})
            fast.write_once(directory / 'review.json', review)
        receipt = fast.publish(batch_dir, confirmed=True)
        self.assertEqual(receipt['new_sources'], 1)
        self.assertEqual(receipt['new_chunks'], 1)
        self.assertEqual(receipt['blocked_or_reused'][0]['reason'], 'no_verified_items')

    def test_self_review_blocks(self):
        self.mutate_review(lambda r: r['reviewer'].update(task_id='synthetic-producer'))
        with self.assertRaisesRegex(ValueError, 'independent'): fast.publish(self.f['batch_dir'])

    def test_whole_read_required(self):
        self.mutate_review(lambda r: r['independent_inventory'].update(pages_read=[]))
        with self.assertRaisesRegex(ValueError, 'whole original PDF'): fast.publish(self.f['batch_dir'])

    def test_wrong_locator_blocks(self):
        self.mutate_review(lambda r: r['decisions'][0].update(source_passage='PDF PAGE 1\nNot in original.'))
        with self.assertRaisesRegex(ValueError, 'absent'): fast.publish(self.f['batch_dir'])

    def test_six_checks_required(self):
        self.mutate_review(lambda r: r['decisions'][0]['checks'].update(numbers_units=False))
        with self.assertRaisesRegex(ValueError, 'six'): fast.publish(self.f['batch_dir'])

    def test_derivative_cannot_rewrite_semantics(self):
        p = fast.materialize_job(self.f['job_dir'], self.f['storage']); m = fast.read_json(p)
        self.assertEqual(m['items'][0]['text_zh'], self.f['analysis']['items'][0]['text_zh'])
        m['items'][0]['text_zh'] = 'Changed'; p.write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError, 'semantics'): fast.validate_final_pack(p)

    def test_rollback_no_half_source_and_one_recovery(self):
        import knowledge_quality
        original = knowledge_quality.write_record
        def fail(con, table, key, record):
            original(con, table, key, record)
            if table == 'chunk_quality': raise RuntimeError('injected failure')
        with patch.object(knowledge_quality, 'write_record', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'injected'): fast.publish(self.f['batch_dir'], confirmed=True)
        with sqlite3.connect(self.f['database']) as con:
            self.assertEqual(con.execute('SELECT count(*) FROM sources').fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT count(*) FROM chunks_fts').fetchone()[0], 0)
        self.assertEqual(len(list(self.f['batch_dir'].glob('receipts/*.recovery.json'))), 1)
        self.assertFalse(list(self.f['storage'].glob('published/**/publication-*.json')))

    def test_scan_and_page_limit_fail_closed(self):
        from reportlab.pdfgen import canvas
        p = self.root / 'blank.pdf'
        d = canvas.Canvas(str(p)); d.showPage(); d.save()
        with self.assertRaisesRegex(ValueError, 'insufficient'): fast.extract_object(p, self.root / 'blank-store', 40)
        with self.assertRaisesRegex(ValueError, 'page_limit'): fast.extract_object(self.root / 'source.pdf', self.f['storage'], 0)

    def test_replaced_pdf_between_initial_hash_and_extraction_never_creates_task(self):
        other_root = self.root / 'replacement'
        other_root.mkdir()
        fixture(other_root)
        original = self.root / 'source.pdf'
        initial_sha = fast.sha(original)
        self.assertNotEqual(initial_sha, fast.sha(other_root / 'source.pdf'))
        actual_extract = fast.extract_object
        def replace_before_copy(pdf, storage, max_pages):
            shutil.copyfile(other_root / 'source.pdf', pdf)
            return actual_extract(pdf, storage, max_pages)
        batch = self.root / 'swapped-batch'
        with patch.object(fast, 'extract_object', side_effect=replace_before_copy):
            status = fast.prepare([original], None, batch, self.f['database'], self.f['storage'],
                                  project_profile='magnetosphere_ionosphere', model_settings=SETTINGS)
        self.assertEqual(status['counts'], {'blocked': 1})
        self.assertIn('changed between initial hash and extraction', status['jobs'][0]['reason'])
        self.assertFalse(list((batch / 'jobs').glob('**/*task.json')))
        self.assertFalse(list((batch / 'jobs').glob('**/source.json')))

    def test_unavailable_preflight_cached_without_timestamp_overwrite(self):
        failure = {'verdict': 'UNAVAILABLE', 'sha256': self.f['source']['pdf_sha256'],
                   'generated_at': 'frozen-test-time', 'warnings': ['synthetic integrity failure']}
        with patch.object(fast, 'preflight', return_value=failure) as run:
            for unused in range(2):
                with self.assertRaisesRegex(ValueError, 'synthetic integrity failure'):
                    fast.extract_object(self.root / 'source.pdf', self.root / 'failure-store', 40)
            self.assertEqual(run.call_count, 1)

    def test_noop_recovers_missing_publication_marker(self):
        fast.publish(self.f['batch_dir'], confirmed=True)
        marker = next(self.f['storage'].glob('published/**/publication-*.json'))
        marker.unlink()
        self.assertEqual(fast.publish(self.f['batch_dir'], confirmed=True)['status'], 'noop')
        self.assertTrue(marker.exists())

    def test_publication_rejects_other_targets_and_accepts_same_normalized_path(self):
        isolated = self.root / 'isolated.sqlite3'
        with sqlite3.connect(self.f['database']) as source, sqlite3.connect(isolated) as dest:
            source.backup(dest)
        with self.assertRaisesRegex(ValueError, 'database override differs'):
            fast.publish(self.f['batch_dir'], database=isolated, confirmed=True)
        with self.assertRaisesRegex(ValueError, 'storage root override differs'):
            fast.publish(self.f['batch_dir'], storage_root=self.root / 'another-storage', confirmed=True)
        with sqlite3.connect(isolated) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM sources').fetchone()[0], 0)
        same_database = self.root / '.' / 'knowledge.sqlite3'
        same_storage = self.root / '.' / 'storage'
        self.assertEqual(fast.publish(self.f['batch_dir'], database=same_database,
                                      storage_root=same_storage, confirmed=True)['status'], 'pass')
        pattern = str(self.f['storage'] / 'published/**/02_machine_evidence_pack.json')
        live = {'database_path': str(self.f['database']), 'fast_pdf_model_globs': [pattern]}
        iso = {'database_path': str(isolated), 'fast_pdf_model_globs': [pattern]}
        self.assertEqual(fast.discover_fast_pdf_sources(iso)[0], [])
        self.assertEqual(len(fast.discover_fast_pdf_sources(live)[0]), 1)
        self.assertEqual(len(list(self.f['storage'].glob('published/**/publication-*.json'))), 1)

    def test_direct_pdf_and_directory_explicit_profile(self):
        first = fast.prepare([self.root / 'source.pdf'], None, self.root / 'direct', self.f['database'], self.f['storage'], project_profile='sxi_cusp', model_settings=SETTINGS)
        self.assertEqual(first['counts'], {'awaiting_analysis': 1})
        source = fast.read_json(Path(first['jobs'][0]['job_dir']) / 'source.json')
        self.assertEqual(source['project_profile'], 'sxi_cusp')
        folder = self.root / 'pdfs'; folder.mkdir()
        (folder / 'SOURCE.PDF').write_bytes((self.root / 'source.pdf').read_bytes())
        second = fast.prepare([folder], None, self.root / 'directory', self.f['database'], self.f['storage'], project_profile='sxi_cusp', model_settings=SETTINGS)
        self.assertEqual(second['counts'], {'awaiting_analysis': 1})
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            fast.prepare([], self.f['manifest'], self.root / 'conflict', self.f['database'], self.f['storage'], project_profile='other', model_settings=SETTINGS)

    def test_new_batch_rejects_unavailable_or_unsafe_profile_before_extraction(self):
        before = fast.sha(self.f['database'])
        for profile in ('unavailable', '../sxi_cusp', 'content_quality_v21'):
            with self.subTest(profile=profile):
                output = self.root / ('blocked-' + profile.replace('/', '_'))
                with patch.object(fast, 'extract_object') as extract:
                    with self.assertRaises(ValueError):
                        fast.prepare([self.root / 'source.pdf'], None, output,
                                     self.f['database'], self.f['storage'],
                                     project_profile=profile, model_settings=SETTINGS)
                    extract.assert_not_called()
                self.assertFalse(output.exists())
                self.assertEqual(fast.sha(self.f['database']), before)

    def test_profile_contract_requires_matching_identity_and_no_symlink(self):
        profile_dir = self.root / 'profiles'; profile_dir.mkdir()
        original = fast.ROOT / 'profiles' / 'sxi_cusp.json'
        renamed = profile_dir / 'renamed.json'
        renamed.write_bytes(original.read_bytes())
        with patch.object(fast, 'ROOT', self.root):
            with self.assertRaisesRegex(ValueError, 'identity'):
                fast.validate_project_profile('renamed')
            renamed.unlink()
            try:
                renamed.symlink_to(original)
            except OSError:
                self.skipTest('creating symlinks is unavailable on this host')
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                fast.validate_project_profile('renamed')


if __name__ == '__main__': unittest.main()
