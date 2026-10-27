#!/usr/bin/env python3
"""Read-only integrity audit of the derived knowledge index and its local inputs.

This checks identity, bytes, index payloads and literal locators, not scientific
truth or human acceptance. It never syncs or modifies the database/canonical inputs.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from knowledge_system_core import (SOURCE_COLUMNS, CHUNK_COLUMNS, cjk_bigrams, chunk_supports_claim,
                                   has_usable_evidence_locator as has_locator_for_support)
from knowledge_quality import quality_decision
from v21_quality import normalize_evidence_text, text_checksum
from artifact_attribution import attribution_risks


def book_quote_key(text: str) -> str:
    """Preserve legacy book locator normalization without a personal corpus."""
    ascii_text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    return ''.join(re.findall(r'[a-z0-9]+', ascii_text.lower()))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def normalize_doi(value: str) -> str:
    value = unquote(value.strip()).casefold()
    return re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', value).rstrip(' .')


def page_numbers(evidence: dict) -> list[int] | None:
    raw = evidence.get('pdf_page_indices')
    if raw is None:
        raw = [evidence['pdf_page_index']] if evidence.get('pdf_page_index') is not None else []
    if not isinstance(raw, list) or any(type(page) is not int or page < 1 for page in raw):
        return None
    return raw


def pdf_pages(path: Path) -> tuple[int | None, str]:
    tool = shutil.which('pdfinfo')
    if not tool:
        return None, 'pdfinfo unavailable'
    try:
        run = subprocess.run([tool, '-enc', 'UTF-8', str(path)], capture_output=True, encoding='utf-8', timeout=60)
        match = re.search(r'^Pages:\s+(\d+)', run.stdout, re.M)
        return (int(match[1]), '') if run.returncode == 0 and match else (None, 'pdfinfo failed')
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        return None, type(exc).__name__


def fresh_page_text(path: Path, page: int, route: str, source_region: dict | None = None) -> str | None:
    if route in {'pdftotext_crop_v1', 'pdftotext_crop_layout_v1'}:
        if (not isinstance(source_region, dict) or source_region.get('extractor') != route
                or source_region.get('pdf_page_index') != page):
            return None
        from prepare_knowledge_revision import extract_source_region
        try:
            text, _ = extract_source_region(path, source_region)
            return text
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    tool = shutil.which('pdftotext')
    if not tool or route not in {'normal', 'layout', 'raw'}:
        return None
    command = [tool, '-enc', 'UTF-8', '-f', str(page), '-l', str(page)]
    if route != 'normal':
        command.append('-' + route)
    try:
        run = subprocess.run(command + [str(path), '-'], capture_output=True, encoding='utf-8', timeout=60)
        return run.stdout if run.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None


def expected_fts(source: dict, chunk: dict) -> dict[str, str]:
    title = str(source.get('title') or '')
    aliases = ' '.join([
        str(source.get('doi') or ''), str(source.get('zotero_item_key') or ''),
        str(chunk.get('item_id') or ''),
        ' '.join(json.loads(chunk.get('retrieval_queries_zh_json') or '[]')),
        ' '.join(json.loads(chunk.get('retrieval_queries_en_json') or '[]')),
        ' '.join(json.loads(source.get('tags_json') or '[]')),
    ])
    body = '\n'.join(str(chunk.get(field) or '') for field in ('text_zh', 'text', 'context', 'cannot_support'))
    return {'title': title, 'body': body, 'aliases': aliases,
            'ngrams': ' '.join(cjk_bigrams(title + ' ' + body + ' ' + aliases))}


def fast_pdf_passage_status(quote: str, pages: list[int], page_texts: dict[int, list[str]]) -> str:
    """Check fast excerpts against their actual pages without rewriting words.

    The fast contract permits whitespace differences and explicit PDF PAGE
    delimiters. It does not case-fold, dehyphenate or remove punctuation. Keeping
    this separate avoids a page's later line-hyphen continuation changing an
    otherwise exact excerpt that ends at that hyphen.
    """
    if not quote.strip() or not pages:
        return ''
    passages = []
    if quote.startswith('PDF PAGE '):
        parts = re.split(r'PDF PAGE (\d+)\n', quote)
        if parts[0].strip() or len(parts) < 3 or len(parts) % 2 != 1:
            return ''
        passages = [(int(parts[i]), parts[i + 1]) for i in range(1, len(parts), 2)]
        if (set(page for page, _body in passages) != set(pages)
                or any(not body.strip() for _page, body in passages)):
            return ''
    else:
        # A single continuous quote need occur on one explicitly cited page;
        # other cited pages do not magically become quotation matches.
        if any(quote in text for page in pages for text in page_texts.get(page, [])):
            return 'raw_exact'
        compact = re.sub(r'\s+', '', quote)
        return ('whitespace_equivalent' if any(compact in re.sub(r'\s+', '', text)
                for page in pages for text in page_texts.get(page, [])) else '')
    all_exact = True
    for page, body in passages:
        texts = page_texts.get(page, [])
        if any(body in text for text in texts):
            continue
        all_exact = False
        if not any(re.sub(r'\s+', '', body) in re.sub(r'\s+', '', text) for text in texts):
            return ''
    return 'raw_exact' if all_exact else 'whitespace_equivalent'


def audit_database(database: Path, *, fresh_quotes: bool = False, source_ids=None) -> dict[str, Any]:
    database = database.expanduser().resolve()
    if not database.is_file():
        raise FileNotFoundError(database)
    findings: list[dict] = []
    coverage: collections.Counter = collections.Counter()
    source_details: list[dict] = []

    def finding(severity: str, code: str, **details: Any) -> None:
        findings.append({'severity': severity, 'code': code, **details})

    def decoded(row: dict, field: str, expected: type) -> Any:
        try:
            value = json.loads(row.get(field) or ('{}' if expected is dict else '[]'))
            if not isinstance(value, expected):
                raise ValueError('unexpected JSON shape')
            return value
        except (ValueError, TypeError):
            finding('error', 'invalid_json', object_id=row.get('chunk_id') or row.get('source_id'), field=field)
            return expected()

    before = {'database_sha256': sha256(database), 'size': database.stat().st_size}
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        integrity = [r[0] for r in connection.execute('PRAGMA integrity_check')]
        if integrity != ['ok']:
            finding('error', 'sqlite_integrity', messages=integrity)
        foreign_keys = [list(r) for r in connection.execute('PRAGMA foreign_key_check')]
        if foreign_keys:
            finding('error', 'foreign_key_violation', rows=foreign_keys)
        sources = {r['source_id']: dict(r) for r in connection.execute('SELECT * FROM sources')}
        chunks = [dict(r) for r in connection.execute('SELECT * FROM chunks')]
        fts = collections.defaultdict(list)
        for row in connection.execute('SELECT * FROM chunks_fts'):
            fts[row['chunk_id']].append(dict(row))
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        work = [dict(r) for r in connection.execute('SELECT * FROM work_memory')] if 'work_memory' in tables else []
        work_fts = [dict(r) for r in connection.execute('SELECT * FROM work_memory_fts')] if 'work_memory_fts' in tables else []
        external = [dict(r) for r in connection.execute('SELECT * FROM external_staging')] if 'external_staging' in tables else []
        quality_records = dict(connection.execute('SELECT chunk_id,record_json FROM chunk_quality')) if 'chunk_quality' in tables else {}
    all_sources = sources
    if source_ids is not None:
        scope = set(source_ids)
        for sid in sorted(scope - sources.keys()): finding('error', 'requested_source_missing', source_id=sid)
        sources = {key: row for key, row in sources.items() if key in scope}
        chunks = [row for row in chunks if row['source_id'] in scope]
        scoped_keys = {row['chunk_id'] for row in chunks}
        fts = {key: value for key, value in fts.items() if key in scoped_keys}
        work, work_fts, external = [], [], []
    active_sources = {key: row for key, row in sources.items() if not row['tombstoned']}
    active_chunks = [row for row in chunks if not row['tombstoned']]
    active_ids = {row['chunk_id'] for row in active_chunks}
    for identifier in sorted(set(fts) - active_ids):
        finding('error', 'fts_orphan_or_tombstoned', chunk_id=identifier)
    provenance = {key: decoded(row, 'provenance_json', dict) for key, row in all_sources.items()}
    for field, transform in [('doi', normalize_doi), ('zotero_item_key', str.strip), ('source_pdf_sha256', str.strip)]:
        identities: dict[str, list[dict]] = collections.defaultdict(list)
        for row in all_sources.values():
            if row['tombstoned']: continue
            identity = transform(row[field])
            if identity:
                identities[identity].append(row)
        for identity, group in sorted(identities.items()):
            if len(group) > 1 and any(r['source_id'] in active_sources for r in group):
                papers = sum(r['source_type'] == 'paper_evidence' for r in group)
                related_versions = (len({r['zotero_item_key'] for r in group}) == 1
                                    and len({r['source_pdf_sha256'] for r in group}) == 1
                                    and all(r['source_id'] == 'zotero:' + r['zotero_item_key']
                                            or (':version:' in r['source_id']
                                                and (provenance[r['source_id']].get('independent_evidence') is False
                                                     or provenance[r['source_id']].get('independent_scientific_source') is False)
                                                and provenance[r['source_id']].get('original_to_indexed_chunk_ids'))
                                            for r in group))
                if not related_versions and len({r['source_pdf_sha256'] for r in group}) == 1:
                    roots = set()
                    for member in group:
                        current = member; seen = set()
                        while provenance[current['source_id']].get('ingestion_mode') == 'reading_revision':
                            if current['source_id'] in seen: break
                            seen.add(current['source_id'])
                            parent = provenance[current['source_id']].get('revision_parent_source_id')
                            if parent not in all_sources: break
                            current = all_sources[parent]
                        roots.add(current['source_id'])
                    related_versions = len(roots) == 1
                finding('info' if related_versions else 'error' if papers > 1 or field == 'zotero_item_key' else 'warning',
                        'related_version_identity' if related_versions else 'active_duplicate_identity', identity_type=field, identity=identity,
                        source_ids=[r['source_id'] for r in group], source_types=[r['source_type'] for r in group])
    page_counts: dict[str, int | None] = {}
    extraction_records: dict[str, list[tuple[Path, dict]]] = {}
    book_source_ids: set[str] = set()
    validated_fast_sources: set[str] = set()
    page_cache: dict[tuple, str | None] = {}
    availability_cache = {}
    by_source: dict[str, list[dict]] = collections.defaultdict(list)
    for chunk in active_chunks:
        by_source[chunk['source_id']].append(chunk)
    for source_id, source in active_sources.items():
        coverage['active_sources_audited'] += 1
        record = {'source_id': source_id, 'source_type': source['source_type'], 'authority': source['authority'],
                  'status': source['status'], 'human_review': provenance[source_id].get('human_review', {}),
                  'ingestion_mode': provenance[source_id].get('ingestion_mode', 'canonical'), 'files': []}
        source_details.append(record)
        if source['source_type'] != 'paper_evidence':
            if source['authority'] == 'evidence':
                finding('error', 'nonpaper_source_evidence_authority', source_id=source_id)
            continue
        coverage['paper_sources_audited'] += 1
        directory = Path(source['source_path'])
        pdf = Path(source['source_pdf_path'])
        model_path = directory / '02_machine_evidence_pack.json'
        rag_path = directory / '02_rag_knowledge.md'
        for label, path, field in [('pdf', pdf, 'source_pdf_sha256'), ('model', model_path, 'model_sha256'), ('rag', rag_path, 'rag_sha256')]:
            coverage['paper_file_bindings_expected'] += 1
            detail = {'kind': label, 'path': str(path), 'expected_sha256': source[field]}
            record['files'].append(detail)
            if not path.is_file():
                finding('error', 'source_file_missing', source_id=source_id, **detail)
                continue
            actual = sha256(path)
            detail['actual_sha256'] = actual
            if actual != source[field]:
                finding('error', 'source_file_hash_mismatch', source_id=source_id, **detail)
            else:
                coverage['paper_file_bindings_match'] += 1
        if pdf.is_file():
            page_counts[source_id], reason = pdf_pages(pdf)
            record['pdf_page_count'] = page_counts[source_id]
            if page_counts[source_id] is None:
                finding('warning', 'pdf_page_count_unavailable', source_id=source_id, reason=reason)
            else:
                coverage['paper_pdf_page_counts_measured'] += 1
        model = {}
        if model_path.is_file():
            try:
                model = json.loads(model_path.read_text())
                is_fast = (model.get('schema_version') == 'fast_pdf_pack_v1'
                           or provenance[source_id].get('ingestion_mode') == 'fast_pdf_two_round')
                if is_fast:
                    coverage['fast_pdf_sources_audited'] += 1
                    try:
                        from fast_pdf_intake import validate_final_pack
                        expected_source, expected_chunks = validate_final_pack(model_path)
                        source_fields = [field for field in SOURCE_COLUMNS
                                         if field not in {'content_hash', 'updated_at', 'tombstoned'}]
                        changed = [field for field in source_fields
                                   if expected_source.get(field) != source.get(field)]
                        projection_matches = not changed
                        if changed:
                            finding('error', 'fast_pdf_source_projection_mismatch', source_id=source_id, fields=changed)
                        expected_by_id = {row['chunk_id']: row for row in expected_chunks}
                        actual_by_id = {row['chunk_id']: row for row in by_source[source_id]}
                        if set(expected_by_id) != set(actual_by_id):
                            projection_matches = False
                            finding('error', 'fast_pdf_model_chunk_inventory_mismatch', source_id=source_id,
                                    missing_from_database=sorted(set(expected_by_id) - set(actual_by_id)),
                                    missing_from_model=sorted(set(actual_by_id) - set(expected_by_id)))
                        for identifier in sorted(set(expected_by_id) & set(actual_by_id)):
                            changed = [field for field in CHUNK_COLUMNS if field not in {'content_hash', 'tombstoned'}
                                       and expected_by_id[identifier].get(field) != actual_by_id[identifier].get(field)]
                            if changed:
                                projection_matches = False
                                finding('error', 'fast_pdf_chunk_projection_mismatch', chunk_id=identifier, fields=changed)
                        if projection_matches:
                            validated_fast_sources.add(source_id)
                        coverage['fast_pdf_final_packs_validated'] += 1
                    except (OSError, ValueError, RuntimeError, KeyError, TypeError, ImportError) as exc:
                        finding('error', 'fast_pdf_final_pack_invalid', source_id=source_id,
                                error_type=type(exc).__name__, reason=str(exc))
                for field in ('zotero_item_key', 'zotero_attachment_key', 'source_pdf_sha256'):
                    if str(model.get(field) or '') != source[field]:
                        finding('error', 'model_source_identity_mismatch', source_id=source_id, field=field)
                if normalize_doi(str(model.get('doi') or '')) != normalize_doi(source['doi']):
                    finding('error', 'model_doi_mismatch', source_id=source_id)
                record['canonical_status'] = model.get('status')
                record['artifact_attribution_risks'] = attribution_risks(model)
                for risk in record['artifact_attribution_risks']:
                    finding('warning', risk['code'], source_id=source_id,
                            paper_field=risk['paper_field'], project_field=risk['project_field'],
                            field_sha256=risk['field_sha256'], semantic_checked=False)
                    coverage['unindexed_artifact_attribution_risks'] += 1
                record['canonical_human_check_status'] = model.get('human_check_status') or model.get('generation', {}).get('human_check_status')
                if (model.get('supports_claim') is False or model.get('claim_policy') == 'context_only') and source['authority'] == 'evidence':
                    finding('error', 'model_context_policy_overridden', source_id=source_id)
            except (ValueError, TypeError, AttributeError):
                finding('error', 'model_invalid_json', source_id=source_id)
        if rag_path.is_file():
            ids = re.findall(r'<!-- chunk_id: ([^ ]+) -->', rag_path.read_text())
            wanted = {row['chunk_id'] for row in by_source[source_id]}
            mapping = provenance[source_id].get('original_to_indexed_chunk_ids', {})
            mapped_ids = ids
            if mapping:
                if (not isinstance(mapping, dict) or set(mapping) != set(ids)
                        or not all(isinstance(value, str) and value for value in mapping.values())
                        or len(set(mapping.values())) != len(mapping)):
                    finding('error', 'invalid_version_chunk_mapping', source_id=source_id)
                else:
                    mapped_ids = [mapping[identifier] for identifier in ids]
            if len(ids) != len(set(ids)) or set(mapped_ids) != wanted:
                finding('error', 'rag_database_chunk_inventory_mismatch', source_id=source_id,
                        missing_from_database=sorted(set(mapped_ids) - wanted), missing_from_rag=sorted(wanted - set(mapped_ids)), duplicate_count=len(ids)-len(set(ids)))
            else:
                coverage['paper_rag_chunk_inventories_match'] += 1
                if mapping:
                    coverage['version_rag_chunk_inventories_match'] += 1
        candidates = [directory] + [Path(run['stage_dir']) for run in provenance[source_id].get('run_records', []) if run.get('stage_dir')]
        manifests = []
        for candidate in dict.fromkeys(candidates):
            manifest_path = candidate / 'extraction' / 'extraction_manifest.json'
            if manifest_path.is_file():
                try:
                    manifest = json.loads(manifest_path.read_text())
                    bound = manifest.get('pdf_sha256') == source['source_pdf_sha256']
                    manifests.append({'path': str(manifest_path), 'pdf_hash_matches_source': bound})
                    if bound:
                        extraction_records.setdefault(source_id, []).append((candidate, manifest))
                except (ValueError, TypeError):
                    finding('warning', 'extraction_manifest_unreadable', source_id=source_id, path=str(manifest_path))
        if provenance[source_id].get('ingestion_mode') == 'fast_pdf_two_round':
            # The fast cache is shared by source SHA and extractor version; it
            # deliberately is not copied into every final artifact directory.
            configured = provenance[source_id].get('extraction_manifest_path')
            if configured:
                manifest_path = Path(configured)
                try:
                    manifest = json.loads(manifest_path.read_text())
                    expected = provenance[source_id].get('lineage', {}).get('extraction_manifest_sha256')
                    current = sha256(manifest_path)
                    hash_bound = bool(expected and current == expected)
                    bound = manifest.get('source_pdf_sha256') == source['source_pdf_sha256'] and hash_bound
                    manifests.append({'path': str(manifest_path), 'pdf_hash_matches_source': bound,
                                      'manifest_sha256': current, 'expected_manifest_sha256': expected,
                                      'normalization_contract': 'v21_quality.text_checksum'})
                    if bound:
                        extraction_records.setdefault(source_id, []).append((manifest_path.parent, manifest))
                    else:
                        finding('error', 'fast_pdf_extraction_manifest_binding_mismatch', source_id=source_id,
                                path=str(manifest_path))
                except (OSError, ValueError, TypeError):
                    finding('error', 'fast_pdf_extraction_manifest_unreadable', source_id=source_id, path=str(manifest_path))
        book_contract = model.get('book_contract', {}) if isinstance(model, dict) else {}
        if book_contract.get('document_mode') == 'book_chapter_full' and book_contract.get('book_output_root'):
            book_source_ids.add(source_id)
            book_root = Path(book_contract['book_output_root'])
            for chapter in book_contract.get('chapter_refs', []):
                chapter_id = str(chapter.get('chapter_id') or '')
                if not re.fullmatch(r'ch[0-9]+', chapter_id):
                    continue
                manifest_path = book_root / 'chapters' / chapter_id / 'extraction' / 'extraction_manifest.json'
                if not manifest_path.is_file():
                    continue
                try:
                    manifest = json.loads(manifest_path.read_text())
                    bound = manifest.get('source_pdf_sha256') == source['source_pdf_sha256']
                    manifests.append({'path': str(manifest_path), 'pdf_hash_matches_source': bound, 'checksum_mode': 'raw_file_sha256', 'normalization_contract': 'book_chapter_full._quote_key'})
                    if bound:
                        manifest['_audit_checksum_mode'] = 'raw_file_sha256'
                        extraction_records.setdefault(source_id, []).append((manifest_path.parent, manifest))
                except (ValueError, TypeError):
                    finding('warning', 'extraction_manifest_unreadable', source_id=source_id, path=str(manifest_path))
        record['extraction_manifests'] = manifests
        coverage['paper_sources_with_pdf_bound_extraction' if source_id in extraction_records else 'paper_sources_without_pdf_bound_extraction'] += 1
        if source_id not in extraction_records:
            finding('warning', 'no_pdf_bound_cached_extraction', source_id=source_id,
                    manifests=manifests, fallback='fresh pdftotext at each referenced page where possible')
    for chunk in chunks:
        coverage['all_chunks_audited'] += 1
        source = sources.get(chunk['source_id'])
        if source is None:
            finding('error', 'orphan_chunk', chunk_id=chunk['chunk_id'], source_id=chunk['source_id'])
            continue
        if chunk['tombstoned']:
            continue
        identifier = chunk['chunk_id']
        coverage['active_chunks_audited'] += 1
        if source['tombstoned']:
            finding('error', 'active_chunk_tombstoned_source', chunk_id=identifier)
        matches = fts.get(identifier, [])
        if len(matches) != 1:
            finding('error', 'fts_cardinality', chunk_id=identifier, actual=len(matches), expected=1)
        else:
            try:
                expected = expected_fts(source, chunk)
                changed = [field for field, value in expected.items() if matches[0][field] != value]
                if changed:
                    finding('error', 'fts_payload_mismatch', chunk_id=identifier, fields=changed)
                else:
                    coverage['fts_payloads_match'] += 1
            except (ValueError, TypeError):
                finding('error', 'fts_input_json_invalid', chunk_id=identifier)
        evidence = decoded(chunk, 'evidence_json', dict)
        for field in ('retrieval_queries_zh_json', 'retrieval_queries_en_json', 'usable_for_json'):
            decoded(chunk, field, list)
        related = decoded(chunk, 'related_chunk_ids_json', list)
        missing = [key for key in related if key not in active_ids]
        if missing:
            finding('warning', 'related_chunk_unresolved', chunk_id=identifier, missing_ids=missing)
        old_label_support = (chunk['authority'] == 'evidence' and chunk['verification_status'] in {'anchored', 'externally_verified'} and chunk['epistemic_status'] not in {'project_application', 'codex_inference'} and chunk['source_kind'] not in {'project', 'external', 'zotero_metadata', 'mission_document'})
        coverage['legacy_label_only_supports_claim_true' if old_label_support else 'legacy_label_only_supports_claim_false'] += 1
        if old_label_support and not evidence:
            coverage['legacy_support_with_empty_evidence_object'] += 1
        joined = {**source, **chunk, 'source_authority': source['authority'],
                  'content_review_json': quality_records.get(identifier, '{}')}
        supports = old_label_support and has_locator_for_support(evidence) and source['authority'] == 'evidence' and quality_decision(joined, availability_cache)['eligible']
        coverage['supports_claim_true' if supports else 'supports_claim_false'] += 1
        lower_layer = (source['authority'] != 'evidence' or chunk['source_kind'] in {'project', 'external', 'zotero_metadata', 'mission_document'}
                       or chunk['kind'] in {'project_relation', 'external_context', 'metadata'}
                       or chunk['epistemic_status'] in {'project_application', 'codex_inference'}
                       or provenance[source['source_id']].get('supports_claim') is False)
        if (lower_layer and supports) or (source['authority'] != 'evidence' and chunk['authority'] == 'evidence'):
            finding('error', 'authority_boundary_violation', chunk_id=identifier)
        if chunk['verification_status'] not in {'anchored', 'externally_verified', 'needs_human_check'}:
            finding('error', 'unknown_verification_status', chunk_id=identifier, value=chunk['verification_status'])
        pages = page_numbers(evidence)
        eligible = (source['source_type'] == 'paper_evidence' and chunk['source_kind'] not in {'project', 'external', 'zotero_metadata', 'mission_document'}
                    and chunk['verification_status'] in {'anchored', 'externally_verified'})
        coverage['eligible_paper_locator_source_kind_' + chunk['source_kind'] if eligible else 'excluded_locator_source_kind_' + chunk['source_kind']] += 1
        if not eligible:
            continue
        coverage['paper_anchored_locators_expected'] += 1
        maximum = page_counts.get(chunk['source_id'])
        if pages is None or not pages or maximum is not None and any(page > maximum for page in pages):
            finding('error', 'invalid_paper_page_locator', chunk_id=identifier, pages=pages, pdf_page_count=maximum)
            continue
        if maximum is not None:
            coverage['paper_anchored_page_ranges_valid'] += 1
        detail_missing = not any(evidence.get(key) for key in ('section', 'figure', 'table', 'sections', 'figures', 'tables'))
        fast_locator = chunk['source_id'] in validated_fast_sources
        if detail_missing and not fast_locator:
            finding('warning', 'paper_detail_locator_missing', chunk_id=identifier)
        # An analyst inference can point to the author's table without being a
        # quotation of it. Keep checking its page locator, but do not invent a
        # missing-quotation defect or treat it as an author-reported result.
        if chunk['epistemic_status'] in {'codex_inference', 'project_application'}:
            if detail_missing and fast_locator:
                finding('warning', 'paper_detail_locator_missing', chunk_id=identifier)
            coverage['context_locators_checked_without_author_quote_requirement'] += 1
            continue
        quote = str(evidence.get('quote') or '')
        if not quote.strip():
            if detail_missing and fast_locator:
                finding('warning', 'paper_detail_locator_missing', chunk_id=identifier)
            coverage['paper_anchored_quotes_absent'] += 1
            finding('warning', 'paper_quote_absent', chunk_id=identifier)
            continue
        coverage['paper_quotes_expected'] += 1
        original_quote = quote
        if (provenance[source['source_id']].get('ingestion_mode') == 'fast_pdf_two_round'
                and quote.startswith('PDF PAGE ')):
            # Numbered multi-page passages use explicit provenance delimiters.
            # Strip only those validated delimiters for the literal text check;
            # the shared final-pack adapter separately verifies every body at
            # its exact named source page.
            parts = re.split(r'PDF PAGE (\d+)\n', quote)
            if (not parts[0].strip() and len(parts) >= 3 and len(parts) % 2 == 1
                    and [int(parts[i]) for i in range(1, len(parts), 2)] == pages):
                quote = ' '.join(parts[i] for i in range(2, len(parts), 2))
                coverage['fast_pdf_numbered_passages_checked'] += 1
        route = str(evidence.get('extraction_route') or 'normal')
        texts: list[str] = []
        texts_by_page: dict[int, list[str]] = collections.defaultdict(list)
        method = 'cached_pdf_bound_extraction'
        checksum_mismatch = False
        for page in pages:
            candidates = []
            for directory, manifest in extraction_records.get(chunk['source_id'], []):
                for entry in manifest.get('pages', []):
                    if entry.get('pdf_page_index') == page:
                        route_record = entry.get('routes', {}).get(route, {})
                        path = directory / str(route_record.get('path') or '__absent__')
                        if path.is_file():
                            key = ('cache', str(path))
                            if key not in page_cache:
                                page_cache[key] = path.read_text(encoding='utf-8', errors='replace')
                            text = page_cache[key]
                            if manifest.get('_audit_checksum_mode') == 'raw_file_sha256':
                                actual = sha256(path)
                                expected_checksum = route_record.get('sha256')
                            else:
                                actual = text_checksum(text)
                                expected_checksum = route_record.get('normalized_sha256')
                            valid = actual == expected_checksum and (not evidence.get('page_checksum') or actual == evidence['page_checksum'])
                            candidates.append((text, valid))
            valid_texts = [text for text, valid in candidates if valid] if not fresh_quotes else []
            if valid_texts:
                texts.extend(valid_texts)
                texts_by_page[page].extend(valid_texts)
            else:
                checksum_mismatch = (bool(candidates) and not any(valid for _, valid in candidates)) or checksum_mismatch
                method = 'fresh_pdf_text'
                region = evidence.get('source_region')
                region_key = hashlib.sha256(json.dumps(region, sort_keys=True).encode()).hexdigest()
                key = ('fresh', source['source_pdf_path'], page, route, region_key)
                if key not in page_cache:
                    if fresh_quotes and route in {'normal', 'layout', 'raw'}:
                        whole_key = ('fresh_whole_pdf', source['source_pdf_path'], route)
                        if whole_key not in page_cache:
                            command = [shutil.which('pdftotext') or 'pdftotext', '-enc', 'UTF-8']
                            if route != 'normal':
                                command.append('-' + route)
                            try:
                                run = subprocess.run(command + [source['source_pdf_path'], '-'],
                                                     capture_output=True, encoding='utf-8', timeout=60)
                                page_cache[whole_key] = run.stdout.split('\f') if run.returncode == 0 else []
                            except (OSError, subprocess.TimeoutExpired, UnicodeError):
                                page_cache[whole_key] = []
                        full_pages = page_cache[whole_key]
                        page_cache[key] = full_pages[page - 1] if page <= len(full_pages) else None
                    else:
                        if route in {'pdftotext_crop_v1', 'pdftotext_crop_layout_v1'}:
                            method = 'fresh_pdf_region_text'
                            page_cache[key] = (fresh_page_text(Path(source['source_pdf_path']), page, route, region)
                                if isinstance(region, dict) and evidence.get('page_checksum') == region.get('text_sha256')
                                else None)
                        else:
                            page_cache[key] = fresh_page_text(Path(source['source_pdf_path']), page, route)
                if page_cache[key] is not None:
                    texts.append(page_cache[key])
                    texts_by_page[page].append(page_cache[key])
        if checksum_mismatch:
            finding('warning', 'cached_page_checksum_mismatch', chunk_id=identifier, fallback='fresh PDF extraction')
        fast_match = fast_pdf_passage_status(original_quote, pages, texts_by_page) if fast_locator else ''
        if detail_missing and fast_locator:
            if fast_match:
                coverage['fast_pdf_page_and_continuous_excerpt_locators_valid'] += 1
            else:
                finding('warning', 'paper_detail_locator_missing', chunk_id=identifier)
        if fast_match:
            # This is an exact source-specific locator contract, independently
            # validated by the final-pack adapter. Report whitespace equivalence
            # distinctly; neither outcome is a new semantic decision.
            coverage['fast_pdf_passages_' + fast_match] += 1
            coverage['fast_pdf_passages_match_' + method] += 1
            if fast_match == 'raw_exact':
                coverage['paper_quotes_literal_match'] += 1
                coverage['paper_quotes_match_' + method] += 1
            continue
        if not texts:
            coverage['paper_quotes_unverifiable'] += 1
            finding('warning', 'paper_quote_unverifiable', chunk_id=identifier, route=route)
        elif any(normalize_evidence_text(quote) in normalize_evidence_text(text) for text in texts) or normalize_evidence_text(quote) in normalize_evidence_text(' '.join(dict.fromkeys(texts))):
            coverage['paper_quotes_literal_match'] += 1
            coverage['paper_quotes_match_' + method] += 1
        elif any(re.sub(r'\s+', '', normalize_evidence_text(quote)) in re.sub(r'\s+', '', normalize_evidence_text(text)) for text in texts):
            coverage['paper_quotes_whitespace_relaxed_match'] += 1
            finding('warning', 'paper_quote_only_whitespace_relaxed_match', chunk_id=identifier, pages=pages, method=method,
                    note='Only whitespace removed after the standard normalization; punctuation and numbers are preserved. This is distinct from strict literal matching.')
        elif chunk['source_id'] in book_source_ids and book_quote_key(quote) and any(book_quote_key(quote) in book_quote_key(text) for text in texts):
            coverage['paper_quotes_existing_book_token_contract_match'] += 1
            finding('warning', 'paper_quote_only_legacy_book_token_match', chunk_id=identifier, pages=pages, method=method,
                    note='Existing book_chapter_full._quote_key contract removes punctuation/non-ASCII and joins alphanumeric tokens. This does not verify mathematical signs, symbols, or strict quotation fidelity.')
        else:
            coverage['paper_quotes_literal_mismatch'] += 1
            finding('warning', 'paper_quote_not_found_at_locator', chunk_id=identifier, pages=pages, method=method,
                    quote_sha256=hashlib.sha256(quote.encode()).hexdigest(), note='Literal matching only; extraction and quotation need review. No scientific falsity is inferred.')
    work_ids = {row['record_id'] for row in work if row['status'] == 'active'}
    fts_work_counts = collections.Counter(row['record_id'] for row in work_fts)
    for identifier in work_ids | set(fts_work_counts):
        if fts_work_counts[identifier] != (1 if identifier in work_ids else 0):
            finding('error', 'work_memory_fts_cardinality', record_id=identifier)
    for row in work:
        if row['authority'] != 'context_only':
            finding('error', 'work_memory_authority_violation', record_id=row['record_id'])
    after = {'database_sha256': sha256(database), 'size': database.stat().st_size}
    if before != after:
        finding('warning', 'database_file_changed_during_audit', note='Audit uses a read transaction; concurrent writer may have changed the file.')
    counts = collections.Counter(item['severity'] for item in findings)
    return {
        'schema_version': 'knowledge_integrity_audit_v1', 'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'status': 'fail' if counts['error'] else 'pass_with_warnings' if counts['warning'] else 'pass',
        'database_path': str(database), 'read_only': True, 'scope_source_ids': sorted(source_ids) if source_ids is not None else None, 'database_before': before, 'database_after': after,
        'sqlite_integrity': integrity, 'foreign_key_violations': foreign_keys,
        'inventory': {'sources_all': len(sources), 'sources_active': len(active_sources), 'chunks_all': len(chunks),
                      'chunks_active': len(active_chunks), 'fts_rows': sum(map(len, fts.values())), 'work_memory_records': len(work), 'external_staging_records': len(external)},
        'coverage': dict(sorted(coverage.items())),
        'human_review_distribution': dict(collections.Counter(source['status'] for source in active_sources.values() if source['source_type'] == 'paper_evidence')),
        'human_review_modes': dict(collections.Counter(str(provenance[source['source_id']].get('human_review', {}).get('mode') or 'none') for source in active_sources.values() if source['source_type'] == 'paper_evidence')),
        'findings_summary': {'by_severity': dict(counts), 'by_code': dict(collections.Counter(row['code'] for row in findings))},
        'findings': findings, 'source_audit': source_details,
        'claim_ceiling': 'Mechanical integrity and normalized literal quotation matching only. No semantic accuracy, translation accuracy, visual figure interpretation, human acceptance or scientific promotion is established.',
        'limits': [
            'All source/chunk/FTS rows are read in one read-only SQLite transaction. File checks are a later filesystem observation, not an atomic cross-filesystem snapshot.',
            'Cached quotation checks require source PDF hash equality and normalized page checksum equality; cached text was not independently re-extracted from every PDF.',
            'Missing or mismatched cached extraction uses fresh pdftotext where available. Literal mismatch is a review finding and can reflect extraction differences.',
            'PDF pages are 1-based. Page range and literal quote matches do not verify section/figure labels or scientific interpretation.',
            'Whitespace-relaxed matches and the existing book token normalization are separate warnings, not strict literal passes. Book token normalization cannot validate mathematical signs or symbols.',
            'Historical user attestation and current draft states are reported as stored, without reconstructing per-paper human checklists.',
            'FTS rows and stored payloads are checked; no FTS write command or destructive rebuild is issued.',
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=Path('~/.codex/knowledge/fte_cusp_sxi/knowledge.sqlite3'))
    parser.add_argument('--output', type=Path, required=True, help='New local JSON report; canonical inputs and database remain read-only.')
    args = parser.parse_args()
    try:
        if args.output.expanduser().resolve() == args.database.expanduser().resolve():
            raise ValueError('report output must not overwrite the database')
        report = audit_database(args.database)
        output_path = args.output.expanduser().resolve()
        for source_record in report['source_audit']:
            for file_record in source_record['files']:
                input_path = Path(file_record['path']).resolve()
                if output_path == input_path or file_record['kind'] == 'model' and input_path.parent in output_path.parents:
                    raise ValueError('report output must not overwrite a canonical input or write inside a canonical artifact directory')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    except Exception as exc:
        print(json.dumps({'status': 'audit_error', 'error': str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({'status': report['status'], 'output': str(args.output.resolve()), 'findings_summary': report['findings_summary'], 'coverage': report['coverage']}, ensure_ascii=False))
    return 1 if report['findings_summary']['by_severity'].get('error') else 0


if __name__ == '__main__':
    raise SystemExit(main())
