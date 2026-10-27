"""Isolation and fail-closed checks; semantic validator is intentionally mocked here."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import local_stage_validation as plan
import portable_deep_read as deep


class FinalizeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name).resolve(); self.stage = self.workspace / 'stage'; self.stage.mkdir()
        self.source = self.workspace / 'source.pdf'; self.source.write_bytes(b'synthetic source')
        self.model = {'source_pdf_sha256': plan.digest(self.source), 'generation': {'generated_at': '2026-01-01T00:00:00+00:00'}}
        (self.stage / '02_machine_evidence_pack.json').write_text(json.dumps(self.model), encoding='utf-8')
        (self.stage / 'project_profile.json').write_text('{}', encoding='utf-8')
        (self.stage / 'review').mkdir()
        (self.stage / 'review/review_report.json').write_text('{}', encoding='utf-8')
        def gate(*args, **kwargs): return self.model, self.source, 'synthetic-hash'
        for obj, name, value in [(plan, 'validate_stage', gate), (deep, 'load_profile', lambda _: {}),
            (__import__('v2_core'), 'render_reader_note', lambda *args, **kw: 'reader'),
            (__import__('v2_core'), 'render_research_card', lambda *args, **kw: 'card'),
            (__import__('v2_core'), 'markdown_to_zotero_html', lambda text: '<p>' + text + '</p>')]:
            item = patch.object(obj, name, value); item.start(); self.addCleanup(item.stop)
    def test_finalize_is_idempotent_and_preserves_reviewed_inputs(self):
        model = (self.stage/'02_machine_evidence_pack.json').read_bytes()
        first = deep.finalize(self.stage,self.workspace)
        second = deep.finalize(self.stage,self.workspace)
        self.assertEqual(first, second)
        self.assertFalse(first['human_reviewed'])
        self.assertEqual(model,(self.stage/'02_machine_evidence_pack.json').read_bytes())
        self.assertEqual((self.stage/'08_reader_note.zotero.html').read_text(), '<p>reader</p>')
    def test_existing_validation_cache_is_excluded_without_deletion(self):
        cache = self.stage / '.quality_check_cache'; cache.mkdir()
        entry = cache / 'check.json'; entry.write_text('mutable cache')
        result = deep.finalize(self.stage,self.workspace)
        self.assertEqual(result['status'], 'finalized_draft_notes')
        self.assertEqual(entry.read_text(), 'mutable cache')

    def test_cache_directory_and_contents_cannot_hide_symlinks(self):
        outside = self.workspace / 'outside'; outside.mkdir()
        cache = self.stage / '.quality_check_cache'
        try:
            cache.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks unavailable')
        with self.assertRaisesRegex(ValueError, 'symbolic links'):
            plan.stage_hashes(self.stage)
        cache.unlink(); cache.mkdir()
        (cache / 'link').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symbolic links'):
            plan.stage_hashes(self.stage)

    def test_quality_failure_produces_no_notes(self):
        with patch.object(plan,'validate_stage',side_effect=ValueError('review invalid')):
            with self.assertRaisesRegex(ValueError,'review invalid'): deep.finalize(self.stage,self.workspace)
        self.assertFalse((self.stage/'01_reader_note.zh.md').exists())
    def test_conflicting_existing_note_is_not_overwritten(self):
        note=self.stage/'08_reader_note.zotero.html'; note.write_text('human content')
        with self.assertRaisesRegex(ValueError,'conflicts'): deep.finalize(self.stage,self.workspace)
        self.assertEqual(note.read_text(),'human content')
        self.assertFalse((self.stage/'01_reader_note.zh.md').exists())


class SchemaGateTests(unittest.TestCase):
    """Use the actual candidate schemas, not a mocked semantic gate."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name).resolve(); self.stage = self.workspace / 'stage'; self.stage.mkdir()
        self.stage.joinpath('review').mkdir(); self.stage.joinpath('extraction').mkdir()
        for name in ('project_profile.json', 'quality_profile.json', 'paper_manifest.json', 'extraction/extraction_manifest.json'):
            (self.stage / name).write_text('{}', encoding='utf-8')
        if __package__:
            from .test_portable_v2_pipeline import model
        else:
            from test_portable_v2_pipeline import model
        self.model = model()
        self.review = {'paper_id': 'paper', 'attachment_key': 'ABCDEFGH',
                       'producer_task_id': 'producer', 'reviewer_task_id': 'reviewer',
                       'model_content_sha256': 'a' * 64, 'reviewed_at': '2026-01-01',
                       'revision_round': 0, 'decision': 'pass', 'checks': [{} for _ in range(6)],
                       'blocking_findings': [], 'non_blocking_findings': []}
    def write_inputs(self):
        (self.stage / '02_machine_evidence_pack.json').write_text(json.dumps(self.model), encoding='utf-8')
        (self.stage / 'review/review_report.json').write_text(json.dumps(self.review), encoding='utf-8')
    def test_missing_model_schema_field_blocks_before_any_note(self):
        del self.model['topic_tags']; self.write_inputs()
        with self.assertRaisesRegex(ValueError, 'model schema validation failed'):
            deep.finalize(self.stage, self.workspace)
        self.assertFalse((self.stage / '01_reader_note.zh.md').exists())
    def test_missing_review_schema_field_blocks_before_any_note(self):
        del self.review['reviewed_at']; self.write_inputs()
        with self.assertRaisesRegex(ValueError, 'review schema validation failed'):
            deep.finalize(self.stage, self.workspace)
        self.assertFalse((self.stage / '01_reader_note.zh.md').exists())
    def test_schema_validator_dependency_is_mandatory(self):
        with patch.dict(sys.modules, {'jsonschema': None}):
            with self.assertRaisesRegex(RuntimeError, 'jsonschema is required'):
                plan.validate_schemas(self.model, self.review)

if __name__ == '__main__': unittest.main()
