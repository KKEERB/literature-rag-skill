"""Safety and contract tests for the read-only onboarding probe."""
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'onboarding_doctor.py'
SPEC = importlib.util.spec_from_file_location('onboarding_doctor', SCRIPT)
doctor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(doctor)


class DoctorTests(unittest.TestCase):
    def test_local_doctor_checks_in_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            result = doctor.diagnose(upstream=Path(directory) / 'missing.py')
            self.assertEqual(result['preflight']['status'], 'missing')
            self.assertIn('pypdfium2', result['python_modules'])
            self.assertNotIn('fitz', result['python_modules'])
            self.assertIn(result['sqlite_fts5']['status'], ('available', 'unavailable'))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_mismatched_preflight_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / 'pdf_read_preflight.py'
            candidate.write_text('not the pinned checker', encoding='utf-8')
            result = doctor.preflight(candidate)
            self.assertEqual(result['status'], 'version_mismatch')
            self.assertFalse(result['sha256_matches'])

    def test_default_preflight_matches_the_runtime_adapter(self):
        adapter_path = SCRIPT.with_name('fast_pdf_preflight.py')
        spec = importlib.util.spec_from_file_location('adapter_for_doctor_test', adapter_path)
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        self.assertEqual(doctor.DEFAULT_PREFLIGHT, adapter.UPSTREAM)
        self.assertEqual(doctor.EXPECTED_PREFLIGHT_SHA256, adapter.UPSTREAM_SHA256)

    def test_matching_checker_requires_companion(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / 'pdf_read_preflight.py'
            candidate.write_text('fixture', encoding='utf-8')
            expected = hashlib.sha256(candidate.read_bytes()).hexdigest()
            with patch.object(doctor, 'EXPECTED_PREFLIGHT_SHA256', expected):
                result = doctor.preflight(candidate)
            self.assertTrue(result['sha256_matches'])
            self.assertEqual(result['status'], 'missing_companion')

    def test_font_presence_does_not_claim_glyph_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            font = Path(directory) / 'candidate.ttf'
            font.write_bytes(b'fixture')
            result = doctor.diagnose(upstream=Path(directory) / 'missing', font=font)
            self.assertEqual(result['fonts']['status'], 'candidates_only')
            self.assertEqual(result['fonts']['visual_and_glyph_check'], 'not_performed')


if __name__ == '__main__':
    unittest.main()
