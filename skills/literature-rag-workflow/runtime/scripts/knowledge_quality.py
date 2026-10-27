"""Input-bound content review, independent of historical human attestation.

Literal matching never creates a semantic PASS. Unknown content stays searchable
for locating its PDF. A verified review must bind the exact assertion and inputs.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from portable_fs import file_lock

STATES = {'unchecked', 'verified', 'needs_review', 'quarantined'}
VERSION = 'knowledge-quality-v2'
FILE_FIELDS = {'pdf': 'source_pdf_sha256', 'model': 'model_sha256', 'rag': 'rag_sha256'}
DEFAULT_WORKFLOW_LOCK = Path(os.environ.get('LITERATURE_RAG_HOME', str(Path.home() / '.literature-rag-workflow'))).expanduser() / 'locks' / 'writeback_workflow.lock'


def revision_lineage_ids(relation):
    """Return only an explicit acyclic chain with the declared endpoints."""
    endpoints = [relation['original_source_id'], relation['preferred_source_id']]
    chain = relation.get('lineage_source_ids', endpoints)
    if (not isinstance(chain, list) or not 2 <= len(chain) <= 30
            or any(not isinstance(source_id, str) or not source_id for source_id in chain)
            or chain[0] != endpoints[0] or chain[-1] != endpoints[-1]
            or len(set(chain)) != len(chain)):
        raise ValueError('explicit acyclic revision lineage endpoints required')
    return chain


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def object_digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def as_dict(row: Any) -> dict:
    return dict(row)


def assertion_digest(row: Any) -> str:
    """Recompute, rather than trust a stored hash after an index mutation."""
    row = as_dict(row)
    fields = ('chunk_id', 'source_id', 'kind', 'item_id', 'text', 'text_zh',
              'context', 'source_kind', 'epistemic_status', 'support_level',
              'verification_status', 'evidence_json', 'cannot_support', 'authority')
    return object_digest({k: row.get(k, '') for k in fields})


def input_binding(row: Any) -> dict:
    row = as_dict(row)
    identity = {k: row.get(k, '') for k in (
        'source_id', 'source_type', 'title', 'authors', 'year', 'doi',
        'zotero_item_key', 'zotero_attachment_key', 'project_profile',
        'source_path', 'source_pdf_path')}
    identity['source_authority'] = row.get('source_authority', row.get('authority', ''))
    identity['work_id'] = work_identity(row)
    return {k: row.get(k, '') for k in FILE_FIELDS.values()} | {
        'assertion_sha256': assertion_digest(row), 'source_identity_sha256': object_digest(identity)}


def work_identity(row: Any) -> str:
    row = as_dict(row)
    try:
        p = json.loads(row.get('provenance_json') or '{}')
    except (TypeError, ValueError):
        p = {}
    if not isinstance(p, dict):
        p = {}
    # Explicit pairing can join different language DOIs. Do not guess a pair.
    explicit = p.get('work_id')
    if explicit:
        return str(explicit)
    doi = str(row.get('doi') or '').strip().casefold()
    for prefix in ('https://doi.org/', 'http://doi.org/', 'doi:'):
        if doi.startswith(prefix):
            doi = doi[len(prefix):].strip()
    return 'doi:' + doi if doi else 'zotero:' + str(row.get('zotero_item_key') or row.get('source_id'))


def source_files(row: Any) -> dict[str, Path]:
    row = as_dict(row)
    directory = Path(row.get('source_path') or '')
    return {'pdf': Path(row.get('source_pdf_path') or ''),
            'model': directory / '02_machine_evidence_pack.json',
            'rag': directory / '02_rag_knowledge.md'}


def availability(row: Any, cache: dict | None = None) -> dict:
    row = as_dict(row)
    if row.get('source_type') != 'paper_evidence':
        return {'state': 'not_scientific_evidence', 'files': {}}
    cache = cache if cache is not None else {}
    files = {}
    for label, path in source_files(row).items():
        expected = row.get(FILE_FIELDS[label], '')
        key = (str(path), expected)
        try:
            current_stat = path.stat()
            signature = [current_stat.st_dev, current_stat.st_ino, current_stat.st_size,
                         current_stat.st_mtime_ns, current_stat.st_ctime_ns]
        except OSError:
            signature = None
        # The cache is limited to one retrieval request. Even there, deletion or
        # replacement invalidates a prior hash observation before reuse.
        if key in cache and cache[key].get('stat_signature') != signature:
            del cache[key]
        if key not in cache:
            try:
                actual = digest(path)
                cache[key] = {'path': str(path), 'expected_sha256': expected,
                              'actual_sha256': actual,
                              'stat_signature': signature,
                              'state': 'valid' if expected and actual == expected else 'hash_mismatch'}
            except (OSError, ValueError):
                cache[key] = {'path': str(path), 'expected_sha256': expected,
                              'stat_signature': signature, 'state': 'unavailable'}
        files[label] = cache[key]
    return {'state': 'valid' if all(f['state'] == 'valid' for f in files.values()) else 'unavailable',
            'files': files}


def quality_decision(row: Any, cache: dict | None = None) -> dict:
    row = as_dict(row)
    try:
        record = json.loads(row.get('content_review_json') or '{}')
    except (ValueError, TypeError):
        record = {}
    if not isinstance(record, dict):
        record = {}
    state = record.get('state', 'unchecked')
    valid = availability(row, cache)
    binding_current = record.get('inputs') == input_binding(row)
    review = record.get('review', {})
    if not isinstance(review, dict):
        review = {}
    try:
        provenance = json.loads(row.get('provenance_json') or '{}')
    except (ValueError, TypeError):
        provenance = {'supports_claim': False}
    scientific_role = (isinstance(provenance, dict)
                       and provenance.get('supports_claim') is not False
                       and provenance.get('role') != 'translation_reference')
    independent = bool(review.get('reviewer_id') and review.get('producer_id')
                       and review['reviewer_id'] != review['producer_id'])
    reviewed = (record.get('semantic_checked') is True
                and review.get('method') == 'original_source_semantic_review'
                and review.get('support') == 'supported'
                and isinstance(review.get('checks'), dict)
                and all(review['checks'].get(k) is True for k in (
                    'whole_assertion', 'conditions_scope', 'numbers_units',
                    'attribution', 'epistemic_layer', 'compound_assertion'))
                and bool(review.get('source_passage')) and independent)
    try:
        source_record = json.loads(row.get('source_quality_json') or '{}')
    except (ValueError, TypeError):
        source_record = {}
    # An aggregate of atomic decisions reports review coverage. It is not a
    # separate decision to quarantine the original paper. Older unscoped source
    # quarantines stay conservative until explicitly reviewed.
    source_quarantined = (isinstance(source_record, dict)
                          and source_record.get('state') == 'quarantined'
                          and source_record.get('state_scope') != 'atomic_summary')
    if source_quarantined:
        state = 'quarantined'
    return {'state': state, 'source_availability': valid['state'],
            'input_binding_current': binding_current,
            'semantic_review_valid': reviewed,
            'eligible': state == 'verified' and valid['state'] == 'valid'
                        and binding_current and reviewed and scientific_role,
            'reason': ('verified_source_support' if state == 'verified' and valid['state'] == 'valid'
                       and binding_current and reviewed and scientific_role else
                       'source_unavailable' if valid['state'] == 'unavailable' else
                       'quarantined' if state == 'quarantined' else
                       'original_source_review_required')}


def initialize_quality(connection: sqlite3.Connection) -> None:
    # Individual statements: executescript would silently commit a caller's transaction.
    for table, key in (('source_quality', 'source_id'), ('chunk_quality', 'chunk_id')):
        connection.execute(f'CREATE TABLE IF NOT EXISTS {table} '
                           f'({key} TEXT PRIMARY KEY, record_json TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS quality_history '
                       '(event_id TEXT PRIMARY KEY, object_id TEXT NOT NULL, record_json TEXT NOT NULL)')


def attach_reviews(connection: sqlite3.Connection, rows: list[Any]) -> list[dict]:
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    records = {}
    source_records = {}
    if 'source_quality' in tables:
        identifiers = sorted({r['source_id'] for r in rows})
        for offset in range(0, len(identifiers), 500):
            subset = identifiers[offset:offset + 500]
            source_records.update(connection.execute('SELECT source_id,record_json FROM source_quality WHERE source_id IN ('
                                                     + ','.join('?' for _ in subset) + ')', subset).fetchall())
    if 'chunk_quality' in tables:
        identifiers = [r['chunk_id'] for r in rows]
        for offset in range(0, len(identifiers), 500):
            subset = identifiers[offset:offset + 500]
            records.update(connection.execute('SELECT chunk_id,record_json FROM chunk_quality WHERE chunk_id IN ('
                                               + ','.join('?' for _ in subset) + ')', subset).fetchall())
    result = []
    for row in rows:
        item = as_dict(row)
        item['content_review_json'] = records.get(item['chunk_id'], '{}')
        item['source_quality_json'] = source_records.get(item['source_id'], '{}')
        result.append(item)
    from reading_sessions import annotate_reading
    return annotate_reading(connection, result)


def write_record(connection: sqlite3.Connection, table: str, key: str, record: dict) -> None:
    if table not in {'source_quality', 'chunk_quality'}:
        raise ValueError('unsupported quality table')
    if record.get('state') not in STATES:
        raise ValueError('invalid content review state')
    field = 'source_id' if table == 'source_quality' else 'chunk_id'
    payload = canonical(record)
    event_id = object_digest({'table': table, 'key': key, 'record': record})
    connection.execute('INSERT OR IGNORE INTO quality_history VALUES (?,?,?)', (event_id, key, payload))
    connection.execute(f'INSERT INTO {table} VALUES (?,?) ON CONFLICT({field}) '
                       'DO UPDATE SET record_json=excluded.record_json', (key, payload))


def record_intake_quality(connection: sqlite3.Connection, source: dict, chunks: list[dict]) -> None:
    """Event-driven input observation. Intake alone never creates semantic PASS."""
    initialize_quality(connection)
    observed = dt.datetime.now(dt.timezone.utc).isoformat()
    valid = availability(source)
    files = {key: source.get(key, '') for key in FILE_FIELDS.values()}
    previous = connection.execute('SELECT record_json FROM source_quality WHERE source_id=?',
                                  (source['source_id'],)).fetchone()
    old = json.loads(previous[0]) if previous else {}
    if old.get('inputs') != files or old.get('source_availability') != valid:
        record = {'checker_version': VERSION, 'observed_at': observed, 'inputs': files,
                      'source_availability': valid, 'work_id': work_identity(source),
                      'state': 'quarantined' if valid['state'] == 'unavailable' else 'unchecked',
                      'state_scope': 'source',
                      'event': 'intake_or_revision', 'semantic_checked': False}
        if old.get('state') == 'quarantined' and old.get('state_scope') != 'atomic_summary':
            # Intake observations cannot release an explicit or legacy source
            # quarantine. A reviewed recovery must make that decision itself.
            record = old | record
            record.update(state='quarantined', state_scope=old.get('state_scope', 'source'))
            if old.get('inputs') != files:
                record['prior_quarantine_inputs'] = old.get('inputs')
                record.pop('revision_relation', None)
        write_record(connection, 'source_quality', source['source_id'], record)
    for chunk in chunks:
        binding = input_binding(source | chunk | {'source_authority': source['authority']})
        previous = connection.execute('SELECT record_json FROM chunk_quality WHERE chunk_id=?',
                                      (chunk['chunk_id'],)).fetchone()
        old = json.loads(previous[0]) if previous else {}
        if old.get('inputs') == binding:
            if valid['state'] != 'unavailable' or old.get('state') == 'quarantined':
                continue
        write_record(connection, 'chunk_quality', chunk['chunk_id'],
                     {'checker_version': VERSION, 'observed_at': observed, 'inputs': binding,
                      'source_id': source['source_id'], 'work_id': work_identity(source),
                      'state': 'quarantined' if valid['state'] == 'unavailable' else 'unchecked',
                      'event': 'intake_or_revision', 'mechanical_checked': False,
                      'semantic_checked': False, 'next_action': 'original_source_semantic_review'})


def consistent_backup(source: Path, destination: Path) -> None:
    """Materialize a readable SQLite baseline; rollback points use recovery_backup."""
    if destination.name.startswith('pre_'):
        raise ValueError('pre-write rollback points must use recovery_backup, not a full SQLite copy')
    if source.resolve() == destination.resolve() or destination.exists():
        raise ValueError('backup destination must be new and different from source')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('invalid backup')


def recovery_backup(source: Path, destination: Path) -> Path:
    """Keep an exact pre-write state in the shared compressed object store.

    The returned path is explicitly a recovery manifest, never a .sqlite3 file.
    Existing raw baselines and historical receipts retain their original contract.
    """
    from knowledge_recovery import create_snapshot
    manifest = Path(destination).with_suffix('.recovery.json')
    create_snapshot(Path(source), manifest)
    return manifest


def recovery_backup_metadata(manifest: Path) -> dict:
    """Bind a write receipt to both its manifest and the restored SQLite bytes."""
    value = json.loads(manifest.read_text(encoding='utf-8'))
    if value.get('schema') != 'sqlite-recovery-snapshot-v1':
        raise ValueError('unsupported recovery snapshot format')
    return {'backup': str(manifest), 'backup_format': value['schema'],
            'backup_manifest_sha256': digest(manifest),
            'backup_database_sha256': value['database_sha256'],
            'backup_database_bytes': value['database_bytes'],
            'backup_new_stored_bytes': value['stats']['new_stored_bytes']}


@contextlib.contextmanager
def workflow_lock(path: Path):
    with file_lock(path):
        yield
