"""Poppler encoding and local-only diagnostic regression tests."""
from __future__ import annotations

import http.server
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import audit_knowledge_integrity as audit
import common
import onboarding_doctor as doctor


class EncodingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('pdftotext') and shutil.which('pdfinfo'), 'Poppler unavailable')
    def test_utf8_poppler_output_under_ascii_locale_and_unicode_path(self):
        from reportlab.pdfgen import canvas
        with tempfile.TemporaryDirectory(prefix='space-') as tmp:
            folder = Path(tmp) / '\u4e2d\u6587 space'
            folder.mkdir()
            pdf = folder / '\u6e90.pdf'
            document = canvas.Canvas(str(pdf), pagesize=(595, 842))
            document.drawString(72, 770, 'Synthetic caf\u00e9 test')
            document.setTitle('caf\u00e9')
            document.save()
            code = '''\
import sys
import hashlib
import subprocess
from pathlib import Path
from audit_knowledge_integrity import fresh_page_text, pdf_pages
from common import pdftotext, pdfinfo
from prepare_knowledge_revision import extract_source_region
path = Path(sys.argv[1])
fresh = fresh_page_text(path, 1, 'normal')
text, warnings = pdftotext(path)
info, info_warnings = pdfinfo(path)
assert fresh is not None and 'caf\\u00e9' in fresh
assert 'caf\\u00e9' in text and not warnings
assert info['Pages'] == '1' and info['Title'] == 'caf\\u00e9' and not info_warnings
assert pdf_pages(path) == (1, '')
command = ['pdftotext', '-enc', 'UTF-8', '-f', '1', '-l', '1', '-r', '72',
           '-x', '0', '-y', '0', '-W', '500', '-H', '200', str(path), '-']
raw = subprocess.run(command, capture_output=True, check=True).stdout
region = {'extractor': 'pdftotext_crop_v1', 'pdf_page_index': 1,
          'bbox_72dpi': [0, 0, 500, 200], 'text_sha256': hashlib.sha256(raw).hexdigest()}
cropped, check = extract_source_region(path, region)
assert 'caf\\u00e9' in cropped and check['command'] == command
'''
            env = dict(os.environ, LC_ALL='C', PYTHONUTF8='0', PYTHONCOERCECLOCALE='0', PYTHONPATH=str(SCRIPTS))
            result = subprocess.run([sys.executable, '-c', code, str(pdf)], env=env, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))

    def test_bad_utf8_is_a_failure_not_source_text(self):
        error = UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid start byte')
        with patch.object(audit.shutil, 'which', return_value='pdftotext'), \
             patch.object(audit.subprocess, 'run', side_effect=error):
            self.assertIsNone(audit.fresh_page_text(Path('synthetic.pdf'), 1, 'normal'))
        result = common.run_command([sys.executable, '-c', "import sys; sys.stdout.buffer.write(b'\\xff')"],
                                    encoding='utf-8')
        self.assertFalse(result['ok'])
        self.assertIsNone(result['returncode'])


if __name__ == '__main__':
    unittest.main()
