"""Synthetic interface tests; these files are not scientific review evidence."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from knowledge_quality import digest, object_digest
from prepare_knowledge_revision import extract_source_region, prepare
from audit_knowledge_integrity import fresh_page_text


class SourceRegionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pdf = self.root / 'synthetic.pdf'
        from reportlab.pdfgen import canvas
        c = canvas.Canvas(str(self.pdf), pagesize=(600, 800))
        c.drawString(40, 730, 'Only under condition A,')
        c.drawString(40, 710, 'not under condition B.')
        c.drawString(330, 730, 'Different column and attribution.')
        c.save()
        self.region = {'extractor': 'pdftotext_crop_v1', 'pdf_page_index': 1,
                       'bbox_72dpi': [30, 40, 260, 80]}
        raw = subprocess.run(['pdftotext', '-enc', 'UTF-8', '-f', '1', '-l', '1', '-r', '72', '-x', '30',
                              '-y', '40', '-W', '260', '-H', '80', str(self.pdf), '-'],
                             capture_output=True, check=True).stdout
        self.region['text_sha256'] = hashlib.sha256(raw).hexdigest()

    def test_real_bounded_extraction_preserves_condition_and_excludes_neighbor(self):
        text, check = extract_source_region(self.pdf, self.region)
        self.assertIn('not under condition B.', text)
        self.assertNotIn('Different column', text)
        self.assertEqual(check['source_pdf_sha256'], digest(self.pdf))

    def test_layout_route_requires_its_actual_layout_stdout_hash(self):
        command = ['pdftotext', '-enc', 'UTF-8', '-layout', '-f', '1', '-l', '1', '-r', '72', '-x', '30',
                   '-y', '40', '-W', '260', '-H', '80', str(self.pdf), '-']
        raw = subprocess.run(command, capture_output=True, check=True).stdout
        region = dict(self.region, extractor='pdftotext_crop_layout_v1',
                      text_sha256=hashlib.sha256(raw).hexdigest())
        text, check = extract_source_region(self.pdf, region)
        self.assertIn('not under condition B.', text)
        self.assertEqual(check['command'], command)

    def test_integrity_locator_accepts_only_bound_matching_region_and_route(self):
        self.assertIsNotNone(fresh_page_text(self.pdf, 1, 'pdftotext_crop_v1', self.region))
        for page, route, region in ((2, 'pdftotext_crop_v1', self.region),
                                     (1, 'pdftotext_crop_layout_v1', self.region),
                                     (1, 'pdftotext_crop_v1', None),
                                     (1, 'pdftotext_crop_v1', dict(self.region, text_sha256='0' * 64))):
            with self.subTest(page=page, route=route):
                self.assertIsNone(fresh_page_text(self.pdf, page, route, region))

    def test_matching_hash_does_not_accept_invalid_utf8_region(self):
        malformed = b'\xff'
        region = dict(self.region, text_sha256=hashlib.sha256(malformed).hexdigest())
        result = subprocess.CompletedProcess(['pdftotext'], 0, stdout=malformed)
        with patch('prepare_knowledge_revision.subprocess.run', return_value=result):
            with self.assertRaises(UnicodeDecodeError):
                extract_source_region(self.pdf, region)
            self.assertIsNone(fresh_page_text(self.pdf, 1, 'pdftotext_crop_v1', region))

    def test_fake_transcription_wrong_region_and_invalid_geometry_rejected(self):
        for edit in ({'text_sha256': '0' * 64}, {'bbox_72dpi': [330, 40, 260, 80]},
                     {'bbox_72dpi': [-1, 0, 260, 80]}, {'bbox_72dpi': [0, 0, 601, 800]},
                     {'pdf_page_index': 2}, {'pdf_page_index': True}, {'extractor': 'visual_attestation'}):
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                extract_source_region(self.pdf, dict(self.region, **edit))

    def make_proposal(self):
        old = {'id': 'C001', 'text': 'synthetic interface object',
               'evidence': {'quote': 'old incomplete quote'}}
        new = dict(old, evidence={'quote': 'Only under condition A,\nnot under condition B.'})
        canonical = self.root / 'historical'; canonical.mkdir()
        model = {'status': 'draft', 'project_profile': 'sxi_cusp', 'core_claims': [old], 'zotero_item_key': 'SYNTHETIC'}
        mp = canonical / '02_machine_evidence_pack.json'; mp.write_text(json.dumps(model))
        rag = canonical / '02_rag_knowledge.md'; rag.write_text('synthetic old RAG')
        proposal = {'proposal_id': 'synthetic', 'canonical_model_path': str(mp), 'source_id': 'synthetic:source',
                    'canonical_json_pointer': '/core_claims/0', 'old_value': old, 'new_value': new,
                    'reason_zh': 'synthetic literal-route test', 'source_verification': {
                        'source_pdf_path': str(self.pdf), 'source_regions': [self.region]},
                    'inputs': {'source_pdf_sha256': digest(self.pdf), 'model_sha256': digest(mp),
                               'rag_sha256': digest(rag), 'assertion_sha256': object_digest(old)}}
        path = self.root / 'proposal.json'; path.write_text(json.dumps(proposal))
        return path, proposal, mp

    @staticmethod
    def render(directory, model, profile):
        (directory / '02_machine_evidence_pack.json').write_text(json.dumps(model))
        (directory / '02_rag_knowledge.md').write_text('synthetic revised RAG')

    def test_region_route_works_when_whole_page_text_is_interleaved(self):
        path, proposal, historical = self.make_proposal()
        before = historical.read_bytes()
        real_run = subprocess.run
        def extraction(command, **kwargs):
            if '-x' in command:
                return real_run(command, **kwargs)
            return subprocess.CompletedProcess(command, 0, stdout=b'synthetic interleaved column text')
        with patch('prepare_knowledge_revision.subprocess.run', side_effect=extraction):
            receipt = prepare([path], self.root / 'new', renderer=self.render)
        self.assertEqual(len(receipt['source_region_checks']), 1)
        self.assertEqual(historical.read_bytes(), before)
        self.assertEqual(receipt['stage'], 'awaiting_independent_revision_review')

    def test_region_cannot_authorize_new_non_source_words(self):
        path, proposal, _ = self.make_proposal()
        proposal['new_value']['evidence']['quote'] = 'This invented conclusion is always confirmed.'
        path.write_text(json.dumps(proposal))
        with self.assertRaisesRegex(ValueError, 'non-source text'):
            prepare([path], self.root / 'new', renderer=self.render)
        self.assertFalse((self.root / 'new').exists())

    def test_invalid_utf8_in_fulltext_blocks_revision_before_render(self):
        path, _, historical = self.make_proposal()
        before = historical.read_bytes()
        real_run = subprocess.run
        def extraction(command, **kwargs):
            if '-x' in command:
                return real_run(command, **kwargs)
            return subprocess.CompletedProcess(command, 0, stdout=b'\xff')
        with patch('prepare_knowledge_revision.subprocess.run', side_effect=extraction):
            with self.assertRaises(UnicodeDecodeError):
                prepare([path], self.root / 'new', renderer=self.render)
        self.assertFalse((self.root / 'new').exists())
        self.assertEqual(historical.read_bytes(), before)

    def test_pdf_changed_during_render_cannot_be_published(self):
        path, _, _ = self.make_proposal()
        def render(directory, model, profile):
            self.render(directory, model, profile)
            self.pdf.write_bytes(b'changed source')
        with self.assertRaisesRegex(ValueError, 'PDF changed'):
            prepare([path], self.root / 'new', renderer=render)
        self.assertFalse((self.root / 'new').exists())



class LocalRevisionIdentityTests(unittest.TestCase):
    def test_version_is_bound_to_actual_local_source_and_pack(self):
        from publish_knowledge_revision import revision_source_id
        first = revision_source_id({'source_id': 'pdf:' + 'a' * 64, 'project_profile': 'sxi_cusp'}, 'b' * 64)
        second = revision_source_id({'source_id': 'pdf:' + 'c' * 64, 'project_profile': 'sxi_cusp'}, 'b' * 64)
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith('pdf:' + 'a' * 64 + ':version:'))
        with self.assertRaises(ValueError): revision_source_id({'source_id': '', 'project_profile': 'sxi_cusp'}, 'b' * 64)
        with self.assertRaises(ValueError): revision_source_id({'source_id': 'pdf:x', 'project_profile': 'sxi_cusp'}, 'not-a-hash')

if __name__ == '__main__':
    unittest.main()
