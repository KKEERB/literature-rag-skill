#!/usr/bin/env python3
"""Default fast intake, explicitly selected deep reading, and reading sessions.

Model production remains in the Codex paper workflow. A work order is not a
completed independent review or a human reading result.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

import reading_sessions as sessions

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'configs/reading_collaboration.json'


def policy():
    value = sessions.read(CONFIG)
    if (value.get('schema') != 'reading-collaboration-policy/1'
            or value.get('default_intake') != 'fast_pdf_two_round'
            or value.get('deep_reading_trigger') != 'explicit_human_selection'
            or value.get('whole_paper_acceptance_from_quiz') is not False):
        raise ValueError('unsupported literature learning policy')
    return value


def intake(inputs, manifest, output, database, project_profile=None, storage_root=None, model_settings=None):
    import fast_pdf_intake
    policy()
    return {'route': 'fast_pdf_two_round', 'deep_reading_selected': False,
            'translation_required_for_fast_intake': False,
            'batch': fast_pdf_intake.prepare(inputs, manifest, output, database, storage_root,
                                            project_profile=project_profile, model_settings=model_settings)}


def select(database, source_id, output, reader='user'):
    policy()
    state = sessions.start(database, source_id, output, reader=reader)
    session, _, _ = sessions.session_state(output)
    source = session['paper']['source']
    work = {'schema': 'selected-deep-reading/1', 'source_id': source_id,
            'work_id': session['paper']['work_id'], 'source_pdf_path': source['source_pdf_path'],
            'source_pdf_sha256': source['source_pdf_sha256'], 'knowledge_model_sha256': source['model_sha256'],
            'selected_by': reader, 'selection_basis': 'explicit_human_selection',
            'material_inventory': session['material_inventory'],
            'original_source_is_answer_authority': True,
            'reuse_policy': 'reuse exact bound source extraction and accepted artifacts; reopen original for omissions and disputed content',
            'required_outputs': ['reader_note', 'research_card'],
            'instructions_zh': [
                '先查原 PDF 身份与现有产物的绑定。存在旧笔记不等于已通过当前语义复核。',
                '缺少精读材料时独立阅读全文，生成 schema v2 精读材料并进行独立复核；不把快速摘要扩写成全文阅读。',
                '当前工作单不表示模型精读、独立复核或人工阅读已完成。',
                '本次交流按 reading_collaboration.md 执行。默认直接展示十条重点论点，支持批量审阅；问答仅在用户主动要求时展开。'],
            'existing_pipeline': str(ROOT / 'docs/reading_collaboration.md'),
            'discussion_protocol': str(ROOT / 'docs/reading_collaboration.md'),
            'material_delivery_status': 'inspect_existing_and_complete_missing',
            'human_reading_status': 'awaiting_actual_human_response'}
    work_path = Path(output) / 'deep-reading-work-order.json'
    if work_path.exists():
        old = sessions.read(work_path)
        if any(old.get(key) != work[key] for key in ('source_id', 'work_id', 'source_pdf_sha256', 'knowledge_model_sha256', 'selected_by')):
            raise ValueError('existing work order identity changed; preserve it and create a current session')
    else:
        sessions.write_new(work_path, work)
    return {'session': str(Path(output).resolve()), 'state': state,
            'deep_reading_work_order': str(Path(output) / 'deep-reading-work-order.json'),
            'next': sessions.next_turn(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('intake'); p.add_argument('--pdf', type=Path, action='append', default=[]); p.add_argument('--manifest', type=Path); p.add_argument('--output', type=Path, required=True); p.add_argument('--database', type=Path, default=sessions.DEFAULT_DB); p.add_argument('--project-profile'); p.add_argument('--storage-root', type=Path); p.add_argument('--model-settings', type=Path)
    p = sub.add_parser('select'); p.add_argument('--source-id', required=True); p.add_argument('--output', type=Path, required=True); p.add_argument('--database', type=Path, default=sessions.DEFAULT_DB); p.add_argument('--reader', default='user')
    sub.add_parser('policy')
    a = parser.parse_args()
    if a.command == 'intake':
        from model_settings import load
        result = intake(a.pdf, a.manifest, a.output, a.database, a.project_profile, a.storage_root,
                        load(a.model_settings) if a.model_settings else None)
    elif a.command == 'select': result = select(a.database, a.source_id, a.output, a.reader)
    else: result = policy()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
