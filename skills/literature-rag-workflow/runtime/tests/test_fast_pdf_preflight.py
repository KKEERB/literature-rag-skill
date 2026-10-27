"""Real local adapter gates; synthetic PDF structure, no model science review."""
import importlib.util
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import fast_pdf_preflight as adapter

fixture_path = adapter.UPSTREAM.with_name('test_pdf_read_preflight.py')
spec = importlib.util.spec_from_file_location('_ars_preflight_test_fixtures', fixture_path)
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


def self_entry_free():
    # Unlike the older structural fixture this page has Resources, so qpdf
    # does not need to repair a missing inherited page dictionary field.
    bodies = [b'<< /Type /Catalog /Pages 2 0 R >>',
              b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
              b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << >> >>']
    offsets, payload = [], b''
    for body in bodies:
        offsets.append(len(payload)); payload += body + b' '
    header = b' '.join(b'%d %d' % (i + 1, off) for i, off in enumerate(offsets)) + b' '
    content = header + payload
    data = bytearray(b'%PDF-1.5\n')
    objstm = len(data)
    data += (b'4 0 obj\n<< /Type /ObjStm /N 3 /First %d /Length %d >>\nstream\n'
             % (len(header), len(content))) + content + b'\nendstream\nendobj\n'
    start = len(data)
    rows = [(0, 0, 255), (2, 4, 0), (2, 4, 1), (2, 4, 2), (1, objstm, 0), (0, 0, 255)]
    stream = b''.join(struct.pack('>BHB', *r) for r in rows)
    data += (b'5 0 obj\n<< /Type /XRef /Size 6 /Root 1 0 R /W [1 2 1] /Index [0 6] /Length %d >>\nstream\n'
             % len(stream)) + stream + b'\nendstream\nendobj\n'
    data += b'startxref\n%d\n%%%%EOF\n' % start
    return bytes(data)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_on(self, data):
        path = Path(self.tmp.name) / 'source.pdf'
        path.write_bytes(data)
        return adapter.run_preflight(path)

    def test_active_parsed_xref_with_free_self_entry(self):
        data = self_entry_free()
        path = Path(self.tmp.name) / 'source.pdf'; path.write_bytes(data)
        original = adapter.upstream_module().run_preflight(path)
        self.assertEqual(original['verdict'], 'UNAVAILABLE')
        actual = adapter.run_preflight(path)
        self.assertEqual(actual['verdict'], 'PASS', actual)
        self.assertEqual(actual['active_xref_streams_calibrated'][0]['object'], 5)
        self.assertEqual(actual['independent_qpdf_check']['exit_code'], 0)

    def test_appended_orphan_with_stale_startxref_is_still_blocked(self):
        data = self_entry_free()
        tail = data[data.rfind(b'startxref'):]
        self.assertNotEqual(self.run_on(data + b'\n6 0 obj\n<< /Type /Page >>\nendobj\n' + tail)['verdict'], 'PASS')

    def test_duplicate_xref_body_with_stale_startxref_is_still_blocked(self):
        data = self_entry_free(); tail = data[data.rfind(b'startxref'):]
        self.assertNotEqual(self.run_on(data + b'\n5 0 obj\n<< /Type /XRef >>\nendobj\n' + tail)['verdict'], 'PASS')

    def test_unreferenced_xref_type_does_not_get_exempted(self):
        data = self_entry_free(); tail = data[data.rfind(b'startxref'):]
        self.assertNotEqual(self.run_on(data + b'\n6 0 obj\n<< /Type /XRef >>\nendobj\n' + tail)['verdict'], 'PASS')

    def test_qpdf_warning_is_not_pass(self):
        with patch.object(adapter.subprocess, 'run', return_value=types.SimpleNamespace(returncode=3, stdout=b'', stderr=b'warning')):
            self.assertEqual(self.run_on(self_entry_free())['verdict'], 'UNAVAILABLE')

    def test_qpdf_missing_is_not_pass(self):
        with patch.object(adapter.subprocess, 'run', side_effect=FileNotFoundError('qpdf')):
            self.assertEqual(self.run_on(self_entry_free())['verdict'], 'UNAVAILABLE')

    def test_upstream_drift_is_not_pass(self):
        with patch.object(adapter, 'UPSTREAM_SHA256', 'changed'):
            self.assertEqual(self.run_on(fixtures._flat_pdf())['verdict'], 'UNAVAILABLE')

    def test_unavailable_parser_stays_unavailable(self):
        upstream = adapter.upstream_module(); upstream.pypdf = None
        with patch.object(adapter, 'upstream_module', return_value=upstream):
            result = self.run_on(fixtures._flat_pdf())
            self.assertEqual(result['verdict'], 'UNAVAILABLE')
            self.assertTrue(any('pypdf-not-installed' in x for x in result['warnings']))


def load_tests(loader, tests, pattern):
    # Run the original concrete parser/structure cases through the actual local
    # adapter, including all stale-startxref/sign/comment/NUL variants. The two
    # upstream mock-module tests run unchanged in the upstream suite separately.
    fixtures.preflight = adapter
    names = [n for n in loader.getTestCaseNames(fixtures.PreflightVerdictTest)
             if n not in {'test_pypdf_missing_unavailable', 'test_parser_warnings_survive_early_exit'}]
    tests.addTests(fixtures.PreflightVerdictTest(name) for name in names)
    return tests


if __name__ == '__main__':
    unittest.main()
