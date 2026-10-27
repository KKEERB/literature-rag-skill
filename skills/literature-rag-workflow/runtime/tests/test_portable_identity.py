"""Portable identity checks use synthetic values and do not access Zotero."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from portable_identity import source_identity, validate_portable_identity
from test_portable_v2_pipeline import model
from v2_core import load_profile
from v21_quality import _validate_deep_read_model, load_quality_profile, validate_deep_read_model


class PortableIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model_schema = Draft202012Validator(json.loads((ROOT / 'schemas/machine_evidence_pack.schema.json').read_text()))
        cls.review_schema = Draft202012Validator(json.loads((ROOT / 'schemas/review_report_v21.schema.json').read_text()))

    def report(self, attachment='', digest='a' * 64):
        return {'paper_id': 'paper', 'attachment_key': attachment, 'source_pdf_sha256': digest,
                'producer_task_id': 'producer', 'reviewer_task_id': 'reviewer',
                'model_content_sha256': 'b' * 64, 'reviewed_at': '2026-01-01',
                'revision_round': 0, 'decision': 'pass', 'checks': [{} for _ in range(6)],
                'blocking_findings': [], 'non_blocking_findings': []}

    def test_model_schema_accepts_both_empty_or_valid_pair_only(self):
        m = model()
        for parent, attachment in [('', ''), ('PARENT01', 'ATTACH01')]:
            m.update(zotero_item_key=parent, zotero_attachment_key=attachment)
            self.assertEqual(list(self.model_schema.iter_errors(m)), [])
        for parent, attachment in [('', 'ATTACH01'), ('PARENT01', ''), ('bad', 'ATTACH01'), ('PARENT01', 'lowercas'), (None, None)]:
            m.update(zotero_item_key=parent, zotero_attachment_key=attachment)
            self.assertTrue(list(self.model_schema.iter_errors(m)), (parent, attachment))

    def test_local_review_schema_requires_source_hash(self):
        report = self.report()
        self.assertEqual(list(self.review_schema.iter_errors(report)), [])
        del report['source_pdf_sha256']
        self.assertTrue(list(self.review_schema.iter_errors(report)))
        report['attachment_key'] = 'bad-key'
        self.assertTrue(list(self.review_schema.iter_errors(report)))
        report['attachment_key'] = 'ATTACH01'
        self.assertEqual(list(self.review_schema.iter_errors(report)), [])

    def test_manifest_model_and_review_bind_source(self):
        m = model(); m.update(zotero_item_key='', zotero_attachment_key='')
        manifest = {'source_id': 'pdf:' + 'a' * 64, 'paper_id': 'paper', 'pdf_sha256': 'a' * 64,
                    'zotero_item_key': '', 'zotero_attachment_key': ''}
        report = self.report()
        self.assertEqual(validate_portable_identity(manifest, m, report), ('', ''))
        wrong_source = dict(manifest, source_id='pdf:' + '0' * 64)
        with self.assertRaises(ValueError): validate_portable_identity(wrong_source, m, report)
        for field, value in [('source_pdf_sha256', '0' * 64), ('attachment_key', 'ATTACH01'), ('paper_id', 'other')]:
            bad = copy.deepcopy(report); bad[field] = value
            with self.assertRaises(ValueError): validate_portable_identity(manifest, m, bad)
        m['zotero_attachment_key'] = 'ATTACH01'
        with self.assertRaises(ValueError): validate_portable_identity(manifest, m, report)

    def test_zotero_source_requires_both_matching_keys(self):
        self.assertEqual(source_identity('zotero:PARENT01', 'PARENT01', 'ATTACH01'), ('PARENT01', 'ATTACH01'))
        for parent, attachment in [('', ''), ('PARENT01', ''), ('PARENT02', 'ATTACH01')]:
            with self.assertRaises(ValueError): source_identity('zotero:PARENT01', parent, attachment)
        m = model(); m.update(zotero_item_key='PARENT01', zotero_attachment_key='ATTACH01')
        manifest = {'source_id': 'zotero:PARENT01', 'paper_id': 'paper', 'pdf_sha256': 'a' * 64,
                    'zotero_item_key': 'PARENT01', 'zotero_attachment_key': 'ATTACH01'}
        self.assertEqual(validate_portable_identity(manifest, m, self.report('ATTACH01')), ('PARENT01', 'ATTACH01'))
        with self.assertRaises(ValueError): validate_portable_identity(manifest, m, self.report('ATTACH02'))

    def test_quality_gate_reports_portable_review_identity_drift(self):
        m = model(); m.update(zotero_item_key='', zotero_attachment_key='')
        manifest = {'schema': 'portable-deep-reading-source/1', 'source_id': 'pdf:' + 'a' * 64,
                    'paper_id': 'paper', 'pdf_sha256': 'a' * 64,
                    'zotero_item_key': '', 'zotero_attachment_key': ''}
        with tempfile.TemporaryDirectory() as temp:
            stage = Path(temp)
            (stage / 'extraction').mkdir(); (stage / 'review').mkdir()
            (stage / 'paper_manifest.json').write_text(json.dumps(manifest))
            (stage / 'extraction/extraction_manifest.json').write_text(json.dumps({'status': 'pass', 'page_count': 0, 'pages': []}))
            (stage / 'review/review_report.json').write_text(json.dumps(self.report(digest='0' * 64)))
            errors, _, _ = _validate_deep_read_model(m, load_profile(ROOT / 'profiles/sxi_cusp.json'),
                load_quality_profile(ROOT / 'profiles/content_quality_v21.json'), stage, require_reviewer=True)
            self.assertTrue(any('portable review identity:' in error for error in errors))

    def test_quality_cache_depends_on_identity_checker(self):
        m = model()
        with tempfile.TemporaryDirectory() as temp:
            stage = Path(temp)
            (stage / 'extraction').mkdir()
            (stage / '02_machine_evidence_pack.json').write_text(json.dumps(m))
            (stage / 'extraction/extraction_manifest.json').write_text(json.dumps({'pages': []}))
            with patch('adaptive_content.cached_check', return_value=(([], [], {}), False)) as cached:
                validate_deep_read_model(m, {}, {}, stage)
            dependencies = cached.call_args.args[2]
            self.assertIn('checker:portable_identity.py', dependencies)


if __name__ == '__main__':
    unittest.main()
