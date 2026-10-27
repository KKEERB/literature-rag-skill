"""Isolated real-PDF/SQLite regression tests. Human and semantic reviews are synthetic."""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
import fast_pdf_intake as fast
import reading_sessions as sessions
import review_checklists as lists
import literature_cross_validation as audit
from knowledge_system_core import get_chunk, search_database
from test_fast_pdf_intake import fixture


class TenItemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.f = fixture(self.root)
        analysis, review = self.f['analysis'], self.f['review']
        # Three admitted, distinct assertions plus an exact duplicate.
        for n, kind, text in [(3, 'method', '方法仅用于条件 A 下的模拟，速度为 20 km/s。'),
                              (4, 'limitation', '条件 A 下的速度为模拟所得，不应当称为观测。'),
                              (5, 'claim', analysis['items'][0]['text_zh'])]:
            item = copy.deepcopy(analysis['items'][0]); item.update(id='c' + str(n), kind=kind, text_zh=text)
            analysis['items'].append(item)
            decision = copy.deepcopy(review['decisions'][0]); decision['item_id'] = item['id']; review['decisions'].append(decision)
        (self.f['job_dir'] / 'analysis.json').write_text(json.dumps(analysis))
        review['analysis_sha256'] = fast.sha(self.f['job_dir'] / 'analysis.json')
        (self.f['job_dir'] / 'review.json').write_text(json.dumps(review))
        self.publication = fast.publish(self.f['batch_dir'], confirmed=True)
        self.db = self.f['database']
        with sessions.connect(self.db) as con: self.sid = con.execute('SELECT source_id FROM sources').fetchone()[0]
        self.folder = self.root / 'human-review'; lists.generate(self.db, self.sid, self.folder)

    def payload(self, text='1 接受；2 不接受：缺少适用条件。', ref='synthetic:human-message'):
        presentation = lists.present(self.folder, 'synthetic:actual-display')
        return {'checklist_id': presentation['checklist_id'], 'presentation_event_id': presentation['event_id'],
                'actor': 'user', 'user_message_ref': ref, 'human_statement': text}

    def test_immutable_short_list_dedup_no_quiz_and_collapsed_details(self):
        before = fast.sha(self.db)
        value = lists.view(self.folder)
        self.assertEqual(len(value['items']), 3)
        frozen = (self.folder / 'checklist.json').read_bytes()
        self.assertEqual(sessions.next_turn(self.folder)['state'], 'ready_for_checklist_review')
        self.assertEqual((self.folder / 'checklist.json').read_bytes(), frozen)
        self.assertEqual(fast.sha(self.db), before)
        self.assertEqual(sessions.status(self.folder)['answers'], 0)
        html = Path(lists.render(self.folder)).read_text()
        self.assertEqual(html.count('<article '), 3)
        self.assertNotIn('<details open', html)
        self.assertNotIn('先解释文章', html)

    def test_batch_partial_optional_comment_retry_and_qualified_answer(self):
        payload = self.payload()
        result = lists.submit(self.folder, payload, confirmed=True)
        self.assertEqual(result['commit']['new_records'], 2)
        keys = [i['chunk_id'] for i in result['items']]
        self.assertTrue(get_chunk(self.db, keys[0])['item_human_accepted'])
        disputed = get_chunk(self.db, keys[1])
        self.assertTrue(disputed['item_human_reviewed']); self.assertFalse(disputed['item_human_accepted'])
        self.assertTrue(disputed['citation_allowed']); self.assertFalse(disputed['supports_claim'])
        self.assertEqual(disputed['citation_mode'], 'with_human_dissent')
        self.assertIn('缺少适用条件', disputed['answer_text'])
        response = search_database(self.db, disputed['text'], top_k=10)
        hit = next(r for r in response['results'] if r['chunk_id'] == keys[1])
        self.assertIn('缺少适用条件', hit['citation']['qualification'])
        self.assertFalse(get_chunk(self.db, keys[2])['item_human_reviewed'])
        before = fast.sha(self.db); count = len(sessions.session_state(self.folder)[1])
        retry = lists.submit(self.folder, payload, confirmed=True)
        self.assertEqual(retry['event_id'], result['event_id']); self.assertEqual(retry['commit']['status'], 'noop')
        self.assertEqual(len(sessions.session_state(self.folder)[1]), count); self.assertEqual(fast.sha(self.db), before)
        later = self.payload('3 接受', 'synthetic:later-partial')
        lists.submit(self.folder, later, confirmed=True)
        self.assertTrue(get_chunk(self.db, keys[2])['item_human_accepted'])

    def test_only_retrievable_result_can_be_disputed_and_keeps_comment(self):
        value = lists.view(self.folder); target = value['items'][0]['chunk_id']
        lists.submit(self.folder, self.payload('1 不接受：模拟不能改成观测。'), confirmed=True)
        with sqlite3.connect(self.db) as con:
            con.execute('DELETE FROM chunks_fts WHERE chunk_id != ?', (target,))
            con.execute('UPDATE chunks SET tombstoned=1 WHERE chunk_id != ?', (target,))
        result = search_database(self.db, '20 km/s', top_k=10)
        self.assertEqual([r['chunk_id'] for r in result['results']], [target])
        self.assertIn('模拟不能改成观测', result['results'][0]['answer_text'])

    def test_bad_binding_conflict_and_source_damage_do_not_accept(self):
        payload = self.payload('1 接受')
        bad = dict(payload, checklist_id='stale')
        with self.assertRaisesRegex(ValueError, 'version differs'): lists.submit(self.folder, bad, confirmed=True)
        with self.assertRaises(ValueError): lists.parse_reply('1–3 接受；2 不接受')
        with self.assertRaises(ValueError): lists.parse_reply('剩下的都接受')
        with self.assertRaises(ValueError): lists.submit(self.folder, dict(payload, human_statement='10 接受'), confirmed=True)
        with sqlite3.connect(self.db) as con: con.execute("UPDATE chunks SET text_zh=text_zh || ' changed'")
        with self.assertRaisesRegex(ValueError, 'stale'): lists.submit(self.folder, payload, confirmed=True)

    def test_machine_failure_remains_blocked_after_human_rejection(self):
        lists.submit(self.folder, self.payload('1 不接受'), confirmed=True)
        key = lists.view(self.folder)['items'][0]['chunk_id']
        with sqlite3.connect(self.db) as con:
            raw = json.loads(con.execute('SELECT record_json FROM chunk_quality WHERE chunk_id=?', (key,)).fetchone()[0])
            raw['state'] = 'quarantined'
            con.execute('UPDATE chunk_quality SET record_json=? WHERE chunk_id=?', (json.dumps(raw), key))
        result = get_chunk(self.db, key)
        self.assertFalse(result['citation_allowed']); self.assertEqual(result['citation_mode'], 'blocked')

    def test_postpublication_checklist_and_stage_receipt(self):
        followup = self.publication['review_followup']
        self.assertEqual(followup['checklists'][0]['items'], 3)
        self.assertEqual(followup['checklists'][0]['status'], 'pass')
        report = sessions.read(followup['stage_validation']['report'])
        self.assertIn(report['status'], {'pass', 'pass_with_warnings'})
        self.assertEqual(report['semantic']['status'], 'not_required')
        self.assertTrue(report['scientific_inputs_unchanged'])

    def review_fixture(self, output, false_check=None):
        plan = sessions.read(output / 'plan.json')
        records = []
        for packet in plan['semantic_packets']:
            checks = {key: True for key in audit.CHECKS}
            if false_check: checks[false_check] = False
            records.append({'chunk_id': packet['chunk_id'], 'inputs': packet['inputs'],
                'page_texts_sha256': packet['page_texts_sha256'], 'checks': checks,
                'check_details': {key: 'Synthetic oracle: condition A; simulation; 20 km/s; not observation.' for key in audit.CHECKS},
                'source_pages': [1], 'source_passage': self.f['analysis']['items'][0]['evidence']['quote'],
                'rationale_zh': '仅工程夹具：该判断来自合成原文的已知答案，不代表模型语义识别率。'})
        result = {'schema': 'literature-semantic-audit/1', 'reviewer_id': 'synthetic-oracle',
                  'plan_sha256': fast.sha(output / 'plan.json'), 'items': records}
        path = output / 'semantic-review.json'; sessions.write_new(path, result)
        return path

    def test_readonly_pending_then_semantic_failure_never_pass(self):
        seeded = {'conditions': '模拟速度为 20 km/s。', 'numbers_units': '条件 A 下的模拟速度为 200 m/s。',
                  'attribution': '条件 A 下实测观测速度为 20 km/s。', 'locator_identity': '条件 B 的观测证明机制身份。'}
        for check in ('conditions', 'numbers_units', 'attribution', 'locator_identity'):
            with self.subTest(check=check):
                with sqlite3.connect(self.db) as con: con.execute('UPDATE chunks SET text_zh=?', (seeded[check],))
                before = fast.sha(self.db)
                out = self.root / check
                report = audit.prepare(self.db, out, source_ids=[self.sid])
                self.assertEqual(report['semantic']['status'], 'pending')
                final = audit.complete(out, self.review_fixture(out, check))
                self.assertEqual(final['status'], 'fail')
                self.assertIn(check, {f['code'] for f in final['semantic']['findings']})
                self.assertEqual(audit.complete(out, out / 'semantic-review.json')['status'], 'fail')
                self.assertEqual(fast.sha(self.db), before)

    def test_review_record_tampering_cannot_forge_acceptance(self):
        lists.submit(self.folder, self.payload('1 不接受：缺少条件'), confirmed=True)
        key = lists.view(self.folder)['items'][0]['chunk_id']
        with sqlite3.connect(self.db) as con:
            event_id, raw = con.execute('SELECT event_id,record_json FROM reading_review_events').fetchone()
            value = json.loads(raw); value['outcome'] = 'faithful'
            con.execute('UPDATE reading_review_events SET record_json=? WHERE event_id=?', (json.dumps(value), event_id))
        result = get_chunk(self.db, key)
        self.assertFalse(result['item_human_accepted'])
        self.assertFalse(result['citation_allowed'])
        self.assertFalse(result['supports_claim'])
        self.assertEqual(result['item_human_review']['invalid_record_count'], 1)
        rows, replacements = audit.inventory(self.db)
        report = audit.retrieval_checks(self.db, rows, replacements)
        self.assertEqual(report['status'], 'fail')

    def test_seeded_source_mismatch_and_residual_fts_detected(self):
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE sources SET source_pdf_sha256='wrong-source'")
            con.execute("UPDATE chunks_fts SET body='old erroneous assertion'")
        report = audit.prepare(self.db, self.root / 'damaged', source_ids=[self.sid])
        self.assertEqual(report['status'], 'fail')
        codes = {f['code'] for f in report['mechanical']['findings']}
        self.assertIn('source_file_hash_mismatch', codes)
        self.assertTrue(any('fts' in code for code in codes))

    def test_missing_fixed_baseline_not_auto_generated(self):
        config = {'review_dir': str(self.root / 'missing-baseline')}
        value = audit.fixed_benchmark(self.db, self.root / 'benchmark-run', config)
        self.assertEqual(value['status'], 'not_run')
        self.assertFalse((self.root / 'missing-baseline/knowledge_benchmark.json').exists())

    def test_followup_failure_does_not_rollback_admitted_items(self):
        before = fast.sha(self.db)
        with patch('review_checklists.generate', side_effect=OSError('synthetic checklist failure')):
            result = fast.publish(self.f['batch_dir'], confirmed=True)
        self.assertEqual(result['status'], 'noop')
        self.assertEqual(result['review_followup']['checklists'][0]['status'], 'fail')
        self.assertEqual(fast.sha(self.db), before)

    def test_monthly_rerun_and_unread_sample_never_pass(self):
        out = self.root / 'monthly'
        report = audit.prepare(self.db, out, mode='monthly', config={'review_dir': str(self.root/'no-baseline')})
        self.assertEqual(report['fixed_benchmark']['status'], 'not_run')
        self.assertEqual(report['semantic']['planned_papers'], 1)
        self.assertEqual(audit.prepare(self.db, out, mode='monthly')['plan_sha256'], report['plan_sha256'])
        path = self.review_fixture(out)
        value = sessions.read(path)
        for item in value['items']:
            item['checks'] = {key: None for key in audit.CHECKS}
            item['unread_reason'] = 'Synthetic unread case; no semantic conclusion.'
            item['source_passage'] = ''
            item['source_pages'] = []
        path.write_text(json.dumps(value))
        result = audit.complete(out, path)
        self.assertEqual(result['status'], 'inconclusive')
        self.assertEqual(result['semantic']['status'], 'inconclusive')

    def test_stratification_rotation_fill_and_no_duplicate_studies(self):
        rows, _ = audit.inventory(self.db); prototype = rows[0]
        synthetic = []
        for n in range(31):
            row = copy.deepcopy(prototype)
            row.update(source_id=f'source-{n}', doi=f'10.0000/{n}', chunk_id=f'chunk-{n}', source_pdf_sha256=f'pdf-{n}', zotero_item_key='', provenance_json='{}')
            if 10 <= n < 15: row['item_human_review']['state'] = 'needs_revision'
            synthetic.append(row)
        all_now = {audit.work_identity(r): audit.fingerprint([r]) for r in synthetic}
        previous = {'inventory': {k: v for k, v in all_now.items() if int(k.split('/')[-1]) >= 10}, 'last_sampled': {}}
        selected, hashes = audit.sample(synthetic, previous, period='2026-11')
        self.assertEqual(len(selected), 20); self.assertEqual(len({p['work_id'] for p in selected}), 20)
        self.assertEqual(dict(__import__('collections').Counter(p['group'] for p in selected)), {'new_or_changed': 10, 'disputed': 5, 'historical_rotation': 5})
        small, _ = audit.sample(synthetic[:4], previous, period='2026-11')
        self.assertEqual(len(small), 4)


class InterpretationSelectionTests(unittest.TestCase):
    def row(self, key, text, kind='claim', epistemic='reported_result', figure=''):
        return {'chunk_id': key, 'text_zh': text, 'text': '', 'kind': kind,
                'epistemic_status': epistemic, 'evidence_json': json.dumps({'figure': figure})}

    def test_figures_and_mechanisms_take_eight_places_when_available(self):
        facts = [self.row(f'n{i}', f'太阳风速度为 {300+i} km/s。', figure='Fig. 1') for i in range(12)]
        figures = [self.row(f'f{i}', f'图 {i} 的极光与电流共位，作者解释为电流系统的一部分。', 'figure', 'author_interpretation', f'Fig. {i}') for i in range(4)]
        mechanisms = [self.row(f'm{i}', f'作者推断过程 {i} 通过场向电势驱动电子沉降。', epistemic='author_interpretation') for i in range(4)]
        result = lists.select_for_human_review(facts + figures + mechanisms)
        self.assertEqual(len(result), 10)
        self.assertEqual(sum(lists.review_focus(r) == 'factual_check' for r in result), 2)
        self.assertEqual({r['chunk_id'] for r in figures + mechanisms}, {r['chunk_id'] for r in result if lists.review_focus(r) != 'factual_check'})
        self.assertEqual([r['chunk_id'] for r in result], [r['chunk_id'] for r in lists.select_for_human_review(list(reversed(facts + figures + mechanisms)))])

    def test_insufficient_interpretations_do_not_expand_fact_quota(self):
        facts = [self.row(f'n{i}', f'模拟速度为 {i} km/s。', figure='Fig. 1') for i in range(12)]
        mechanism = self.row('m', '作者认为重联通过电流系统驱动沉降。', epistemic='author_interpretation')
        result = lists.select_for_human_review(facts + [mechanism])
        self.assertEqual(len(result), 4)
        self.assertEqual(sum(lists.review_focus(r) == 'factual_check' for r in result), 3)

    def test_figure_number_or_code_availability_is_not_interpretation(self):
        sensor = self.row('sensor', '图 3 的 a–c 面板来自 SSIES，d–e 面板来自 SSM。', 'method', figure='Fig. 3')
        code = self.row('code', '模拟代码需要超级计算机运行，仅按请求提供。', 'limitation', 'author_limitation')
        limit = self.row('limit', '因图像分辨率不足，无法辨认旋涡结构，不能据此排除该现象。', 'limitation', 'author_limitation')
        self.assertEqual(lists.review_focus(sensor), 'factual_check')
        self.assertEqual(lists.review_focus(code), 'factual_check')
        self.assertEqual(lists.review_focus(limit), 'interpretive_boundary')


if __name__ == '__main__': unittest.main()
