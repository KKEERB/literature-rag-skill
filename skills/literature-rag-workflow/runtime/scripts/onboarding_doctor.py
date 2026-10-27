#!/usr/bin/env python3
"""Read-only local capability probe for the literature workflow.

Checks availability only, not quality, licenses, or successful PDF processing.
No model or network service is invoked.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import sys

EXPECTED_PREFLIGHT_SHA256 = 'e4a2431fe2f2525ec755877869a93ee4ff2dd13a44f294d38ceb2e0736b5af16'
DEFAULT_PREFLIGHT = (Path(__file__).resolve().parents[1] / 'third_party' /
                     'academic-research-skills' / 'scripts' / 'pdf_read_preflight.py')

def sqlite_fts():
    try:
        with sqlite3.connect(':memory:') as connection:
            connection.execute('CREATE VIRTUAL TABLE capability_probe USING fts5(text)')
        return {'status': 'available', 'version': sqlite3.sqlite_version}
    except sqlite3.Error as exc:
        return {'status': 'unavailable', 'reason': str(exc)}


def preflight(path):
    if not path.is_file():
        return {'status': 'missing', 'sha256_matches': False}
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        return {'status': 'unavailable', 'reason': str(exc)}
    matches = digest == EXPECTED_PREFLIGHT_SHA256
    companion = (path.parent / 'audit_snapshot.py').is_file()
    status = 'version_mismatch' if not matches else ('available' if companion else 'missing_companion')
    return {'status': status, 'sha256_matches': matches,
            'companion_present': companion}


def font_candidates():
    system = platform.system()
    if system == 'Windows':
        root = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts'
        return [root / 'msyh.ttc', root / 'simsun.ttc', root / 'cambria.ttc']
    if system == 'Darwin':
        return [Path('/System/Library/Fonts/PingFang.ttc'),
                Path('/System/Library/Fonts/Supplemental/Songti.ttc'),
                Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf')]
    return []


def diagnose(upstream=None, font=None):
    module_names = ('pypdfium2', 'pypdf', 'jsonschema', 'pdfplumber', 'reportlab')
    tools = ('pdftotext', 'pdfinfo', 'pdftoppm', 'qpdf')
    upstream = upstream or DEFAULT_PREFLIGHT
    paths = [Path(font).expanduser()] if font else font_candidates()
    fonts = [str(path) for path in paths if path.is_file()]
    return {
        'schema': 'literature-onboarding-doctor/1',
        'system': {'name': platform.system(), 'python': platform.python_version(),
                   'supported_target': platform.system() == 'Darwin'},
        'sqlite_fts5': sqlite_fts(),
        'python_modules': {name: {'available': importlib.util.find_spec(name) is not None}
                           for name in module_names},
        'executables': {name: {'available': shutil.which(name) is not None} for name in tools},
        'preflight': preflight(upstream),
        'fonts': {'candidates_found': fonts,
                  'status': 'candidates_only' if fonts else 'not_found',
                  'visual_and_glyph_check': 'not_performed'},
        'codex_model_availability': 'not_checked',
        'notes': ['qpdf is conditional for certain PDFs',
                  'preflight also requires its upstream companion dependencies',
                  'font file presence does not prove embedding or glyph coverage',
                  'no model, source PDF, database, or scientific claim was checked'],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--font', type=Path, help='Check a user-provided font file')
    args = parser.parse_args()
    print(json.dumps(diagnose(font=args.font), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
