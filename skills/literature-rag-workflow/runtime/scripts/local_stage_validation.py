#!/usr/bin/env python3
"""Validate independently reviewed local PDF reading artifacts."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re

from note_html_integrity import validate_note_artifacts
from v21_quality import _validate_deep_read_model, load_quality_profile, model_content_hash
from v2_core import load_profile, markdown_to_zotero_html, render_reader_note, render_research_card

SHA = re.compile(r'^[0-9a-f]{64}$')
NOTE_FILES = {'reader_note': ('01_reader_note.zh.md', '08_reader_note.zotero.html'),
              'research_card': ('03_research_card.zh.md', '09_research_card.zotero.html')}


def digest(path):
    hasher = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(block)
    return hasher.hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def safe_file(path, base=None):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError('an explicit, existing non-symlink file is required')
    resolved = path.resolve(strict=True)
    if base is not None and not resolved.is_relative_to(Path(base).resolve()):
        raise ValueError('file is outside its permitted directory')
    return resolved


def stage_hashes(stage):
    stage = Path(stage)
    if not stage.is_absolute() or stage.is_symlink() or not stage.is_dir():
        raise ValueError('stage must be an existing non-symlink directory')
    hashes = {}
    for path in sorted(stage.rglob('*')):
        relative = path.relative_to(stage).as_posix()
        if path.is_symlink():
            raise ValueError('stage cannot contain symbolic links')
        # Validator caches are neither source evidence nor delivery artifacts.
        if relative == '.quality_check_cache' or relative.startswith('.quality_check_cache/'):
            continue
        if path.is_file():
            hashes[relative] = digest(path)
    return hashes



def validate_schemas(model, review):
    """Validate the checked-in schemas; an absent validator is never a pass."""
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise RuntimeError('jsonschema is required for deep-reading schema validation') from exc
    schema_dir = Path(__file__).resolve().parents[1] / 'schemas'
    for label, value, filename in (
        ('model', model, 'machine_evidence_pack.schema.json'),
        ('review', review, 'review_report_v21.schema.json'),
    ):
        schema = read(schema_dir / filename)
        Draft202012Validator.check_schema(schema)
        errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda error: list(map(str, error.absolute_path)))
        if errors:
            first = errors[0]
            location = '/'.join(map(str, first.absolute_path)) or '<root>'
            raise ValueError(f'{label} schema validation failed at {location}: {first.message}')

def validate_stage(stage, *, require_notes=True):
    stage = Path(stage).resolve(strict=True)
    required = ['02_machine_evidence_pack.json', 'review/review_report.json',
                'project_profile.json', 'quality_profile.json',
                'extraction/extraction_manifest.json', 'paper_manifest.json']
    files = required + ([name for pair in NOTE_FILES.values() for name in pair] if require_notes else [])
    for filename in files:
        safe_file(stage / filename, stage)
    model_path = stage / '02_machine_evidence_pack.json'
    model, review = read(model_path), read(stage / 'review/review_report.json')
    validate_schemas(model, review)
    profile = load_profile(stage / 'project_profile.json')
    quality = load_quality_profile(stage / 'quality_profile.json')
    if model.get('status') != 'draft' or model.get('schema_version') != '2.0':
        raise ValueError('an independently reviewed draft schema v2 model is required')
    source_pdf = safe_file(Path(model.get('source_pdf_path', '')))
    source_sha = str(model.get('source_pdf_sha256') or '')
    if not SHA.fullmatch(source_sha) or digest(source_pdf) != source_sha:
        raise ValueError('model source PDF hash mismatch')
    if (review.get('decision') != 'pass' or review.get('blocking_findings')
            or not review.get('producer_task_id') or not review.get('reviewer_task_id')
            or review['producer_task_id'] == review['reviewer_task_id']
            or review.get('model_content_sha256') != model_content_hash(model)
            or review.get('model_file_sha256') != digest(model_path)
            or review.get('source_pdf_sha256') != source_sha
            or Path(str(review.get('source_pdf_path') or '')).resolve() != source_pdf):
        raise ValueError('independent review does not bind the current source and exact model')
    errors, _warnings, _report = _validate_deep_read_model(model, profile, quality, stage,
                                                          require_reviewer=True)
    if errors:
        raise ValueError('deep-reading quality gate failed: ' + '; '.join(errors))
    if require_notes:
        validate_note_artifacts(stage)
        date = dt.datetime.fromisoformat(model['generation']['generated_at']).date().isoformat()
        for kind, (markdown, html) in NOTE_FILES.items():
            content = (stage / markdown).read_text(encoding='utf-8')
            expected = (render_reader_note(model, profile, today=date) if kind == 'reader_note'
                        else render_research_card(model, profile, today=date))
            if content != expected or (stage / html).read_text(encoding='utf-8') != markdown_to_zotero_html(content):
                raise ValueError('note is not the deterministic projection of the reviewed model: ' + kind)
    return model, source_pdf, model_content_hash(model)

