#!/usr/bin/env python3
"""Immutable, source-bound ten-item review; no quiz or synthetic human decision."""
from __future__ import annotations
import argparse
import html
import json
from pathlib import Path
import re

import reading_sessions as sessions
from knowledge_quality import attach_reviews, object_digest, quality_decision

LABELS = {'faithful': '已审阅 · 接受', 'needs_revision': '已审阅 · 不接受／有争议',
          'unresolved': '已审阅 · 保留异议', 'not_reviewed': '未审阅'}
FOCUS_LABELS = {'figure_interpretation': '核对图中证据与文字解释的对应关系',
                'mechanism_inference': '核对作者机制推断、替代解释及证据强度',
                'interpretive_boundary': '核对解释所依赖的条件与适用边界',
                'factual_check': '保留少量关键事实或数值核对'}


def selection_policy():
    config = sessions.read(sessions.ROOT / 'configs/reading_collaboration.json')
    policy = config['checklist_selection']
    if (policy.get('version') != 'interpretation-first-v2'
            or type(policy.get('factual_target')) is not int or type(policy.get('factual_max')) is not int
            or not 0 <= policy['factual_target'] <= policy['factual_max'] <= 3):
        raise ValueError('invalid human review selection policy')
    return policy


def review_focus(row):
    """Use existing epistemic labels and text; a figure locator alone is not interpretation."""
    text = row['text_zh'] or row['text']
    evidence = json.loads(row.get('evidence_json') or '{}')
    interpretation = row.get('epistemic_status') == 'author_interpretation'
    interpretive_text = bool(re.search(r'解释|推断|提示|共位|共现|因果|证据|支持|说明|表明|揭示|符合|不能.*(?:证明|确认)|suggest|interpret|infer|evidence|support', text, re.I))
    # Sensor/panel mapping and numerical readout are factual even with Fig. anchors.
    has_figure = row['kind'] in {'figure', 'figure_evidence'} or any(evidence.get(k) for k in ('figure', 'figure_id'))
    if row['kind'] in {'figure', 'figure_evidence'} and (interpretation or interpretive_text):
        return 'figure_interpretation'
    if interpretation:
        return 'mechanism_inference'
    if row['kind'] == 'limitation' and re.search(r'未能|无法|不能|不足|局限|假设|分辨率|不确定|不唯一|因果|必要|充分|limit|uncertain|assum', text, re.I):
        return 'interpretive_boundary'
    if has_figure and interpretive_text:
        return 'figure_interpretation'
    if re.search(r'机制|因果|驱动|导致|mechanism|causal', text, re.I) and interpretive_text:
        return 'mechanism_inference'
    return 'factual_check'


def select_for_human_review(rows, *, size=10, policy=None):
    policy = policy or selection_policy()
    ranked = sorted(rows, key=lambda r: (-rank(r)[0], r['chunk_id']))
    focus = {r['chunk_id']: review_focus(r) for r in ranked}
    facts = [r for r in ranked if focus[r['chunk_id']] == 'factual_check']
    substantive = [r for r in ranked if focus[r['chunk_id']] != 'factual_check']
    fact_count = min(len(facts), policy['factual_target'], size)
    selected = []
    budget = size - fact_count
    # Preserve both figure interpretation and mechanism review when available.
    for kind, count in (('figure_interpretation', 3), ('mechanism_inference', 3), ('interpretive_boundary', 1)):
        selected.extend([r for r in substantive if focus[r['chunk_id']] == kind][:min(count, budget - len(selected))])
    selected.extend([r for r in substantive if r not in selected][:budget - len(selected)])
    fact_count = min(len(facts), policy['factual_max'], size - len(selected))
    selected.extend(facts[:fact_count])
    selected.sort(key=lambda r: (focus[r['chunk_id']] == 'factual_check', -rank(r)[0], r['chunk_id']))
    return selected


def rank(row):
    text = row['text_zh'] or row['text']
    reasons = []
    score = {'claim': 50, 'method': 25, 'limitation': 25, 'figure': 15, 'figure_evidence': 15, 'definition': 10}.get(row['kind'], 0)
    if row['kind'] == 'claim': reasons.append('论文主要论点')
    if re.search(r'\d', text): score += 8; reasons.append('含关键数值或时空尺度')
    if re.search(r'条件|仅|假设|限制|不能|不等于|依赖|边界', text): score += 8; reasons.append('适用条件或结论边界')
    if re.search(r'解释|机制|模拟|认为|提出|推断', text): score += 7; reasons.append('需区分观测与作者解释')
    if row['kind'] in {'method', 'limitation'}: reasons.append('方法条件或局限')
    return score, '；'.join(reasons) or '补充论文证据范围'


def ensure(directory):
    directory = Path(directory)
    session, _, _ = sessions.session_state(directory)
    sessions.check_current(session)
    path = directory / 'checklist.json'
    version = object_digest({k: v['inputs'] for k, v in session['paper']['assertions'].items()})
    if path.exists():
        saved = sessions.read(path)
        if (saved.get('version') != version or saved.get('checklist_id') !=
                object_digest({k: v for k, v in saved.items() if k != 'checklist_id'})):
            raise ValueError('checklist changed or stale; preserve it and create a current version session')
        return saved
    with sessions.connect(session['database']) as con:
        rows = attach_reviews(con, [a['row'] for a in session['paper']['assertions'].values()])
    unique = {}
    for row in rows:
        if (row['kind'] not in {'claim', 'method', 'limitation', 'figure', 'figure_evidence', 'definition'}
                or row['source_kind'] in {'project', 'external'}
                or row['epistemic_status'] in {'codex_inference', 'project_application'}
                or quality_decision(row)['state'] == 'quarantined'):
            continue
        text = row['text_zh'] or row['text']
        normalized = re.sub(r'\W+', '', text).casefold()
        if normalized not in unique:
            unique[normalized] = row
    policy = selection_policy()
    selected = select_for_human_review(list(unique.values()), policy=policy)
    items = [{'number': n, 'chunk_id': row['chunk_id'], 'item_id': row['item_id'], 'kind': row['kind'],
              'text': row['text_zh'] or row['text'], 'evidence': json.loads(row['evidence_json']),
              'review_focus': review_focus(row),
              'selection_reason': FOCUS_LABELS[review_focus(row)] + '；' + rank(row)[1],
              'inputs': session['paper']['assertions'][row['chunk_id']]['inputs']}
             for n, row in enumerate(selected, 1)]
    result = {'schema': 'ten-item-review/1', 'selection_policy': policy['version'],
              'selection_settings': policy,
              'version': version, 'source_id': session['paper']['source']['source_id'],
              'title': session['paper']['source']['title'], 'eligible_count': len(unique), 'items': items,
              'selection_shortfall': '优先解释与机制条目；不足时不增加事实核对题凑数。' if len(selected) < 10 else None}
    result['checklist_id'] = object_digest(result)
    sessions.write_new(path, result)
    return result


def locator(evidence):
    pages = evidence.get('pdf_page_indices') or [evidence.get('pdf_page_index') or evidence.get('printed_page_label')]
    parts = ['PDF 第 ' + '、'.join(str(p) for p in pages if p is not None) + ' 页'] if any(p is not None for p in pages) else ['页码待核对']
    for key in ('section', 'figure', 'figure_id', 'table', 'table_id'):
        if evidence.get(key): parts.append(str(evidence[key]))
    return ' · '.join(parts)


def view(directory):
    checklist = ensure(directory)
    session, _, _ = sessions.session_state(directory)
    with sessions.connect(session['database']) as con:
        rows = sessions.annotate_reading(con, [a['row'] for a in session['paper']['assertions'].values()])
    reviews = {r['chunk_id']: r['item_human_review'] for r in rows}
    return {'state': 'ready_for_checklist_review', 'question': None,
            'instruction': '直接展示全部清单；只提交用户明确判断的编号。接受表示忠实于论文，意见可选；不追加测试。',
            **checklist, 'items': [dict(i, locator=locator(i['evidence']), review=reviews[i['chunk_id']],
                                      review_label=LABELS[reviews[i['chunk_id']]['state']]) for i in checklist['items']]}


def display_text(directory):
    value = view(directory)
    return '\n\n'.join(f"{i['number']}. {i['text']}\n   {i['locator']} · {i['review_label']}"
                         + ('\n   用户意见：' + i['review']['current']['rationale_zh']
                            if i['review'].get('current') and i['review']['current']['rationale_zh'] else '') for i in value['items'])


def present(directory, message_ref):
    value = ensure(directory)
    event = sessions.record(directory, {'kind': 'checklist_presentation', 'checklist_id': value['checklist_id'],
                           'shown_text': display_text(directory), 'shown_to_user_ref': message_ref})
    return {'event_id': event['event_id'], 'checklist_id': value['checklist_id'], 'shown_text': event['shown_text']}


def parse_reply(text):
    """Only unambiguous numbered clauses; never infer unmentioned decisions."""
    decisions = []
    for clause in re.split(r'[；;\n]+', text.strip().rstrip('。')):
        match = re.fullmatch(r'\s*([\d\s、,，\-–—]+)\s*(不接受|接受|拒绝|保留未决)(?:\s*[:：]\s*(.*))?\s*', clause)
        if not match: raise ValueError('ambiguous reply; supply explicit numbered decisions without guessing')
        ids = []
        for token in re.split(r'[、,，]', match[1].strip()):
            bounds = re.split(r'[-–—]', token.strip())
            if len(bounds) == 1: ids.append(int(bounds[0]))
            elif len(bounds) == 2 and int(bounds[0]) <= int(bounds[1]):
                ids.extend(range(int(bounds[0]), int(bounds[1]) + 1))
            else: raise ValueError('invalid numbered range')
        outcome = {'接受': 'faithful', '不接受': 'needs_revision', '拒绝': 'needs_revision', '保留未决': 'unresolved'}[match[2]]
        decisions.extend({'number': n, 'outcome': outcome, 'rationale_zh': match[3] or ''} for n in ids)
    if len({d['number'] for d in decisions}) != len(decisions): raise ValueError('duplicate or conflicting numbered decisions')
    return decisions


def submit(directory, payload, *, confirmed=False):
    checklist = ensure(directory)
    if payload.get('checklist_id') != checklist['checklist_id']:
        raise ValueError('submitted checklist version differs')
    session, events, _ = sessions.session_state(directory)
    presentation = sessions.by_id(events, payload.get('presentation_event_id'), 'checklist_presentation')
    if presentation['checklist_id'] != checklist['checklist_id']: raise ValueError('presentation version differs')
    numbered = payload.get('decisions') if 'decisions' in payload else parse_reply(payload.get('human_statement', ''))
    lookup = {i['number']: i['chunk_id'] for i in checklist['items']}
    if not numbered or any(type(d.get('number')) is not int or d['number'] not in lookup for d in numbered):
        raise ValueError('decisions must name actual displayed numbers')
    decisions = [{'chunk_id': lookup[d['number']], 'outcome': d['outcome'], 'rationale_zh': d.get('rationale_zh', '')} for d in numbered]
    event = sessions.record(directory, {'kind': 'judgment', 'actor': payload.get('actor'),
        'user_message_ref': payload.get('user_message_ref'), 'human_statement': payload.get('human_statement'),
        'explicit_item_judgment': True, 'comparison_event_id': presentation['event_id'], 'decisions': decisions})
    return {'event_id': event['event_id'], 'commit': sessions.commit_reviews(directory, confirmed=confirmed),
            'items': view(directory)['items']}


def render(directory):
    from portable_fs import ensure_material_alias
    directory = Path(directory)
    stale = None
    try: value = view(directory)
    except (ValueError, OSError) as exc:
        stale = str(exc); value = sessions.read(directory / 'checklist.json')
    esc = html.escape
    parts = [f'<h1>{esc(value["title"])}</h1>', '<p class="lead">十条重点论点审阅</p>',
             '<p>接受表示认可条目对论文的表达。可在对话中一次回复多个编号；未回答的保持未审阅。</p>',
             '<p class="example">例如：1–3、5–10 接受；4 不接受：缺少适用条件。</p>']
    if stale: parts.append('<p class="notice">版本已变化，需重新核对：' + esc(stale) + '</p>')
    for item in value['items']:
        review = item.get('review', {}).get('current') or {}
        page = item['evidence'].get('pdf_page_index') or next(iter(item['evidence'].get('pdf_page_indices') or []), None)
        location = esc(locator(item['evidence']))
        if type(page) is int and page > 0:
            location = f'<a href="materials/original.pdf#page={page}">{location}</a>'
        parts.append(f'<article id="item-{item["number"]}"><div class="meta">{item["number"]:02d} · {esc(item.get("review_label", "历史清单 · 待重新核对"))}</div><p class="assertion">{esc(item["text"])}</p><p class="locator">{location}</p>')
        if review.get('rationale_zh'): parts.append('<p class="notice">用户意见：' + esc(review['rationale_zh']) + '</p>')
        parts.append('<details><summary>原文摘录与选中理由</summary><blockquote>' + esc(str(item['evidence'].get('quote', '原条目未保存逐字摘录，按页码回看原文。'))) + '</blockquote><p>' + esc(item['selection_reason']) + '</p></details></article>')
    session, events, _ = sessions.session_state(directory)
    links = []
    materials = directory / 'materials'; materials.mkdir(exist_ok=True)
    original = materials / 'original.pdf'
    ensure_material_alias(original, session['paper']['source']['source_pdf_path'])
    if original.exists(): links.append('<a href="materials/original.pdf">原始 PDF</a>')
    if (directory / 'reader-note.html').exists(): links.append('<a href="reader-note.html">Reader Note</a>')
    for item in sessions.delivered_materials(session, events):
        if item['role'] == '08_reader_note.zotero.html' and item['state'] == 'available_not_revalidated':
            link = sessions.render_note_view(directory, item['path'], selected=item.get('selected_reading_copy', False))
            links.append('<a href="' + esc(link, quote=True) + '">Reader Note</a>')
        if item['role'] == '10_fulltext_translation.zh-CN.pdf' and item['state'] == 'available_not_revalidated':
            alias = directory / 'materials/selected_10_fulltext_translation.zh-CN.pdf'
            ensure_material_alias(alias, item['path'])
            if alias.exists(): links.append('<a href="materials/selected_10_fulltext_translation.zh-CN.pdf">完整中文精译</a>')
    parts.append('<nav>' + ' · '.join(dict.fromkeys(links)) + '</nav><p class="muted">不接受的条目仍可带异议引用。问答仅在主动要求时展开；历史问答与认可记录保留。机器复核、条目认可与科学结论验证分别记录。</p>')
    document = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>十条重点论点审阅</title><style>body{margin:0;background:#f4f4ee;color:#223337;font:17px/1.8 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}main{max-width:960px;margin:48px auto;padding:0 36px 64px}h1{font-size:32px;line-height:1.35}.lead{font-size:23px;color:#176764}.example,.notice{background:#e6efea;padding:14px 20px}article{background:#fff;padding:22px 26px;margin:18px 0;border:1px solid #dce2da;border-radius:10px}.assertion{margin:10px 0;font-size:18px}.meta{color:#176764;font-weight:600}.locator,.muted{color:#667776;font-size:15px}details{border-top:1px solid #e5e9e4;padding-top:10px}summary{cursor:pointer;color:#526a68}blockquote{margin:12px 0;white-space:pre-wrap;font-size:15px}nav{margin:26px 0}a{color:#176764}p,blockquote{overflow-wrap:anywhere}</style><main>' + '\n'.join(parts) + '</main></html>'
    (directory / 'index.html').write_text(document, encoding='utf-8')
    return str(directory / 'index.html')


def generate(database, source_id, output):
    sessions.start(database, source_id, output)
    result = view(output)
    render(output)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('generate'); p.add_argument('--database', type=Path, default=sessions.DEFAULT_DB); p.add_argument('--source-id', required=True); p.add_argument('--output', type=Path, required=True)
    for name in ('show', 'present', 'submit', 'render'):
        p = sub.add_parser(name); p.add_argument('--session', type=Path, required=True)
        if name == 'present': p.add_argument('--message-ref', required=True)
        if name == 'submit': p.add_argument('--input', type=Path, required=True); p.add_argument('--confirmed', action='store_true')
    a = parser.parse_args()
    if a.command == 'generate': result = generate(a.database, a.source_id, a.output)
    elif a.command == 'show': result = {'checklist': view(a.session), 'display_text': display_text(a.session)}
    elif a.command == 'present': result = present(a.session, a.message_ref)
    elif a.command == 'submit': result = submit(a.session, sessions.read(a.input), confirmed=a.confirmed)
    else: result = render(a.session)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
