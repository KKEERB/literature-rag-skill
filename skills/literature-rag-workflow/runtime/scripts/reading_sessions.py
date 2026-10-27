#!/usr/bin/env python3
"""Source-bound reading conversations. Learning evidence is never scientific evidence.

The host agent supplies actual questions, answers and source-first judgements.
This module validates provenance/sequence, not the truth of a human's knowledge.
No model call, Zotero mutation, or blanket human acceptance is hidden here.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from portable_identity import identity_pair, source_identity

from knowledge_quality import (availability, canonical, digest, input_binding, object_digest,
                               work_identity, workflow_lock, DEFAULT_WORKFLOW_LOCK,
                               recovery_backup, recovery_backup_metadata)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(os.environ.get('LITERATURE_RAG_HOME', str(Path.home() / '.literature-rag-workflow'))).expanduser() / 'knowledge.sqlite3'
STAGES = ('mainline', 'explain', 'compare', 'omissions', 'recap')
DIMENSIONS = ('research_logic', 'methods', 'results', 'mechanisms', 'boundaries', 'transfer')
LEVELS = ('independent', 'with_help', 'needs_reading', 'not_assessed')
DECISIONS = ('faithful', 'needs_revision', 'unresolved')
JOIN = '''SELECT c.*,s.title,s.authors,s.year,s.source_type,s.status,s.project_profile,s.doi,
 s.zotero_item_key,s.zotero_attachment_key,s.source_path,s.source_pdf_path,s.source_pdf_sha256,
 s.model_sha256,s.rag_sha256,s.authority AS source_authority,s.provenance_json
 FROM chunks c JOIN sources s ON s.source_id=c.source_id
 WHERE c.tombstoned=0 AND s.tombstoned=0'''


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    if path.exists():
        if path.read_text(encoding='utf-8') != payload:
            raise ValueError('immutable record already exists: ' + str(path))
    else:
        with path.open('x', encoding='utf-8') as stream:
            stream.write(payload)


def connect(database, *, writable=False):
    con = sqlite3.connect(Path(database).resolve().as_uri() + ('?mode=rw' if writable else '?mode=ro'),
                          uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def table_exists(con, name):
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def initialize_reading(con):
    # Independent statements preserve the caller's transaction.
    con.execute('CREATE TABLE IF NOT EXISTS reading_review_events '
                '(event_id TEXT PRIMARY KEY, chunk_id TEXT NOT NULL, record_json TEXT NOT NULL)')
    con.execute('CREATE INDEX IF NOT EXISTS reading_review_chunk_idx ON reading_review_events(chunk_id)')
    con.execute('CREATE TABLE IF NOT EXISTS reading_replacements '
                '(chunk_id TEXT PRIMARY KEY, new_chunk_id TEXT NOT NULL, record_json TEXT NOT NULL)')
    con.execute('CREATE TABLE IF NOT EXISTS reading_artifact_impacts '
                '(impact_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, record_json TEXT NOT NULL)')
    con.execute('CREATE TABLE IF NOT EXISTS reading_publications '
                '(source_id TEXT PRIMARY KEY, record_json TEXT NOT NULL)')


def source_packet(con, source_id):
    row = con.execute('SELECT * FROM sources WHERE source_id=? AND tombstoned=0', (source_id,)).fetchone()
    if row is None or row['source_type'] != 'paper_evidence':
        raise ValueError('active paper source required')
    source = dict(row)
    # A selected revised fragment still opens the complete paper's effective
    # assertion set. Preserve explicit ancestry; never merge by title alone.
    seen = set()
    while json.loads(source.get('provenance_json') or '{}').get('ingestion_mode') == 'reading_revision':
        if source['source_id'] in seen:
            raise ValueError('cyclic reading revision ancestry')
        seen.add(source['source_id'])
        parent = json.loads(source['provenance_json'])['revision_parent_source_id']
        row = con.execute('SELECT * FROM sources WHERE source_id=? AND tombstoned=0', (parent,)).fetchone()
        if row is None:
            raise ValueError('revision ancestor unavailable')
        source = dict(row)
    if availability(source)['state'] != 'valid':
        raise ValueError('original PDF or knowledge artifact unavailable/changed')
    family = {source['source_id']}
    possible = [dict(r) for r in con.execute('SELECT * FROM sources WHERE tombstoned=0 AND source_pdf_sha256=? AND project_profile=?',
                                           (source['source_pdf_sha256'], source['project_profile']))]
    for _ in range(len(possible)):
        added = {r['source_id'] for r in possible if json.loads(r.get('provenance_json') or '{}').get('ingestion_mode') == 'reading_revision'
                 and json.loads(r['provenance_json']).get('revision_parent_source_id') in family}
        if added <= family: break
        family.update(added)
    chunks = [dict(r) for r in con.execute(JOIN + ' AND c.source_id IN (' + ','.join('?' for _ in family) + ') ORDER BY c.chunk_id', sorted(family))]
    replaced = {r[0] for r in con.execute('SELECT chunk_id FROM reading_replacements')} if table_exists(con, 'reading_replacements') else set()
    chunks = [r for r in chunks if r['chunk_id'] not in replaced]
    for r in chunks:
        if availability(r)['state'] != 'valid':
            raise ValueError('current revised source unavailable/changed')
    if not chunks:
        raise ValueError('source has no active assertions')
    return {'source': source, 'work_id': work_identity(source),
            'assertions': {r['chunk_id']: {'row': r, 'inputs': input_binding(r)} for r in chunks}}


def session_state(directory):
    directory = Path(directory)
    session = read(directory / 'session.json')
    if session.get('schema') != 'reading-session/1':
        raise ValueError('unsupported reading session')
    previous = digest(directory / 'session.json')
    events = []
    for sequence, path in enumerate(sorted((directory / 'events').glob('*.json')), 1):
        event = read(path)
        expected = object_digest({k: v for k, v in event.items() if k != 'event_id'})
        if (path.name != f'{sequence:06d}.json' or event.get('sequence') != sequence
                or event.get('previous_sha256') != previous or event.get('event_id') != expected):
            raise ValueError('conversation event chain changed or incomplete')
        events.append(event)
        previous = digest(path)
    return session, events, previous


def check_current(session, con=None):
    if con is None:
        with connect(session['database']) as connection:
            return check_current(session, connection)
    packet = source_packet(con, session['paper']['source']['source_id'])
    old = session['paper']
    for key, assertion in old['assertions'].items():
        if key not in packet['assertions'] or packet['assertions'][key]['inputs'] != assertion['inputs']:
            raise ValueError('session is stale; assertion/version changed: ' + key)
        if table_exists(con, 'reading_replacements') and con.execute(
                'SELECT 1 FROM reading_replacements WHERE chunk_id=?', (key,)).fetchone():
            raise ValueError('session is stale; reviewed revision supersedes: ' + key)
    if set(packet['assertions']) != set(old['assertions']):
        raise ValueError('session is stale; assertion inventory changed')
    return packet


def material_inventory(source):
    directory = Path(source['source_path'])
    names = ['01_reader_note.zh.md', '08_reader_note.zotero.html',
             '10_fulltext_translation.zh-CN.pdf', 'fulltext_translation/qa.json']
    return [{'role': name, 'path': str(directory / name),
             'state': 'available_not_revalidated' if (directory / name).is_file() else 'missing',
             'sha256': digest(directory / name) if (directory / name).is_file() else None} for name in names]


def delivered_materials(session, events):
    """Prefer explicitly registered reading copies; never relabel scientific data."""
    items = {x['role']: x for x in material_inventory(session['paper']['source'])}
    for event in events:
        if event['kind'] != 'material_delivery':
            continue
        model = Path(event['model_path'])
        package_valid = model.is_file() and digest(model) == event['model_sha256']
        for item in event['materials']:
            path = Path(item['path'])
            valid = package_valid and path.is_file() and digest(path) == item['sha256']
            items[item['role']] = {**item, 'selected_reading_copy': True,
                'state': 'available_not_revalidated' if valid else 'changed_or_missing'}
    return list(items.values())


def register_materials(directory, package):
    package = Path(package).resolve()
    model_path = package / '02_machine_evidence_pack.json'
    model = read(model_path)
    roles = ['01_reader_note.zh.md', '08_reader_note.zotero.html', '03_research_card.zh.md',
             '10_fulltext_translation.zh-CN.pdf', 'fulltext_translation/qa.json',
             'review/translation_review_report.json']
    return record(directory, {'kind': 'material_delivery', 'producer': 'reading_material_preparation',
        'model_path': str(model_path), 'model_sha256': digest(model_path),
        'source_pdf_sha256': model['source_pdf_sha256'],
        'materials': [{'role': role, 'path': str(package / role), 'sha256': digest(package / role)}
                      for role in roles if (package / role).is_file()]})


def start(database, source_id, output, *, reader='user', goal='理解思路、方法、结果、机制和适用边界'):
    output = Path(output).resolve()
    if output.exists():
        session, _, _ = session_state(output)
        if session['database'] != str(Path(database).resolve()) or session['paper']['source']['source_id'] != source_id or session['reader'] != reader:
            raise ValueError('existing session belongs to a different paper, database or reader')
        check_current(session)
        return status(output)
    with connect(database) as con:
        packet = source_packet(con, source_id)
    session = {'schema': 'reading-session/1', 'created_at': now(), 'reader': reader, 'goal': goal,
               'database': str(Path(database).resolve()), 'paper': packet,
               'material_inventory': material_inventory(packet['source']),
               'learning_is_scientific_evidence': False, 'human_review_scope': 'explicit_assertions_only'}
    check_current(session)
    write_new(output / 'session.json', session)
    (output / 'events').mkdir()
    from review_checklists import ensure
    ensure(output)
    render(output)
    return status(output)


def original_passage(session, evidence):
    """Check a literal locator freshly against the PDF, never a prior summary."""
    source = session['paper']['source']
    if digest(Path(source['source_pdf_path'])) != source['source_pdf_sha256']:
        raise ValueError('original PDF changed')
    page, quote = evidence.get('pdf_page_index'), evidence.get('quote')
    route = evidence.get('text_route', 'normal')
    if type(page) is not int or page < 1 or not isinstance(quote, str) or not quote.strip() or route not in {'normal', 'layout', 'raw'}:
        raise ValueError('one original page and a nonempty continuous passage required')
    cmd = ['pdftotext'] + ([] if route == 'normal' else ['-' + route]) + [source['source_pdf_path'], '-']
    raw = subprocess.run(cmd, capture_output=True, check=True, timeout=60).stdout
    pages = raw.decode('utf-8').split('\f')
    if page > len(pages) or ''.join(quote.split()) not in ''.join(pages[page - 1].split()):
        raise ValueError('passage not found on original PDF page')
    return dict(evidence, source_pdf_sha256=source['source_pdf_sha256'], fresh_text_sha256=hashlib.sha256(raw).hexdigest(),
                literal_locator_checked=True, semantic_support_inferred=False)


def by_id(events, identifier, kind=None):
    event = next((e for e in events if e['event_id'] == identifier), None)
    if event is None or kind and event['kind'] != kind:
        raise ValueError('referenced conversation event is missing or has the wrong kind')
    return event


def record(directory, payload):
    directory = Path(directory)
    with workflow_lock(directory / '.session.lock'):
        session, events, previous = session_state(directory)
        check_current(session)
        data = json.loads(json.dumps(payload))
        kind = data.get('kind')
        controls = [e for e in events if e['kind'] == 'control']
        if controls and controls[-1]['action'] in {'pause', 'finish'} and kind not in {'checklist_presentation', 'judgment'} and not (kind == 'control' and data.get('action') == 'resume'):
            raise ValueError('resume the conversation before adding another event')
        if any(k in data for k in ('sequence', 'previous_sha256', 'event_id', 'recorded_at')):
            raise ValueError('event identity fields are managed by the recorder')
        if kind == 'material_delivery':
            source = session['paper']['source']
            model_path = Path(data.get('model_path', ''))
            if not model_path.is_file() or digest(model_path) != data.get('model_sha256'):
                raise ValueError('reading material model binding differs')
            model = read(model_path)
            pair = source_identity(source['source_id'], source.get('zotero_item_key'),
                                   source.get('zotero_attachment_key'))
            if (data.get('source_pdf_sha256') != source['source_pdf_sha256']
                    or model.get('source_pdf_sha256') != source['source_pdf_sha256']
                    or 'zotero_item_key' not in model or 'zotero_attachment_key' not in model
                    or identity_pair(model['zotero_item_key'], model['zotero_attachment_key']) != pair
                    or not data.get('producer') or not data.get('materials')):
                raise ValueError('reading material paper identity differs')
            allowed = {'01_reader_note.zh.md', '08_reader_note.zotero.html', '03_research_card.zh.md',
                       '10_fulltext_translation.zh-CN.pdf', 'fulltext_translation/qa.json',
                       'review/translation_review_report.json'}
            seen = set()
            for item in data['materials']:
                path = Path(item.get('path', ''))
                if (item.get('role') not in allowed or item['role'] in seen
                        or path.resolve() != (model_path.parent / item['role']).resolve()
                        or not path.resolve().is_relative_to(model_path.parent.resolve())
                        or not path.is_file() or digest(path) != item.get('sha256')):
                    raise ValueError('reading material file binding differs')
                seen.add(item['role'])
        elif kind == 'checklist_presentation':
            from review_checklists import ensure, display_text
            checklist = ensure(directory)
            if (data.get('checklist_id') != checklist['checklist_id']
                    or data.get('shown_text') != display_text(directory)
                    or not data.get('shown_to_user_ref')):
                raise ValueError('exact current checklist and actual display reference required')
            data['chunk_ids'] = [i['chunk_id'] for i in checklist['items']]
            data['assertion_inputs'] = {key: session['paper']['assertions'][key]['inputs'] for key in data['chunk_ids']}
            data['source_check_ids'] = []
        elif kind == 'reading_ready':
            if data.get('actor') != session['reader'] or not data.get('user_message_ref') or not data.get('human_statement'):
                raise ValueError('actual human readiness statement required')
        elif kind == 'question':
            if not any(e['kind'] == 'reading_ready' for e in events):
                raise ValueError('human has not said they are ready to discuss this paper')
            if data.get('stage') not in STAGES or not data.get('text'):
                raise ValueError('one question with an explicit stage is required')
            if data['stage'] == 'mainline' and data.get('revealed_chunk_ids'):
                raise ValueError('mainline question must not reveal knowledge answers')
        elif kind in {'answer', 'hint'}:
            by_id(events, data.get('question_event_id'), 'question')
            if not data.get('text'):
                raise ValueError('nonempty actual answer/hint required')
            if kind == 'answer' and (not data.get('user_message_ref') or data.get('actor') != session['reader']):
                raise ValueError('actual human message reference and matching reader required')
        elif kind == 'source_check':
            if not data.get('rationale_zh') or not data.get('checked_by'):
                raise ValueError('source-reading rationale and actor required')
            data['evidence'] = original_passage(session, data.get('evidence', {}))
        elif kind == 'source_outline':
            if not data.get('units') or not data.get('checked_by') or 'unread_scope' not in data:
                raise ValueError('original outline needs source units, actual reader and explicit unread scope')
            for unit in data['units']:
                if not unit.get('label_zh') or not unit.get('source_check_ids'):
                    raise ValueError('each original outline unit needs an original source anchor')
                for identifier in unit['source_check_ids']:
                    by_id(events, identifier, 'source_check')
        elif kind == 'feedback':
            answer = by_id(events, data.get('answer_event_id'), 'answer')
            if data.get('dimension') not in DIMENSIONS or data.get('level') not in LEVELS or not data.get('rationale_zh'):
                raise ValueError('dimension, qualitative feedback and rationale required')
            if data['level'] != 'not_assessed' and not data.get('source_check_ids'):
                raise ValueError('source-first feedback needs actual original checks')
            for identifier in data.get('source_check_ids', []):
                by_id(events, identifier, 'source_check')
            if data['level'] == 'independent' and any(
                    e['kind'] == 'hint' and e['question_event_id'] == answer['question_event_id']
                    and e['sequence'] < answer['sequence'] for e in events):
                raise ValueError('hint-assisted answer cannot be marked independent')
        elif kind == 'comparison':
            if not any(e['kind'] == 'answer' for e in events):
                raise ValueError('invite the human explanation before revealing indexed statements')
            ids = data.get('chunk_ids', [])
            if not ids or len(ids) != len(set(ids)) or not set(ids) <= session['paper']['assertions'].keys():
                raise ValueError('exact nonempty compared assertion IDs required')
            if not data.get('source_check_ids') or not data.get('shown_text') or not data.get('shown_to_user_ref'):
                raise ValueError('comparison must have actual displayed text, message reference and original passages')
            for identifier in data['source_check_ids']:
                by_id(events, identifier, 'source_check')
            for key in ids:
                row = session['paper']['assertions'][key]['row']
                if (row['text_zh'] or row['text']) not in data['shown_text']:
                    raise ValueError('comparison text must contain each exact indexed assertion')
            data['assertion_inputs'] = {key: session['paper']['assertions'][key]['inputs'] for key in ids}
        elif kind == 'judgment':
            comparison = by_id(events, data.get('comparison_event_id'))
            if comparison['kind'] not in {'comparison', 'checklist_presentation'}:
                raise ValueError('judgment requires actually displayed assertions')
            if (data.get('actor') != session['reader'] or not data.get('user_message_ref')
                    or not data.get('human_statement') or data.get('explicit_item_judgment') is not True):
                raise ValueError('explicit human judgment and actual message required; correct answers do not qualify')
            decisions = data.get('decisions', [])
            if not decisions or len({d['chunk_id'] for d in decisions}) != len(decisions):
                raise ValueError('nonempty unique decisions required')
            for decision in decisions:
                if (decision.get('chunk_id') not in comparison['chunk_ids'] or decision.get('outcome') not in DECISIONS
                        or not isinstance(decision.get('rationale_zh', ''), str)):
                    raise ValueError('judgment must name displayed items and outcome; comment is optional')
                decision.setdefault('rationale_zh', '')
            # Retrying the same actual message must not manufacture a second review.
            for prior in events:
                if prior['kind'] == 'judgment' and all(prior.get(k) == data.get(k) for k in
                        ('actor', 'user_message_ref', 'human_statement', 'comparison_event_id', 'decisions')):
                    return prior
        elif kind == 'revision_approval':
            proposal = read(Path(data.get('proposal_path', '')))
            if (data.get('actor') != session['reader'] or not data.get('user_message_ref')
                    or not data.get('human_statement') or data.get('proposal_sha256') != object_digest(proposal)
                    or data.get('explicit_revision_approval') is not True):
                raise ValueError('actual human approval must name the exact displayed revision proposal')
        elif kind in {'omission', 'correction'}:
            if not data.get('statement_zh') or not data.get('source_check_ids'):
                raise ValueError('source-bound correction or omission proposal required')
            for identifier in data['source_check_ids']:
                by_id(events, identifier, 'source_check')
            if kind == 'correction' and data.get('chunk_id') not in session['paper']['assertions']:
                raise ValueError('correction targets an unknown assertion')
            data['publication_status'] = 'proposal_only'
        elif kind == 'personal_note':
            if not data.get('text'):
                raise ValueError('nonempty personal note required')
            data['authority'] = 'personal_understanding_or_hypothesis'
        elif kind == 'control':
            if data.get('action') not in {'pause', 'resume', 'skip', 'finish'}:
                raise ValueError('unknown conversation control')
            if data['action'] == 'skip' and data.get('stage') not in STAGES:
                raise ValueError('skip requires a stage')
        else:
            raise ValueError('unsupported conversation event kind')
        data.update(sequence=len(events) + 1, previous_sha256=previous, recorded_at=now())
        data['event_id'] = object_digest(data)
        write_new(directory / 'events' / f'{data["sequence"]:06d}.json', data)
    render(directory)
    return data


def status(directory):
    session, events, _ = session_state(directory)
    stale = None
    try:
        check_current(session)
    except (ValueError, OSError) as exc:
        stale = str(exc)
    levels = {key: {'level': 'not_assessed', 'rationale_zh': '本次尚未涉及'} for key in DIMENSIONS}
    decisions = {}
    for event in events:
        if event['kind'] == 'feedback':
            levels[event['dimension']] = {'level': event['level'], 'rationale_zh': event['rationale_zh'], 'event_id': event['event_id']}
        if event['kind'] == 'judgment':
            for item in event['decisions']:
                decisions[item['chunk_id']] = dict(item, event_id=event['event_id'])
    controls = [e for e in events if e['kind'] == 'control']
    action = controls[-1]['action'] if controls else None
    return {'schema': 'reading-session-status/1', 'title': session['paper']['source']['title'],
            'state': 'stale' if stale else 'paused' if action == 'pause' else 'finished' if action == 'finish' else 'active',
            'stale_reason': stale, 'events': len(events), 'answers': sum(e['kind'] == 'answer' for e in events),
            'ready_to_discuss': any(e['kind'] == 'reading_ready' for e in events),
            'learning_feedback': levels, 'item_judgments': list(decisions.values()),
            'reviewed_items': len(decisions), 'total_indexed_items': len(session['paper']['assertions']),
            'unreviewed_chunk_ids': sorted(session['paper']['assertions'].keys() - decisions.keys()),
            'unresolved_proposals': [e for e in events if e['kind'] in {'omission', 'correction'}],
            'original_outline': next((e for e in reversed(events) if e['kind'] == 'source_outline'), None),
            'whole_paper_accepted': False, 'scientific_promotion': False,
            'metrics': {'elapsed_reading_minutes': None, 'model_tokens': None, 'cost': None}}


def next_turn(directory, *, discussion=False):
    if not discussion:
        from review_checklists import view
        return view(directory)
    session, events, _ = session_state(directory)
    state = status(directory)
    if state['state'] != 'active':
        return {'state': state['state'], 'reason': state['stale_reason'], 'question': None}
    if not state['ready_to_discuss']:
        return {'state': 'awaiting_reading', 'question': None,
                'instruction': '先按用户当前阅读状态提供材料。用户表示读完或希望开始讨论后，记录真实 reading_ready；不能把尚未阅读评为理解不足。'}
    answered = {e['question_event_id'] for e in events if e['kind'] == 'answer'}
    questions = [e for e in events if e['kind'] == 'question']
    skipped_pending = questions and any(e['kind'] == 'control' and e['action'] == 'skip'
        and e['stage'] == questions[-1]['stage'] and e['sequence'] > questions[-1]['sequence'] for e in events)
    if questions and questions[-1]['event_id'] not in answered and not skipped_pending:
        return {'state': 'awaiting_answer', 'question': questions[-1]['text'], 'question_event_id': questions[-1]['event_id']}
    completed = {e['stage'] for e in questions if e['event_id'] in answered}
    completed.update(e['stage'] for e in events if e['kind'] == 'control' and e['action'] == 'skip')
    stage = next((s for s in STAGES if s not in completed), 'recap')
    prompts = {
        'mainline': '请先用自己的话串起这篇文章的主线：作者要解决什么问题，用了什么关键办法，最后得到什么结论？不必追求完整，拿不准的地方也可以直接说。',
        'explain': '请选择你认为最关键的一张图或一个方法，解释它怎样支撑文章的主要结论，以及这个解释依赖什么条件。',
        'compare': '接下来把你刚才的解释与对应入库条目及原文并排核对：哪些表述准确，哪些条件、归属或结论强度需要修改？',
        'omissions': '对照原文主线和关键图表，是否还有重要的方法条件、负面结果或局限没有进入知识库？',
        'recap': '回看本次讨论，哪些部分你已经能独立解释，哪些还希望回到原文再推敲？'}
    return {'state': 'ready', 'stage': stage, 'question': prompts[stage],
            'agent_protocol': str(ROOT / 'docs/reading_collaboration.md'),
            'source_pdf_path': session['paper']['source']['source_pdf_path'],
            'instruction': '根据实际回答调整下一问；先读原文再评价。此处问题是建议，需实际展示后记录 question。mainline 阶段不要透露入库摘要。'}


def review_records(directory):
    session, events, _ = session_state(directory)
    check_current(session)
    records = []
    for event in events:
        if event['kind'] != 'judgment':
            continue
        comparison = by_id(events, event['comparison_event_id'])
        for decision in event['decisions']:
            key = decision['chunk_id']
            records.append({'schema': 'human-item-review/1', 'review_id': event['event_id'] + ':' + key,
                            'source_id': session['paper']['source']['source_id'], 'chunk_id': key,
                            'inputs': comparison['assertion_inputs'][key], 'reviewer': session['reader'],
                            'outcome': decision['outcome'], 'rationale_zh': decision['rationale_zh'],
                            'reviewed_at': event['recorded_at'], 'session_path': str(Path(directory).resolve()),
                            'session_sha256': digest(Path(directory) / 'session.json'),
                            'judgment_event_id': event['event_id'], 'judgment_file_sha256': digest(Path(directory) / 'events' / f'{event["sequence"]:06d}.json'),
                            'judgment_sequence': event['sequence'], 'human_message_ref': event['user_message_ref'],
                            'source_checks': [by_id(events, x, 'source_check')['evidence'] for x in comparison['source_check_ids']],
                            'scope': 'this_assertion_only', 'scientific_promotion': False})
    return records


def commit_reviews(directory, *, confirmed=False, lock_path=DEFAULT_WORKFLOW_LOCK):
    session, _, _ = session_state(directory)
    records = review_records(directory)
    result = {'status': 'preview', 'records': len(records), 'whole_paper_accepted': False, 'scientific_promotion': False}
    if not records or not confirmed:
        return result
    database = Path(session['database'])
    with workflow_lock(lock_path), workflow_lock(database.with_suffix(database.suffix + '.write.lock')):
        with connect(database, writable=True) as con:
            con.execute('BEGIN IMMEDIATE')
            check_current(session, con)
            records = review_records(directory)
            existing = dict(con.execute('SELECT event_id,record_json FROM reading_review_events')) if table_exists(con, 'reading_review_events') else {}
            for r in records:
                if r['review_id'] in existing and existing[r['review_id']] != canonical(r):
                    raise ValueError('existing human review differs; preserve history')
            new = [r for r in records if r['review_id'] not in existing]
            if not new:
                return dict(result, status='noop')
            from fast_pdf_intake import _logical_digest
            before = _logical_digest(con)
            backup = recovery_backup(database, Path(directory) / 'receipts' / ('pre_review_' + object_digest(new) + '.sqlite3'))
            initialize_reading(con)
            for r in new:
                con.execute('INSERT INTO reading_review_events VALUES (?,?,?)', (r['review_id'], r['chunk_id'], canonical(r)))
            after = _logical_digest(con)
            for table, value in before.items():
                if table != 'reading_review_events' and after.get(table) != value:
                    raise ValueError('unrelated knowledge records changed')
            con.commit()
        with connect(database) as con:
            for r in records:
                if con.execute('SELECT record_json FROM reading_review_events WHERE event_id=?', (r['review_id'],)).fetchone()[0] != canonical(r):
                    raise ValueError('human review postcommit readback failed')
        result.update(status='pass', new_records=len(new), postcommit_readback=True, **recovery_backup_metadata(backup))
        write_new(Path(directory) / 'receipts' / ('reviews_' + object_digest(new) + '.json'), result)
    render(directory)
    return result


def annotate_reading(con, rows):
    """Attach scoped human decisions and immutable replacement history to retrieval."""
    events, replacements, impacts = {}, {}, {}
    if table_exists(con, 'reading_review_events'):
        keys = [r['chunk_id'] for r in rows]
        for offset in range(0, len(keys), 400):
            subset = keys[offset:offset + 400]
            for key, raw in con.execute('SELECT chunk_id,record_json FROM reading_review_events WHERE chunk_id IN (' + ','.join('?' for _ in subset) + ')', subset):
                events.setdefault(key, []).append(json.loads(raw))
    if table_exists(con, 'reading_replacements'):
        replacements = {key: json.loads(raw) for key, raw in con.execute('SELECT chunk_id,record_json FROM reading_replacements')}
    if table_exists(con, 'reading_artifact_impacts'):
        for sid, raw in con.execute('SELECT source_id,record_json FROM reading_artifact_impacts'):
            impacts.setdefault(sid, []).append(json.loads(raw))
    result = []
    session_cache = {}
    for raw in rows:
        row = dict(raw)
        history = events.get(row['chunk_id'], [])
        current = []
        invalid = 0
        for event in history:
            if event.get('inputs') != input_binding(row):
                continue
            folder = Path(event['session_path'])
            try:
                if folder not in session_cache:
                    session_cache[folder] = session_state(folder)
                _, saved_events, _ = session_cache[folder]
                judgment = by_id(saved_events, event['judgment_event_id'], 'judgment')
                comparison = by_id(saved_events, judgment['comparison_event_id'])
                decision = next((d for d in judgment['decisions'] if d['chunk_id'] == row['chunk_id']), {})
                valid = (digest(folder / 'session.json') == event['session_sha256']
                         and digest(folder / 'events' / f'{event["judgment_sequence"]:06d}.json') == event['judgment_file_sha256']
                         and judgment['actor'] == event['reviewer']
                         and event['review_id'] == judgment['event_id'] + ':' + row['chunk_id']
                         and event['human_message_ref'] == judgment['user_message_ref']
                         and event['reviewed_at'] == judgment['recorded_at']
                         and comparison['assertion_inputs'].get(row['chunk_id']) == event['inputs']
                         and decision.get('outcome') == event['outcome']
                         and decision.get('rationale_zh', '') == event['rationale_zh'])
            except (OSError, ValueError, KeyError):
                valid = False
            if valid:
                current.append(event)
            else:
                invalid += 1
        latest = max(current, key=lambda x: (x['reviewed_at'], x['review_id'])) if current else None
        row['item_human_review'] = {'state': latest['outcome'] if latest else 'not_reviewed',
                                   'current': latest, 'historical_record_count': len(history),
                                   'invalid_record_count': invalid,
                                   'scope': 'this_assertion_only'}
        row['reading_replacement'] = replacements.get(row['chunk_id'])
        row['reading_artifact_impacts'] = impacts.get(row['source_id'], [])
        result.append(row)
    return result


def resolve_replacement_candidates(con, rows):
    if not table_exists(con, 'reading_replacements'):
        return rows
    result = {r['chunk_id']: r for r in rows}
    pending = list(result)
    visited = set()
    while pending:
        key = pending.pop()
        if key in visited:
            continue
        visited.add(key)
        pair = con.execute('SELECT new_chunk_id FROM reading_replacements WHERE chunk_id=?', (key,)).fetchone()
        if pair:
            new = con.execute(JOIN + ' AND c.chunk_id=?', (pair[0],)).fetchone()
            if new is not None and pair[0] not in result:
                result[pair[0]] = dict(new, fts_rank=0.0)
                pending.append(pair[0])
    return list(result.values())


def render_note_view(directory, source_path, *, selected=False):
    """Browser wrapper for Zotero's UTF-8 fragment; source artifact stays intact."""
    from v2_core import AllowlistHTMLSanitizer
    class NoteSanitizer(AllowlistHTMLSanitizer):
        allowed_tags = AllowlistHTMLSanitizer.allowed_tags | {'div'}
    parser = NoteSanitizer()
    parser.feed(Path(source_path).read_text(encoding='utf-8'))
    fragment = parser.html()
    start, end = '<h2>答案键</h2>', '<h2>人工审核记录</h2>'
    if fragment.count(start) == 1 and fragment.count(end) == 1:
        a, b = fragment.index(start) + len(start), fragment.index(end)
        if a < b and '<details>' not in fragment[a:b]:
            fragment = fragment[:a] + '<details><summary>完成作答后展开参考答案与原文锚点</summary>' + fragment[a:b] + '</details>' + fragment[b:]
    output = Path(directory) / 'reader-note.html'
    notice = ('本次精读材料 · draft / NOT HUMAN CHECKED；读完后回到会话开始讨论。' if selected else
              '既有笔记的阅读视图，内容验收状态待核对；仅调整编码、排版及答案折叠。')
    output.write_text('''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Reader Note 阅读版</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>
body{max-width:1100px;margin:40px auto;padding:0 32px 64px;font:17px/1.8 -apple-system,"PingFang SC",sans-serif;color:#253332;background:#fafbf7}h1{font-size:30px}h2{margin-top:42px;color:#17655d}table{border-collapse:collapse;width:100%;font-size:15px}td,th{padding:10px;border:1px solid #d5dcd6;text-align:left;overflow-wrap:anywhere}p,li{overflow-wrap:anywhere}details{background:#eef2ec;padding:18px}summary{cursor:pointer}aside{padding:18px;background:#e8eeea}a{color:#17655d}
</style><aside><a href="index.html">返回精读会话</a> · ''' + notice + '</aside>'
        + fragment + '</html>', encoding='utf-8')
    return output.name


def render(directory, *, discussion=False):
    if not discussion and (Path(directory) / 'checklist.json').exists():
        from review_checklists import render as render_checklist
        return render_checklist(directory)
    session, events, _ = session_state(directory)
    state = status(directory)
    esc = html.escape
    labels = dict(zip(DIMENSIONS, ('思路脉络', '方法技巧', '结果证据', '物理机制', '适用边界', '迁移应用')))
    levels = dict(zip(LEVELS, ('可独立解释', '提示后能解释', '仍需补读', '本次未涉及')))
    state_label = {'active': '讨论中' if state['ready_to_discuss'] else '等待读完后开始',
                   'stale': '内容版本已变化', 'paused': '已暂停', 'finished': '本次已结束'}[state['state']]
    parts = [f'<h1>{esc(state["title"])}</h1>', '<p class="lead">先解释文章，再核对原文与入库条目。</p>',
             f'<p>{state_label} · 实际回答 {state["answers"]} 次 · 已判断 {state["reviewed_items"]} / {state["total_indexed_items"]} 条</p>']
    if state['stale_reason']:
        parts.append('<p class="notice">版本已变化：' + esc(state['stale_reason']) + '</p>')
    question = next_turn(directory, discussion=True).get('question')
    if question:
        parts += ['<section><h2>当前讨论</h2><p>' + esc(question) + '</p><p class="muted">请在对话中回答。页面显示本地记录，不根据浏览或预选项生成审阅。</p></section>']
    elif not state['ready_to_discuss']:
        parts.append('<section><h2>先完成阅读，再开始讨论</h2><p>读完或希望开始讨论时，在对话里告诉我。届时先由你解释文章主线，再核对原文和入库条目。</p><p class="muted">尚未阅读不会被评为理解不足。</p></section>')
    def material_link(path, name):
        from portable_fs import ensure_material_alias
        folder = Path(directory) / 'materials'; folder.mkdir(exist_ok=True)
        alias = folder / name
        target = Path(path).resolve()
        ensure_material_alias(alias, target)
        return 'materials/' + name
    original_link = material_link(session['paper']['source']['source_pdf_path'], 'original.pdf')
    parts.append('<section><h2>阅读材料</h2><p><a href="' + original_link + '">原始 PDF</a></p>')
    material_names = {'01_reader_note.zh.md': 'Reader Note（源稿）',
                      '08_reader_note.zotero.html': 'Reader Note（阅读版）',
                      '10_fulltext_translation.zh-CN.pdf': '完整中文精译 PDF',
                      'fulltext_translation/qa.json': '译文验收记录',
                      '03_research_card.zh.md': '日报式调研卡',
                      'review/translation_review_report.json': '独立译文审查记录'}
    # Session provenance stays frozen; only current file availability is refreshed.
    # A file appearing later is never presented as a validated delivery.
    for item in delivered_materials(session, events):
        name = esc(material_names[item['role']])
        if item['state'] in {'missing', 'changed_or_missing'}:
            parts.append('<p>' + name + (' · 文件或绑定已变化，需重新核对' if item['state'] == 'changed_or_missing' else ' · 尚未准备') + '</p>')
        else:
            alias = ('selected_' if item.get('selected_reading_copy') else '') + item['role'].replace('/', '_')
            link = render_note_view(directory, item['path'], selected=item.get('selected_reading_copy', False)) if item['role'] == '08_reader_note.zotero.html' else material_link(item['path'], alias)
            parts.append(f'<p><a href="{esc(link, quote=True)}">{name}</a> · 文件可用，验收状态需核对</p>')
    parts.append('</section><section><h2>理解反馈</h2><table><thead><tr><th>维度</th><th>本次表现</th><th>依据</th></tr></thead><tbody>')
    for key, value in state['learning_feedback'].items():
        parts.append(f'<tr><td>{labels[key]}</td><td>{levels[value["level"]]}</td><td>{esc(value["rationale_zh"])}</td></tr>')
    parts.append('</tbody></table></section><section><h2>知识库审阅</h2>')
    parts.append('<p>只记录明确核对的条目；答对问题不会自动变成整篇审核通过。</p>')
    for decision in state['item_judgments']:
        outcome = {'faithful': '忠实于原文', 'needs_revision': '需要修订', 'unresolved': '保留未决'}[decision['outcome']]
        parts.append(f'<p>{esc(decision["chunk_id"])} · {outcome}：{esc(decision["rationale_zh"])}</p>')
    if not state['item_judgments']:
        parts.append('<p class="muted">尚无本次人工审阅。</p>')
    parts.append('</section><details><summary>实际讨论记录</summary>')
    for event in events:
        if event['kind'] in {'question', 'answer', 'hint', 'personal_note'}:
            kind_label = {'question': '提问', 'answer': '你的回答', 'hint': '提示', 'personal_note': '个人理解'}[event['kind']]
            parts.append(f'<p><strong>{kind_label}</strong> {esc(event.get("text", ""))}</p>')
    parts.append('</details><p class="muted">学习表现、条目审阅与科学结论接受分别记录；费用和长期学习收益尚未测量。</p>')
    document = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>文献精读与审阅</title><style>
body{margin:0;background:#f5f5f0;color:#202a2c;font:17px/1.75 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}main{max-width:1000px;margin:48px auto;padding:0 32px 60px}h1{font-size:34px;line-height:1.3}h2{font-size:23px}section{background:white;padding:22px 28px;margin:22px 0;border:1px solid #dedfd7;border-radius:12px}.lead{font-size:21px;color:#216a67}.muted{color:#637171;font-size:15px}.notice{padding:16px;background:#fff0d7}a{color:#176d71}table{width:100%;border-collapse:collapse}td,th{padding:10px 14px;text-align:left;border-bottom:1px solid #e4e8e4}p,td{overflow-wrap:anywhere}details{padding:18px;background:#e9eeeb}summary{cursor:pointer}
</style><main>''' + '\n'.join(parts) + '</main></html>'
    (Path(directory) / 'index.html').write_text(document, encoding='utf-8')
    return str(Path(directory) / 'index.html')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('start'); p.add_argument('--database', type=Path, default=DEFAULT_DB); p.add_argument('--source-id', required=True); p.add_argument('--output', type=Path, required=True); p.add_argument('--reader', default='user')
    for name in ('status', 'next', 'render', 'record', 'commit-reviews'):
        p = commands.add_parser(name); p.add_argument('--session', type=Path, required=True)
        if name == 'record': p.add_argument('--input', type=Path, required=True)
        if name == 'commit-reviews': p.add_argument('--confirmed', action='store_true')
        if name == 'next': p.add_argument('--discussion', action='store_true')
    p = commands.add_parser('register-materials'); p.add_argument('--session', type=Path, required=True); p.add_argument('--package', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'start': value = start(args.database, args.source_id, args.output, reader=args.reader)
    elif args.command == 'record': value = record(args.session, read(args.input))
    elif args.command == 'commit-reviews': value = commit_reviews(args.session, confirmed=args.confirmed)
    elif args.command == 'register-materials': value = register_materials(args.session, args.package)
    elif args.command == 'next': value = next_turn(args.session, discussion=args.discussion)
    else: value = {'status': status, 'next': next_turn, 'render': render}[args.command](args.session)
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
