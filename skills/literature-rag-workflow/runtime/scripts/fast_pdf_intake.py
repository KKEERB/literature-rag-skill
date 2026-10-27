#!/usr/bin/env python3
"""Two independent Codex rounds, local PDF evidence only; no model API or Zotero writes.

prepare is filesystem-only. publish previews by default; --confirmed commits one
verified subset transaction with the existing compressed recovery mechanism.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(os.environ.get('LITERATURE_RAG_HOME', str(Path.home() / '.literature-rag-workflow'))).expanduser() / 'knowledge.sqlite3'
PREFLIGHT = ROOT / 'scripts/fast_pdf_preflight.py'
CHECKS = ('whole_assertion', 'conditions_scope', 'numbers_units', 'attribution', 'epistemic_layer', 'compound_assertion')
VERSION = 'fast-pdf-intake/1'


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def object_sha(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_once(path, value):
    path = Path(path)
    data = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding='utf-8') != data:
            raise ValueError('immutable artifact already exists with different content: ' + str(path))
        return
    with path.open('x', encoding='utf-8') as stream:
        stream.write(data)


def normalize_doi(value):
    value = str(value or '').strip().casefold()
    value = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', value)
    return value.rstrip('.,;')


def database_sources(database):
    database = Path(database)
    if not database.is_file():
        raise ValueError('existing database required: ' + str(database))
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as con:
        con.row_factory = sqlite3.Row
        return [dict(row) for row in con.execute('SELECT * FROM sources WHERE tombstoned=0')]


def validate_project_profile(value):
    """Validate against the same installed profile contract used by deep reading."""
    if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', value):
        raise ValueError('explicit safe project profile required')
    path = ROOT / 'profiles' / (value + '.json')
    if path.is_symlink() or not path.is_file():
        raise ValueError('project profile is unavailable: ' + value)
    from v2_core import load_profile
    profile = load_profile(path)
    if profile.get('project_id') != value:
        raise ValueError('project profile identity does not match its filename')
    return value


def preflight(path):
    if not PREFLIGHT.is_file():
        return {'verdict': 'UNAVAILABLE', 'warnings': ['read-integrity preflight unavailable']}
    # Run its public CLI in a clean process: this workflow also has a scripts
    # package, so importing the vendored scripts package in-process is ambiguous.
    result = subprocess.run([sys.executable, str(PREFLIGHT), str(path)],
                            capture_output=True, check=True, timeout=120,
                            cwd=PREFLIGHT.parent.parent)
    return json.loads(result.stdout.decode('utf-8'))


def extractor_identity():
    results = {}
    for program in ('pdftotext', 'pdfinfo'):
        result = subprocess.run([program, '-v'], capture_output=True, timeout=15, check=True)
        results[program] = (result.stdout + result.stderr).decode(errors='replace').splitlines()[0]
    try:
        results['pypdf'] = importlib.metadata.version('pypdf')
    except importlib.metadata.PackageNotFoundError:
        results['pypdf'] = 'UNAVAILABLE'
    try:
        qpdf_version = subprocess.run(['qpdf', '--version'], capture_output=True, timeout=15, check=True)
        results['qpdf'] = qpdf_version.stdout.decode(errors='replace').splitlines()[0]
    except (OSError, subprocess.SubprocessError):
        results['qpdf'] = 'UNAVAILABLE'
    return {'versions': results, 'routes': ['normal', 'layout'], 'encoding': 'UTF-8',
            'page_order': 'pdftotext form-feed, 1-based PDF pages', 'version': VERSION,
            'preflight_sha256': sha(PREFLIGHT)}


def extract_object(pdf, storage_root, max_pages):
    """One extraction per exact PDF and extractor configuration; retain invalid preflight."""
    pdf = Path(pdf).resolve()
    pdf_sha = sha(pdf)
    object_dir = Path(storage_root).resolve() / 'objects' / pdf_sha
    object_dir.mkdir(parents=True, exist_ok=True)
    retained = object_dir / 'source.pdf'
    if not retained.exists():
        # Exclusive create protects a concurrent prepare from replacing provenance.
        try:
            with retained.open('xb') as dest, pdf.open('rb') as source:
                shutil.copyfileobj(source, dest)
        except FileExistsError:
            pass
    if sha(retained) != pdf_sha or sha(pdf) != pdf_sha:
        raise ValueError('source changed during stable-copy preparation')
    identity = extractor_identity()
    cache_key = object_sha(identity)
    cache = object_dir / 'cache' / cache_key
    manifest_path = cache / 'extraction_manifest.json'
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        verify_extraction(manifest_path, pdf_sha)
        if manifest['page_count'] > max_pages:
            raise ValueError('page_limit_exceeded: ' + str(manifest['page_count']))
        return manifest_path, manifest, True
    cache.mkdir(parents=True, exist_ok=True)
    # Preflight reports carry timestamps. Reuse the immutable report for the
    # exact PDF+tool identity, including FAIL/UNAVAILABLE; never erase an earlier
    # integrity failure by replacing its sidecar on a resume.
    check_path = cache / 'preflight.json'
    check = read_json(check_path) if check_path.exists() else preflight(retained)
    if check.get('sha256') and check['sha256'] != pdf_sha:
        raise ValueError('preflight input SHA changed')
    write_once(check_path, check)
    if check.get('verdict') != 'PASS':
        raise ValueError('preflight_' + str(check.get('verdict')) + ': ' + '; '.join(check.get('warnings', [])))
    count = check.get('reader_page_count')
    if type(count) is not int or count < 1:
        raise ValueError('invalid page count')
    if count > max_pages:
        raise ValueError('page_limit_exceeded: ' + str(count))
    started = time.perf_counter()
    texts = {}
    full_hashes = {}
    for route in ('normal', 'layout'):
        result = subprocess.run(['pdftotext'] + ([] if route == 'normal' else ['-layout']) +
                                ['-enc', 'UTF-8', str(retained), '-'], capture_output=True,
                                check=True, timeout=120)
        raw = result.stdout
        text = raw.decode('utf-8', errors='strict')
        pages = text.split('\f')
        if pages and not pages[-1].strip():
            pages.pop()
        if len(pages) != count:
            raise ValueError('extracted page count differs from trusted PDF tree')
        texts[route] = pages
        full = cache / (route + '.txt')
        full.write_bytes(raw)
        full_hashes[route] = sha(full)
    # A low-text page can be a substantive figure. Keep it and flag it; entirely
    # image-only/scanned documents are exceptions rather than silently OCRed.
    if sum(len(p.strip()) for p in texts['normal']) < max(120, count * 40):
        raise ValueError('scan_or_unreadable_text: insufficient whole-document text')
    from v21_quality import text_checksum
    records = []
    for index in range(count):
        entry = {'pdf_page_index': index + 1, 'routes': {}}
        for route in ('normal', 'layout'):
            relative = f'pages/{index + 1:04d}-{route}.txt'
            target = cache / relative
            target.parent.mkdir(exist_ok=True)
            target.write_text(texts[route][index], encoding='utf-8')
            entry['routes'][route] = {'path': relative, 'sha256': sha(target),
                                      'normalized_sha256': text_checksum(texts[route][index])}
        entry['low_text'] = len(texts['normal'][index].strip()) < 40
        records.append(entry)
    manifest = {'schema': 'fast-pdf-extraction/1', 'source_pdf_sha256': pdf_sha,
                'source_pdf_path': str(retained), 'extractor': identity, 'cache_key': cache_key,
                'page_count': count, 'pages': records, 'normal_text_path': str(cache / 'normal.txt'),
                'layout_text_path': str(cache / 'layout.txt'), 'full_text_sha256': full_hashes,
                'preflight_path': str(cache / 'preflight.json'), 'preflight_sha256': sha(cache / 'preflight.json'),
                'extraction_seconds': time.perf_counter() - started, 'created_at': now()}
    write_once(cache / 'pages.json', records)
    manifest['pages_path'] = str(cache / 'pages.json')
    manifest['pages_sha256'] = sha(cache / 'pages.json')
    write_once(manifest_path, manifest)
    return manifest_path, manifest, False


def verify_extraction(path, expected_sha):
    path = Path(path)
    manifest = read_json(path)
    if manifest.get('source_pdf_sha256') != expected_sha or sha(manifest['source_pdf_path']) != expected_sha:
        raise ValueError('retained PDF binding changed')
    for route in ('normal', 'layout'):
        if sha(manifest[route + '_text_path']) != manifest['full_text_sha256'][route]:
            raise ValueError('cached text changed: ' + route)
    if sha(manifest['preflight_path']) != manifest['preflight_sha256'] or read_json(manifest['preflight_path']).get('verdict') != 'PASS':
        raise ValueError('preflight cache changed or not PASS')
    if sha(manifest['pages_path']) != manifest['pages_sha256']:
        raise ValueError('page index changed')
    for page in manifest['pages']:
        for route, record in page['routes'].items():
            if sha(path.parent / record['path']) != record['sha256']:
                raise ValueError('cached page changed')
    return manifest


def _render_page_to_png(pdf_path, page_number, page_count, destination):
    """Render one page with PDFium; source identity is checked by the caller."""
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError('PDF page rendering requires pypdfium2') from exc
    try:
        with pdfium.PdfDocument(str(pdf_path)) as document:
            if len(document) != page_count:
                raise ValueError('PDF page count changed')
            page = document[page_number - 1]
            try:
                bitmap = page.render(scale=101 / 72)
                try:
                    bitmap.to_pil().save(str(destination), format='PNG')
                finally:
                    bitmap.close()
            finally:
                page.close()
    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError('PDFium page rendering failed') from exc


def render_pdf_pages(source, pages):
    """Explicit helper for a model's actual visual inspection; never attests reading."""
    manifest = verify_extraction(source['extraction_manifest_path'], source['pdf_sha256'])
    pages = list(pages)
    if any(type(page) is not int or not 1 <= page <= manifest['page_count'] for page in pages):
        raise ValueError('invalid render page')
    result = []
    for page in sorted(set(pages)):
        target = Path(source['extraction_manifest_path']).parent / 'rendered' / f'page-{page:04d}.png'
        target.parent.mkdir(exist_ok=True)
        if target.parent.is_symlink() or target.is_symlink():
            raise ValueError('render target cannot be a symlink')
        if not target.exists():
            with tempfile.TemporaryDirectory(prefix='.render-', dir=target.parent) as temp:
                rendered = Path(temp) / 'page.png'
                _render_page_to_png(manifest['source_pdf_path'], page, manifest['page_count'], rendered)
                if not rendered.is_file():
                    raise RuntimeError('PDFium page rendering failed: no image produced')
                # Do not commit an image if its source or extraction changed during rendering.
                verify_extraction(source['extraction_manifest_path'], source['pdf_sha256'])
                try:
                    os.link(rendered, target)
                except FileExistsError:
                    if target.is_symlink() or sha(target) != sha(rendered):
                        raise ValueError('concurrent render target differs')
        if not target.is_file():
            raise ValueError('render target is not a PNG')
        with target.open('rb') as stream:
            signature = stream.read(8)
        if signature != b'\x89PNG\r\n\x1a\n':
            raise ValueError('render target is not a PNG')
        result.append({'page': page, 'path': str(target), 'sha256': sha(target)})
    verify_extraction(source['extraction_manifest_path'], source['pdf_sha256'])
    return result


def _input_records(inputs, manifest):
    records = []
    if manifest:
        payload = read_json(manifest)
        if isinstance(payload, dict):
            payload = payload.get('inputs', payload.get('jobs'))
        if not isinstance(payload, list):
            raise ValueError('manifest must be an array or {inputs:[...]}')
        for item in payload:
            item = {'path': item} if isinstance(item, str) else dict(item)
            path = Path(item['path']).expanduser()
            item['path'] = str((Path(manifest).resolve().parent / path).resolve() if not path.is_absolute() else path.resolve())
            records.append(item)
    for input_path in inputs or []:
        path = Path(input_path).expanduser().resolve()
        records.extend({'path': str(p)} for p in sorted(p for p in path.rglob('*') if p.is_file() and p.suffix.casefold() == '.pdf')) if path.is_dir() else records.append({'path': str(path)})
    if not records:
        raise ValueError('nonempty PDF inputs required')
    return records


def prepare(inputs, manifest, output, database, storage_root=None, max_pages=40, project_profile=None, model_settings=None):
    output, database = Path(output).resolve(), Path(database).resolve()
    if type(max_pages) is not int or max_pages < 1:
        raise ValueError('positive max-pages required')
    storage_root = Path(storage_root or database.parent / 'fast_pdf').resolve()
    records = _input_records(inputs, manifest)
    if project_profile is not None:
        project_profile = str(project_profile).strip()
        if not project_profile:
            raise ValueError('nonempty explicit project-profile required')
        for record in records:
            if record.get('project_profile') and record['project_profile'] != project_profile:
                raise ValueError('command project-profile conflicts with explicit manifest profile')
            record['project_profile'] = project_profile
    config = {'inputs': records, 'database': str(database), 'storage_root': str(storage_root), 'max_pages': max_pages}
    batch_path = output / 'batch.json'
    if batch_path.exists():
        previous = read_json(batch_path)
        frozen = previous.get('model_settings')
        binding_config = dict(config)
        if frozen is not None:
            from model_settings import validate
            validate(frozen)
            binding_config['model_settings'] = frozen
            if model_settings is not None and model_settings != frozen:
                raise ValueError('existing batch model settings differ; use a new batch path')
        elif model_settings is not None:
            raise ValueError('existing legacy batch has frozen model settings; do not replace them')
        if previous.get('prepare_binding') != object_sha(binding_config):
            raise ValueError('existing batch input/config differs; use a new batch path')
        return batch_status(output)
    if model_settings is None:
        raise ValueError('model settings required for a new batch; select both rounds first')
    from model_settings import validate
    settings = validate(model_settings)
    # Do this before any PDF extraction or database write: every new entry
    # must be eligible for the same installed profile used by deep reading.
    for record in records:
        validate_project_profile(record.get('project_profile'))
    config['model_settings'] = settings
    sources = database_sources(database)
    indexed_sha = {s['source_pdf_sha256'] for s in sources if s.get('source_pdf_sha256')}
    seen = set()
    jobs = []
    for number, record in enumerate(records, 1):
        began = time.perf_counter()
        job = {'ordinal': number, 'input_path': record['path'], 'state': 'blocked', 'reason': '', 'prepared_at': now()}
        try:
            pdf = Path(record['path'])
            digest = sha(pdf)
            job['pdf_sha256'] = digest
            expected_sha = record.get('expected_pdf_sha256')
            if expected_sha is not None:
                if not isinstance(expected_sha, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_sha):
                    raise ValueError('invalid expected_pdf_sha256')
                if expected_sha != digest:
                    raise ValueError('expected_pdf_sha256 does not match source PDF')
            if digest in indexed_sha:
                job.update(state='reused', reason='source_sha_already_indexed')
                jobs.append(job)
                continue
            if digest in seen:
                job.update(state='reused', reason='duplicate_input_sha')
                jobs.append(job)
                continue
            seen.add(digest)
            extraction_path, extracted, cache_hit = extract_object(pdf, storage_root, max_pages)
            # The source may be replaced between the initial digest and the
            # extractor's own copy. Never dispatch a task for a different PDF.
            extraction_descriptor = read_json(extraction_path)
            if (extracted.get('source_pdf_sha256') != digest
                    or extraction_descriptor.get('source_pdf_sha256') != digest
                    or sha(extracted['source_pdf_path']) != digest
                    or sha(extraction_descriptor['source_pdf_path']) != digest
                    or sha(pdf) != digest):
                raise ValueError('source PDF changed between initial hash and extraction')
            first_page = (extraction_path.parent / extracted['pages'][0]['routes']['normal']['path']).read_text(encoding='utf-8')
            matches = re.findall(r'10\.\d{4,9}/[^\s<>"\[\]]+', first_page, flags=re.I)
            doi = normalize_doi(record.get('doi') or (matches[0] if matches else ''))
            collisions = [s['source_id'] for s in sources if doi and normalize_doi(s.get('doi')) == doi and s.get('source_pdf_sha256') != digest]
            profile = str(record.get('project_profile') or '')
            if not profile:
                raise ValueError('missing explicit project_profile (no SXI default)')
            if collisions:
                raise ValueError('doi_collision_different_pdf: ' + ','.join(collisions))
            job_dir = output / 'jobs' / digest
            source = {'schema': 'fast-pdf-source/1', 'pdf_sha256': digest, 'source_id': 'pdf:' + digest,
                      'input_path': str(pdf.resolve()), 'source_pdf_path': extracted['source_pdf_path'],
                      'extraction_manifest_path': str(extraction_path), 'extraction_manifest_sha256': sha(extraction_path),
                      'page_count': extracted['page_count'], 'project_profile': profile, 'doi': doi,
                      'metadata': record, 'created_at': now(), 'cache_hit': cache_hit,
                      'model_settings': settings, 'model_settings_sha256': object_sha(settings),
                      'execution_model_identity': 'NOT_VERIFIED',
                      'extraction_seconds': extracted['extraction_seconds'] if not cache_hit else 0.0}
            producer_task = {'schema': 'fast-pdf-task/1', 'stage': 'analysis', **settings['analysis'],
                'execution_model_identity': 'NOT_VERIFIED',
                'source_json': str(job_dir / 'source.json'), 'output': str(job_dir / 'analysis.json'),
                'schema_path': str(ROOT / 'schemas/fast_pdf_analysis.schema.json'),
                'requirements': ['Read all original cached pages, not prior notes.', 'Preserve conditions, numbers, units, negatives and epistemic layers.',
                    'Cover sections, all substantive figures/tables and equations; caption-only is not image inspection.', 'Use exact continuous original passages and 1-based PDF page indices.',
                    'Compact structured entries, no fixed item count and no Zotero/translation/Reader Note.', 'Freeze output once complete; actual task identity required.']}
            reviewer_task = {'schema': 'fast-pdf-task/1', 'stage': 'review', **settings['review'],
                'execution_model_identity': 'NOT_VERIFIED',
                'source_json': str(job_dir / 'source.json'), 'analysis': str(job_dir / 'analysis.json'), 'output': str(job_dir / 'review.json'),
                'schema_path': str(ROOT / 'schemas/fast_pdf_review.schema.json'),
                'requirements': ['Independent task with no producer history; first recover inventory from original full text.',
                    'Read all original pages; compare original sections/results/figures with first-round coverage.',
                    'Every item needs all six checks and original numbered continuous passages.', 'Any newly proposed assertions remain gaps, never self-authored verified entries.',
                    'Inspect substantive images as needed; record only actually seen cached renders.', 'Do not rewrite analysis or silently start a third round.']}
            source['task_sha256'] = {'analysis': object_sha(producer_task), 'review': object_sha(reviewer_task)}
            write_once(job_dir / 'source.json', source)
            write_once(job_dir / 'producer-task.json', producer_task)
            write_once(job_dir / 'reviewer-task.json', reviewer_task)
            job.update(state='prepared', reason='', job_dir=str(job_dir), source_json_sha256=sha(job_dir / 'source.json'), cache_hit=cache_hit)
        except (OSError, ValueError, subprocess.SubprocessError, ImportError) as exc:
            job.update(state='blocked', reason=str(exc))
        job['prepare_seconds'] = time.perf_counter() - began
        jobs.append(job)
    batch = {'schema': 'fast-pdf-batch/1', 'version': VERSION, 'created_at': now(), 'prepare_binding': object_sha(config),
             **config, 'jobs': jobs, 'models': {stage: [settings[stage]['model'], settings[stage]['reasoning_effort']] for stage in ('analysis', 'review')},
             'max_concurrent_model_tasks': 3, 'zotero_modified': False}
    write_once(batch_path, batch)
    return batch_status(output)


def validate_batch_binding(batch, batch_dir):
    """Check portable batch, sources and task descriptors before use.

    These hashes detect binding conflicts; they do not attest model execution.
    Historical batches keep their prior structure and checks.
    """
    settings = batch.get('model_settings')
    if settings is None:
        try:
            legacy_config = {key: batch[key] for key in ('inputs', 'database', 'storage_root', 'max_pages')}
        except KeyError as exc:
            raise ValueError('legacy batch binding missing required field') from exc
        if batch.get('prepare_binding') != object_sha(legacy_config):
            raise ValueError('legacy batch input/config binding changed')
        for job in batch['jobs']:
            if job.get('state') == 'prepared':
                source_path = Path(job['job_dir']) / 'source.json'
                if sha(source_path) != job.get('source_json_sha256'):
                    raise ValueError('immutable legacy source descriptor changed')
                if read_json(source_path).get('model_settings') is not None:
                    raise ValueError('configured source cannot be treated as a legacy batch')
        return
    from model_settings import validate
    validate(settings)
    try:
        expected = {key: batch[key] for key in ('inputs', 'database', 'storage_root', 'max_pages', 'model_settings')}
    except KeyError as exc:
        raise ValueError('batch binding missing required field') from exc
    if batch.get('prepare_binding') != object_sha(expected):
        raise ValueError('batch input/config binding changed')
    models = {stage: [settings[stage]['model'], settings[stage]['reasoning_effort']] for stage in ('analysis', 'review')}
    if batch.get('models') != models:
        raise ValueError('batch model summary differs from frozen settings')
    seen = set()
    for job in batch['jobs']:
        if job.get('state') != 'prepared':
            continue
        directory = Path(job['job_dir']).resolve()
        digest = job.get('pdf_sha256')
        if directory != (Path(batch_dir).resolve() / 'jobs' / str(digest)).resolve() or directory in seen:
            raise ValueError('prepared job path or identity differs from batch')
        seen.add(directory)
        source_path = directory / 'source.json'
        if sha(source_path) != job.get('source_json_sha256'):
            raise ValueError('immutable source descriptor changed')
        source = read_json(source_path)
        ordinal = job.get('ordinal')
        if type(ordinal) is not int or not 1 <= ordinal <= len(batch['inputs']):
            raise ValueError('prepared job ordinal differs from batch')
        if (source.get('pdf_sha256') != digest or source.get('metadata') != batch['inputs'][ordinal - 1]
                or source.get('model_settings') != settings):
            raise ValueError('source input/model settings differ from batch')
        validate_task_bindings(directory, source)


def validate_task_bindings(directory, source):
    settings = source.get('model_settings')
    if settings is None:
        return
    from model_settings import validate
    validate(settings)
    if source.get('model_settings_sha256') != object_sha(settings):
        raise ValueError('immutable requested model settings changed')
    task_hashes = source.get('task_sha256')
    if not isinstance(task_hashes, dict) or set(task_hashes) != {'analysis', 'review'}:
        raise ValueError('source is missing frozen task bindings')
    for stage, filename in (('analysis', 'producer-task.json'), ('review', 'reviewer-task.json')):
        task = read_json(directory / filename)
        if object_sha(task) != task_hashes[stage]:
            raise ValueError(f'{stage} immutable task binding changed')
        if task.get('stage') != stage or any(task.get(key) != value for key, value in settings[stage].items()):
            raise ValueError(f'{stage} requested settings differ from frozen source')
        expected = {'source_json': directory / 'source.json', 'output': directory / (stage + '.json'),
                    'schema_path': ROOT / 'schemas' / ('fast_pdf_analysis.schema.json' if stage == 'analysis' else 'fast_pdf_review.schema.json')}
        if stage == 'review':
            expected['analysis'] = directory / 'analysis.json'
        if any(Path(task.get(key, '')).resolve() != path.resolve() for key, path in expected.items()):
            raise ValueError(f'{stage} task file binding changed')


def batch_status(batch_dir):
    batch = read_json(Path(batch_dir) / 'batch.json')
    validate_batch_binding(batch, batch_dir)
    jobs = []
    for original in batch['jobs']:
        job = dict(original)
        if job.get('job_dir'):
            directory = Path(job['job_dir'])
            publications = [read_json(path) for path in sorted(directory.glob('publish-receipt-*.json'))]
            job['publications'] = publications
            target_database = str(Path(batch['database']).expanduser().resolve())
            if any(p.get('database') == target_database for p in publications):
                job['stage'] = 'published_to_batch_database'
            elif (directory / 'review.json').exists():
                job['stage'] = 'review_available_unvalidated'
            elif (directory / 'analysis.json').exists():
                job['stage'] = 'awaiting_review'
            else:
                job['stage'] = 'awaiting_analysis'
            for filename, label in [('analysis.json', 'analysis'), ('review.json', 'review')]:
                path = directory / filename
                if path.exists():
                    try:
                        document = read_json(path)
                    except (OSError, ValueError) as exc:
                        job.setdefault('staging_errors', []).append({'file': str(path), 'reason': str(exc)})
                        job['stage'] = 'interrupted_staging'
                        continue
                    job[label + '_sha256'] = sha(path)
                    job[label + '_metrics'] = document.get('metrics', {'token_usage': 'UNAVAILABLE'})
                    who = 'producer' if label == 'analysis' else 'reviewer'
                    job[label + '_output_declaration'] = document.get(who)
                    job[label + '_output_declaration_is_execution_attestation'] = False
                record_path = directory / (label + '-dispatch-record.json')
                if record_path.exists():
                    job[label + '_dispatch_record'] = read_json(record_path)
            if batch.get('model_settings') is not None:
                missing_records = [stage for stage in ('analysis', 'review')
                                   if (directory / (stage + '.json')).exists()
                                   and not (directory / (stage + '-dispatch-record.json')).is_file()]
                if missing_records:
                    job['missing_dispatch_records'] = missing_records
                    if not job.get('staging_errors'):
                        job['stage'] = 'dispatch_record_missing'
            if (directory / 'review.json').exists() and not job.get('staging_errors'):
                review = read_json(directory / 'review.json')
                job['decisions'] = {state: sum(d.get('state') == state for d in review.get('decisions', []))
                                    for state in ['verified', 'needs_review', 'quarantined']}
                job['coverage_status'] = review.get('coverage_status')
                job['gaps'] = review.get('gaps', [])
        jobs.append(job)
    stages = sorted(set(j.get('stage', j['state']) for j in jobs))
    return {'schema': 'fast-pdf-status/1', 'batch_dir': str(Path(batch_dir).resolve()), 'jobs': jobs,
            'requested_model_settings': batch.get('model_settings', batch.get('models')),
            'execution_model_identity': 'NOT_VERIFIED',
            'counts': {state: sum(j.get('stage', j['state']) == state for j in jobs) for state in stages},
            'token_usage': 'UNAVAILABLE unless actual per-task metrics supplied', 'zotero_modified': False}


def schema_validate(document, name):
    import jsonschema
    jsonschema.Draft202012Validator(read_json(ROOT / 'schemas' / name)).validate(document)


def record_dispatch(job_dir, stage, metadata):
    """Save caller-supplied host dispatch metadata without claiming model identity.

    The caller should supply only metadata actually returned by the host. Model
    output or a model's own assertion is not a host execution receipt.
    """
    if stage not in ('analysis', 'review'):
        raise ValueError('stage must be analysis or review')
    directory = Path(job_dir).resolve()
    batch_dir = directory.parent.parent
    batch = read_json(batch_dir / 'batch.json')
    validate_batch_binding(batch, batch_dir)
    if not any(job.get('state') == 'prepared' and Path(job['job_dir']).resolve() == directory for job in batch['jobs']):
        raise ValueError('dispatch job is not bound to batch')
    source = read_json(directory / 'source.json')
    requested = source.get('model_settings', {}).get(stage)
    if not requested:
        raise ValueError('dispatch records require a batch with explicit model settings')
    allowed = {'task_id', 'host_tool', 'observed_model', 'observed_reasoning_effort'}
    if not isinstance(metadata, dict) or set(metadata) - allowed:
        raise ValueError('invalid dispatch metadata fields')
    for key in ('task_id', 'host_tool'):
        if not isinstance(metadata.get(key), str) or not metadata[key].strip():
            raise ValueError('task_id and host_tool are required')
    for key, setting in (('observed_model', 'model'), ('observed_reasoning_effort', 'reasoning_effort')):
        if metadata.get(key) is not None and metadata[key] != requested[setting]:
            raise ValueError('dispatch metadata conflicts with requested settings')
    record = {'schema': 'fast-pdf-dispatch-record/1', 'stage': stage, 'requested': requested,
              'caller_supplied_host_metadata': metadata,
              'underlying_model_identity': 'NOT_VERIFIED'}
    path = Path(job_dir) / (stage + '-dispatch-record.json')
    write_once(path, record)
    return record


def _page_passage(passage, indices, pages, alternate_pages=None):
    sections = re.split(r'PDF PAGE (\d+)\n', passage)
    if sections[0].strip() or len(sections) < 3 or len(sections) % 2 != 1:
        raise ValueError('passage must bind actual numbered PDF pages')
    located = []
    for offset in range(1, len(sections), 2):
        page, body = int(sections[offset]), sections[offset + 1]
        if page not in indices or not 1 <= page <= len(pages) or not body.strip():
            raise ValueError('invalid passage locator')
        candidates = [pages[page - 1]]
        if alternate_pages is not None:
            candidates.append(alternate_pages[page - 1])
        if not any(re.sub(r'\s+', '', body) in re.sub(r'\s+', '', text) for text in candidates):
            raise ValueError('recorded passage absent from original page')
        located.append(page)
    if set(located) != set(indices):
        raise ValueError('passage does not cover every stated original page')


def _evidence_pages(evidence, pages, layout_pages):
    """Select complete cached reading orders, never splice text on one page."""
    route = evidence.get('text_route')
    if route == 'mixed':
        indices = evidence['pdf_page_indices']
        routes = evidence.get('page_text_routes')
        if not evidence['quote'].startswith('PDF PAGE ') or not isinstance(routes, dict):
            raise ValueError('mixed route needs numbered passages and explicit page routes')
        if set(routes) != {str(page) for page in indices} or any(value not in ('normal', 'layout') for value in routes.values()):
            raise ValueError('mixed route must select one reading order per cited page')
        selected = list(pages)
        for page in indices:
            selected[page - 1] = (layout_pages if routes[str(page)] == 'layout' else pages)[page - 1]
        return selected, None
    if route not in (None, 'normal', 'layout'):
        raise ValueError('unknown evidence text route')
    return (layout_pages if route == 'layout' else pages), (layout_pages if route is None else None)


def validate_job(job_dir, expected_source_json_sha=None):
    directory = Path(job_dir).resolve()
    source_path = directory / 'source.json'
    if expected_source_json_sha and sha(source_path) != expected_source_json_sha:
        raise ValueError('immutable source descriptor changed')
    source = read_json(source_path)
    settings = source.get('model_settings')
    validate_task_bindings(directory, source)
    extraction_path = Path(source['extraction_manifest_path'])
    if sha(extraction_path) != source['extraction_manifest_sha256']:
        raise ValueError('extraction manifest changed')
    extraction = verify_extraction(extraction_path, source['pdf_sha256'])
    original = Path(source['input_path'])
    if original.exists() and sha(original) != source['pdf_sha256']:
        raise ValueError('original input PDF changed')
    analysis_path, review_path = directory / 'analysis.json', directory / 'review.json'
    analysis, review = read_json(analysis_path), read_json(review_path)
    schema_validate(analysis, 'fast_pdf_analysis.schema.json')
    schema_validate(review, 'fast_pdf_review.schema.json')
    if settings is not None:
        if analysis['producer']['task_id'] == review['reviewer']['task_id']:
            raise ValueError('independent second-round reviewer required')
        for stage, document, who in (('analysis', analysis, 'producer'), ('review', review, 'reviewer')):
            if any(document[who].get(key) != value for key, value in settings[stage].items()):
                raise ValueError(f'{stage} output declaration differs from requested settings')
            record_path = directory / (stage + '-dispatch-record.json')
            if not record_path.is_file():
                raise ValueError(f'{stage} host dispatch record required for configured batch')
            record = read_json(record_path)
            if (record.get('schema') != 'fast-pdf-dispatch-record/1'
                    or record.get('requested') != settings[stage] or record.get('stage') != stage
                    or record.get('underlying_model_identity') != 'NOT_VERIFIED'):
                raise ValueError(f'{stage} dispatch record differs from frozen settings')
            metadata = record.get('caller_supplied_host_metadata')
            allowed = {'task_id', 'host_tool', 'observed_model', 'observed_reasoning_effort'}
            if (not isinstance(metadata, dict) or set(metadata) - allowed
                    or any(not isinstance(metadata.get(key), str) or not metadata[key].strip()
                           for key in ('task_id', 'host_tool'))):
                raise ValueError(f'{stage} invalid host dispatch metadata')
            if metadata['task_id'] != document[who]['task_id']:
                raise ValueError(f'{stage} output task id differs from dispatch record')
            if any(metadata.get(key) not in (None, settings[stage][field]) for key, field in
                   (('observed_model', 'model'), ('observed_reasoning_effort', 'reasoning_effort'))):
                raise ValueError(f'{stage} dispatch report conflicts with requested settings')
    else:
        # The prior /1 schemas required these exact declarations.  Preserve that
        # historical gate without treating model-authored metadata as a receipt.
        for stage, document, who, effort in (('analysis', analysis, 'producer', 'medium'),
                                              ('review', review, 'reviewer', 'high')):
            if document[who]['model'] != 'gpt-6.1-sol' or document[who]['reasoning_effort'] != effort:
                raise ValueError(f'{stage} legacy output declaration differs from its historical schema')
    if analysis['source_pdf_sha256'] != source['pdf_sha256'] or review['source_pdf_sha256'] != source['pdf_sha256']:
        raise ValueError('model inputs bind a different PDF')
    if review['analysis_sha256'] != sha(analysis_path):
        raise ValueError('first-round analysis changed after review')
    if analysis['producer']['task_id'] == review['reviewer']['task_id']:
        raise ValueError('independent second-round reviewer required')
    all_pages = list(range(1, source['page_count'] + 1))
    if sorted(analysis['coverage']['pages_read']) != all_pages or sorted(review['independent_inventory']['pages_read']) != all_pages:
        raise ValueError('both rounds must explicitly cover the whole original PDF')
    pages = Path(extraction['normal_text_path']).read_text(encoding='utf-8').split('\f')[:source['page_count']]
    layout_pages = Path(extraction['layout_text_path']).read_text(encoding='utf-8').split('\f')[:source['page_count']]
    items = analysis['items']
    item_ids = [item['id'] for item in items]
    decision_ids = [item['item_id'] for item in review['decisions']]
    if len(set(item_ids)) != len(item_ids) or len(set(decision_ids)) != len(decision_ids) or set(item_ids) != set(decision_ids):
        raise ValueError('exactly one review decision required per first-round item')
    for category in ('sections', 'figures_tables', 'equations'):
        for unit in analysis['coverage'][category]:
            if any(page not in all_pages for page in unit['pages']):
                raise ValueError('invalid coverage page')
    for item in items:
        decision = next(d for d in review['decisions'] if d['item_id'] == item['id'])
        if decision['state'] != 'verified':
            # Preserve a bad first-round locator verbatim in staging. It is not
            # an accepted anchor and is absent from final items/chunks/FTS. The
            # independent review must still provide a valid original passage
            # below, and source/cache/hash/whole-read checks remain global.
            continue
        indices, quote = item['evidence']['pdf_page_indices'], item['evidence']['quote']
        if any(page not in all_pages for page in indices):
            raise ValueError('invalid first-round page locator')
        primary, alternate = _evidence_pages(item['evidence'], pages, layout_pages)
        # A contiguous excerpt must occur on one stated page. Multi-page quotes
        # use the same explicit numbering as second-round passages.
        if quote.startswith('PDF PAGE '):
            _page_passage(quote, indices, primary, alternate)
        elif not any(re.sub(r'\s+', '', quote) in re.sub(r'\s+', '', text)
                     for p in indices for text in ([primary[p - 1]] + ([alternate[p - 1]] if alternate is not None else []))):
            raise ValueError('first-round excerpt absent from original PDF')
    visually_seen = set()
    for observation in review.get('visual_inspections', []):
        image = Path(observation['path']).resolve()
        cache = extraction_path.parent.resolve()
        if not image.is_relative_to(cache / 'rendered') or sha(image) != observation['sha256']:
            raise ValueError('visual inspection must bind an unchanged cached source-page render')
        if observation['page'] not in all_pages:
            raise ValueError('visual inspection page absent')
        visually_seen.add(observation['page'])
    for decision in review['decisions']:
        _page_passage(decision['source_passage'], decision['source_pages'], pages, layout_pages)
        if decision['state'] == 'verified' and (decision['support'] != 'supported' or not all(decision['checks'].get(key) is True for key in CHECKS)):
            raise ValueError('verified assertion does not pass six source-support checks')
        if decision['state'] != 'verified' and decision['support'] == 'supported':
            raise ValueError('blocked assertion must explain unresolved/unsupported support')
        item = next(i for i in items if i['id'] == decision['item_id'])
        if item.get('evidence_basis') == 'image' or item.get('visual_verified') is True:
            if not set(item['evidence']['pdf_page_indices']).issubset(visually_seen):
                raise ValueError('image assertion has no bound actual visual-inspection record')
    if review['coverage_status'] == 'complete' and review['gaps']:
        raise ValueError('complete coverage cannot conceal recorded gaps')
    return source, extraction, analysis, review


def _final_content(source, analysis, review, analysis_sha, review_sha):
    """Deterministic selection only: no producer assertion is rewritten."""
    approved = {d['item_id'] for d in review['decisions'] if d['state'] == 'verified'}
    items = []
    for original in analysis['items']:
        if original['id'] in approved:
            item = dict(original)
            item['chunk_id'] = f"pdf:{source['pdf_sha256']}:{original['id']}"
            item['origin_item_sha256'] = object_sha(original)
            items.append(item)
    if not items:
        raise ValueError('no independently verified assertions; nothing publishable')
    paper = analysis['paper']
    doi = normalize_doi(paper.get('doi') or source.get('doi'))
    if source.get('doi') and doi != normalize_doi(source['doi']):
        raise ValueError('paper DOI changed from prepared identity')
    gaps = list(review['gaps']) + [{'item_id': d['item_id'], 'state': d['state'], 'reason': d['rationale_zh']}
                                  for d in review['decisions'] if d['state'] != 'verified']
    return {'schema_version': 'fast_pdf_pack_v1', 'ingestion_mode': 'fast_pdf_two_round',
            'paper_id': 'pdf:' + source['pdf_sha256'], 'source_pdf_path': source['source_pdf_path'],
            'source_pdf_sha256': source['pdf_sha256'], 'title': paper['title'], 'authors': paper['authors'],
            'year': str(paper['year']), 'doi': doi, 'paper_type': paper['paper_type'],
            'zotero_item_key': '', 'zotero_attachment_key': '', 'status': 'draft', 'human_checked': False,
            'review_label': 'NOT HUMAN CHECKED', 'project_profile': source['project_profile'],
            'coverage': analysis['coverage'], 'independent_inventory': review['independent_inventory'],
            'coverage_status': 'partial' if gaps else review['coverage_status'], 'coverage_gaps': gaps, 'items': items,
            'generation': {'method': VERSION, 'producer': analysis['producer'], 'reviewer': review['reviewer']},
            'review_provenance': {'analysis_sha256': analysis_sha, 'review_sha256': review_sha,
                                  'selection': 'unchanged_verified_first_round_items_only'}}


def _rag(model):
    lines = ['# ' + model['title'], '', 'draft / NOT HUMAN CHECKED', '',
             '入库方式：fast_pdf_two_round；覆盖：' + model['coverage_status'], '']
    for item in model['items']:
        lines += ['<!-- chunk_id: ' + item['chunk_id'] + ' -->', '## ' + item['id'], '', item['text_zh'], '']
        if item.get('text'):
            lines += [item['text'], '']
        lines += ['原文页：' + ', '.join(map(str, item['evidence']['pdf_page_indices'])),
                  '认识论身份：' + item['epistemic_status'], '不能支持：' + item['cannot_support'], '']
    if model['coverage_gaps']:
        lines += ['## 未覆盖与待处理', '', json.dumps(model['coverage_gaps'], ensure_ascii=False, indent=2), '']
    return '\n'.join(lines)


def materialize_job(job_dir, storage_root, expected_source_json_sha=None):
    source, extraction, analysis, review = validate_job(job_dir, expected_source_json_sha)
    directory = Path(job_dir).resolve()
    analysis_sha, review_sha = sha(directory / 'analysis.json'), sha(directory / 'review.json')
    target = Path(storage_root).resolve() / 'published' / source['pdf_sha256'] / (analysis_sha[:16] + '-' + review_sha[:16])
    model = _final_content(source, analysis, review, analysis_sha, review_sha)
    model['lineage'] = {'source_json_path': str(directory / 'source.json'), 'source_json_sha256': sha(directory / 'source.json'),
                        'analysis_path': str(directory / 'analysis.json'), 'review_path': str(directory / 'review.json'),
                        'extraction_manifest_path': source['extraction_manifest_path'],
                        'extraction_manifest_sha256': source['extraction_manifest_sha256']}
    target.mkdir(parents=True, exist_ok=True)
    model_path = target / '02_machine_evidence_pack.json'
    write_once(model_path, model)
    rag_path = target / '02_rag_knowledge.md'
    rag = _rag(model)
    if rag_path.exists() and rag_path.read_text(encoding='utf-8') != rag:
        raise ValueError('final RAG artifact changed')
    if not rag_path.exists():
        rag_path.write_text(rag, encoding='utf-8')
    projection = {'schema': 'fast-pdf-projection/1', 'source_pdf_sha256': source['pdf_sha256'],
                  'analysis_sha256': analysis_sha, 'review_sha256': review_sha,
                  'model_sha256': sha(model_path), 'rag_sha256': sha(rag_path),
                  'admitted': [{'item_id': i['id'], 'chunk_id': i['chunk_id'], 'origin_item_sha256': i['origin_item_sha256']}
                               for i in model['items']],
                  'excluded': [d['item_id'] for d in review['decisions'] if d['state'] != 'verified'],
                  'semantic_rewriting': False}
    write_once(target / 'projection.json', projection)
    return model_path


def validate_final_pack(model_path):
    """Shared discovery/audit contract; strict lineage + current quality binding."""
    model_path = Path(model_path).resolve()
    model = read_json(model_path)
    if model.get('schema_version') != 'fast_pdf_pack_v1':
        raise ValueError('not a fast PDF final pack')
    lineage = model['lineage']
    source_json = Path(lineage['source_json_path'])
    source, extraction, analysis, review = validate_job(source_json.parent, lineage['source_json_sha256'])
    analysis_sha, review_sha = sha(lineage['analysis_path']), sha(lineage['review_path'])
    required_lineage = {'source_json_path': str(source_json), 'source_json_sha256': sha(source_json),
                        'analysis_path': str(source_json.parent / 'analysis.json'),
                        'review_path': str(source_json.parent / 'review.json'),
                        'extraction_manifest_path': source['extraction_manifest_path'],
                        'extraction_manifest_sha256': source['extraction_manifest_sha256']}
    if lineage != required_lineage:
        raise ValueError('final lineage differs from frozen original job')
    expected = _final_content(source, analysis, review, analysis_sha, review_sha)
    expected['lineage'] = lineage
    if model != expected:
        raise ValueError('final pack changed producer semantics or selection')
    rag_path = model_path.with_name('02_rag_knowledge.md')
    if rag_path.read_text(encoding='utf-8') != _rag(expected):
        raise ValueError('RAG differs from deterministic admitted subset')
    projection = read_json(model_path.with_name('projection.json'))
    if projection.get('model_sha256') != sha(model_path) or projection.get('rag_sha256') != sha(rag_path):
        raise ValueError('final artifact projection binding changed')
    if projection.get('analysis_sha256') != analysis_sha or projection.get('review_sha256') != review_sha or projection.get('semantic_rewriting') is not False:
        raise ValueError('origin review binding changed')
    if projection.get('admitted') != [{'item_id': i['id'], 'chunk_id': i['chunk_id'], 'origin_item_sha256': i['origin_item_sha256']} for i in model['items']]:
        raise ValueError('admitted subset proof changed')
    from knowledge_system_core import normalized_paper_chunk, normalize_text, finalize_source_hashes
    authors = model['authors'] if isinstance(model['authors'], str) else '; '.join(map(str, model['authors']))
    source_id = 'pdf:' + source['pdf_sha256']
    provenance = {'ingestion_mode': 'fast_pdf_two_round', 'work_id': 'doi:' + model['doi'] if model['doi'] else source_id,
                  'coverage_status': model['coverage_status'], 'coverage_gaps': model['coverage_gaps'],
                  'review_provenance': model['review_provenance'], 'projection_path': str(model_path.with_name('projection.json')),
                  'projection_sha256': sha(model_path.with_name('projection.json')), 'extraction_manifest_path': source['extraction_manifest_path'],
                  'lineage': lineage, 'human_checked': False, 'supports_claim': True,
                  'scientific_acceptance': 'NOT HUMAN CHECKED'}
    row = {'source_id': source_id, 'source_type': 'paper_evidence', 'authority': 'evidence',
           'title': model['title'], 'title_normalized': normalize_text(model['title']), 'authors': authors,
           'year': str(model['year']), 'doi': model['doi'], 'zotero_item_key': '', 'zotero_attachment_key': '',
           'collections_json': '[]', 'tags_json': '[]', 'priority': '', 'status': 'draft',
           'project_profile': model['project_profile'], 'source_path': str(model_path.parent),
           'source_pdf_path': model['source_pdf_path'], 'source_pdf_sha256': model['source_pdf_sha256'],
           'model_sha256': sha(model_path), 'rag_sha256': sha(rag_path), 'provenance_json': canonical(provenance),
           'updated_at': source['created_at'], 'tombstoned': 0}
    kind_map = {'claim': 'core_claims', 'method': 'methods', 'limitation': 'limitations', 'figure': 'figure_evidence', 'definition': 'definitions'}
    chunks = []
    for item in model['items']:
        normalized = dict(item, source_kind='paper', support_level='direct', verification_status='anchored')
        # Normalized generic kind keeps caption text distinct from an actually
        # inspected figure. No visual-review badge is derived from kind alone.
        chunk = normalized_paper_chunk(source_id, model, kind_map[item['kind']], normalized)
        chunks.append(chunk)
    finalize_source_hashes([row], chunks)
    # Reuse exactly the existing independent semantic gate, fresh extraction and
    # continuous PDF passage validation; lexical matches are not semantic checks.
    from apply_knowledge_reviews import validate_review
    text_cache = {}
    for chunk in chunks:
        record = projected_quality(row, chunk, analysis, review, extraction, model)
        validate_review(record, row | chunk | {'source_authority': row['authority']}, text_cache)
    return row, chunks


def projected_quality(source, chunk, analysis, review, extraction, model):
    from knowledge_quality import input_binding, VERSION as QUALITY_VERSION
    decision = next(d for d in review['decisions'] if d['item_id'] == chunk['item_id'])
    if decision['state'] != 'verified':
        raise ValueError('unreviewed assertion cannot receive final quality')
    return {'checker_version': QUALITY_VERSION, 'observed_at': model['generation'].get('reviewed_at', ''),
            'chunk_id': chunk['chunk_id'], 'source_id': source['source_id'], 'state': 'verified',
            'semantic_checked': True, 'mechanical_checked': True,
            'inputs': input_binding(source | chunk | {'source_authority': source['authority']}),
            'projection': model['review_provenance'] | {'origin_item_sha256': next(i['origin_item_sha256'] for i in model['items'] if i['id'] == chunk['item_id']),
                                                     'semantic_rewriting': False},
            'review': {'method': 'original_source_semantic_review', 'reviewer_id': review['reviewer']['task_id'],
                       'producer_id': analysis['producer']['task_id'], 'rationale_zh': decision['rationale_zh'],
                       'reviewer_attestation': 'Independent original-source review recorded in hash-bound second-round artifact; deterministic unchanged subset projection.',
                       'checks': decision['checks'], 'check_details': decision['check_details'], 'support': decision['support'],
                       'source_pdf_path': source['source_pdf_path'], 'source_pages': decision['source_pages'],
                       'source_passage': decision['source_passage'],
                       **_quality_text_binding(decision, extraction)}}


def _quality_text_binding(decision, extraction):
    """Bind every unchanged review excerpt to one complete cached text route."""
    pages = {route: Path(extraction[route + '_text_path']).read_text(encoding='utf-8').split('\f')
             for route in ('normal', 'layout')}
    sections = re.split(r'PDF PAGE (\d+)\n', decision['source_passage'])
    if sections[0].strip() or len(sections) < 3 or len(sections) % 2 != 1:
        raise ValueError('quality passage needs explicit numbered excerpts')
    routes = []
    for offset in range(1, len(sections), 2):
        page, body = int(sections[offset]), sections[offset + 1]
        route = next((route for route in ('normal', 'layout')
                      if page in decision['source_pages'] and 1 <= page <= len(pages[route]) and body.strip()
                      and re.sub(r'\s+', '', body) in re.sub(r'\s+', '', pages[route][page - 1])), None)
        if route is None:
            raise ValueError('quality excerpt absent from bound cached source page')
        routes.append(route)
    binding = {'fresh_text_extraction_route': routes[0],
               'fresh_text_sha256': extraction['full_text_sha256'][routes[0]]}
    if len(set(routes)) > 1:
        binding.update(source_passage_routes=routes,
                       fresh_text_sha256_by_route={route: extraction['full_text_sha256'][route] for route in sorted(set(routes))})
    return binding


def publication_marker_path(model_path, database):
    database = str(Path(database).expanduser().resolve())
    return Path(model_path).with_name('publication-' + object_sha(database)[:24] + '.json')


def write_publication_marker(model_path, source, database):
    database = str(Path(database).expanduser().resolve())
    marker = {'schema': 'fast-pdf-publication/2', 'source_id': source['source_id'],
              'database': database, 'database_identity_sha256': object_sha(database),
              'model_sha256': source['model_sha256'], 'rag_sha256': source['rag_sha256'],
              'verified_subset': True}
    write_once(publication_marker_path(model_path, database), marker)


def discover_fast_pdf_sources(config):
    sources, chunks, warnings = [], [], []
    database = str(Path(config.get('database_path') or DEFAULT_DB).expanduser().resolve())
    seen = set()
    paths = sorted({Path(path).resolve() for pattern in config.get('fast_pdf_model_globs', []) for path in glob.glob(str(pattern), recursive=True)})
    for path in paths:
        marker_path = publication_marker_path(path, database)
        if not marker_path.is_file():
            warnings.append('uncommitted fast PDF derivative ignored: ' + str(path))
            continue
        marker = read_json(marker_path)
        if (marker.get('schema') != 'fast-pdf-publication/2' or marker.get('database') != database
                or marker.get('database_identity_sha256') != object_sha(database)
                or marker.get('model_sha256') != sha(path)
                or marker.get('rag_sha256') != sha(path.with_name('02_rag_knowledge.md'))
                or marker.get('verified_subset') is not True):
            raise ValueError('published fast PDF marker binding changed')
        source, local = validate_final_pack(path)
        if source['source_id'] in seen:
            raise ValueError('duplicate fast PDF version for same SHA: ' + source['source_id'])
        seen.add(source['source_id'])
        sources.append(source)
        chunks.extend(local)
    return sources, chunks, warnings


def _logical_digest(connection, excluded_sources=(), excluded_chunks=()):
    """Stable complete logical digest of unrelated application rows (including FTS)."""
    results = {}
    tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    # FTS shadow tables are implementation state; compare the public FTS rows.
    tables = [name for name in tables if not any(name.startswith(prefix + '_') for prefix in ('chunks_fts', 'work_memory_fts'))]
    for table in tables:
        columns = [row[1] for row in connection.execute('PRAGMA table_info("' + table.replace('"', '""') + '")')]
        rows = []
        for raw in connection.execute('SELECT * FROM "' + table.replace('"', '""') + '"'):
            record = dict(zip(columns, raw))
            if record.get('source_id') in excluded_sources or record.get('chunk_id') in excluded_chunks or record.get('object_id') in set(excluded_sources) | set(excluded_chunks):
                continue
            rows.append(canonical(record))
        results[table] = {'count': len(rows), 'sha256': hashlib.sha256('\n'.join(sorted(rows)).encode()).hexdigest()}
    # These optional tables are created transactionally by initialize_quality.
    # Absence and an empty table have the same unrelated-row content; arbitrary
    # new tables and any unrelated quality records must still change the digest.
    empty = {'count': 0, 'sha256': hashlib.sha256(b'').hexdigest()}
    for table in ('source_quality', 'chunk_quality', 'quality_history'):
        results.setdefault(table, empty.copy())
    return results


def _check_identity(connection, source):
    existing = connection.execute('SELECT source_id,source_pdf_sha256,doi FROM sources WHERE tombstoned=0').fetchall()
    for source_id, digest, doi in existing:
        if source['doi'] and normalize_doi(doi) == source['doi'] and digest != source['source_pdf_sha256']:
            raise ValueError('DOI collision with a different PDF: ' + source_id)
        if digest == source['source_pdf_sha256'] and source_id != source['source_id']:
            return source_id
    return None


def publish(batch_dir, database=None, storage_root=None, confirmed=False):
    from knowledge_quality import (DEFAULT_WORKFLOW_LOCK, workflow_lock, recovery_backup,
        recovery_backup_metadata, initialize_quality, write_record, input_binding)
    from knowledge_system_core import insert_source_and_chunks
    batch_dir = Path(batch_dir).resolve()
    batch = read_json(batch_dir / 'batch.json')
    validate_batch_binding(batch, batch_dir)
    frozen_database = Path(batch['database']).expanduser().resolve()
    frozen_storage_root = Path(batch['storage_root']).expanduser().resolve()
    database = Path(database).expanduser().resolve() if database is not None else frozen_database
    storage_root = Path(storage_root).expanduser().resolve() if storage_root is not None else frozen_storage_root
    if database != frozen_database:
        raise ValueError('database override differs from frozen batch target')
    if storage_root != frozen_storage_root:
        raise ValueError('storage root override differs from frozen batch target')
    if not database.is_file():
        raise ValueError('existing database required for publication')
    candidates, pending, blocked = [], [], []
    for job in batch['jobs']:
        if job['state'] != 'prepared':
            blocked.append({'pdf_sha256': job.get('pdf_sha256'), 'reason': job.get('reason')})
            continue
        directory = Path(job['job_dir'])
        if not (directory / 'review.json').is_file() or not (directory / 'analysis.json').is_file():
            pending.append(str(directory))
            continue
        # A completely rejected paper is a local outcome, not a reason to stop
        # admitting another paper's unchanged independently verified assertions.
        _, _, _, checked_review = validate_job(directory, job['source_json_sha256'])
        if not any(d['state'] == 'verified' for d in checked_review['decisions']):
            blocked.append({'pdf_sha256': job['pdf_sha256'], 'reason': 'no_verified_items',
                            'job_dir': str(directory)})
            continue
        model_path = materialize_job(directory, storage_root, job['source_json_sha256'])
        source, chunks = validate_final_pack(model_path)
        candidates.append((job, model_path, source, chunks))
    receipt = {'schema': 'fast-pdf-publish/1', 'status': 'preview', 'database': str(database),
               'batch_dir': str(batch_dir), 'batch_sha256': sha(batch_dir / 'batch.json'),
               'candidate_sources': len(candidates), 'candidate_chunks': sum(len(v[3]) for v in candidates),
               'pending_jobs': pending, 'blocked_or_reused': blocked, 'zotero_modified': False,
               'human_attestation_changed': False, 'scientific_promotion_changed': False}
    if not candidates:
        receipt['status'] = 'nothing_publishable'
        return receipt
    new, noops = [], []
    started = time.perf_counter()
    with workflow_lock(DEFAULT_WORKFLOW_LOCK), workflow_lock(database.with_suffix(database.suffix + '.write.lock')):
        with sqlite3.connect(database, timeout=10) as connection:
            connection.execute('BEGIN IMMEDIATE' if confirmed else 'BEGIN')
            for candidate in candidates:
                job, model_path, source, chunks = candidate
                reused = _check_identity(connection, source)
                if reused:
                    blocked.append({'pdf_sha256': source['source_pdf_sha256'], 'reason': 'source_sha_already_indexed', 'source_id': reused})
                    continue
                existing = connection.execute('SELECT content_hash FROM sources WHERE source_id=?', (source['source_id'],)).fetchone()
                if existing:
                    if existing[0] != source['content_hash']:
                        raise ValueError('source already exists with different final subset; explicit revision required')
                    # No-op still verifies every admitted assertion and its quality.
                    from review_knowledge_quality import rows
                    from knowledge_quality import attach_reviews, quality_decision
                    current = [r for r in rows(connection) if r['source_id'] == source['source_id']]
                    if {c['chunk_id'] for c in current} != {c['chunk_id'] for c in chunks} or not all(quality_decision(r)['eligible'] for r in attach_reviews(connection, current)):
                        raise ValueError('existing publication differs or quality is no longer current')
                    noops.append(candidate)
                    continue
                new.append(candidate)
            source_ids = {v[2]['source_id'] for v in new}
            chunk_ids = {c['chunk_id'] for v in new for c in v[3]}
            before = _logical_digest(connection, source_ids, chunk_ids)
            receipt['unrelated_before'] = before
            receipt['new_sources'], receipt['new_chunks'] = len(new), len(chunk_ids)
            if not confirmed or not new:
                connection.rollback()
                receipt['status'] = 'preview' if not confirmed else 'noop'
                receipt['commit_seconds'] = time.perf_counter() - started
                if confirmed:
                    # Recover the tiny postcommit marker if a prior process died
                    # after DB commit but before filesystem publication receipt.
                    for job, model_path, source, chunks in noops:
                        write_publication_marker(model_path, source, database)
                    from literature_cross_validation import post_publish
                    receipt['review_followup'] = post_publish(database, [v[2]['source_id'] for v in noops], batch_dir / 'review-followup') if noops else {'status': 'no_admitted_sources'}
                return receipt
            # One recovery state per actual batch transaction, never per assertion.
            recovery_dir = batch_dir / 'receipts'
            recovery_dir.mkdir(exist_ok=True)
            snapshot = recovery_backup(database, recovery_dir / ('pre_fast_pdf_' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.sqlite3'))
            receipt.update(recovery_backup_metadata(snapshot))
            initialize_quality(connection)
            for job, model_path, source, chunks in new:
                # Re-read after waiting for locks: raw inputs can change during prepare.
                checked_source, checked_chunks = validate_final_pack(model_path)
                if checked_source != source or checked_chunks != chunks:
                    raise ValueError('publication inputs changed while awaiting database lock')
                insert_source_and_chunks(connection, source, chunks)
                model = read_json(model_path)
                origin = model['lineage']
                analysis, review = read_json(origin['analysis_path']), read_json(origin['review_path'])
                extraction = read_json(origin['extraction_manifest_path'])
                for chunk in chunks:
                    record = projected_quality(source, chunk, analysis, review, extraction, model)
                    write_record(connection, 'chunk_quality', chunk['chunk_id'], record)
                    observed = connection.execute('SELECT record_json FROM chunk_quality WHERE chunk_id=?', (chunk['chunk_id'],)).fetchone()
                    if not observed or json.loads(observed[0]) != record:
                        raise ValueError('transactional quality readback failed')
                write_record(connection, 'source_quality', source['source_id'], {
                    'checker_version': VERSION, 'state': 'verified', 'state_scope': 'atomic_summary',
                    'semantic_checked': False, 'atomic_semantic_review_count': len(chunks),
                    'atomic_state_counts': {'verified': len(chunks)}, 'human_checked': False,
                    'inputs': {field: source[field] for field in ['source_pdf_sha256', 'model_sha256', 'rag_sha256']},
                    'review_provenance': model['review_provenance'], 'coverage_gaps': model['coverage_gaps']})
            after = _logical_digest(connection, source_ids, chunk_ids)
            if before != after:
                raise ValueError('unrelated database rows changed during intake')
            receipt['unrelated_after'] = after
            if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('database integrity check failed before commit')
            from review_knowledge_quality import rows
            from knowledge_quality import attach_reviews, quality_decision
            current = [r for r in rows(connection) if r['source_id'] in source_ids]
            if len(current) != len(chunk_ids) or not all(quality_decision(r)['eligible'] for r in attach_reviews(connection, current)):
                raise ValueError('newly admitted subset does not satisfy normal claim-support gate')
            connection.commit()
        with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as check:
            count = check.execute('SELECT count(*) FROM chunks WHERE source_id IN (' + ','.join('?' for _ in source_ids) + ')', tuple(source_ids)).fetchone()[0]
            if count != len(chunk_ids) or check.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('postcommit publication readback failed')
            if _logical_digest(check, source_ids, chunk_ids) != before:
                raise ValueError('postcommit unrelated rows differ')
    receipt.update(status='pass', postcommit_readback=True, committed_at=now(), commit_seconds=time.perf_counter() - started,
                   admitted_sources=[{'source_id': v[2]['source_id'], 'model_path': str(v[1]), 'model_sha256': v[2]['model_sha256'],
                                      'chunks': len(v[3]), 'coverage_gaps': read_json(v[1])['coverage_gaps']} for v in new])
    receipt_path = batch_dir / 'receipts' / ('publish_' + object_sha(receipt)[:24] + '.json')
    write_once(receipt_path, receipt)
    # Stable publication markers are written only after commit; discovery ignores
    # uncommitted preview artifacts, including failures that left file staging.
    for job, model_path, source, chunks in new:
        write_publication_marker(model_path, source, database)
        # Multiple databases can test the same immutable final artifacts. Receipts
        # are per database and must not overwrite the isolation run.
        job_receipt = Path(job['job_dir']) / ('publish-receipt-' + object_sha(str(database))[:12] + '.json')
        write_once(job_receipt, {'receipt': str(receipt_path), 'receipt_sha256': sha(receipt_path), 'database': str(database)})
    from literature_cross_validation import post_publish
    receipt['review_followup'] = post_publish(database, [v[2]['source_id'] for v in new + noops], batch_dir / 'review-followup')
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('prepare')
    p.add_argument('--input', action='append', type=Path, default=[])
    p.add_argument('--manifest', type=Path)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--database', type=Path, default=DEFAULT_DB)
    p.add_argument('--storage-root', type=Path)
    p.add_argument('--max-pages', type=int, default=40)
    p.add_argument('--project-profile', help='Explicit profile for PDF/directory inputs; conflicts with manifest profiles are rejected')
    p.add_argument('--model-settings', type=Path, help='User-selected two-round model settings; required for a new batch')
    p = commands.add_parser('status')
    p.add_argument('--batch', '--batch-dir', dest='batch', type=Path, required=True)
    p = commands.add_parser('record-dispatch')
    p.add_argument('--job', type=Path, required=True)
    p.add_argument('--stage', choices=('analysis', 'review'), required=True)
    p.add_argument('--metadata', type=Path, required=True, help='JSON containing only returned host dispatch metadata')
    p = commands.add_parser('publish')
    p.add_argument('--batch', '--batch-dir', dest='batch', type=Path, required=True)
    p.add_argument('--database', type=Path)
    p.add_argument('--storage-root', type=Path)
    p.add_argument('--confirmed', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            from model_settings import load
            settings = load(args.model_settings) if args.model_settings else None
            result = prepare(args.input, args.manifest, args.output, args.database, args.storage_root, args.max_pages, args.project_profile, settings)
        elif args.command == 'status':
            result = batch_status(args.batch)
        elif args.command == 'record-dispatch':
            result = record_dispatch(args.job, args.stage, read_json(args.metadata))
        else:
            result = publish(args.batch, args.database, args.storage_root, args.confirmed)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get('status') == 'nothing_publishable' else 0
    except Exception as exc:
        print(json.dumps({'schema': 'fast-pdf-error/1', 'status': 'failed', 'error': str(exc),
                          'error_type': type(exc).__name__, 'zotero_modified': False}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
