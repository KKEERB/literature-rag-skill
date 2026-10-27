"""Admit source-supported dependencies without merging papers or judging claims."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

from knowledge_quality import (DEFAULT_WORKFLOW_LOCK, availability, canonical,
                               recovery_backup, recovery_backup_metadata, digest, work_identity, workflow_lock)

CHECKS = ('source_identity', 'original_source_quotes', 'relationship_scope',
          'distinct_analyses_preserved', 'no_data_byte_identity_claim',
          'independent_of_candidate_author')
INPUTS = ('source_pdf_sha256', 'model_sha256', 'rag_sha256')


def initialize_dependencies(connection):
    connection.execute('CREATE TABLE IF NOT EXISTS evidence_dependencies '
                       '(dependency_id TEXT PRIMARY KEY, record_json TEXT NOT NULL)')


def checked_candidate(connection, candidate_path, candidate_sha, review_path, review_sha, cache=None):
    if digest(candidate_path) != candidate_sha or digest(review_path) != review_sha:
        raise ValueError('dependency candidate or independent review changed')
    candidate = json.loads(candidate_path.read_text())
    review = json.loads(review_path.read_text())
    if (not isinstance(candidate, dict) or not isinstance(review, dict)
            or candidate.get('schema') != 'source-bound-scientific-dependency-candidate-v1'
            or candidate.get('relation_type') != 'shared_simulation_run'
            or not candidate.get('scope_zh') or not candidate.get('source_relation_statement')
            or review.get('schema') != 'independent-evidence-dependency-review-v1'
            or review.get('candidate_sha256') != candidate_sha
            or review.get('decision') != 'pass' or review.get('blocking_findings') != []
            or not candidate.get('reviewer_id') or not review.get('reviewer_id')
            or review.get('candidate_author_id') != candidate['reviewer_id']
            or review['reviewer_id'] == candidate['reviewer_id']
            or not isinstance(review.get('checks'), dict)
            or not all(review['checks'].get(key) is True for key in CHECKS)
            or review.get('sources') != candidate.get('sources')):
        raise ValueError('independent source dependency review does not bind the exact candidate')
    members = candidate.get('sources')
    if (not isinstance(members, list) or len(members) != 2
            or any(not isinstance(member, dict) for member in members)
            or len({m.get('source_id') for m in members}) != 2
            or len({m.get('work_id') for m in members}) != 2):
        raise ValueError('two distinct source and paper identities required')
    cache = cache if cache is not None else {}
    for member in members:
        cur = connection.execute('SELECT * FROM sources WHERE source_id=? AND tombstoned=0',
                                 (member['source_id'],))
        raw = cur.fetchone()
        source = dict(zip((c[0] for c in cur.description), raw)) if raw else None
        if source is None:
            raise ValueError('dependency source is missing')
        provenance = json.loads(source['provenance_json'])
        if (source['source_type'] != 'paper_evidence'
                or source['authority'] != 'evidence'
                or provenance.get('role') == 'translation_reference'
                or provenance.get('supports_claim') is False
                or work_identity(source) != member.get('work_id')
                or {k: source[k] for k in INPUTS} != member.get('inputs')
                or source['source_pdf_path'] != member.get('source_pdf_path')
                or member.get('actual_PDF_sha256') != source['source_pdf_sha256']
                or availability(source, cache)['state'] != 'valid'):
            raise ValueError('dependency source identity or files changed')
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='source_quality'").fetchone():
            raw_quality = connection.execute('SELECT record_json FROM source_quality WHERE source_id=?',
                                             (source['source_id'],)).fetchone()
            quality = json.loads(raw_quality[0]) if raw_quality else {}
            if (quality.get('state') == 'quarantined' and quality.get('state_scope') != 'atomic_summary'):
                raise ValueError('dependency source is under whole-source quarantine')
    return candidate, review


def check_literal_quotes(candidate):
    # Performed once at admission. Retrieval rechecks hashes of all those inputs
    # and the independent report; it does not rerun identical extraction.
    from audit_knowledge_integrity import fresh_page_text
    from v21_quality import normalize_evidence_text
    for source in candidate['sources']:
        quotes = source.get('original_continuous_quotes')
        if not isinstance(quotes, list) or not quotes:
            raise ValueError('original source passages required')
        for quote in quotes:
            page, text = quote.get('pdf_page_index'), quote.get('quote')
            if type(page) is not int or page < 1 or not isinstance(text, str) or not text.strip():
                raise ValueError('invalid original dependency passage')
            key = normalize_evidence_text(text)
            if not any(key in normalize_evidence_text(fresh_page_text(Path(source['source_pdf_path']), page, route) or '')
                       for route in ('normal', 'layout', 'raw')):
                raise ValueError('dependency passage is not present on the bound original page')


def admit(database, candidate_path, review_path, output, *, confirmed=False, lock_path=DEFAULT_WORKFLOW_LOCK):
    database, candidate_path, review_path = (Path(p).resolve() for p in (database, candidate_path, review_path))
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    candidate_sha, review_sha = digest(candidate_path), digest(review_path)
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        candidate, review = checked_candidate(connection, candidate_path, candidate_sha, review_path, review_sha)
    check_literal_quotes(candidate)
    record = {'schema': 'admitted-evidence-dependency-v1', 'dependency_id': candidate_sha,
              'candidate_path': str(candidate_path), 'candidate_sha256': candidate_sha,
              'review_path': str(review_path), 'review_sha256': review_sha,
              'reviewer_id': review['reviewer_id'], 'relation_type': 'shared_simulation_run',
              'scope': 'simulation_run_only', 'scope_zh': candidate['scope_zh'],
              'work_ids': sorted(m['work_id'] for m in candidate['sources']),
              'source_ids': sorted(m['source_id'] for m in candidate['sources']),
              'admitted_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'experimental_independence_count_inferred': False,
              'paper_identity_or_support_decisions_modified': False}
    receipt = {'status': 'preview', 'record': record, 'production_modified': False}
    if confirmed:
        with workflow_lock(lock_path), workflow_lock(database.with_suffix(database.suffix + '.write.lock')):
            with sqlite3.connect(database, timeout=5) as connection:
                connection.execute('BEGIN IMMEDIATE')
                checked_candidate(connection, candidate_path, candidate_sha, review_path, review_sha)
                backup = recovery_backup(database, output / 'pre_dependency.sqlite3')
                backup_info = recovery_backup_metadata(backup)
                initialize_dependencies(connection)
                prior = connection.execute('SELECT record_json FROM evidence_dependencies WHERE dependency_id=?',
                                           (candidate_sha,)).fetchone()
                if prior:
                    existing = json.loads(prior[0])
                    if {k: v for k, v in existing.items() if k != 'admitted_at'} != {
                            k: v for k, v in record.items() if k != 'admitted_at'}:
                        raise ValueError('existing dependency admission differs; preserve history')
                    record = existing
                else:
                    connection.execute('INSERT INTO evidence_dependencies VALUES (?,?)', (candidate_sha, canonical(record)))
                connection.commit()
            with sqlite3.connect(database) as connection:
                assert connection.execute('SELECT record_json FROM evidence_dependencies WHERE dependency_id=?',
                                          (candidate_sha,)).fetchone()[0] == canonical(record)
            receipt.update(status='reused' if prior else 'pass', record=record, production_modified=True,
                           **backup_info, postcommit_readback=True)
    (output / 'dependency_admission.actual.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    return receipt


def annotate_dependencies(connection, items, cache=None):
    """Return current known relationships, keeping experimental independence unknown."""
    works = {item['work_id'] for item in items}
    active, unresolved = [], []
    for item in items:
        item['known_evidence_dependencies'] = []
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence_dependencies'").fetchone():
        for identifier, raw in connection.execute('SELECT dependency_id,record_json FROM evidence_dependencies'):
            try:
                record = json.loads(raw)
                if not isinstance(record, dict) or not isinstance(record.get('work_ids'), list):
                    raise ValueError('invalid dependency admission payload')
                if not works.intersection(record['work_ids']):
                    continue
                if (record.get('schema') != 'admitted-evidence-dependency-v1'
                        or record.get('dependency_id') != identifier or record.get('candidate_sha256') != identifier
                        or record.get('scope') != 'simulation_run_only'):
                    raise ValueError('dependency admission payload differs')
                candidate, review = checked_candidate(connection, Path(record['candidate_path']), identifier,
                                                     Path(record['review_path']), record['review_sha256'], cache)
                if (record['work_ids'] != sorted(m['work_id'] for m in candidate['sources'])
                        or record['source_ids'] != sorted(m['source_id'] for m in candidate['sources'])
                        or record['relation_type'] != candidate['relation_type']
                        or record['scope_zh'] != candidate['scope_zh'] or record['reviewer_id'] != review['reviewer_id']):
                    raise ValueError('dependency admission does not match its reviewed scope')
                public = {k: record[k] for k in ('dependency_id', 'relation_type', 'scope', 'scope_zh', 'work_ids', 'source_ids')}
                public.update(relationship_basis='independently_checked_author_report', simulation_output_bytes_verified=False,
                              returned_work_ids=sorted(works.intersection(record['work_ids'])))
                active.append(public)
                for item in items:
                    if item['work_id'] in record['work_ids']:
                        item['known_evidence_dependencies'].append(public)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                unresolved.append({'dependency_id': identifier, 'state': 'needs_review', 'reason': str(exc)})
    return {'known_evidence_dependencies': active, 'dependency_review_gaps': unresolved,
            'experimentally_independent_work_count': None,
            'experimental_independence_established': False}
