"""Mechanical provenance risks in unindexed Reader Note and card summaries.

An exact copy from a project-use field is a review trigger, not a scientific
verdict. Atomic assertions retain their own independent source-support decisions.
"""
from __future__ import annotations
import hashlib
import re
import subprocess
from pathlib import Path


def normalized(value):
    return re.sub(r'\s+', '', value) if isinstance(value, str) else ''


def attribution_risks(model):
    if not isinstance(model, dict):
        return []
    note = model.get('reader_note') or {}
    card = model.get('research_card') or {}
    if not isinstance(note, dict) or not isinstance(card, dict):
        return []
    meeting = note.get('group_meeting_summary') or {}
    contribution = note.get('contribution_audit') or {}
    candidates = {
        '/reader_note/group_meeting_summary/main_contribution_zh': meeting.get('main_contribution_zh') if isinstance(meeting, dict) else None,
        '/reader_note/contribution_audit/publication_value_zh': contribution.get('publication_value_zh') if isinstance(contribution, dict) else None,
    }
    project_fields = {key: card.get(key) for key in ('project_value_zh', 'next_action_zh')}
    risks = []
    for pointer, value in candidates.items():
        text = normalized(value)
        if not text:
            continue
        for project_field, project_value in project_fields.items():
            if text == normalized(project_value):
                risks.append({'code': 'project_text_in_paper_contribution',
                              'paper_field': pointer,
                              'project_field': '/research_card/' + project_field,
                              'field_sha256': hashlib.sha256(value.encode()).hexdigest(),
                              'statement': value,
                              'semantic_checked': False,
                              'action': 'independently_check_author_contribution_against_original_pdf',
                              'reason_zh': '本文贡献字段与项目应用字段完全相同；需从原文独立区分作者工作和后续项目设想。机械相同不自动判定内容错误。'})
    return risks


def attribution_review_errors(model, risk, decision, cache=None):
    """Validate a field review's original-PDF receipt, not its judgement.

    An attestation or an arbitrary nonempty passage cannot waive an attribution
    risk. The independent report must identify the current field and actual
    numbered original pages. This uses fresh PDF extraction rather than notes.
    """
    errors = []
    if not isinstance(decision, dict):
        return ['missing original-source attribution review']
    if (decision.get('paper_field') != risk['paper_field']
            or decision.get('field_sha256') != risk['field_sha256']
            or decision.get('semantic_checked') is not True
            or decision.get('support') != 'supported'
            or not decision.get('rationale_zh')):
        return ['attribution review does not support the current complete field']
    checks = ('whole_assertion', 'conditions_scope', 'numbers_units', 'attribution',
              'epistemic_layer', 'compound_assertion')
    if any(decision.get('checks', {}).get(key) is not True for key in checks):
        errors.append('attribution review lacks complete semantic check outcomes')
    try:
        pdf = Path(model['source_pdf_path']).resolve()
        if (Path(decision['source_pdf_path']).resolve() != pdf
                or decision.get('source_pdf_sha256') != model['source_pdf_sha256']):
            return errors + ['attribution review used different original PDF inputs']
        actual_sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
        if actual_sha != model['source_pdf_sha256']:
            return errors + ['original PDF changed before attribution validation']
        route = decision.get('fresh_text_extraction_route')
        if route not in {'normal', 'layout', 'raw'}:
            return errors + ['invalid original PDF text extraction route']
        cache = cache if cache is not None else {}
        key = (str(pdf), actual_sha, route)
        if key not in cache:
            command = ['pdftotext'] + ([] if route == 'normal' else ['-' + route]) + [str(pdf), '-']
            raw = subprocess.run(command, capture_output=True, check=True, timeout=60).stdout
            cache[key] = (hashlib.sha256(raw).hexdigest(), raw.decode('utf-8').split('\f'))
        text_sha, pages = cache[key]
        if decision.get('fresh_text_sha256') != text_sha:
            errors.append('attribution review does not bind fresh original text')
        indices = decision.get('source_pages')
        if (not isinstance(indices, list) or not indices
                or any(type(p) is not int or p < 1 or p > len(pages) for p in indices)):
            return errors + ['invalid original page indices']
        passage = decision.get('source_passage')
        if not isinstance(passage, str):
            return errors + ['missing numbered original page passage']
        parts = re.split(r'PDF PAGE (\d+)\n', passage)
        if parts[0].strip() or len(parts) < 3 or len(parts) % 2 != 1:
            return errors + ['attribution passage must bind numbered original pages']
        located = []
        for offset in range(1, len(parts), 2):
            page, body = int(parts[offset]), parts[offset + 1]
            if (page not in indices or not body.strip()
                    or normalized(body) not in normalized(pages[page - 1])):
                errors.append('attribution passage absent from the original page')
            located.append(page)
        if set(located) != set(indices):
            errors.append('attribution passage does not cover all stated pages')
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        errors.append('attribution original-source receipt unavailable: ' + str(exc))
    return errors


def revision_attribution_errors(model, report):
    """Require original-source review for duplicated unindexed contribution text.

    Reuse the same receipt contract as intake validation. Identical wording is
    only a review trigger; an independently supported current field may pass.
    """
    risks = attribution_risks(model)
    if not risks:
        return []
    producer = report.get('producer_task_id')
    reviewer = report.get('reviewer_task_id')
    if not producer or not reviewer or producer == reviewer:
        return ['independent unindexed attribution reviewer required']
    errors, cache = [], {}
    for risk in risks:
        decisions = [item for item in report.get('artifact_attribution_reviews', [])
                     if isinstance(item, dict) and item.get('paper_field') == risk['paper_field']
                     and item.get('field_sha256') == risk['field_sha256']]
        findings = (attribution_review_errors(model, risk, decisions[0], cache)
                    if len(decisions) == 1 else ['one bound original-source field review required'])
        errors.extend(risk['paper_field'] + ': ' + message for message in findings)
    return list(dict.fromkeys(errors))
