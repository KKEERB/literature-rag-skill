#!/usr/bin/env python3
"""Read-only stage/monthly validation with explicit semantic work and frozen benchmark."""
from __future__ import annotations
import argparse
import collections
import datetime as dt
import json
import os
from pathlib import Path
import shutil

import reading_sessions as sessions
from audit_knowledge_integrity import audit_database, fresh_page_text, page_numbers, normalize_doi
from knowledge_quality import attach_reviews, digest, input_binding, object_digest, work_identity
from knowledge_system_core import get_chunk, load_config, run_benchmark, search_database
from review_checklists import rank

CHECKS = ('complete_support', 'conditions', 'numbers_units', 'attribution', 'claim_strength', 'locator_identity')
DEFAULT_OUTPUT = Path(os.environ.get('LITERATURE_RAG_HOME', Path.home() / '.literature-rag-workflow')).expanduser() / 'reports/literature_cross_validation'


def inventory(database):
    with sessions.connect(database) as con:
        rows = sessions.annotate_reading(con, attach_reviews(con, list(con.execute(sessions.JOIN))))
        replacements = dict(con.execute('SELECT chunk_id,new_chunk_id FROM reading_replacements')) if sessions.table_exists(con, 'reading_replacements') else {}
    return rows, replacements


def fingerprint(rows):
    return object_digest({r['chunk_id']: {'inputs': input_binding(r),
        'human_review_id': (r.get('item_human_review', {}).get('current') or {}).get('review_id')} for r in rows})


def sample(rows, previous=None, *, period, limit=20):
    previous = previous or {}
    groups = collections.defaultdict(list)
    for row in rows:
        if (row['source_type'] == 'paper_evidence' and not row.get('reading_replacement')
                and row['source_kind'] not in {'project', 'external'}
                and row['epistemic_status'] not in {'codex_inference', 'project_application'}):
            groups[work_identity(row)].append(row)
    current = {key: fingerprint(value) for key, value in groups.items()}
    old = previous.get('inventory', {})
    disputed = {key for key, value in groups.items() if any(r['item_human_review']['state'] in {'needs_revision', 'unresolved'} for r in value)}
    changed = {key for key in groups if old.get(key) != current[key]}
    rotated = sorted(groups, key=lambda k: (previous.get('last_sampled', {}).get(k, ''), object_digest([period, k])))
    selected = []
    def take(keys, count, group):
        for key in keys:
            if len([v for v in selected if v['group'] == group]) >= count or len(selected) >= limit: break
            if any(v['work_id'] == key for v in selected): continue
            candidates = sorted(groups[key], key=lambda r: (r['item_human_review']['state'] not in {'needs_revision', 'unresolved'}, -rank(r)[0], r['chunk_id']))
            selected.append({'work_id': key, 'group': group, 'chunk_ids': [r['chunk_id'] for r in candidates[:3]]})
    take(sorted(changed - disputed), 10, 'new_or_changed')
    take(sorted(disputed), 5, 'disputed')
    take([key for key in rotated if key not in changed and key not in disputed], 5, 'historical_rotation')
    take(rotated, limit, 'quota_fill')
    return selected, current


def retrieval_checks(database, rows, replacements):
    checks = []
    for row in rows:
        if row['source_type'] != 'paper_evidence': continue
        key = row['chunk_id']
        result = get_chunk(Path(database), key)
        expected = key
        visited = set()
        while expected in replacements and expected not in visited:
            visited.add(expected); expected = replacements[expected]
        valid = bool(result and result['chunk_id'] == expected)
        details = []
        if not valid: details.append('active_version_lookup_failed')
        if result:
            if result['item_human_review'].get('invalid_record_count'):
                details.append('human_review_record_binding_invalid')
            queries = json.loads(row.get('retrieval_queries_zh_json') or '[]')
            query = queries[0] if queries else row['text_zh'] or row['text']
            response = search_database(Path(database), query, top_k=20)
            results = response['results']
            hit = next((item for item in results if item['chunk_id'] == expected), None)
            if hit is None and result['item_human_review']['state'] == 'not_reviewed':
                hit = next((item for item in results if all(item[field] == result[field] for field in
                    ('work_id', 'kind', 'text', 'epistemic_status', 'evidence_anchor'))
                    and item['item_human_review']['state'] == 'not_reviewed'), None)
            # A known replacement must never reappear as active text.
            if any(item['chunk_id'] in replacements for item in results): details.append('superseded_content_returned')
            if result.get('citation_allowed') and not hit: details.append('current_assertion_not_retrieved')
            state = result['item_human_review']['state']
            if state in {'needs_revision', 'unresolved'}:
                comment = result['item_human_review']['current'].get('rationale_zh', '')
                for emitted in [result] + ([hit] if hit else []):
                    if (not emitted['citation'].get('qualification_required')
                            or not emitted.get('citation_qualification')
                            or comment not in emitted.get('answer_text', '')
                            or emitted.get('item_human_accepted')
                            or (emitted.get('citation_allowed') and emitted.get('citation_mode') != 'with_human_dissent')):
                        details.append('human_dissent_not_propagated')
            if expected != key and result.get('item_human_accepted'):
                original = row['item_human_review'].get('current')
                current = result['item_human_review'].get('current')
                if original and current and original['review_id'] == current['review_id']:
                    details.append('old_acceptance_inherited')
            valid = valid and not details
        checks.append({'chunk_id': key, 'expected_active_chunk_id': expected,
                       'status': 'pass' if valid else 'fail', 'findings': details})
    return {'status': 'fail' if any(c['status'] == 'fail' for c in checks) else 'pass' if checks else 'not_run',
            'checked_items': len(checks), 'checks': checks}


def fixed_benchmark(database, output, config=None):
    config = dict(config or load_config())
    baseline = Path(config['review_dir']) / 'knowledge_benchmark.json'
    if not baseline.is_file(): return {'status': 'not_run', 'reason': 'fixed_benchmark_missing; explicit baseline creation required'}
    target = Path(output) / 'benchmark'; target.mkdir(parents=True, exist_ok=True)
    copy = target / baseline.name
    if copy.exists() and digest(copy) != digest(baseline): raise ValueError('fixed benchmark differs from saved run')
    if not copy.exists(): shutil.copy2(baseline, copy)
    before = digest(baseline)
    result = run_benchmark(dict(config, review_dir=str(target)), db_override=database, refresh_spec=False)
    if digest(baseline) != before or digest(copy) != before: raise ValueError('fixed benchmark modified')
    return dict(result, baseline_path=str(baseline), baseline_sha256=before, automatically_refreshed=False)


def prepare(database, output, *, mode='stage', source_ids=None, period=None, previous=None, benchmark=False, config=None, semantic_required=True):
    database, output = Path(database).expanduser().resolve(), Path(output).resolve()
    if mode not in {'stage', 'monthly'}: raise ValueError('unknown validation mode')
    if mode == 'stage' and not source_ids: raise ValueError('stage validation requires affected source IDs')
    rows, replacements = inventory(database)
    # Reports must never overwrite knowledge artifacts, PDFs, or the database.
    forbidden = [database] + [Path(r['source_path']).resolve() for r in rows if r['source_path']]
    if any(output == p or p in output.parents for p in forbidden): raise ValueError('report destination overlaps scientific input')
    output.mkdir(parents=True, exist_ok=True)
    if (output / 'plan.json').exists():
        old = sessions.read(output / 'plan.json')
        if old['database_sha256'] != digest(database) or old['mode'] != mode or old['scope_source_ids'] != source_ids:
            raise ValueError('audit input or scope changed; use a new report directory')
        return sessions.read(output / 'report.json')
    prior = sessions.read(previous) if previous else {}
    period = period or dt.datetime.now(dt.timezone.utc).strftime('%Y-%m')
    scoped = rows if mode == 'monthly' else [r for r in rows if r['source_id'] in source_ids]
    if mode == 'monthly': selected, hashes = sample(rows, prior, period=period)
    else:
        selected, hashes = sample(scoped, prior, period=period, limit=len(scoped))
    selected_ids = {key for p in selected for key in p['chunk_ids']}
    lookup = {r['chunk_id']: r for r in rows}
    packets = []
    for key in sorted(selected_ids):
        row = lookup[key]; evidence = json.loads(row['evidence_json'])
        pages = page_numbers(evidence) or []
        texts = {str(p): fresh_page_text(Path(row['source_pdf_path']), p, 'normal') for p in pages}
        packets.append({'chunk_id': key, 'work_id': work_identity(row), 'title': row['title'],
                        'text': row['text_zh'] or row['text'], 'inputs': input_binding(row), 'evidence': evidence,
                        'source_pdf_path': row['source_pdf_path'], 'source_pdf_sha256': row['source_pdf_sha256'],
                        'page_texts': texts, 'page_texts_sha256': object_digest(texts),
                        'item_human_review': row['item_human_review']})
    plan = {'schema': 'literature-cross-validation/1', 'created_at': sessions.now(), 'mode': mode, 'period': period,
            'database': str(database), 'database_sha256': digest(database), 'scope_source_ids': source_ids,
            'inventory': hashes, 'selected_papers': selected, 'semantic_packets': packets,
            'last_sampled': prior.get('last_sampled', {}), 'semantic_checks': list(CHECKS),
            'read_only_scientific_inputs': True, 'automatic_scientific_changes': False}
    sessions.write_new(output / 'plan.json', plan)
    mechanical = audit_database(database, source_ids=source_ids if mode == 'stage' else None)
    mechanical['findings_summary'] = {'by_severity': dict(collections.Counter(f['severity'] for f in mechanical['findings'])),
                                     'by_code': dict(collections.Counter(f['code'] for f in mechanical['findings']))}
    retrieval_rows = scoped if mode == 'stage' else [lookup[key] for key in selected_ids]
    # Also exercise every dissent and replacement, even outside the semantic sample.
    retrieval_rows = list({r['chunk_id']: r for r in retrieval_rows + [r for r in scoped if r['chunk_id'] in replacements or r['item_human_review']['state'] in {'needs_revision', 'unresolved'}]}.values())
    retrieval = retrieval_checks(database, retrieval_rows, replacements)
    baseline = fixed_benchmark(database, output, config) if benchmark or mode == 'monthly' else {'status': 'not_required', 'reason': 'no parser/retrieval rule change in this publication'}
    report = {'schema': 'literature-cross-validation-report/1', 'created_at': sessions.now(), 'mode': mode,
              'plan_path': str(output / 'plan.json'), 'plan_sha256': digest(output / 'plan.json'),
              'database': str(database), 'scientific_inputs_unchanged': digest(database) == plan['database_sha256'],
              'mechanical': mechanical, 'retrieval': retrieval, 'fixed_benchmark': baseline,
              'semantic': {'status': ('pending' if packets else 'not_run') if semantic_required else 'not_required', 'planned_papers': len(selected),
                           'planned_items': len(packets), 'checked_items': 0,
                           'basis': 'fresh_sample_required' if semantic_required else 'publication source-review gate reused; no new semantic check claimed',
                           'instruction': '实际读回 source PDF 和完整上下文；逐条填写六项核对及理由，不能将字面命中当作语义通过。'},
              'inventory': hashes, 'last_sampled': plan['last_sampled'],
              'previous_report': str(Path(previous).resolve()) if previous else None,
              'previous_semantic_findings': prior.get('semantic', {}).get('findings', []),
              'limits': ['机械检查不等于语义正确。', '抽样结论仅适用于已检查条目，不代表全库科学正确。',
                         '自动检查只写独立报告；修订须具体提案、确认与独立原文复核。']}
    checks = (mechanical, retrieval, baseline)
    report['status'] = ('fail' if any(x['status'] == 'fail' for x in checks) or not report['scientific_inputs_unchanged']
                        else 'inconclusive' if any(x['status'] in {'not_run', 'inconclusive'} for x in checks)
                        else 'pending_semantic_review' if semantic_required
                        else 'pass_with_warnings' if mechanical['status'] == 'pass_with_warnings' else 'pass')
    previous_findings = {object_digest(f) for f in prior.get('mechanical', {}).get('findings', [])}
    report['new_mechanical_findings'] = [f for f in mechanical['findings'] if object_digest(f) not in previous_findings]
    old_retrieval_failures = {c['chunk_id'] for c in prior.get('retrieval', {}).get('checks', []) if c['status'] == 'fail'}
    new_retrieval_failures = {c['chunk_id'] for c in retrieval['checks'] if c['status'] == 'fail'}
    checked_now = {c['chunk_id'] for c in retrieval['checks']}
    report['retrieval_changes'] = {'new_failures': sorted(new_retrieval_failures - old_retrieval_failures),
        'recovered': sorted((old_retrieval_failures & checked_now) - new_retrieval_failures),
        'previous_failures_not_rechecked': sorted(old_retrieval_failures - checked_now)}
    sessions.write_new(output / 'report.json', report)
    sessions.write_new(output / 'semantic-task.json', {'plan_path': str(output / 'plan.json'), 'plan_sha256': digest(output / 'plan.json'),
        'required_output': {'schema': 'literature-semantic-audit/1', 'plan_sha256': digest(output / 'plan.json'),
                            'reviewer_id': '<actual reader>', 'items': '<one record per semantic packet: chunk_id, inputs, page_texts_sha256, checks (six booleans or null), check_details (six source-based explanations), source_pages, source_passage, rationale_zh, suggested_revision_zh (optional)>'},
        'instructions_zh': '检查完整论点、条件、数字单位、观测或解释的归属、结论强度和定位。原文提取不足时查看 PDF；无法判断用 null，不写通过。修订仅作为建议，不写回科学库。'})
    return report


def complete(output, review_path):
    output = Path(output); plan = sessions.read(output / 'plan.json'); report = sessions.read(output / 'report.json')
    review = sessions.read(review_path)
    if (review.get('schema') != 'literature-semantic-audit/1' or not review.get('reviewer_id')
            or review.get('plan_sha256') != digest(output / 'plan.json')
            or digest(Path(plan['database'])) != plan['database_sha256']): raise ValueError('semantic review input binding changed')
    if (output / 'completed-report.json').exists():
        saved = sessions.read(output / 'completed-report.json')
        if saved['semantic']['review_sha256'] != digest(Path(review_path)):
            raise ValueError('completed review differs; preserve it and start a new audit')
        return saved
    packets = {p['chunk_id']: p for p in plan['semantic_packets']}
    records = review.get('items', [])
    if len(records) != len(packets) or {r['chunk_id'] for r in records} != set(packets): raise ValueError('semantic coverage incomplete or duplicated')
    findings, pending = [], []
    for record in records:
        p = packets[record['chunk_id']]
        if (record.get('inputs') != p['inputs'] or record.get('page_texts_sha256') != p['page_texts_sha256']
                or set(record.get('checks', {})) != set(CHECKS) or not record.get('rationale_zh')
                or set(record.get('check_details', {})) != set(CHECKS)
                or any(not isinstance(v, str) or not v.strip() for v in record['check_details'].values())
                or any(v is not None and type(v) is not bool for v in record['checks'].values())):
            raise ValueError('semantic record needs exact bindings and six explained checks')
        if digest(Path(p['source_pdf_path'])) != p['source_pdf_sha256']: raise ValueError('semantic source PDF changed')
        pages = record.get('source_pages', [])
        passage = record.get('source_passage', '')
        all_unread = all(v is None for v in record['checks'].values())
        if all_unread:
            if not record.get('unread_reason'): raise ValueError('unread items require an explicit reason')
        else:
            if not pages or any(type(n) is not int or n < 1 for n in pages): raise ValueError('semantic review needs exact original pages')
            text = '\n'.join(fresh_page_text(Path(p['source_pdf_path']), page, 'normal') or '' for page in pages)
            if not passage.strip() or ''.join(passage.split()) not in ''.join(text.split()): raise ValueError('semantic passage not found in original pages')
        for check, value in record['checks'].items():
            if value is False: findings.append({'chunk_id': record['chunk_id'], 'code': check, 'reason': record['check_details'][check], 'suggested_revision_zh': record.get('suggested_revision_zh', '')})
            if value is None: pending.append({'chunk_id': record['chunk_id'], 'check': check})
    fully_checked = {r['chunk_id'] for r in records if all(v is not None for v in r['checks'].values())}
    checked_works = {p['work_id'] for p in plan['selected_papers'] if set(p['chunk_ids']) <= fully_checked}
    previous_keys = {(f['chunk_id'], f['code']) for f in report.get('previous_semantic_findings', [])}
    current_keys = {(f['chunk_id'], f['code']) for f in findings}
    semantic = {'status': 'fail' if findings else 'inconclusive' if pending else 'pass' if records else 'not_run',
                'checked_items': len(fully_checked), 'checked_papers': len(checked_works), 'attempted_items': len(records),
                'findings': findings, 'unchecked': pending, 'review_path': str(Path(review_path).resolve()),
                'review_sha256': digest(Path(review_path)), 'reviewer_id': review['reviewer_id']}
    final = dict(report, semantic=semantic, completed_at=sessions.now())
    final['semantic_changes'] = {'new_findings': [f for f in findings if (f['chunk_id'], f['code']) not in previous_keys],
        'recovered': [list(key) for key in sorted(previous_keys - current_keys) if key[0] in fully_checked],
        'previous_findings_not_rechecked': [list(key) for key in sorted(previous_keys) if key[0] not in fully_checked]}
    for work in checked_works: final['last_sampled'][work] = plan['period']
    states = [final[k]['status'] for k in ('mechanical', 'retrieval', 'fixed_benchmark', 'semantic')]
    final['status'] = 'fail' if 'fail' in states else 'inconclusive' if any(s in {'not_run', 'inconclusive', 'pending'} for s in states) else 'pass_with_warnings' if 'pass_with_warnings' in states else 'pass'
    sessions.write_new(output / 'completed-report.json', final)
    return final


def _post_publish(database, source_ids, output):
    """Independent recoverable follow-up. Failure must not roll back admitted content."""
    from review_checklists import generate
    output = Path(output); results = []
    for sid in sorted(set(source_ids)):
        try:
            with sessions.connect(database) as con: packet = sessions.source_packet(con, sid)
            version = object_digest({k: a['inputs'] for k, a in packet['assertions'].items()})
            folder = output / 'checklists' / (object_digest(sid)[:12] + '-' + version[:12])
            value = generate(database, packet['source']['source_id'], folder)
            results.append({'source_id': sid, 'status': 'pass', 'checklist': str(folder / 'index.html'), 'items': len(value['items'])})
        except Exception as exc: results.append({'source_id': sid, 'status': 'fail', 'error': str(exc)})
    try:
        identity = object_digest({'database_sha256': digest(Path(database)), 'source_ids': sorted(set(source_ids))})
        directory = output / 'stage-checks' / identity[:20]
        report = prepare(database, directory, source_ids=sorted(set(source_ids)), semantic_required=False)
        audit = {'status': report['status'], 'report': str(directory / 'report.json')}
    except Exception as exc: audit = {'status': 'fail', 'error': str(exc)}
    result = {'checklists': results, 'stage_validation': audit, 'admitted_content_rolled_back': False}
    output.mkdir(parents=True, exist_ok=True)
    path = output / ('followup-' + object_digest(result)[:20] + '.json')
    sessions.write_new(path, result)
    return result


def post_publish(database, source_ids, output):
    try:
        return _post_publish(database, source_ids, output)
    except Exception as exc:
        return {'status': 'followup_failed', 'error': str(exc), 'output': str(output),
                'admitted_content_rolled_back': False, 'retry_entry': 'literature_cross_validation.post_publish'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare'); p.add_argument('--database', type=Path, default=sessions.DEFAULT_DB); p.add_argument('--output', type=Path, required=True); p.add_argument('--mode', choices=['stage', 'monthly'], default='stage'); p.add_argument('--source-id', action='append'); p.add_argument('--period'); p.add_argument('--previous', type=Path); p.add_argument('--benchmark', action='store_true')
    p = sub.add_parser('complete'); p.add_argument('--output', type=Path, required=True); p.add_argument('--review', type=Path, required=True)
    a = parser.parse_args()
    result = complete(a.output, a.review) if a.command == 'complete' else prepare(a.database, a.output, mode=a.mode, source_ids=a.source_id, period=a.period, previous=a.previous, benchmark=a.benchmark)
    print(json.dumps({'status': result['status'], 'output': str(a.output.resolve()), 'semantic': result['semantic']}, ensure_ascii=False, indent=2))
    return 1 if result['status'] == 'fail' else 0


if __name__ == '__main__': raise SystemExit(main())
