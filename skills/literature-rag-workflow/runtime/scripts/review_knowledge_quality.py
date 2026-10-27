#!/usr/bin/env python3
"""Snapshot, exhaustive mechanical checks, explicit semantic-review worklist.

No semantic result is inferred from literal matching. `apply` writes only the
separate quality tables, with optimistic concurrency checks on every binding.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import sqlite3
from pathlib import Path

from audit_knowledge_integrity import audit_database
from knowledge_quality import (VERSION, DEFAULT_WORKFLOW_LOCK, assertion_digest, availability, canonical,
                               consistent_backup, recovery_backup, recovery_backup_metadata, digest, initialize_quality,
                               input_binding, object_digest, work_identity,
                               workflow_lock, write_record)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path.home() / '.codex/knowledge/fte_cusp_sxi/knowledge.sqlite3'


def rows(connection):
    connection.row_factory = sqlite3.Row
    return [dict(row) for row in connection.execute('''
        SELECT c.*,s.source_type,s.source_path,s.source_pdf_path,s.source_pdf_sha256,
               s.model_sha256,s.rag_sha256,s.provenance_json,s.doi,s.zotero_item_key,
               s.project_profile,s.title,s.year,s.authors,s.zotero_attachment_key,
               s.authority AS source_authority
        FROM chunks c JOIN sources s ON s.source_id=c.source_id
        WHERE c.tombstoned=0 AND s.tombstoned=0''')]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def prepare(database: Path, output: Path, fresh_quotes: bool = False) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    snapshot = output / 'baseline.sqlite3'
    consistent_backup(database, snapshot)
    audit = audit_database(snapshot, fresh_quotes=fresh_quotes)
    write_json(output / 'integrity_audit.json', audit)
    with sqlite3.connect(snapshot) as connection:
        all_rows = rows(connection)
        sources = {row['source_id']: dict(row) for row in connection.execute('SELECT * FROM sources WHERE tombstoned=0')}
        existing = {}
        existing_sources = {}
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='chunk_quality'").fetchone():
            existing = {key: json.loads(value) for key, value in connection.execute('SELECT * FROM chunk_quality')}
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='source_quality'").fetchone():
            existing_sources = {key: json.loads(value) for key, value in connection.execute('SELECT * FROM source_quality')}
    findings = collections.defaultdict(list)
    source_findings = collections.defaultdict(list)
    for finding in audit['findings']:
        if finding.get('chunk_id'):
            findings[finding['chunk_id']].append(finding)
        if finding.get('source_id'):
            source_findings[finding['source_id']].append(finding)
    observed = dt.datetime.now(dt.timezone.utc).isoformat()
    cache = {}
    source_records, chunk_records = {}, {}
    for key, source in sources.items():
        available = availability(source, cache)
        issues = source_findings[key]
        source_records[key] = {
            'checker_version': VERSION, 'observed_at': observed, 'mechanical_observed_at': observed, 'work_id': work_identity(source),
            'source_availability': available, 'state_scope': 'source', 'state': 'quarantined' if available['state'] == 'unavailable'
            else 'needs_review' if any(i['severity'] == 'error' for i in issues) else 'unchecked',
            'inputs': {k: source[k] for k in ('source_pdf_sha256', 'model_sha256', 'rag_sha256')},
            'mechanical_findings': issues, 'historical_human_review': json.loads(source['provenance_json']).get('human_review', {})}
        artifact_issues = [i for i in issues if i['code'] == 'project_text_in_paper_contribution']
        source_records[key]['artifact_content_review'] = {
            'state': 'needs_review' if artifact_issues else 'unchecked',
            'semantic_checked': False, 'model_sha256': source['model_sha256'],
            'findings': artifact_issues,
            'scope': 'unindexed_reader_note_and_research_card_fields',
            'atomic_support_decisions_independent': True}
        previous = existing_sources.get(key, {})
        whole_source_quarantine = (previous.get('state') == 'quarantined'
                                   and previous.get('state_scope') != 'atomic_summary')
        if whole_source_quarantine or previous.get('inputs') == source_records[key]['inputs']:
            current = source_records[key]
            if whole_source_quarantine:
                # Keep the actual whole-source decision and its rationale. New
                # filesystem observations must not reinterpret its scope.
                source_records[key] = previous | current
                source_records[key]['state'] = 'quarantined'
                source_records[key]['state_scope'] = previous.get('state_scope', 'source')
                if 'observed_at' in previous:
                    source_records[key]['observed_at'] = previous['observed_at']
                else:
                    source_records[key].pop('observed_at', None)
                if previous.get('inputs') != current['inputs']:
                    source_records[key]['prior_quarantine_inputs'] = previous.get('inputs')
                    source_records[key].pop('revision_relation', None)
            elif previous.get('state_scope') == 'atomic_summary' and current['state'] == 'unchecked':
                # Atomic coverage is separate from fresh source-level defects.
                # A refresh cannot turn one bad figure into a paper quarantine.
                source_records[key] = previous | current
                source_records[key]['state'] = previous['state']
                source_records[key]['state_scope'] = 'atomic_summary'
            if previous.get('inputs') == current['inputs'] and previous.get('revision_relation'):
                source_records[key]['revision_relation'] = previous['revision_relation']
    for row in all_rows:
        issues = findings[row['chunk_id']]
        sr = source_records[row['source_id']]
        source_state = sr['state'] if sr.get('state_scope') != 'atomic_summary' else 'unchecked'
        state = 'quarantined' if source_state == 'quarantined' else 'needs_review' if issues or source_state == 'needs_review' else 'unchecked'
        previous = existing.get(row['chunk_id'], {})
        record = {'checker_version': VERSION, 'observed_at': observed, 'mechanical_observed_at': observed, 'state': state,
                  'inputs': input_binding(row), 'mechanical_checked': True,
                  'semantic_checked': False, 'mechanical_findings': issues,
                  'source_id': row['source_id'], 'work_id': sr['work_id'],
                  'next_action': 'recover_source' if state == 'quarantined' else 'original_source_semantic_review'}
        if previous.get('inputs') == record['inputs'] and (previous.get('semantic_checked')
                                                         or previous.get('state') == 'quarantined'):
            # Retain confirmed reviews and quarantine across incremental mechanical scans.
            # Preserve the complete independent decision and its technical
            # provenance. A new mechanical observation is a separate event.
            record = previous | record
            if 'observed_at' in previous:
                record['observed_at'] = previous['observed_at']
            else:
                record.pop('observed_at', None)
            record.update(state='quarantined' if source_state == 'quarantined' else previous['state'],
                          review=previous.get('review', {}),
                          semantic_checked=previous.get('semantic_checked', False))
        chunk_records[row['chunk_id']] = record
    papers = [r for r in all_rows if r['source_type'] == 'paper_evidence']
    by_work = collections.defaultdict(list)
    for row in papers:
        by_work[work_identity(row)].append(row)
    priority = []
    low_risk = []
    for work, group in sorted(by_work.items()):
        reasons = sorted({i['code'] for r in group for i in findings[r['chunk_id']]} |
                         {i['code'] for r in group for i in source_findings[r['source_id']]
                          if i['severity'] == 'error' or i['code'] == 'project_text_in_paper_contribution'})
        if any(any(term in (r['text_zh'] + r['text']).casefold() for term in (
                'cause', 'mechanism', 'proxy', 'identify', '因果', '机制', '唯一', '代理', '确认')) for r in group):
            reasons.append('mechanism_or_identification_review')
        entry = {'work_id': work, 'source_ids': sorted({r['source_id'] for r in group}),
                 'title': group[0]['title'], 'project': group[0]['project_profile'],
                 'year': group[0]['year'], 'reasons': reasons,
                 'complete_reread': False, 'visual_review': False}
        source = sources[entry['source_ids'][0]]
        model_path = Path(source['source_path']) / '02_machine_evidence_pack.json'
        try:
            model = json.loads(model_path.read_text()) if model_path.is_file() else {}
        except (OSError, ValueError):
            model = {}
        entry['paper_type'] = str(model.get('paper_type', 'unknown')) if isinstance(model, dict) else 'unknown'
        entry['extraction_routes'] = sorted({str(json.loads(r['evidence_json']).get('extraction_route') or 'unknown')
                                              for r in group})
        entry['historical_review_mode'] = json.loads(source['provenance_json']).get('human_review', {}).get('mode', 'none')
        (priority if reasons else low_risk).append(entry)
    # Reproducible round-robin strata, randomized within each stratum by a fixed seed digest.
    strata = collections.defaultdict(list)
    for entry in low_risk:
        source = sources[entry['source_ids'][0]]
        human = json.loads(source['provenance_json']).get('human_review', {}).get('mode', 'none')
        strata[(entry['project'], str(entry['year'])[:3], entry['paper_type'],
                '|'.join(entry['extraction_routes']), human)].append(entry)
    for group in strata.values():
        group.sort(key=lambda item: object_digest({'seed': '20261010-quality-v1', 'work': item['work_id']}))
    sampled = []
    while len(sampled) < min(40, len(low_risk)):
        for key in sorted(strata):
            if strata[key] and len(sampled) < 40:
                sampled.append(strata[key].pop(0))
    plan = {'schema_version': '1.0', 'database': str(database.resolve()),
            'snapshot_sha256': digest(snapshot), 'checker_version': VERSION,
            'checker_sha256': digest(Path(__file__)), 'observed_at': observed,
            'source_records': source_records, 'chunk_records': chunk_records,
            'prior_chunk_quality': {key: object_digest(value) for key, value in existing.items()},
            'prior_source_quality': {key: object_digest(value) for key, value in existing_sources.items()},
            'coverage': {'sources': len(sources), 'chunks': len(all_rows), 'independent_paper_works': len(by_work),
                         'mechanical_checked': len(all_rows), 'semantic_checked': sum(r['semantic_checked'] for r in chunk_records.values()),
                         'priority_reread_works': len(priority), 'stratified_sample_works': len(sampled)},
            'state_distribution': dict(collections.Counter(r['state'] for r in chunk_records.values())),
            'complete': False,
            'limit': 'Full mechanical inventory completed; original-source semantic and full visual rereads require explicit review records.'}
    write_json(output / 'quality_plan.json', plan)
    write_json(output / 'reread_worklist.json', {'priority': priority, 'sample': sampled})
    with (output / 'atomic_review_worklist.jsonl').open('w') as f:
        for row in papers:
            f.write(canonical({'chunk_id': row['chunk_id'], 'source_id': row['source_id'], 'kind': row['kind'],
                               'statement': row['text_zh'] or row['text'], 'epistemic_status': row['epistemic_status'],
                               'source_pdf_path': row['source_pdf_path'], 'evidence': json.loads(row['evidence_json']),
                               'inputs': input_binding(row), 'state': chunk_records[row['chunk_id']]['state']}) + '\n')
    return {k: plan[k] for k in ('coverage', 'state_distribution', 'complete', 'limit')}


def apply(database: Path, plan_path: Path, lock_path: Path) -> dict:
    plan = json.loads(plan_path.read_text())
    if str(database.resolve()) != plan['database']:
        raise ValueError('plan targets a different database')
    backup = plan_path.parent / ('pre_quality_apply_' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.sqlite3')
    with workflow_lock(lock_path), workflow_lock(database.with_suffix(database.suffix + '.write.lock')):
        with sqlite3.connect(database, timeout=5) as connection:
            connection.execute('BEGIN IMMEDIATE')
            current = {r['chunk_id']: r for r in rows(connection)}
            for key, record in plan['chunk_records'].items():
                if key not in current or input_binding(current[key]) != record['inputs']:
                    raise ValueError('source/index changed since baseline: ' + key)
                # Recheck filesystem immediately before writing a valid-source observation.
            cache = {}
            for key, record in plan['source_records'].items():
                source = connection.execute('SELECT * FROM sources WHERE source_id=? AND tombstoned=0', (key,)).fetchone()
                if source is None or availability(source, cache) != record['source_availability']:
                    raise ValueError('source files changed since baseline: ' + key)
            backup = recovery_backup(database, backup)
            backup_info = recovery_backup_metadata(backup)
            initialize_quality(connection)
            for table, field in (('source_quality', 'source_records'), ('chunk_quality', 'chunk_records')):
                for key, record in plan[field].items():
                    previous = connection.execute(f'SELECT record_json FROM {table} WHERE '
                                                  + ('source_id' if table == 'source_quality' else 'chunk_id') + '=?', (key,)).fetchone()
                    current_digest = object_digest(json.loads(previous[0])) if previous else None
                    expected_digest = plan.get('prior_source_quality' if table == 'source_quality' else 'prior_chunk_quality', {}).get(key)
                    if current_digest != expected_digest and current_digest != object_digest(record):
                        raise ValueError('newer quality decision would be overwritten: ' + key)
                    write_record(connection, table, key, record)
            for key, record in plan['chunk_records'].items():
                actual = connection.execute('SELECT record_json FROM chunk_quality WHERE chunk_id=?', (key,)).fetchone()[0]
                if actual != canonical(record):
                    raise ValueError('quality readback failed: ' + key)
            connection.commit()
        with sqlite3.connect(database) as connection:
            readback = dict(connection.execute('SELECT chunk_id,record_json FROM chunk_quality'))
        if not all(readback.get(k) == canonical(v) for k, v in plan['chunk_records'].items()):
            raise ValueError('postcommit readback failed')
    receipt = {'status': 'pass', 'scope': 'separate_quality_tables_only', **backup_info,
               'sources': len(plan['source_records']), 'chunks': len(plan['chunk_records']),
               'postcommit_readback': True, 'canonical_or_zotero_modified': False,
               'unreviewed_content_promoted': False,
               'postbaseline_chunks_pending': len(set(current) - set(plan['chunk_records']))}
    write_json(plan_path.parent / 'quality_apply_receipt.json', receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'apply'))
    parser.add_argument('--database', type=Path, default=DEFAULT_DB)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--fresh-quotes', action='store_true')
    parser.add_argument('--lock', type=Path, default=DEFAULT_WORKFLOW_LOCK)
    parser.add_argument('--confirmed', action='store_true')
    args = parser.parse_args()
    if args.command == 'prepare':
        if not args.output:
            parser.error('--output required')
        result = prepare(args.database, args.output, args.fresh_quotes)
    else:
        if not args.plan or not args.confirmed:
            parser.error('apply requires --plan and --confirmed')
        result = apply(args.database, args.plan, args.lock)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
