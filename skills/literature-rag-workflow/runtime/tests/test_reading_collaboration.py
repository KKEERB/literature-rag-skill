"""Real parser/SQLite/FTS engineering tests using explicitly synthetic judgments.

These exercise provenance and state transitions, not the accuracy of an LLM
grader or a real person's understanding. No production database is opened.
"""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
import fast_pdf_intake as fast
import reading_sessions as sessions
import publish_reading_revision as revisions
import literature_learning as learning
import portable_deep_read as deep_read
from test_fast_pdf_intake import fixture
from knowledge_system_core import get_chunk, search_database, sync_database


class ReadingCollaborationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.f = fixture(self.root)
        fast.publish(self.f['batch_dir'], confirmed=True)
        self.db = self.f['database']
        with sessions.connect(self.db) as con:
            self.sid = con.execute('SELECT source_id FROM sources').fetchone()[0]
            self.key = con.execute('SELECT chunk_id FROM chunks').fetchone()[0]
        self.session = self.root / 'session'
        sessions.start(self.db, self.sid, self.session)
        sessions.record(self.session, {'kind': 'reading_ready', 'actor': 'user',
            'user_message_ref': 'synthetic:ready', 'human_statement': '仅用于工程夹具：已准备讨论。'})

    def discussion(self, text='条件 A 下的模拟结果，并非观测或排他机制证据。'):
        q = sessions.record(self.session, {'kind': 'question', 'stage': 'mainline', 'text': '请解释主线。'})
        a = sessions.record(self.session, {'kind': 'answer', 'actor': 'user', 'user_message_ref': 'synthetic:test-answer',
                                         'question_event_id': q['event_id'], 'text': text})
        s = sessions.record(self.session, {'kind': 'source_check', 'checked_by': 'synthetic-source-reader',
                'rationale_zh': '合成夹具的原文明确保留条件、模拟身份和否定。',
                'evidence': {'pdf_page_index': 1, 'quote': self.f['analysis']['items'][0]['evidence']['quote']}})
        return q, a, s

    def judgment(self, outcome='faithful'):
        q, a, s = self.discussion()
        with sessions.connect(self.db) as con:
            text = con.execute('SELECT text_zh FROM chunks WHERE chunk_id=?', (self.key,)).fetchone()[0]
        comparison = sessions.record(self.session, {'kind': 'comparison', 'chunk_ids': [self.key],
                'source_check_ids': [s['event_id']], 'shown_text': text,
                'shown_to_user_ref': 'synthetic:displayed-comparison'})
        event = sessions.record(self.session, {'kind': 'judgment', 'actor': 'user',
                'comparison_event_id': comparison['event_id'], 'user_message_ref': 'synthetic:human-judgment',
                'human_statement': '工程测试的模拟判断，不是真实人类审阅。', 'explicit_item_judgment': True,
                'decisions': [{'chunk_id': self.key, 'outcome': outcome, 'rationale_zh': '合成测试的原文核对。'}]})
        return event, s

    def revision(self, *, add=False, approve=True, changes_text='修订表述：仅在条件 A 下，模拟速度为 20 km/s；不是观测。'):
        _, _, s = self.discussion()
        item = copy.deepcopy(self.f['analysis']['items'][0]); item['text_zh'] = changes_text
        if add:
            item.update(id='new-method', kind='method')
        proposal = {'schema': 'reading-correction/1', 'producer_id': 'synthetic-correction-author',
                    'reason_zh': '仅用于工程验证的修订。', 'changes': [{
                        'original_chunk_id': None if add else self.key, 'item': item,
                        'source_check_ids': [s['event_id']], 'rationale_zh': '回查合成原文。'}]}
        p = self.root / 'proposal.json'; sessions.write_new(p, proposal)
        out = self.root / 'revision'; result = revisions.prepare(self.session, p, out)
        if approve:
            sessions.record(self.session, {'kind': 'revision_approval', 'actor': 'user',
                'proposal_path': str(p), 'proposal_sha256': result['proposal_sha256'],
                'explicit_revision_approval': True, 'user_message_ref': 'synthetic:revision-approval',
                'human_statement': '工程测试确认该版本。'})
        model = sessions.read(out / '02_machine_evidence_pack.json')
        raw = subprocess.run(['pdftotext', self.f['source']['source_pdf_path'], '-'], capture_output=True, check=True).stdout
        review = {'schema': 'reading-revision-review/1', 'decision': 'pass', 'blocking_findings': [],
                  'model_sha256': fast.sha(out / '02_machine_evidence_pack.json'),
                  'manifest_sha256': fast.sha(out / 'revision_manifest.json'),
                  'producer_id': proposal['producer_id'], 'reviewer_id': 'synthetic-independent-reviewer',
                  'items': [{'item_id': i['id'], 'review': {
                      'method': 'original_source_semantic_review', 'reviewer_id': 'synthetic-independent-reviewer',
                      'producer_id': proposal['producer_id'], 'reviewer_attestation': 'synthetic fixture only',
                      'rationale_zh': '合成测试，不是实际科学复核。', 'support': 'supported',
                      'checks': {k: True for k in fast.CHECKS}, 'check_details': {k: 'synthetic check ' + k for k in fast.CHECKS},
                      'source_pdf_path': self.f['source']['source_pdf_path'], 'fresh_text_sha256': hashlib.sha256(raw).hexdigest(),
                      'source_pages': [1], 'source_passage': 'PDF PAGE 1\n' + i['evidence']['quote']}}
                      for i in model['items']]}
        sessions.write_new(out / 'revision_review.json', review)
        return out

    def test_start_is_readonly_and_blind_mainline(self):
        before = fast.sha(self.db)
        packet = sessions.next_turn(self.session, discussion=True)
        self.assertEqual(packet['stage'], 'mainline')
        self.assertNotIn('20 km/s', packet['question'])
        self.assertEqual(sessions.status(self.session)['reviewed_items'], 0)
        self.assertEqual(fast.sha(self.db), before)
        self.assertFalse(get_chunk(self.db, self.key)['item_human_reviewed'])

    def test_correct_answer_and_learning_feedback_never_create_review(self):
        _, a, s = self.discussion()
        sessions.record(self.session, {'kind': 'feedback', 'answer_event_id': a['event_id'],
            'source_check_ids': [s['event_id']], 'dimension': 'boundaries', 'level': 'independent', 'rationale_zh': '合成回答保留边界。'})
        self.assertEqual(sessions.commit_reviews(self.session, confirmed=True)['records'], 0)
        self.assertFalse(get_chunk(self.db, self.key)['item_human_reviewed'])

    def test_feedback_requires_original_and_hint_cannot_be_independent(self):
        q = sessions.record(self.session, {'kind': 'question', 'stage': 'mainline', 'text': '测试问题'})
        sessions.record(self.session, {'kind': 'hint', 'question_event_id': q['event_id'], 'text': '注意模拟条件。'})
        a = sessions.record(self.session, {'kind': 'answer', 'actor': 'user', 'user_message_ref': 'synthetic:hinted', 'question_event_id': q['event_id'], 'text': '提示后回答'})
        s = sessions.record(self.session, {'kind': 'source_check', 'checked_by': 'test', 'rationale_zh': '原文检查',
                'evidence': {'pdf_page_index': 1, 'quote': self.f['analysis']['items'][0]['evidence']['quote']}})
        payload = {'kind': 'feedback', 'answer_event_id': a['event_id'], 'dimension': 'methods', 'level': 'independent', 'rationale_zh': '测试'}
        with self.assertRaisesRegex(ValueError, 'original checks'): sessions.record(self.session, payload)
        payload['source_check_ids'] = [s['event_id']]
        with self.assertRaisesRegex(ValueError, 'hint-assisted'): sessions.record(self.session, payload)
        payload['level'] = 'with_help'; sessions.record(self.session, payload)

    def test_no_comparison_before_explanation(self):
        with self.assertRaisesRegex(ValueError, 'human explanation'):
            sessions.record(self.session, {'kind': 'comparison'})

    def test_wrong_page_or_fake_quote_rejected(self):
        for evidence in [{'pdf_page_index': 2, 'quote': '20 km/s'}, {'pdf_page_index': 1, 'quote': 'The observation proves mechanism M.'}]:
            with self.subTest(evidence=evidence), self.assertRaisesRegex(ValueError, 'not found'):
                sessions.record(self.session, {'kind': 'source_check', 'checked_by': 'test', 'rationale_zh': 'test', 'evidence': evidence})

    def test_real_human_identity_and_message_required(self):
        q = sessions.record(self.session, {'kind': 'question', 'stage': 'mainline', 'text': 'test'})
        with self.assertRaisesRegex(ValueError, 'actual human'):
            sessions.record(self.session, {'kind': 'answer', 'actor': 'assistant', 'question_event_id': q['event_id'], 'text': 'fake'})

    def test_scoped_review_preview_commit_noop_and_no_promotion(self):
        self.judgment()
        before = fast.sha(self.db)
        self.assertEqual(sessions.commit_reviews(self.session)['status'], 'preview')
        self.assertEqual(before, fast.sha(self.db))
        receipt = sessions.commit_reviews(self.session, confirmed=True, lock_path=self.root / 'workflow.lock')
        self.assertEqual(receipt['status'], 'pass'); self.assertTrue(receipt['postcommit_readback'])
        result = get_chunk(self.db, self.key)
        self.assertEqual(result['item_human_review']['state'], 'faithful')
        self.assertFalse(result['human_reviewed'])
        with sessions.connect(self.db) as con:
            self.assertEqual(con.execute('SELECT status FROM sources').fetchone()[0], 'draft')
            self.assertEqual(con.execute('SELECT count(*) FROM promotions').fetchone()[0], 0)
        self.assertEqual(sessions.commit_reviews(self.session, confirmed=True, lock_path=self.root / 'workflow.lock')['status'], 'noop')

    def test_unresolved_judgment_withholds_support(self):
        self.judgment('unresolved')
        sessions.commit_reviews(self.session, confirmed=True, lock_path=self.root / 'lock')
        self.assertFalse(get_chunk(self.db, self.key)['supports_claim'])

    def test_human_review_does_not_cover_unseen_items(self):
        _, _, s = self.discussion()
        with self.assertRaisesRegex(ValueError, 'exact nonempty'):
            sessions.record(self.session, {'kind': 'comparison', 'chunk_ids': ['other:chunk'], 'source_check_ids': [s['event_id']]})

    def test_pause_resume_and_skip(self):
        sessions.record(self.session, {'kind': 'control', 'action': 'pause'})
        self.assertIsNone(sessions.next_turn(self.session, discussion=True)['question'])
        with self.assertRaisesRegex(ValueError, 'resume'):
            sessions.record(self.session, {'kind': 'question', 'stage': 'mainline', 'text': 'test'})
        sessions.record(self.session, {'kind': 'control', 'action': 'resume'})
        sessions.record(self.session, {'kind': 'control', 'action': 'skip', 'stage': 'mainline'})
        self.assertEqual(sessions.next_turn(self.session, discussion=True)['stage'], 'explain')

    def test_skip_current_unanswered_question(self):
        sessions.record(self.session, {'kind': 'question', 'stage': 'mainline', 'text': 'test'})
        self.assertEqual(sessions.next_turn(self.session, discussion=True)['state'], 'awaiting_answer')
        sessions.record(self.session, {'kind': 'control', 'action': 'skip', 'stage': 'mainline'})
        self.assertEqual(sessions.next_turn(self.session, discussion=True)['stage'], 'explain')

    def test_render_refreshes_material_availability_without_acceptance(self):
        original = (self.session / 'session.json').read_bytes()
        source = sessions.read(self.session / 'session.json')['paper']['source']
        material = Path(source['source_path']) / '10_fulltext_translation.zh-CN.pdf'
        material.write_bytes(b'synthetic file presence, not a validated PDF')
        page = Path(sessions.render(self.session, discussion=True)).read_text()
        self.assertIn('materials/10_fulltext_translation.zh-CN.pdf', page)
        self.assertIn('文件可用，验收状态需核对', page)
        self.assertEqual((self.session / 'session.json').read_bytes(), original)

    def test_tampered_chain_and_source_drift(self):
        self.discussion()
        p = self.session / 'events/000001.json'; v = sessions.read(p); v['text'] = 'tampered'; p.write_text(json.dumps(v))
        with self.assertRaisesRegex(ValueError, 'chain'): sessions.status(self.session)

    def test_current_source_change_invalidates_session(self):
        p = Path(self.f['source']['source_pdf_path']); p.write_bytes(p.read_bytes() + b'changed')
        self.assertEqual(sessions.status(self.session)['state'], 'stale')
        with self.assertRaisesRegex(ValueError, 'unavailable/changed'):
            sessions.record(self.session, {'kind': 'question', 'stage': 'mainline', 'text': 'test'})

    def test_revision_preview_and_missing_approval(self):
        out = self.revision(approve=False)
        before = fast.sha(self.db)
        with self.assertRaisesRegex(ValueError, 'explicit human approval'):
            revisions.publish(out, lock_path=self.root / 'lock')
        self.assertEqual(before, fast.sha(self.db))

    def test_revision_self_review_rejected(self):
        out = self.revision()
        p = out / 'revision_review.json'; v = sessions.read(p); v['reviewer_id'] = v['producer_id']; p.write_text(json.dumps(v))
        with self.assertRaisesRegex(ValueError, 'independent revision PASS'):
            revisions.publish(out, confirmed=True, lock_path=self.root / 'lock')

    def test_corrected_content_retrieved_and_old_kept_as_history(self):
        self.judgment('needs_revision')
        sessions.commit_reviews(self.session, confirmed=True, lock_path=self.root / 'lock')
        old = get_chunk(self.db, self.key)
        out = self.revision()
        before = fast.sha(self.db)
        self.assertEqual(revisions.publish(out, lock_path=self.root / 'lock')['status'], 'preview')
        self.assertEqual(before, fast.sha(self.db))
        result = revisions.publish(out, confirmed=True, lock_path=self.root / 'lock')
        self.assertEqual(result['status'], 'pass')
        new_key = get_chunk(self.db, self.key)['superseded_by']
        self.assertFalse(get_chunk(self.db, self.key)['supports_claim'])
        self.assertEqual(get_chunk(self.db, self.key)['text'], old['text'])
        self.assertTrue(get_chunk(self.db, new_key)['supports_claim'])
        self.assertFalse(get_chunk(self.db, new_key)['item_human_reviewed'])
        # An exact old ID need not match the revised wording or new FTS row.
        results = search_database(self.db, self.key)['results']
        self.assertEqual([x['chunk_id'] for x in results], [new_key])
        self.assertEqual(sessions.status(self.session)['state'], 'stale')
        self.assertEqual(revisions.publish(out, confirmed=True, lock_path=self.root / 'lock')['status'], 'noop')
        with sessions.connect(self.db) as con:
            self.assertEqual(con.execute('SELECT count(*) FROM promotions').fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT count(*) FROM sources WHERE zotero_item_key!=""').fetchone()[0], 0)

    def test_source_supported_omission_can_be_added_without_replacing_old(self):
        out = self.revision(add=True)
        revisions.publish(out, confirmed=True, lock_path=self.root / 'lock')
        self.assertTrue(get_chunk(self.db, self.key)['supports_claim'])
        self.assertIsNone(get_chunk(self.db, self.key)['superseded_by'])
        with sessions.connect(self.db) as con:
            self.assertEqual(con.execute('SELECT count(*) FROM chunks').fetchone()[0], 2)

    def test_new_session_uses_effective_revision_and_keeps_unmodified_items(self):
        out = self.revision(); revisions.publish(out, confirmed=True, lock_path=self.root / 'lock')
        new_key = get_chunk(self.db, self.key)['superseded_by']
        fresh = self.root / 'new-session'; sessions.start(self.db, self.sid, fresh)
        self.assertEqual(sessions.status(fresh)['state'], 'active')
        paper = sessions.session_state(fresh)[0]['paper']
        self.assertIn(new_key, paper['assertions']); self.assertNotIn(self.key, paper['assertions'])
        self.assertEqual(sessions.status(fresh)['reviewed_items'], 0)

    def test_receipt_can_recover_after_committed_publication(self):
        out = self.revision(); revisions.publish(out, confirmed=True, lock_path=self.root / 'lock')
        (out / 'publication.json').unlink()
        result = revisions.publish(out, confirmed=True, lock_path=self.root / 'lock')
        self.assertEqual(result['status'], 'noop'); self.assertTrue(result['postcommit_readback'])
        self.assertTrue((out / 'publication.json').is_file())

    def test_prepare_is_idempotent_for_same_frozen_proposal(self):
        out = self.revision()
        before = fast.sha(out / 'revision_manifest.json')
        revisions.prepare(self.session, self.root / 'proposal.json', out)
        self.assertEqual(before, fast.sha(out / 'revision_manifest.json'))

    def test_original_outline_is_distinct_from_index_coverage(self):
        _, _, s = self.discussion()
        sessions.record(self.session, {'kind': 'source_outline', 'checked_by': 'synthetic-reader',
            'units': [{'label_zh': '原文的模拟条件和排他性限制', 'source_check_ids': [s['event_id']]}],
            'unread_scope': ['图像在此夹具中不适用']})
        self.assertTrue(sessions.status(self.session)['original_outline'])
        self.assertEqual(sessions.status(self.session)['reviewed_items'], 0)

    def test_changed_proposal_cannot_use_old_human_approval(self):
        out = self.revision()
        p = out / 'proposal.json'; v = sessions.read(p); v['changes'][0]['item']['text_zh'] = '改动'; p.write_text(json.dumps(v))
        with self.assertRaisesRegex(ValueError, 'proposal or generated'):
            revisions.publish(out, confirmed=True, lock_path=self.root / 'lock')

    def test_missing_revised_source_never_resurrects_old_answer(self):
        out = self.revision(); revisions.publish(out, confirmed=True, lock_path=self.root / 'lock')
        (out / '02_machine_evidence_pack.json').unlink()
        results = search_database(self.db, self.key)['results']
        self.assertNotIn(self.key, [r['chunk_id'] for r in results])
        self.assertFalse(any(r['supports_claim'] for r in results))

    def test_real_sync_preserves_revision_and_human_review(self):
        self.judgment(); sessions.commit_reviews(self.session, confirmed=True, lock_path=self.root / 'lock')
        out = self.revision(); revisions.publish(out, confirmed=True, lock_path=self.root / 'lock')
        new_key = get_chunk(self.db, self.key)['superseded_by']
        config = {'database_path': str(self.db), 'review_dir': str(self.root / 'kb-review'),
                  'baseline': {'validation_mode': 'dynamic'}, 'paper_model_globs': [], 'additional_sources': [],
                  'fast_pdf_model_globs': [str(self.f['storage'] / 'published/*/*/02_machine_evidence_pack.json')]}
        sync_database(config)
        self.assertEqual(get_chunk(self.db, self.key)['superseded_by'], new_key)
        self.assertTrue(get_chunk(self.db, new_key)['supports_claim'])
        self.assertEqual(get_chunk(self.db, self.key)['item_human_review']['state'], 'faithful')
        self.assertEqual([r['chunk_id'] for r in search_database(self.db, self.key)['results']], [new_key])

    def test_selected_paper_and_default_route_are_separate(self):
        output = self.root / 'selected'
        result = learning.select(self.db, self.sid, output)
        order = sessions.read(output / 'deep-reading-work-order.json')
        self.assertEqual(order['human_reading_status'], 'awaiting_actual_human_response')
        self.assertEqual(result['state']['answers'], 0)
        self.assertEqual(result['next']['state'], 'ready_for_checklist_review')
        with self.assertRaisesRegex(ValueError, 'not said they are ready'):
            sessions.record(output, {'kind': 'question', 'stage': 'mainline', 'text': 'test'})
        self.assertEqual(learning.policy()['default_intake'], 'fast_pdf_two_round')
        self.assertNotIn('background_schedule', learning.policy())

    def test_local_pdf_deep_stage_preserves_empty_identity_and_hash(self):
        runtime = self.root / 'fixture-runtime'
        (runtime / 'profiles').mkdir(parents=True)
        for source_name, target_name in [('magnetosphere_ionosphere.json', 'magnetosphere_ionosphere.json'),
                                         ('content_quality_v21.json', 'content_quality_v21.json')]:
            (runtime / 'profiles' / target_name).write_bytes((ROOT / 'profiles' / source_name).read_bytes())
        with patch.object(deep_read, 'ROOT', runtime):
            result = deep_read.prepare(self.session, self.root)
        stage = Path(result['stage'])
        manifest = sessions.read(stage / 'paper_manifest.json')
        scaffold = sessions.read(stage / 'model.scaffold.json')
        self.assertEqual((manifest['zotero_item_key'], manifest['zotero_attachment_key']), ('', ''))
        self.assertEqual((scaffold['zotero_item_key'], scaffold['zotero_attachment_key']), ('', ''))
        self.assertEqual(manifest['source_id'], 'pdf:' + manifest['pdf_sha256'])
        self.assertEqual(scaffold['source_pdf_sha256'], manifest['pdf_sha256'])
        self.assertFalse(result['model_executed'])

    def test_registered_reading_copy_is_bound_and_never_human_review(self):
        folder = self.root / 'reading-copy'; folder.mkdir()
        session, _, _ = sessions.session_state(self.session)
        source = session['paper']['source']
        model = {'source_pdf_sha256': source['source_pdf_sha256'],
                 'zotero_item_key': source.get('zotero_item_key') or '',
                 'zotero_attachment_key': source.get('zotero_attachment_key') or ''}
        (folder / '02_machine_evidence_pack.json').write_text(json.dumps(model))
        note = folder / '08_reader_note.zotero.html'
        note.write_text('<h1>中文精读</h1>')
        sessions.register_materials(self.session, folder)
        session, events, _ = sessions.session_state(self.session)
        item = next(x for x in sessions.delivered_materials(session, events)
                    if x['role'] == note.name)
        self.assertEqual(item['state'], 'available_not_revalidated')
        self.assertEqual(sessions.status(self.session)['reviewed_items'], 0)
        model['zotero_attachment_key'] = 'ATTACH01'
        (folder / '02_machine_evidence_pack.json').write_text(json.dumps(model))
        with self.assertRaises(ValueError):
            sessions.register_materials(self.session, folder)
        model['zotero_attachment_key'] = ''
        note.write_text('<h1>changed</h1>')
        item = next(x for x in sessions.delivered_materials(session, events) if x['role'] == note.name)
        self.assertEqual(item['state'], 'changed_or_missing')
        model['source_pdf_sha256'] = '0' * 64
        (folder / '02_machine_evidence_pack.json').write_text(json.dumps(model))
        with self.assertRaisesRegex(ValueError, 'identity differs'):
            sessions.register_materials(self.session, folder)

    def test_seeded_errors_have_source_first_correction_path(self):
        # Distinct known error classes are source-bound test cases; no simulated
        # grader performance is reported as measured model accuracy.
        cases = ['证明机制 M', '所有条件下都成立', '20 m/s', '这是本文首次发现的观测']
        for statement in cases:
            with self.subTest(error=statement):
                _, a, s = self.discussion(statement)
                event = sessions.record(self.session, {'kind': 'feedback', 'answer_event_id': a['event_id'],
                    'source_check_ids': [s['event_id']], 'dimension': 'boundaries', 'level': 'needs_reading',
                    'rationale_zh': '合成原文仅描述条件 A 下 20 km/s 模拟；此答案超出原文。'})
                self.assertEqual(event['level'], 'needs_reading')
        self.assertEqual(sessions.status(self.session)['reviewed_items'], 0)


class AnswerDisclosureTests(unittest.TestCase):
    def test_legacy_fragment_view_keeps_utf8_source_and_hides_answers(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'source.html'
            source = '<div><h1>中文笔记</h1><p>问题</p><h2>答案键</h2><p>参考答案</p><h2>人工审核记录</h2></div>'
            path.write_text(source, encoding='utf-8')
            output = sessions.render_note_view(folder, path)
            rendered = (Path(folder) / output).read_text()
            self.assertIn('<meta charset="utf-8">', rendered)
            self.assertIn('<details>', rendered)
            self.assertNotIn('<details open', rendered)
            self.assertEqual(path.read_text(), source)
            self.assertIn('中文笔记', rendered)

    def test_html_reference_answers_are_closed_and_questions_outside(self):
        from test_portable_v2_pipeline import model
        from v2_core import render_reader_note, load_profile, markdown_to_zotero_html
        note = render_reader_note(model(), load_profile(ROOT / 'profiles/sxi_cusp.json'))
        rendered = markdown_to_zotero_html(note)
        self.assertIn('<details>', rendered)
        self.assertNotIn('<details open', rendered)
        self.assertLess(rendered.index('Q01'), rendered.index('<details>'))


if __name__ == '__main__':
    unittest.main()
