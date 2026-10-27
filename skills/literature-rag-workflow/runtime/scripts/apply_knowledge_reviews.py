#!/usr/bin/env python3
"""Import independent, original-PDF reviews with current input bindings.

This validates the review receipt, not the reviewer's scientific judgement.
Literal checks only establish that the recorded passage came from this PDF.
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import re
import sqlite3
import subprocess
from pathlib import Path
from knowledge_quality import (VERSION, STATES, DEFAULT_WORKFLOW_LOCK, availability, canonical,
                               recovery_backup, recovery_backup_metadata, digest, initialize_quality,
                               input_binding, quality_decision, workflow_lock,
                               write_record)
from review_knowledge_quality import DEFAULT_DB, ROOT, rows, write_json

CHECKS = ('whole_assertion', 'conditions_scope', 'numbers_units', 'attribution',
          'epistemic_layer', 'compound_assertion')


def validate_review(record, row, text_cache):
    if record.get('chunk_id') != row['chunk_id'] or record.get('source_id') != row['source_id']:
        raise ValueError('review targets different assertion')
    if record.get('inputs') != input_binding(row):
        raise ValueError('review binding changed: ' + row['chunk_id'])
    if record.get('state') not in STATES or record.get('semantic_checked') is not True:
        raise ValueError('explicit semantic decision required')
    review = record.get('review', {})
    if not isinstance(review, dict) or review.get('method') != 'original_source_semantic_review':
        raise ValueError('original source review required')
    if not review.get('reviewer_id') or not review.get('producer_id') or review['reviewer_id'] == review['producer_id']:
        raise ValueError('independent reviewer required')
    if not review.get('rationale_zh') or not review.get('reviewer_attestation'):
        raise ValueError('actual review rationale and attestation required')
    if any(type(review.get('checks', {}).get(key)) is not bool for key in CHECKS):
        raise ValueError('all six check outcomes required')
    if any(not review.get('check_details', {}).get(key) for key in CHECKS):
        raise ValueError('all six check explanations required')
    if availability(row)['state'] != 'valid':
        raise ValueError('original inputs unavailable: ' + row['source_id'])
    if Path(review.get('source_pdf_path', '')).resolve() != Path(row['source_pdf_path']).resolve():
        raise ValueError('review used a different original PDF')
    path = row['source_pdf_path']
    route = review.get('fresh_text_extraction_route', 'normal')
    if route not in {'normal', 'layout', 'raw'}:
        raise ValueError('unsupported independent text extraction route')
    def fresh_route(selected):
        cache_key = (path, selected)
        if cache_key not in text_cache:
            command = ['pdftotext'] + ([] if selected == 'normal' else ['-' + selected]) + [path, '-']
            fresh = subprocess.run(command, capture_output=True,
                                   check=True, timeout=60).stdout
            text_cache[cache_key] = (hashlib.sha256(fresh).hexdigest(), fresh.decode('utf-8').split('\f'))
        return text_cache[cache_key]
    fresh_sha, pages = fresh_route(route)
    if review.get('fresh_text_sha256') != fresh_sha:
        raise ValueError('review text is not the independently extracted original PDF')
    indices = review.get('source_pages', [])
    if not indices or any(type(page) is not int or page < 1 for page in indices):
        raise ValueError('invalid or empty original page locator')
    if review.get('source_passage_routes') is None and any(page > len(pages) or not pages[page-1].strip() for page in indices):
        raise ValueError('invalid or empty original page locator')
    passage = review.get('source_passage', '')
    sections = re.split(r'PDF PAGE (\d+)\n', passage)
    if sections[0].strip() or len(sections) < 3 or len(sections) % 2 != 1:
        raise ValueError('passage must bind actual numbered PDF pages')
    segment_routes = review.get('source_passage_routes')
    route_hashes = review.get('fresh_text_sha256_by_route')
    if segment_routes is not None:
        if (not isinstance(segment_routes, list) or len(segment_routes) != (len(sections)-1)//2
                or any(selected not in {'normal','layout','raw'} for selected in segment_routes)
                or segment_routes[0] != route or not isinstance(route_hashes,dict)
                or set(route_hashes) != set(segment_routes)):
            raise ValueError('invalid explicit per-excerpt extraction binding')
        for selected in set(segment_routes):
            if fresh_route(selected)[0] != route_hashes[selected]:
                raise ValueError('review text route hash is not the independently extracted original PDF')
        if route_hashes[route] != review['fresh_text_sha256']:
            raise ValueError('primary and per-excerpt text hashes disagree')
    elif route_hashes is not None:
        raise ValueError('per-route hashes need explicit excerpt routes')
    located = []
    for offset in range(1, len(sections), 2):
        page, body = int(sections[offset]), sections[offset + 1]
        selected_pages = fresh_route(segment_routes[(offset-1)//2])[1] if segment_routes is not None else pages
        if (page not in indices or not 1 <= page <= len(selected_pages) or not body.strip()
                or re.sub(r'\s+', '', body) not in re.sub(r'\s+', '', selected_pages[page - 1])):
            raise ValueError('recorded passage absent from bound original page')
        located.append(page)
    if set(located) != set(indices):
        raise ValueError('source passage does not cover every stated page')
    if record['state'] == 'verified':
        decision = quality_decision(row | {'content_review_json': canonical(record)})
        if not decision['eligible']:
            raise ValueError('verified record does not meet source-support contract')
    elif record['state'] in {'needs_review', 'quarantined'} and review.get('support') == 'supported':
        raise ValueError('blocking state needs an explicit unresolved/unsupported explanation')


def import_reviews(database: Path, decisions: Path, output: Path, *, confirmed=False,
                   lock_path=None):
    raw = decisions.read_bytes()
    records = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    identifiers = [record['chunk_id'] for record in records]
    if not records or len(set(identifiers)) != len(identifiers):
        raise ValueError('nonempty, unique assertion review records required')
    receipt = {'status': 'preview', 'database': str(database.resolve()),
               'decisions_sha256': hashlib.sha256(raw).hexdigest(), 'records': len(records),
               'canonical_or_zotero_modified': False, 'human_attestation_changed': False}
    lock_path = lock_path or DEFAULT_WORKFLOW_LOCK
    with workflow_lock(lock_path), workflow_lock(database.with_suffix(database.suffix + '.write.lock')):
        with sqlite3.connect(database, timeout=5) as connection:
            connection.execute('BEGIN IMMEDIATE' if confirmed else 'BEGIN')
            current = {row['chunk_id']: row for row in rows(connection)}
            text_cache = {}
            for record in records:
                row = current.get(record['chunk_id'])
                if row is None:
                    raise ValueError('assertion no longer active: ' + record['chunk_id'])
                validate_review(record, row, text_cache)
            if confirmed:
                output.mkdir(parents=True, exist_ok=True)
                backup = output / ('pre_review_' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.sqlite3')
                backup = recovery_backup(database, backup)
                backup_info = recovery_backup_metadata(backup)
                initialize_quality(connection)
                for record in records:
                    key = record['chunk_id']
                    previous = connection.execute('SELECT record_json FROM chunk_quality WHERE chunk_id=?', (key,)).fetchone()
                    merged = json.loads(previous[0]) if previous else {}
                    # Keep mechanical findings, while preserving the separate original-source decision.
                    mechanical = merged.get('mechanical_checked', False)
                    merged.update(record)
                    merged['mechanical_checked'] = mechanical or record.get('mechanical_checked', False)
                    write_record(connection, 'chunk_quality', key, merged)
                    if connection.execute('SELECT record_json FROM chunk_quality WHERE chunk_id=?', (key,)).fetchone()[0] != canonical(merged):
                        raise ValueError('transactional review readback failed')
                # Source-level state summarizes remaining work; it never substitutes for an atomic review.
                for source_id in sorted({record['source_id'] for record in records}):
                    states = [json.loads(value)['state'] for (value,) in connection.execute('''
                        SELECT q.record_json FROM chunk_quality q JOIN chunks c ON c.chunk_id=q.chunk_id
                        WHERE c.source_id=? AND c.tombstoned=0''', (source_id,))]
                    previous = connection.execute('SELECT record_json FROM source_quality WHERE source_id=?', (source_id,)).fetchone()
                    summary = json.loads(previous[0]) if previous else {'checker_version': VERSION}
                    # Preserve an explicit or historical whole-source quarantine.
                    # A bad local figure/claim never quarantines independently
                    # verified, unrelated content in the same valid original.
                    whole_source_quarantine = (summary.get('state') == 'quarantined'
                                               and summary.get('state_scope') != 'atomic_summary')
                    aggregate = ('quarantined' if 'quarantined' in states else 'needs_review'
                                 if 'needs_review' in states else 'unchecked' if 'unchecked' in states else 'verified')
                    summary['atomic_state_counts'] = {state: states.count(state) for state in sorted(STATES)}
                    if not whole_source_quarantine:
                        summary['state'] = aggregate
                        summary['state_scope'] = 'atomic_summary'
                    summary['atomic_semantic_review_count'] = sum(1 for (value,) in connection.execute('''
                        SELECT q.record_json FROM chunk_quality q JOIN chunks c ON c.chunk_id=q.chunk_id
                        WHERE c.source_id=? AND c.tombstoned=0''', (source_id,))
                        if json.loads(value).get('semantic_checked'))
                    write_record(connection, 'source_quality', source_id, summary)
                connection.commit()
                with sqlite3.connect(database) as readback:
                    checked = dict(readback.execute('SELECT chunk_id,record_json FROM chunk_quality'))
                if any(json.loads(checked[key]).get('review') != record['review'] for key, record in zip(identifiers, records)):
                    raise ValueError('postcommit review readback failed')
                receipt.update(status='pass', **backup_info, postcommit_readback=True,
                               observed_at=dt.datetime.now(dt.timezone.utc).isoformat())
                write_json(output / ('review_receipt_' + receipt['decisions_sha256'][:16] + '.json'), receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=DEFAULT_DB)
    parser.add_argument('--decisions', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--confirmed', action='store_true')
    args = parser.parse_args()
    print(json.dumps(import_reviews(args.database, args.decisions, args.output,
                                    confirmed=args.confirmed), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
