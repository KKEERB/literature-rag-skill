#!/usr/bin/env python3
"""Add an independently reviewed immutable version with exact, additive readback.

Publication alone does not grant semantic verification. New atomic reviews are
imported separately, bound to the actual new indexed assertions.
"""
from __future__ import annotations
import argparse
import copy
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path
from knowledge_quality import (DEFAULT_WORKFLOW_LOCK, availability, canonical, recovery_backup, recovery_backup_metadata, digest,
                               input_binding, object_digest, record_intake_quality,
                               workflow_lock, work_identity)
from knowledge_system_core import (SOURCE_COLUMNS, CHUNK_COLUMNS, external_context_chunk,
                                  finalize_source_hashes, insert_source_and_chunks,
                                  json_text, normalized_paper_chunk, sha256_text,
                                  walk_chunk_items)
from note_html_integrity import validate_note_artifacts
from artifact_attribution import revision_attribution_errors
from source_edition_projection import (edition_projection, edition_provenance,
                                       validate_edition_projection)
from review_knowledge_quality import DEFAULT_DB, ROOT, write_json



def _fts_values(source: dict, chunk: dict) -> tuple:
    from knowledge_system_core import cjk_bigrams
    title = str(source.get('title') or '')
    aliases = ' '.join([str(source.get('doi') or ''), str(chunk.get('item_id') or ''),
                        ' '.join(json.loads(chunk.get('retrieval_queries_zh_json') or '[]')),
                        ' '.join(json.loads(chunk.get('retrieval_queries_en_json') or '[]')),
                        ' '.join(json.loads(source.get('tags_json') or '[]'))])
    body = '\n'.join(str(chunk.get(field) or '') for field in ('text_zh', 'text', 'context', 'cannot_support'))
    return (chunk['chunk_id'], title, body, aliases, ' '.join(cjk_bigrams(title + ' ' + body + ' ' + aliases)))


def revision_source_id(parent: dict, pack_sha: str) -> str:
    """Bind immutable version to the actual predecessor source, including local PDFs."""
    original = str(parent.get('source_id') or '').strip()
    profile = str(parent.get('project_profile') or '').strip()
    if not original or not profile or not re.fullmatch(r'[0-9a-f]{64}', pack_sha):
        raise ValueError('revision requires a source, profile and model hash')
    return original + ':version:' + profile + ':' + pack_sha

def check_artifacts(directory, hashes):
    for name, expected in hashes.items():
        if Path(name).name != name or digest(directory / name) != expected:
            raise ValueError('reviewed artifact changed: ' + name)


def project_chunks(model, source_id, original_ids):
    """Reconstruct index content from reviewed artifacts at prepare and commit."""
    explicit = {}
    for identifier, path, item in walk_chunk_items(model):
        if identifier in explicit:
            raise ValueError('duplicate model chunk ID')
        explicit[identifier] = (str(path[0]), item)
    external = {str(x.get('chunk_id') or f"{model['paper_id']}:e:e{i:03d}"):
                external_context_chunk(model, x, i)
                for i, x in enumerate(model.get('external_context', []), 1)}
    if not original_ids or len(original_ids) != len(set(original_ids)):
        raise ValueError('nonempty unique RAG inventory required')
    if any(identifier not in original_ids and kind != 'controversies'
           for identifier, (kind, _) in explicit.items()):
        raise ValueError('RAG omits model-backed chunks')
    mapping = {identifier: source_id + '::' + identifier for identifier in original_ids}
    chunks = []
    for identifier in original_ids:
        if identifier in explicit:
            kind, item = explicit[identifier]
            if kind == 'external_context':
                item = external[identifier]
        elif identifier in external:
            kind, item = 'external_context', external[identifier]
        else:
            raise ValueError('RAG chunk has no model lineage')
        item = copy.deepcopy(item)
        item['chunk_id'] = mapping[identifier]
        item['related_chunk_ids'] = [mapping.get(value, value) for value in item.get('related_chunk_ids', [])]
        chunks.append(normalized_paper_chunk(source_id, model, kind, item))
    return mapping, chunks


def revision_update_timestamp(manifest, report):
    """Keep creation time distinct from a bound independent review time."""
    created = manifest.get('created_at')
    if isinstance(created, str) and created.strip():
        return created, 'revision_manifest.created_at'
    if (created is None and manifest.get('schema') == 'bounded-legacy-correction-revision-manifest-v1'
            and isinstance(report.get('reviewed_at'), str) and report['reviewed_at'].strip()):
        reviewed = report['reviewed_at']
        parsed = dt.datetime.fromisoformat(reviewed.replace('Z', '+00:00'))
        if parsed.tzinfo is not None:
            return reviewed, 'independent_revision_review.reviewed_at; creation_time_unknown'
    raise ValueError('revision timestamp lacks a bound creation or independent review time')


def build_plan(database: Path, revision: Path, output: Path, *, archive_root=None):
    revision = revision.resolve()
    report_path = revision / 'revision_review.json'
    report = json.loads(report_path.read_text())
    manifest = json.loads((revision / 'revision_manifest.json').read_text())
    model = json.loads((revision / '02_machine_evidence_pack.json').read_text())
    if (report.get('decision') != 'pass' or report.get('blocking_findings')
            or not report.get('reviewer_task_id') or not report.get('producer_task_id')
            or report['reviewer_task_id'] == report['producer_task_id']):
        raise ValueError('independent revision PASS required')
    attribution_errors = revision_attribution_errors(model, report)
    if attribution_errors:
        raise ValueError('unresolved unindexed artifact attribution: ' + '; '.join(attribution_errors))
    check_artifacts(revision, report['artifact_hashes'])
    check_artifacts(revision, manifest['new_artifact_hashes'])
    validate_note_artifacts(revision)
    original = Path(manifest['historical_canonical'])
    check_artifacts(original, manifest['historical_artifact_hashes'])
    parent_id = manifest['source_id']
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as con:
        con.row_factory = sqlite3.Row
        value = con.execute('SELECT * FROM sources WHERE source_id=? AND tombstoned=0', (parent_id,)).fetchone()
        predecessor_chunks = [dict(row) for row in con.execute('SELECT * FROM chunks WHERE source_id=? ORDER BY chunk_id', (parent_id,))]
    if value is None:
        raise ValueError('active predecessor missing')
    parent = dict(value)
    if (Path(parent['source_path']).resolve() != original.resolve()
            or parent['model_sha256'] != manifest['historical_artifact_hashes']['02_machine_evidence_pack.json']
            or parent['rag_sha256'] != manifest['historical_artifact_hashes']['02_rag_knowledge.md']
            or availability(parent)['state'] != 'valid'):
        raise ValueError('predecessor inputs or live original changed')
    for key in ('zotero_item_key', 'zotero_attachment_key', 'doi', 'project_profile', 'source_pdf_sha256'):
        if str(model.get(key, '')) != parent[key]:
            raise ValueError('revision identity/route differs: ' + key)
    pack_sha = digest(revision / '02_machine_evidence_pack.json')
    rag_sha = digest(revision / '02_rag_knowledge.md')
    if (report['pack_sha256'] != pack_sha or report['rag_sha256'] != rag_sha
            or report['source_pdf_sha256'] != parent['source_pdf_sha256']
            or report['revision_manifest_sha256'] != digest(revision / 'revision_manifest.json')
            or model.get('status') != 'draft'
            or model.get('generation', {}).get('revision', {}).get('parent_model_sha256') != parent['model_sha256']):
        raise ValueError('new version review or lineage binding differs')
    source_edition = edition_projection(model, parent['source_pdf_sha256'], pack_sha)
    updated_at, update_time_basis = revision_update_timestamp(manifest, report)
    # Reject ambiguous or incomplete inventory before creating an archive.
    # This reviewed RAG is hash-bound above and copied unchanged below.
    source_id = revision_source_id(parent, pack_sha)
    original_ids = re.findall(r'<!-- chunk_id: ([^ ]+) -->', (revision / '02_rag_knowledge.md').read_text())
    mapping, chunks = project_chunks(model, source_id, original_ids)
    archive_root = archive_root or original.parent / (parent['project_profile'] + '_literature_versions')
    destination = archive_root.resolve() / pack_sha
    expected_files = dict(report['artifact_hashes']) | {
        'revision_review.json': digest(report_path),
        'revision_manifest.json': report['revision_manifest_sha256'],
    }
    if destination.exists():
        check_artifacts(destination, expected_files)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix='.revision-', dir=destination.parent))
        try:
            for name in expected_files:
                shutil.copy2(revision / name, temporary / name)
            check_artifacts(temporary, expected_files)
            os.rename(temporary, destination)
        except BaseException:
            shutil.rmtree(temporary)
            raise
    source = dict(parent)
    # Correct bibliography belongs to the reviewed version, while the original
    # index row stays immutable. Historical plans explicitly retain its authors.
    authors = model.get('authors')
    metadata_projection = ({'authors': authors} if isinstance(authors, str) and authors.strip() else {})
    source.update(metadata_projection)
    # Historical human declarations remain on the original source. They are not
    # assertions about this newly generated draft version.
    provenance = {'lineage': 'independently reviewed additive content correction',
                  'work_id': work_identity(parent), 'independent_scientific_source': False,
                  'original_to_indexed_chunk_ids': mapping,
                  'revision_parent_source_id': parent_id,
                  'revision_parent_model_sha256': parent['model_sha256'],
                  'revision_manifest_sha256': report['revision_manifest_sha256'],
                  'revision_review_sha256': digest(report_path),
                  'revision_updated_at_basis': update_time_basis,
                  'canonical_artifact_dir': str(destination),
                  'producer_task_id': report['producer_task_id'],
                  'independent_reviewer_task_id': report['reviewer_task_id']}
    provenance.update(edition_provenance(source_edition))
    source.update(source_id=source_id, source_path=str(destination), model_sha256=pack_sha,
                  rag_sha256=rag_sha, status='draft', updated_at=updated_at,
                  tags_json=json_text(model.get('topic_tags') or []),
                  provenance_json=json_text(provenance))
    finalize_source_hashes([source], chunks)
    plan = {'schema': 'knowledge-revision-publication-v1', 'database': str(database.resolve()),
            'source': source, 'chunks': chunks, 'predecessor': parent,
            'predecessor_chunks_sha256': object_digest(predecessor_chunks),
            'historical_directory': str(original), 'historical_artifact_hashes': manifest['historical_artifact_hashes'],
            'archive_artifact_hashes': expected_files,
            'independent_review_path': str(destination / 'revision_review.json'),
            'independent_review_sha256': digest(report_path),
            'new_chunk_bindings': {c['chunk_id']: input_binding(source | c | {'source_authority': source['authority']}) for c in chunks},
            'source_metadata_projection': metadata_projection,
            'source_edition_projection': source_edition,
            'semantic_review_imported': False, 'scientific_promotion': False,
            'zotero_write_performed': False}
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / 'publication_plan.json', plan)
    return {'status': 'prepared', 'source_id': source_id, 'chunks': len(chunks),
            'archive': str(destination), 'plan': str(output / 'publication_plan.json')}


def exact_readback(con, source, chunks):
    row = con.execute('SELECT * FROM sources WHERE source_id=?', (source['source_id'],)).fetchone()
    if row is None:
        return False
    if dict(row) != {k: source[k] for k in SOURCE_COLUMNS}:
        raise ValueError('existing revision source conflicts with prepared version')
    actual = {r['chunk_id']: dict(r) for r in con.execute('SELECT * FROM chunks WHERE source_id=?', (source['source_id'],))}
    expected = {c['chunk_id']: {k: c[k] for k in CHUNK_COLUMNS} for c in chunks}
    if actual != expected:
        raise ValueError('revision chunk content/inventory conflict')
    for chunk in chunks:
        fts = [tuple(r) for r in con.execute('SELECT chunk_id,title,body,aliases,ngrams FROM chunks_fts WHERE chunk_id=?', (chunk['chunk_id'],))]
        if fts != [_fts_values(source, chunk)]:
            raise ValueError('revision FTS content/inventory conflict')
    return True


def publish(database: Path, plan_path: Path, output: Path, *, confirmed=False, lock_path=None):
    plan = json.loads(plan_path.read_text())
    if plan.get('schema') != 'knowledge-revision-publication-v1':
        raise ValueError('explicit version publication schema required')
    if plan['database'] != str(database.resolve()):
        raise ValueError('publication database differs from plan')
    source, chunks, parent = plan['source'], plan['chunks'], plan['predecessor']
    lock = lock_path or DEFAULT_WORKFLOW_LOCK
    result = {'status': 'preview', 'source_id': source['source_id'], 'chunks': len(chunks),
              'canonical_unchanged': True, 'historical_versions_preserved': True,
              'zotero_write_performed': False, 'scientific_promotion': False}
    with workflow_lock(lock), workflow_lock(database.with_suffix(database.suffix + '.write.lock')):
        with sqlite3.connect(database, timeout=10) as con:
            con.row_factory = sqlite3.Row
            con.execute('PRAGMA foreign_keys=ON')
            con.execute('BEGIN IMMEDIATE' if confirmed else 'BEGIN')
            current = con.execute('SELECT * FROM sources WHERE source_id=?', (parent['source_id'],)).fetchone()
            if current is None or dict(current) != parent:
                raise ValueError('predecessor changed since publication preparation')
            current_chunks = [dict(row) for row in con.execute('SELECT * FROM chunks WHERE source_id=? ORDER BY chunk_id', (parent['source_id'],))]
            if object_digest(current_chunks) != plan['predecessor_chunks_sha256']:
                raise ValueError('predecessor indexed assertions changed since preparation')
            check_artifacts(Path(plan['historical_directory']), plan['historical_artifact_hashes'])
            check_artifacts(Path(source['source_path']), plan['archive_artifact_hashes'])
            if availability(source)['state'] != 'valid':
                raise ValueError('revision source inputs unavailable')
            archive = Path(source['source_path'])
            model = json.loads((archive / '02_machine_evidence_pack.json').read_text())
            review_path = archive / 'revision_review.json'
            if (plan.get('independent_review_path') != str(review_path) or
                    not review_path.is_file() or digest(review_path) != plan.get('independent_review_sha256')):
                raise ValueError('publication lacks the bound independent revision review')
            review = json.loads(review_path.read_text())
            manifest_path = archive / 'revision_manifest.json'
            if (not manifest_path.is_file()
                    or digest(manifest_path) != review.get('revision_manifest_sha256')
                    or plan['archive_artifact_hashes'].get('revision_manifest.json')
                    != review.get('revision_manifest_sha256')):
                raise ValueError('publication lacks the bound revision manifest')
            revision_manifest = json.loads(manifest_path.read_text())
            if (revision_manifest.get('source_id') != parent['source_id']
                    or revision_manifest.get('source_pdf_sha256') != source['source_pdf_sha256']):
                raise ValueError('publication revision manifest source identity differs')
            if (review.get('decision') != 'pass' or review.get('blocking_findings')
                    or not review.get('producer_task_id') or not review.get('reviewer_task_id')
                    or review['producer_task_id'] == review['reviewer_task_id']
                    or review.get('pack_sha256') != source['model_sha256']
                    or review.get('rag_sha256') != source['rag_sha256']
                    or review.get('source_pdf_sha256') != source['source_pdf_sha256']
                    or model.get('generation', {}).get('revision', {}).get('parent_model_sha256') != parent['model_sha256']):
                raise ValueError('publication independent review/lineage gate failed')
            check_artifacts(archive, review['artifact_hashes'])
            validate_note_artifacts(archive)
            attribution_errors = revision_attribution_errors(model, review)
            if attribution_errors:
                raise ValueError('unresolved unindexed artifact attribution: ' + '; '.join(attribution_errors))
            original_ids = re.findall(r'<!-- chunk_id: ([^ ]+) -->', (archive / '02_rag_knowledge.md').read_text())
            metadata_projection = plan.get('source_metadata_projection')
            if metadata_projection is None:
                if source['authors'] != parent['authors']:
                    raise ValueError('historical publication bibliography differs from predecessor')
            else:
                authors = model.get('authors')
                expected_metadata = ({'authors': authors} if isinstance(authors, str) and authors.strip() else {})
                if (metadata_projection != expected_metadata
                        or source['authors'] != expected_metadata.get('authors', parent['authors'])):
                    raise ValueError('publication bibliography differs from reviewed model projection')
            validate_edition_projection(model, source, plan.get('source_edition_projection'))
            mapping, projected = project_chunks(model, source['source_id'], original_ids)
            expected_source = copy.deepcopy(source)
            finalize_source_hashes([expected_source], projected)
            if projected != chunks or expected_source['content_hash'] != source['content_hash']:
                raise ValueError('publication index payload differs from reviewed model projection')
            if json.loads(source['provenance_json']).get('original_to_indexed_chunk_ids') != mapping:
                raise ValueError('publication version mapping differs from reviewed RAG inventory')
            if exact_readback(con, source, chunks):
                result.update(status='reused', postcommit_readback=True)
            elif confirmed:
                for c in chunks:
                    if any(con.execute('SELECT 1 FROM ' + table + ' WHERE chunk_id=?', (c['chunk_id'],)).fetchone()
                           for table in ('chunks', 'chunks_fts')):
                        raise ValueError('new revision chunk collision')
                output.mkdir(parents=True, exist_ok=True)
                backup = output / ('pre_revision_' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.sqlite3')
                backup = recovery_backup(database, backup)
                backup_info = recovery_backup_metadata(backup)
                insert_source_and_chunks(con, source, chunks)
                record_intake_quality(con, source, chunks)
                if not exact_readback(con, source, chunks):
                    raise ValueError('transactional publication readback failed')
                con.commit()
                with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as readback:
                    readback.row_factory = sqlite3.Row
                    if not exact_readback(readback, source, chunks):
                        raise ValueError('postcommit publication readback failed')
                result.update(status='pass', **backup_info, postcommit_readback=True,
                              semantic_review_required_for_new_chunks=True)
            if confirmed:
                output.mkdir(parents=True, exist_ok=True)
                result['publication_plan_sha256'] = digest(plan_path)
                write_json(output / 'publication_receipt.json', result)
    if confirmed and result.get('postcommit_readback'):
        from literature_cross_validation import post_publish
        result['review_followup'] = post_publish(database, [source['source_id']], output / 'review-followup')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'publish'])
    parser.add_argument('--database', type=Path, default=DEFAULT_DB)
    parser.add_argument('--revision', type=Path)
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--archive-root', type=Path)
    parser.add_argument('--confirmed', action='store_true')
    args = parser.parse_args()
    if args.action == 'prepare':
        result = build_plan(args.database, args.revision, args.output, archive_root=args.archive_root)
    else:
        result = publish(args.database, args.plan, args.output, confirmed=args.confirmed)
    print(json.dumps(result, ensure_ascii=False, indent=2))
