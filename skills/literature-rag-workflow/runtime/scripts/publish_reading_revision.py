#!/usr/bin/env python3
"""Publish explicitly approved atomic corrections, including PDF-only sources.

New assertions are additive, independently source-reviewed versions. Original
packs, notes and human attestations are immutable; dependent reading materials
are marked for review. No Zotero identity is invented for a local PDF.
"""
from __future__ import annotations

import argparse
import copy
import json
import sqlite3
from pathlib import Path

from reading_sessions import (JOIN, DEFAULT_DB, check_current, connect, initialize_reading,
                              material_inventory, read, session_state, table_exists, write_new, now)
from knowledge_quality import (DEFAULT_WORKFLOW_LOCK, canonical, digest, object_digest,
                               input_binding, availability, initialize_quality, write_record,
                               work_identity, workflow_lock, recovery_backup, recovery_backup_metadata)
from fast_pdf_intake import CHECKS, _logical_digest, schema_validate

KINDS = {'claim': 'core_claims', 'method': 'methods', 'limitation': 'limitations',
         'figure': 'figure_evidence', 'definition': 'definitions'}
INDEX_KINDS = {'claim': 'claim', 'method': 'method', 'limitation': 'limitation',
               'figure': 'figure_evidence', 'definition': 'definition'}


def prepare(session_dir, proposal_path, output):
    session_dir, proposal_path, output = (Path(p).resolve() for p in (session_dir, proposal_path, output))
    session, events, _ = session_state(session_dir)
    packet = check_current(session)
    proposal = read(proposal_path)
    if proposal.get('schema') != 'reading-correction/1' or not proposal.get('producer_id') or not proposal.get('reason_zh'):
        raise ValueError('typed correction, actual producer and rationale required')
    changes = proposal.get('changes', [])
    if not changes or len({c['item']['id'] for c in changes}) != len(changes):
        raise ValueError('nonempty unique revised item IDs required')
    old_keys = [c['original_chunk_id'] for c in changes if c.get('original_chunk_id')]
    if len(old_keys) != len(set(old_keys)):
        raise ValueError('each predecessor may be replaced only once')
    # Validate the same minimal assertion shape as compact intake, not the
    # much larger legacy Reader Note shape.
    for change in changes:
        item = change['item']
        schema = read(Path(__file__).resolve().parents[1] / 'schemas/fast_pdf_analysis.schema.json')['properties']['items']['items']
        import jsonschema
        jsonschema.validate(item, schema)
        if item['kind'] not in KINDS or not item['id'].strip() or not item['text_zh'].strip():
            raise ValueError('supported nonempty atomic assertion required')
        key = change.get('original_chunk_id')
        if key:
            old = packet['assertions'].get(key)
            if old is None or item['id'] != old['row']['item_id'] or INDEX_KINDS[item['kind']] != old['row']['kind']:
                raise ValueError('replacement must preserve exact predecessor identity and kind')
        elif any(a['row']['item_id'] == item['id'] for a in packet['assertions'].values()):
            raise ValueError('addition reuses an existing item ID; declare an explicit replacement')
        if not change.get('source_check_ids') or not change.get('rationale_zh'):
            raise ValueError('correction/addition requires original source checks and rationale')
        checks = {e['event_id']: e for e in events if e['kind'] == 'source_check'}
        if any(identifier not in checks for identifier in change['source_check_ids']):
            raise ValueError('correction points to an absent source check')
    source = packet['source']
    prior_manifest = read(output / 'revision_manifest.json') if (output / 'revision_manifest.json').exists() else {}
    manifest = {'schema': 'reading-revision-manifest/1', 'created_at': prior_manifest.get('created_at', now()),
                'session_path': str(session_dir), 'session_sha256': digest(session_dir / 'session.json'),
                'source_id': source['source_id'], 'work_id': packet['work_id'],
                'predecessor': source, 'assertions': packet['assertions'],
                'proposal_sha256': object_digest(proposal),
                'derivatives': material_inventory(source)}
    model = {'schema_version': 'reading_revision_pack_v1', 'paper_id': 'pdf:' + source['source_pdf_sha256'],
             'title': source['title'], 'authors': source['authors'], 'year': source['year'], 'doi': source['doi'],
             'source_pdf_sha256': source['source_pdf_sha256'], 'project_profile': source['project_profile'],
             'zotero_item_key': source['zotero_item_key'], 'zotero_attachment_key': source['zotero_attachment_key'],
             'status': 'draft', 'human_checked': False, 'coverage_status': 'atomic_revision_only',
             'generation': {'producer_task_id': proposal['producer_id'], 'revision': {
                 'parent_source_id': source['source_id'], 'parent_model_sha256': source['model_sha256']}},
             'items': [copy.deepcopy(c['item']) for c in changes]}
    write_new(output / 'proposal.json', proposal)
    write_new(output / 'revision_manifest.json', manifest)
    write_new(output / '02_machine_evidence_pack.json', model)
    source_row, chunks = project(output)
    rag = '\n'.join(['# ' + source['title'], '', '条目级修订 · draft / NOT HUMAN CHECKED', ''] +
                    [f'<!-- chunk_id: {c["chunk_id"]} -->\n## {c["item_id"]}\n{c["text_zh"]}\n原文：{c["evidence_json"]}\n不能支持：{c["cannot_support"]}\n' for c in chunks])
    rag_path = output / '02_rag_knowledge.md'
    if rag_path.exists() and rag_path.read_text() != rag:
        raise ValueError('revision RAG already differs')
    if not rag_path.exists(): rag_path.write_text(rag)
    task = {'schema': 'reading-revision-review-task/1', 'producer_id': proposal['producer_id'],
            'source_pdf_path': source['source_pdf_path'], 'source_pdf_sha256': source['source_pdf_sha256'],
            'model_path': str(output / '02_machine_evidence_pack.json'),
            'model_sha256': digest(output / '02_machine_evidence_pack.json'),
            'manifest_sha256': digest(output / 'revision_manifest.json'),
            'proposal_sha256': object_digest(proposal),
            'required_checks': list(CHECKS),
            'instructions_zh': '由未起草修订的审查者重新读原文。逐条核对完整断言、条件、数值单位、归属、认识论层次与复合断言。核对新增项是否原文支持、是否与已有内容重复。图像断言须实际看原图。不得从原入库摘要或人类回答推定通过。返回 revision_review.json，保留真实 reviewer_id 与 source_passage。',
            'output_contract': {'schema': 'reading-revision-review/1', 'decision': 'pass|revise',
                                'blocking_findings': [], 'model_sha256': task_hash(output),
                                'manifest_sha256': digest(output / 'revision_manifest.json'),
                                'producer_id': proposal['producer_id'], 'reviewer_id': '<actual independent reviewer>',
                                'items': '<one item_id and complete original-source review per revised item>'}}
    write_new(output / 'reviewer-task.json', task)
    return {'status': 'awaiting_independent_review_and_human_revision_approval', 'revision': str(output),
            'proposal_sha256': object_digest(proposal), 'source_id': source_row['source_id'],
            'new_assertions': len(chunks), 'original_artifacts_modified': False}


def task_hash(directory):
    return digest(Path(directory) / '02_machine_evidence_pack.json')


def project(directory):
    from knowledge_system_core import normalized_paper_chunk, finalize_source_hashes
    directory = Path(directory).resolve()
    model = read(directory / '02_machine_evidence_pack.json')
    manifest = read(directory / 'revision_manifest.json')
    if model.get('schema_version') != 'reading_revision_pack_v1':
        raise ValueError('not an atomic reading revision pack')
    parent = manifest['predecessor']
    pack_sha = digest(directory / '02_machine_evidence_pack.json')
    sid = 'pdf:' + parent['source_pdf_sha256'] + ':version:' + pack_sha
    source = dict(parent)
    provenance = {'ingestion_mode': 'reading_revision', 'work_id': manifest['work_id'],
                  'independent_scientific_source': False, 'revision_parent_source_id': parent['source_id'],
                  'revision_parent_model_sha256': parent['model_sha256'],
                  'coverage_status': 'atomic_revision_only', 'coverage_gaps': ['本版本只包含明确修订或补充的条目。'],
                  'original_to_indexed_chunk_ids': {i['id']: sid + '::' + i['id'] for i in model['items']},
                  'scientific_acceptance': 'NOT HUMAN CHECKED'}
    source.update(source_id=sid, source_path=str(directory), model_sha256=pack_sha,
                  rag_sha256=digest(directory / '02_rag_knowledge.md') if (directory / '02_rag_knowledge.md').exists() else '',
                  status='draft', updated_at=manifest['created_at'], provenance_json=canonical(provenance))
    chunks = []
    for item in model['items']:
        normalized = dict(item, chunk_id=sid + '::' + item['id'], source_kind='paper',
                          support_level='direct', verification_status='anchored')
        chunks.append(normalized_paper_chunk(sid, model, KINDS[item['kind']], normalized))
    finalize_source_hashes([source], chunks)
    return source, chunks


def checked_revision(directory, con):
    from apply_knowledge_reviews import validate_review
    directory = Path(directory).resolve()
    manifest = read(directory / 'revision_manifest.json')
    proposal = read(directory / 'proposal.json')
    model = read(directory / '02_machine_evidence_pack.json')
    review = read(directory / 'revision_review.json')
    session_dir = Path(manifest['session_path'])
    session, events, _ = session_state(session_dir)
    if digest(session_dir / 'session.json') != manifest['session_sha256']:
        raise ValueError('session binding changed')
    packet = check_current(session, con)
    if packet != {'source': manifest['predecessor'], 'work_id': manifest['work_id'], 'assertions': manifest['assertions']}:
        raise ValueError('revision predecessor changed')
    if object_digest(proposal) != manifest['proposal_sha256'] or model['items'] != [c['item'] for c in proposal['changes']]:
        raise ValueError('revision proposal or generated assertion semantics changed')
    if (model['generation']['producer_task_id'] != proposal['producer_id']
            or model['generation']['revision'] != {'parent_source_id': packet['source']['source_id'], 'parent_model_sha256': packet['source']['model_sha256']}):
        raise ValueError('revision generation lineage differs')
    for key in ('title', 'authors', 'year', 'doi', 'source_pdf_sha256', 'project_profile', 'zotero_item_key', 'zotero_attachment_key'):
        if model[key] != packet['source'][key]:
            raise ValueError('revision identity changed: ' + key)
    approval = [e for e in events if e['kind'] == 'revision_approval' and e['proposal_sha256'] == manifest['proposal_sha256']]
    if not approval:
        raise ValueError('explicit human approval of this exact revision proposal is required')
    if (review.get('schema') != 'reading-revision-review/1' or review.get('decision') != 'pass'
            or review.get('blocking_findings') != [] or review.get('model_sha256') != task_hash(directory)
            or review.get('manifest_sha256') != digest(directory / 'revision_manifest.json')
            or review.get('producer_id') != proposal['producer_id'] or not review.get('reviewer_id')
            or review['reviewer_id'] == proposal['producer_id']):
        raise ValueError('independent revision PASS must bind current content and manifest')
    source, chunks = project(directory)
    if availability(source)['state'] != 'valid':
        raise ValueError('revision artifacts unavailable')
    expected_rag = '\n'.join(['# ' + source['title'], '', '条目级修订 · draft / NOT HUMAN CHECKED', ''] +
                            [f'<!-- chunk_id: {c["chunk_id"]} -->\n## {c["item_id"]}\n{c["text_zh"]}\n原文：{c["evidence_json"]}\n不能支持：{c["cannot_support"]}\n' for c in chunks])
    if (directory / '02_rag_knowledge.md').read_text() != expected_rag:
        raise ValueError('revision RAG does not match reviewed assertions')
    items = review.get('items', [])
    if len({i['item_id'] for i in items}) != len(items) or {i['item_id'] for i in items} != {c['item_id'] for c in chunks}:
        raise ValueError('exactly one independent review per new assertion required')
    qualities = []
    cache = {}
    for chunk in chunks:
        item = next(i for i in items if i['item_id'] == chunk['item_id'])
        actual = dict(item['review'])
        if actual.get('reviewer_id') != review['reviewer_id'] or actual.get('producer_id') != proposal['producer_id']:
            raise ValueError('atomic reviewer identity differs')
        record = {'state': 'verified', 'semantic_checked': True, 'chunk_id': chunk['chunk_id'],
                  'source_id': source['source_id'], 'inputs': input_binding(source | chunk | {'source_authority': source['authority']}),
                  'review': actual, 'event': 'reading_revision', 'observed_at': now()}
        validate_review(record, source | chunk | {'source_authority': source['authority']}, cache)
        producer_item = next(i for i in model['items'] if i['id'] == chunk['item_id'])
        if producer_item.get('evidence_basis') == 'image' or producer_item.get('visual_verified') is True:
            seen = item.get('visual_inspections', [])
            if not set(producer_item['evidence']['pdf_page_indices']) <= {v['page'] for v in seen}:
                raise ValueError('revised image assertion lacks actual visual inspection')
            for view in seen:
                if view.get('source_pdf_sha256') != source['source_pdf_sha256'] or digest(Path(view['path'])) != view['sha256']:
                    raise ValueError('revised image inspection binding differs')
        qualities.append(record)
    return manifest, proposal, source, chunks, qualities, approval[-1]


def publish(directory, *, confirmed=False, lock_path=DEFAULT_WORKFLOW_LOCK):
    directory = Path(directory).resolve()
    manifest = read(directory / 'revision_manifest.json')
    session, _, _ = session_state(Path(manifest['session_path']))
    database = Path(session['database'])
    receipt_path = directory / 'publication.json'
    with connect(database) as con:
        source, _ = project(directory)
        stored = con.execute('SELECT record_json FROM reading_publications WHERE source_id=?', (source['source_id'],)).fetchone() if table_exists(con, 'reading_publications') else None
    if receipt_path.exists() or stored:
        prior = json.loads(stored[0]) if stored else read(receipt_path)
        # Idempotency is based on exact persisted content, not just receipt existence.
        source, chunks = project(directory)
        with connect(database) as con:
            row = con.execute('SELECT model_sha256,rag_sha256 FROM sources WHERE source_id=?', (source['source_id'],)).fetchone()
            if (prior.get('model_sha256') != source['model_sha256'] or prior.get('rag_sha256') != source['rag_sha256']
                    or row is None or tuple(row) != (source['model_sha256'], source['rag_sha256'])
                    or prior.get('review_sha256') != digest(directory / 'revision_review.json')
                    or prior.get('manifest_sha256') != digest(directory / 'revision_manifest.json')):
                raise ValueError('publication receipt no longer matches current version')
            actual = [dict(r) for r in con.execute('SELECT * FROM chunks WHERE source_id=? ORDER BY chunk_id', (source['source_id'],))]
            if actual != sorted(chunks, key=lambda c: c['chunk_id']):
                raise ValueError('published assertion readback differs')
            if con.execute('SELECT count(*) FROM chunks_fts WHERE chunk_id IN (' + ','.join('?' for _ in chunks) + ')', [c['chunk_id'] for c in chunks]).fetchone()[0] != len(chunks):
                raise ValueError('published FTS readback differs')
        prior = dict(prior, postcommit_readback=True)
        if confirmed and not receipt_path.exists(): write_new(receipt_path, prior)
        result = dict(prior, status='noop')
        if confirmed:
            from literature_cross_validation import post_publish
            result['review_followup'] = post_publish(database, [source['source_id']], directory.parent / (directory.name + '-review-followup'))
        return result
    with workflow_lock(lock_path), workflow_lock(database.with_suffix(database.suffix + '.write.lock')):
        with connect(database, writable=confirmed) as con:
            con.execute('BEGIN IMMEDIATE' if confirmed else 'BEGIN')
            manifest, proposal, source, chunks, qualities, approval = checked_revision(directory, con)
            if con.execute('SELECT 1 FROM sources WHERE source_id=?', (source['source_id'],)).fetchone():
                raise ValueError('version exists without a publication receipt; inspect before recovery')
            old_keys = [c['original_chunk_id'] for c in proposal['changes'] if c.get('original_chunk_id')]
            chunk_ids = old_keys + [c['chunk_id'] for c in chunks]
            result = {'status': 'preview', 'source_id': source['source_id'], 'new_assertions': len(chunks),
                      'replaced_assertions': len(old_keys), 'model_sha256': source['model_sha256'], 'rag_sha256': source['rag_sha256'],
                      'human_acceptance_inherited': False, 'scientific_promotion': False}
            if not confirmed:
                return result
            before = _logical_digest(con, [source['source_id']], chunk_ids)
            backup = recovery_backup(database, directory / 'pre_publication.sqlite3')
            initialize_quality(con); initialize_reading(con)
            from knowledge_system_core import insert_source_and_chunks
            insert_source_and_chunks(con, source, chunks)
            for record in qualities:
                write_record(con, 'chunk_quality', record['chunk_id'], record)
            write_record(con, 'source_quality', source['source_id'], {
                'state': 'verified', 'state_scope': 'atomic_summary', 'event': 'reading_revision',
                'inputs': {k: source[k] for k in ('source_pdf_sha256', 'model_sha256', 'rag_sha256')},
                'source_availability': availability(source), 'work_id': work_identity(source)})
            for change in proposal['changes']:
                old_key = change.get('original_chunk_id')
                if not old_key:
                    continue
                new = next(c for c in chunks if c['item_id'] == change['item']['id'])
                pair = {'schema': 'reading-replacement/1', 'chunk_id': old_key, 'new_chunk_id': new['chunk_id'],
                        'original_inputs': manifest['assertions'][old_key]['inputs'],
                        'new_inputs': input_binding(source | new | {'source_authority': source['authority']}),
                        'work_id': manifest['work_id'], 'revision_path': str(directory),
                        'manifest_sha256': digest(directory / 'revision_manifest.json'),
                        'review_sha256': digest(directory / 'revision_review.json'),
                        'approval_event_id': approval['event_id']}
                con.execute('INSERT INTO reading_replacements VALUES (?,?,?)', (old_key, new['chunk_id'], canonical(pair)))
                previous = con.execute('SELECT record_json FROM chunk_quality WHERE chunk_id=?', (old_key,)).fetchone()
                old_record = json.loads(previous[0]) if previous else {}
                write_record(con, 'chunk_quality', old_key, dict(old_record, state='quarantined',
                             event='superseded_by_reviewed_revision', replacement=pair, observed_at=now()))
            # Explicitly mark the old reading material as potentially stale. No
            # generated note/translation is silently represented as repaired.
            for artifact in manifest['derivatives']:
                if artifact['state'] == 'missing':
                    continue
                impact = {'schema': 'reading-artifact-impact/1', 'artifact': artifact,
                          'state': 'needs_review_after_revision', 'affected_chunk_ids': old_keys,
                          'revision_source_id': source['source_id'], 'revision_path': str(directory)}
                key = object_digest(impact)
                for sid in (manifest['source_id'], source['source_id']):
                    con.execute('INSERT INTO reading_artifact_impacts VALUES (?,?,?)', (key + ':' + sid, sid, canonical(impact)))
            result.update(status='pass', postcommit_readback=False,
                          review_sha256=digest(directory / 'revision_review.json'),
                          manifest_sha256=digest(directory / 'revision_manifest.json'),
                          **recovery_backup_metadata(backup))
            con.execute('INSERT INTO reading_publications VALUES (?,?)', (source['source_id'], canonical(result)))
            after = _logical_digest(con, [source['source_id']], chunk_ids)
            for table, value in before.items():
                if table != 'reading_artifact_impacts' and after.get(table) != value:
                    raise ValueError('unrelated rows changed during atomic revision: ' + table)
            if dict(con.execute('SELECT * FROM sources WHERE source_id=?', (manifest['source_id'],)).fetchone()) != manifest['predecessor']:
                raise ValueError('original source was modified')
            con.commit()
        with connect(database) as con:
            if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('postcommit integrity failure')
            actual = [dict(r) for r in con.execute('SELECT * FROM chunks WHERE source_id=? ORDER BY chunk_id', (source['source_id'],))]
            if actual != sorted(chunks, key=lambda c: c['chunk_id']):
                raise ValueError('postcommit revised assertion mismatch')
            if con.execute('SELECT count(*) FROM chunks_fts WHERE chunk_id IN (' + ','.join('?' for _ in chunks) + ')', [c['chunk_id'] for c in chunks]).fetchone()[0] != len(chunks):
                raise ValueError('postcommit FTS mismatch')
        result['postcommit_readback'] = True
        write_new(receipt_path, result)
    from literature_cross_validation import post_publish
    result['review_followup'] = post_publish(database, [source['source_id']], directory.parent / (directory.name + '-review-followup'))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare'); p.add_argument('--session', type=Path, required=True); p.add_argument('--proposal', type=Path, required=True); p.add_argument('--output', type=Path, required=True)
    p = sub.add_parser('publish'); p.add_argument('--revision', type=Path, required=True); p.add_argument('--confirmed', action='store_true')
    a = parser.parse_args()
    value = prepare(a.session, a.proposal, a.output) if a.command == 'prepare' else publish(a.revision, confirmed=a.confirmed)
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
