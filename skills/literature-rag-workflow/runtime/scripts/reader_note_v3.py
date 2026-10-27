#!/usr/bin/env python3
"""Deterministic renderer for the space-physics research-training Reader Note.

The renderer consumes the canonical schema-v2 evidence model. Reader-facing
synthesis lives in the optional reader_note object and points back to canonical
claim, method, and project-relation IDs. Older models remain renderable through
conservative fallbacks; missing judgments are disclosed instead of invented.
"""

from __future__ import annotations

import re
from typing import Any


FIRST_PRINCIPLES_GRADE_LABELS = {
    "strong_support": "强支持",
    "reasonable_inference": "合理推断",
    "model_dependent": "模型依赖",
    "not_demonstrated": "尚未证明",
}


def clean_text(value: Any, limit: int | None = None) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit].rstrip() if limit and len(text) > limit else text


def table_cell(value: Any) -> str:
    return re.sub(r"(?<!\\)\|", r"\\|", str(value or "")).replace("\n", "<br>")


def anchor_label(anchor: Any) -> str:
    if not isinstance(anchor, dict):
        return "unanchored"
    parts: list[str] = []
    if anchor.get("pdf_page_index"):
        parts.append(f"PDF page {anchor['pdf_page_index']}")
    if anchor.get("printed_page_label"):
        parts.append(f"printed {anchor['printed_page_label']}")
    for field, label in (("section", "section"), ("figure", "figure"), ("table", "table")):
        if anchor.get(field):
            parts.append(f"{label} {anchor[field]}")
    return "; ".join(parts) if parts else "unanchored"


def text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [clean_text(item) for item in value if clean_text(item)]


REVIEW_METADATA_FIELDS = {'human_reviewed', 'human_read_scope', 'read_scope',
                         'human_check_status', 'reviewer_confirmed', 'status'}


def scientific_formula_terms(sections: Any) -> list[str]:
    """Render scientific mapping values, excluding attached review metadata."""
    result = []
    for section in sections if isinstance(sections, list) else []:
        terms = section.get('formula_terms', []) if isinstance(section, dict) else []
        if isinstance(terms, dict):
            values = [f'{key}：{value}' for key, value in terms.items()
                      if key not in REVIEW_METADATA_FIELDS and isinstance(value, str) and value.strip()]
        elif isinstance(terms, list):
            values = [item for item in terms if isinstance(item, str)]
            for item in terms:
                if isinstance(item, dict) and item.get('symbol') and item.get('definition'):
                    values.append(f"{item['symbol']}：{item['definition']}")
        else:
            values = []
        for value in values:
            value = clean_text(value)
            if value and value not in result:
                result.append(value)
    return result


def original_evidence_readback(model: dict[str, Any]) -> list[str]:
    """Rebuild the appendix from canonical author-source spans, not old HTML."""
    lines = []
    seen = set()
    for collection in ('core_claims', 'methods', 'limitations', 'figures', 'self_test'):
        for item in model.get(collection, []):
            if not isinstance(item, dict) or item.get('source_kind') in {'model', 'project', 'external'}:
                continue
            evidence = item.get('evidence', {})
            if not isinstance(evidence, dict):
                continue
            spans = evidence.get('supported_evidence_spans') or [evidence]
            for span in spans:
                if not isinstance(span, dict) or not span.get('quote'):
                    continue
                quote = str(span['quote']).strip()
                key = (collection, item.get('id'), anchor_label(span), quote)
                if key in seen:
                    continue
                seen.add(key)
                lines.extend(['', f"- {collection} {item.get('id')} | {anchor_label(span)}",
                              '  > ' + quote.replace('\n', '\n  > ')])
    return ['## 关键原文证据回读', '', '以下引句逐字来自 canonical 中绑定的原文锚点，供回到源页核验。', *lines] if lines else []


def records_by_id(items: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(items, list):
        return {}
    return {
        str(item.get("id")): item
        for item in items
        if isinstance(item, dict) and item.get("id")
    }


def resolved(index: dict[str, dict[str, Any]], item_ids: Any) -> list[dict[str, Any]]:
    if not isinstance(item_ids, list):
        return []
    return [index[item_id] for item_id in item_ids if item_id in index]


def append_bullets(lines: list[str], values: list[str], fallback: str) -> None:
    if values:
        lines.extend(f"- {value}" for value in values)
    else:
        lines.append(f"- {fallback}")


def reference_text(items: list[dict[str, Any]]) -> str:
    if not items:
        return "none"
    return "；".join(
        f"{item.get('id')}: {clean_text(item.get('text_zh') or item.get('text'), 240)}"
        f"（{anchor_label(item.get('evidence'))}）"
        for item in items
    )


def source_edition_details(model: dict[str, Any]) -> dict[str, Any]:
    value = model.get("source_edition")
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        return {"evidence_pdf_version": value.strip(), "boundary_note_zh": ""}
    return {}


def default_core_work(
    claims: list[dict[str, Any]],
    methods: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    work: list[dict[str, Any]] = []
    for index, claim in enumerate(claims[:3], start=1):
        method = methods[index - 1] if index - 1 < len(methods) else (methods[0] if methods else {})
        work.append(
            {
                "title": f"核心工作 {index}",
                "what_zh": claim.get("text_zh") or claim.get("text"),
                "why_zh": "旧模型未单独结构化该工作的领域缺口；请回到引言和研究问题核对。",
                "how_zh": method.get("text_zh") or method.get("text") or "方法锚点待核查。",
                "result_zh": claim.get("text_zh") or claim.get("text"),
                "claim_ids": [claim.get("id")] if claim.get("id") else [],
                "method_ids": [method.get("id")] if method.get("id") else [],
            }
        )
    return work


def render_research_training_reader_note(
    model: dict[str, Any],
    profile: dict[str, Any],
    *,
    today: str,
    template_version: str,
) -> str:
    note = model.get("reader_note") if isinstance(model.get("reader_note"), dict) else {}
    reading_map = model.get("reading_map") if isinstance(model.get("reading_map"), dict) else {}
    claims = model.get("core_claims") if isinstance(model.get("core_claims"), list) else []
    methods = model.get("methods") if isinstance(model.get("methods"), list) else []
    limitations = model.get("limitations") if isinstance(model.get("limitations"), list) else []
    figures = model.get("figures") if isinstance(model.get("figures"), list) else []
    project_relations = model.get("project_relations") if isinstance(model.get("project_relations"), list) else []
    claim_index = records_by_id(claims)
    method_index = records_by_id(methods)
    relation_index = records_by_id(project_relations)

    version = clean_text(note.get("template_version")) or template_version
    one_sentence = clean_text(note.get("one_sentence_summary") or model.get("abstract_zh"), 500)
    if not one_sentence:
        one_sentence = "旧模型未单独结构化一句话结论；请回到摘要、核心 claim 与证据锚点核对。"
    guiding_questions = text_list(note.get("guiding_questions"))
    if not guiding_questions:
        guiding_questions = [
            clean_text(reading_map.get("research_question")) or "本文真正回答了什么问题？",
            "哪一条结果图或原文锚点构成最强证据？",
            "作者结论依赖哪些假设，是否存在替代解释？",
            f"哪些内容可以迁移到{profile['display_name']}，哪些仍不能支持？",
        ]

    lines = [
        f"# Reader Note | {profile['display_name']} | {model.get('priority')} | {model.get('status')} | {today}",
        "",
        f"**论文**：{model.get('title')}",
        f"**模板**：{version}（空间物理科研训练版）",
        f"**证据状态**：{model.get('status')}；机器生成内容保留 NOT HUMAN CHECKED，不等于人工确认。",
    ]
    edition = source_edition_details(model)
    if edition:
        lines.append(
            f"**证据版本**：{edition.get('evidence_pdf_version', 'unknown')}；"
            f"精确 quote 和图值仅核对该版本。{edition.get('boundary_note_zh', '')}"
        )

    lines.extend(
        [
            "",
            "## 0. 阅读导航（Reading Guide）",
            "",
            "### 0.1 一句话理解论文",
            one_sentence,
            "",
            "### 0.2 阅读本文需要回答的问题",
        ]
    )
    lines.extend(f"{index}. {question}" for index, question in enumerate(guiding_questions[:5], start=1))
    lines.extend(["", "### 0.3 推荐阅读路线"])
    route = text_list(reading_map.get("reading_route"))
    if route:
        lines.extend(f"{index}. {step}" for index, step in enumerate(route, start=1))
    else:
        lines.append("1. 先核对研究问题与核心工作，再读方法、证据图、批判性审查和自测。")

    lines.extend(
        [
            "",
            "## 1. 论文身份卡（Paper Identity Card）",
            "",
            "### 1.1 基本信息",
            f"- **标题**：{model.get('title')}",
            f"- **作者**：{model.get('authors') or '未记录'}",
            f"- **期刊**：{model.get('journal') or '未记录'}",
            f"- **年份**：{model.get('year') or '未记录'}",
            f"- **DOI**：{model.get('doi') or '未记录'}",
            f"- **研究类型**：{model.get('paper_type', 'unknown')}",
            f"- **研究对象**：{', '.join(model.get('topic_tags', [])) or '未结构化'}",
            "",
            "### 1.2 核心方法",
        ]
    )
    if methods:
        for item in methods[:6]:
            lines.append(
                f"- **{item.get('id')}**：{item.get('text_zh') or item.get('text')}"
                f"（{anchor_label(item.get('evidence'))}）"
            )
    else:
        lines.append("- 尚无可回查的方法锚点。")

    if model.get('abstract_original') or model.get('abstract_zh'):
        lines.extend(['', '### 1.3 原文摘要材料与中文摘要／概要', '',
                      '**原文摘要材料**', '', str(model.get('abstract_original') or '尚未取得可核验的原摘要材料。'),
                      '', '**中文摘要／概要**', '', str(model.get('abstract_zh') or '尚未记录中文摘要或概要。')])

    lines.extend(["", "## 2. 本文最核心的工作内容", ""])
    core_work = note.get("core_work") if isinstance(note.get("core_work"), list) else []
    if not core_work:
        core_work = default_core_work(claims, methods)
    if not core_work:
        lines.append("- 当前模型没有可安全提炼的核心工作。")
    for index, work in enumerate(core_work[:3], start=1):
        work_claims = resolved(claim_index, work.get("claim_ids"))
        work_methods = resolved(method_index, work.get("method_ids"))
        lines.extend(
            [
                f"### Work {index}：{work.get('title') or f'核心工作 {index}'}",
                f"- **作者做了什么**：{work.get('what_zh') or '未结构化'}",
                f"- **为什么需要做**：{work.get('why_zh') or '未结构化'}",
                f"- **怎么做**：{clean_text(work.get('how_zh')) or reference_text(work_methods)}",
                f"- **得到了什么**：{clean_text(work.get('result_zh')) or reference_text(work_claims)}",
                f"- **证据引用**：claims {reference_text(work_claims)}；methods {reference_text(work_methods)}",
                "",
            ]
        )

    background = note.get("background") if isinstance(note.get("background"), dict) else {}
    lines.extend(
        [
            "## 3. 科学背景与研究问题",
            "",
            "### 3.1 已知什么？",
            background.get("known_zh") or "旧模型未将既有认识单独结构化；请回到引言的原文锚点。",
            "",
            "### 3.2 不知道什么？",
            background.get("gap_zh") or clean_text(reading_map.get("research_question")) or "研究缺口待核查。",
            "",
            "### 3.3 本文解决什么？",
            background.get("paper_response_zh") or one_sentence,
        ]
    )

    mechanism = note.get("physical_mechanism") if isinstance(note.get("physical_mechanism"), dict) else {}
    formula_terms = scientific_formula_terms(model.get('section_guides', []))
    default_observables = [
        clean_text(figure.get("direct_observation"))
        for figure in figures
        if isinstance(figure, dict) and clean_text(figure.get("direct_observation"))
    ][:4]
    first_claim_text = clean_text(claims[0].get("text_zh") or claims[0].get("text")) if claims else ""
    lines.extend(
        [
            "",
            "## 4. 物理机制理解",
            "",
            "### 4.1 现象层",
            mechanism.get("phenomenon_zh") or first_claim_text or "未结构化。",
            "",
            "### 4.2 物理原因层",
        ]
    )
    append_bullets(lines, text_list(mechanism.get("cause_chain")), "旧模型未提供独立的条件→过程→结果因果链。")
    lines.extend(["", "### 4.3 数学 / 模型层"])
    append_bullets(
        lines,
        text_list(mechanism.get("equations_or_constraints")) or formula_terms,
        "未记录可回查的方程、守恒关系或模型约束。",
    )
    lines.extend(["", "### 4.4 可观测层"])
    append_bullets(
        lines,
        text_list(mechanism.get("observables")) or default_observables,
        "未记录可独立验证该机制的观测或模拟诊断。",
    )

    first_principles = note.get("first_principles") if isinstance(note.get("first_principles"), dict) else {}
    lines.extend(
        [
            "",
            "## 5. 第一性原理物理审查（First-Principles Physics Analysis）",
            "",
            "> 本节是对论文证据的物理重审，不是新增观测、模拟或拓扑证据。",
            "",
            f"**适用性**：{first_principles.get('applicability', 'not_structured')}；"
            f"{first_principles.get('reason_zh') or '旧模型未按新版模板记录适用性。'}",
            "",
            "### 5.1 基本物理起点",
        ]
    )
    append_bullets(lines, text_list(first_principles.get("starting_points")), "未结构化；不得由论文结论反向杜撰基本规律。")
    lines.extend(["", "### 5.2 从基本规律推导科学问题"])
    append_bullets(lines, text_list(first_principles.get("derivation_chain")), "未结构化。")
    lines.extend(
        [
            "",
            "### 5.3 核心假设审查",
            "| 论文假设 | 物理依据 | 可信度 | 风险 | 关联 claim |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    assumptions = first_principles.get("assumptions") if isinstance(first_principles.get("assumptions"), list) else []
    if assumptions:
        for assumption in assumptions:
            ids = ", ".join(assumption.get("related_claim_ids", [])) or "none"
            lines.append(
                f"| {table_cell(assumption.get('assumption_zh'))} | "
                f"{table_cell(assumption.get('physical_basis_zh'))} | "
                f"{assumption.get('confidence', 'unknown')} | "
                f"{table_cell(assumption.get('risk_zh'))} | {ids} |"
            )
    else:
        lines.append("| 未结构化 | 未结构化 | unknown | 不得从旧 note 自动补写 | none |")
    lines.extend(
        [
            "",
            "### 5.4 方法选择合理性",
            first_principles.get("method_rationale_zh") or "旧模型未单独解释为什么该方法足以回答研究问题。",
            "",
            "### 5.5 从第一性原理重新检查论文结论",
            "| Claim | 分级 | 审查理由 | 原证据 |",
            "| --- | --- | --- | --- |",
        ]
    )
    assessments = first_principles.get("claim_assessments") if isinstance(first_principles.get("claim_assessments"), list) else []
    if assessments:
        for assessment in assessments:
            claim = claim_index.get(assessment.get("claim_id"), {})
            grade = FIRST_PRINCIPLES_GRADE_LABELS.get(
                assessment.get("grade"),
                assessment.get("grade") or "unknown",
            )
            lines.append(
                f"| {assessment.get('claim_id') or 'unknown'} | {grade} | "
                f"{table_cell(assessment.get('rationale_zh'))} | "
                f"{anchor_label(claim.get('evidence'))} |"
            )
    else:
        lines.append("| none | 未结构化 | 旧模型未提供独立的第一性原理 claim 分级 | unanchored |")
    lines.extend(["", "### 5.6 第一性原理启发的新研究方向"])
    append_bullets(
        lines,
        text_list(first_principles.get("new_questions")),
        "未结构化；不要用“提高分辨率”代替可判别的物理问题。",
    )

    method_audit = note.get("method_audit") if isinstance(note.get("method_audit"), dict) else {}
    lines.extend(
        [
            "",
            "## 6. 方法、数据、模型与实验设计",
            "",
            f"**主类型**：{model.get('paper_type', 'unknown')}",
            "",
            "### 6.1 方法链与证据锚点",
            "| ID | 方法说明 | 证据锚点 | 状态 | 不能支持 |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    if methods:
        for item in methods:
            lines.append(
                f"| {item.get('id')} | {table_cell(item.get('text_zh') or item.get('text'))} | "
                f"{anchor_label(item.get('evidence'))} | {item.get('verification_status')} | "
                f"{table_cell(item.get('cannot_support'))} |"
            )
    else:
        lines.append("| none | 尚无方法锚点 | unanchored | needs_human_check | 不能判断复现性 |")
    lines.extend(
        [
            "",
            "### 6.2 类型化方法审查",
            method_audit.get("design_summary_zh")
            or "旧模型仅保留方法链；Observation / Simulation / Theory 的类型化参数尚未单独结构化。",
        ]
    )
    checks = method_audit.get("checks") if isinstance(method_audit.get("checks"), list) else []
    if checks:
        lines.extend(
            [
                "",
                "| 检查项 | 论文给出的设置 / 结果 | 证据 ID | 未确定性或风险 |",
                "| --- | --- | --- | --- |",
            ]
        )
        for check in checks:
            evidence_ids = ", ".join(check.get("evidence_ids", [])) or "none"
            lines.append(
                f"| {table_cell(check.get('item_zh'))} | "
                f"{table_cell(check.get('finding_zh'))} | {evidence_ids} | "
                f"{table_cell(check.get('risk_zh'))} |"
            )

    lines.extend(["", "## 7. Figure / Table 科学精读"])
    if not figures:
        lines.append("- 未发现可稳定定位的关键图；状态为 not_available。")
    for figure in figures:
        related_claims = resolved(claim_index, figure.get("related_claim_ids"))
        question = clean_text(figure.get("scientific_question_zh"))
        if not question:
            depicted = clean_text(figure.get("caption_zh") or figure.get("direct_observation"))
            if depicted:
                question = "图示内容：" + depicted
        if not question:
            question = "该图回答的科学问题尚未单独结构化。"
        alternatives = text_list(figure.get("alternative_explanations"))
        lines.extend(
            [
                "",
                f"### {figure.get('label', figure.get('id', 'Figure'))} [{figure.get('status', 'unknown')}]",
                f"- **1. 这张图回答什么科学问题？** {question}",
                f"- **2. 如何阅读？** {figure.get('axes_encoding') or '坐标、颜色和变量尚未视觉复核。'}",
                f"- **图中直接可见**：{figure.get('direct_observation') or '尚未视觉复核；不得从 caption 补写。'}",
                f"- **3. 物理意义**：{figure.get('author_interpretation') or '作者解释待核查。'}",
                f"- **4. 关联论文结论**：{reference_text(related_claims)}",
                f"- **5. 易误读点 / 替代解释**：{'; '.join(alternatives) or '待核查'}；"
                f"不能推出：{figure.get('cannot_conclude') or '未记录'}",
                f"- **证据锚点**：{anchor_label(figure.get('evidence'))}",
                f"- **图注中文**：{figure.get('caption_zh') or '未记录'}",
            ]
        )

    contribution = note.get("contribution_audit") if isinstance(note.get("contribution_audit"), dict) else {}
    lines.extend(
        [
            "",
            "## 8. 科学贡献与发表价值分析",
            "",
            "> “发表价值”是基于论文证据的研究训练判断，不代表真实审稿人或期刊决定。",
            "",
            "### 8.1 过去领域状态",
            contribution.get("previous_state_zh") or "旧模型未将先前领域能力与本文增量单独结构化。",
            "",
            "### 8.2 本文突破与创新类型",
        ]
    )
    contributions = contribution.get("contributions") if isinstance(contribution.get("contributions"), list) else []
    if contributions:
        for item in contributions:
            evidence_claims = resolved(claim_index, item.get("claim_ids"))
            lines.append(
                f"- **{item.get('type', 'unspecified')}**："
                f"{item.get('text_zh') or '未结构化'}；证据：{reference_text(evidence_claims)}"
            )
    else:
        lines.append("- 旧模型未提供“相对既有工作”的结构化增量；不得把摘要主结果自动改写成创新性。")
    first_limitation = (
        clean_text(limitations[0].get("text_zh") or limitations[0].get("text"))
        if limitations
        else ""
    )
    lines.extend(
        [
            "",
            "### 8.3 为什么这项工作具有发表价值？",
            contribution.get("publication_value_zh")
            or "尚未完成基于先前工作、证据强度和增量能力的审稿式判断。",
            "",
            "### 8.4 仍未解决什么？",
            contribution.get("remaining_uncertainty_zh") or first_limitation or "未结构化。",
        ]
    )

    critical = note.get("critical_audit") if isinstance(note.get("critical_audit"), dict) else {}
    lines.extend(
        [
            "",
            "## 9. 批判性审查",
            "",
            "### 9.1 证据强度",
            critical.get("evidence_strength_zh")
            or "按下表逐条区分论文报告结果、作者解释、模型依赖与项目迁移；不能以叙述流畅度代替证据。",
            "",
            "| ID | 结论 | 证据层 | 支持 | 锚点 | 不能支持 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for item in claims:
        lines.append(
            f"| {item.get('id')} | {table_cell(item.get('text_zh') or item.get('text'))} | "
            f"{item.get('epistemic_status')} | {item.get('support_level')} | "
            f"{anchor_label(item.get('evidence'))} | {table_cell(item.get('cannot_support'))} |"
        )
    lines.extend(["", "### 9.2 方法限制"])
    for item in limitations:
        lines.append(
            f"- **{item.get('id')} [{item.get('verification_status')}]**："
            f"{item.get('text_zh') or item.get('text')}"
            f"（{anchor_label(item.get('evidence'))}）"
        )
    append_bullets(
        lines,
        text_list(critical.get("method_limits")),
        "除已锚定 limitation 外，未新增无来源的方法缺陷。",
    )
    lines.extend(["", "### 9.3 替代解释"])
    alternatives = text_list(critical.get("alternative_explanations"))
    for controversy in model.get("controversies", []):
        if not isinstance(controversy, dict):
            continue
        counter = clean_text(controversy.get("counter_position"))
        if counter:
            alternatives.append(
                f"{controversy.get('id')}：{counter}；当前处理：{controversy.get('resolution')}"
            )
    append_bullets(lines, alternatives, "未记录可检验的替代解释。")
    lines.extend(["", "### 9.4 未来研究方向"])
    future_directions = text_list(critical.get("future_directions")) or text_list(model.get("open_questions"))
    append_bullets(lines, future_directions, "下一步应先补最小可判别验证，而不是直接扩大 claim。")
    lines.extend(["", "### 9.5 全局 claim boundary"])
    append_bullets(lines, text_list(model.get("cannot_support")), "未记录；需人工阻断任何证据越级。")
    lines.extend(["", "### 9.6 外部文献定位（与本文证据分层）"])
    external = model.get("external_context") if isinstance(model.get("external_context"), list) else []
    if not external:
        lines.append(
            f"- external_research_status: {model.get('external_research_status', 'not_run')}；"
            "未联网时不生成虚构争议。"
        )
    for source in external:
        url = source.get("doi_or_url") or ""
        link = f"[{url}]({url})" if url else "未提供 URL"
        lines.append(
            f"- **[{source.get('role')}] {source.get('citation')}**："
            f"{source.get('finding_zh')}（{link}；{source.get('verification_status')}）"
        )

    lines.extend(
        [
            "",
            "## 10. 读者自测 QA",
            "",
            "请先在不看答案的情况下作答，再到文末核对。",
        ]
    )
    questions = model.get("self_test") if isinstance(model.get("self_test"), list) else []
    for question in questions:
        lines.extend(
            [
                "",
                f"### {question.get('id')} [{question.get('category')}]",
                question.get("question", ""),
                "",
                "我的回答：",
            ]
        )

    transfer = note.get("research_transfer") if isinstance(note.get("research_transfer"), dict) else {}
    direct_items = resolved(relation_index, transfer.get("direct_relation_ids"))
    adaptable_items = resolved(relation_index, transfer.get("adaptable_relation_ids"))
    if not direct_items and not adaptable_items:
        adaptable_items = project_relations
    lines.extend(
        [
            "",
            f"## 11. 与{profile['display_name']}的关系",
            "",
            "> 本节属于 project_application；不得改写成论文作者的直接结论。",
            "",
            "### 11.1 可直接借鉴",
        ]
    )
    if direct_items:
        for item in direct_items:
            lines.append(
                f"- **{item.get('id')}**：{item.get('text_zh') or item.get('text')}；"
                f"证据状态 {item.get('verification_status')}。"
            )
    else:
        lines.append("- 没有被新版模型明确分类为“可直接借鉴”的项目关系。")
    lines.extend(["", "### 11.2 可改造"])
    if adaptable_items:
        for item in adaptable_items:
            lines.append(
                f"- **{item.get('id')}**：{item.get('text_zh') or item.get('text')}；"
                "不得当作论文直接结论。"
            )
    else:
        lines.append("- 未结构化。")
    lines.extend(["", "### 11.3 不适用"])
    append_bullets(
        lines,
        text_list(transfer.get("not_applicable_zh")),
        "未结构化；至少遵守上文 cannot_support。",
    )
    lines.extend(["", "### 11.4 可产生的新问题"])
    transfer_questions = text_list(transfer.get("new_questions")) or text_list(first_principles.get("new_questions"))
    append_bullets(lines, transfer_questions, "尚未形成可判别的新问题。")

    meeting = note.get("group_meeting_summary") if isinstance(note.get("group_meeting_summary"), dict) else {}
    core_work_titles = [
        f"{item.get('title') or f'核心工作 {index}'}："
        f"{item.get('result_zh') or item.get('what_zh') or '未结构化'}"
        for index, item in enumerate(core_work[:3], start=1)
    ]
    lines.extend(
        [
            "",
            "## 12. 组会汇报版总结（10 分钟）",
            f"1. **背景**：{meeting.get('background_zh') or background.get('known_zh') or '回到引言说明既有认识。'}",
            f"2. **科学问题**：{meeting.get('research_question_zh') or clean_text(reading_map.get('research_question')) or '未结构化。'}",
            "3. **核心工作**：",
        ]
    )
    for item in text_list(meeting.get("core_work_zh")) or core_work_titles:
        lines.append(f"   - {item}")
    lines.extend(
        [
            f"4. **最大创新**：{meeting.get('main_contribution_zh') or contribution.get('publication_value_zh') or '尚未完成证据化创新审查。'}",
            f"5. **最大不足**：{meeting.get('main_limitation_zh') or first_limitation or '未结构化。'}",
            f"6. **对当前课题的启发**：{meeting.get('research_takeaway_zh') or (transfer_questions[0] if transfer_questions else '先补最小可判别验证。')}",
        ]
    )

    lines.extend(
        [
            "",
            "## 13. 附录模块（按需）",
            "",
            "### Appendix A：关键段落双语锚点",
        ]
    )
    bilingual_count = 0
    for section in model.get("section_guides", []):
        if not isinstance(section, dict):
            continue
        for sentence in section.get("key_sentences", []):
            if not isinstance(sentence, dict):
                continue
            bilingual_count += 1
            lines.extend(
                [
                    f"- **{section.get('title')} / EN**：{sentence.get('en', '')}",
                    f"  - **ZH**：{sentence.get('zh', '')}",
                    f"  - **锚点**：{anchor_label(sentence.get('evidence'))}",
                ]
            )
    if not bilingual_count:
        lines.append("- 没有稳定的关键段落双语锚点；不放入全文逐段翻译。")
    lines.extend(["", "### Appendix B：图中文字与编码摘要"])
    if figures:
        for figure in figures:
            lines.append(
                f"- **{figure.get('label')}**：{figure.get('axes_encoding') or '未记录'}；"
                f"图注：{figure.get('caption_zh') or '未记录'}"
            )
    else:
        lines.append("- 无可复核图表。")
    lines.extend(["", "### Appendix C：关键公式与术语表"])
    terms = reading_map.get("terms") if isinstance(reading_map.get("terms"), dict) else {}
    if terms:
        for term, explanation in terms.items():
            if term in REVIEW_METADATA_FIELDS:
                continue
            lines.append(f"- **{term}**：{explanation}")
    for term in formula_terms:
        lines.append(f"- **公式 / 变量**：{term}")
    if not terms and not formula_terms:
        lines.append("- 未结构化。")

    lines.extend(["", "## 答案键", "", "<details>", "<summary>完成作答后展开参考答案与原文锚点</summary>", ""])
    for question in questions:
        correction = question.get("correction")
        correction_layer = question.get("correction_epistemic_status")
        if correction_layer in {"codex_inference", "project_application"}:
            correction = f"[{correction_layer}] {correction}"
        lines.extend(
            [
                "",
                f"### {question.get('id')} 答案",
                f"- **参考答案**：{question.get('answer')}",
                f"- **证据锚点**：{anchor_label(question.get('evidence'))}",
                f"- **常见误答**：{question.get('common_error')}",
                f"- **纠正方式**：{correction}",
            ]
        )
    lines.extend(["", "</details>", ""])
    lines.extend(
        [
            "",
            "## 人工审核记录",
            "- 我确认可信的部分：",
            "- 我发现的问题：",
            "- 需要更新的 claim / figure / answer：",
            "- 人工阅读范围与页码：",
        ]
    )
    lines.extend(['', *original_evidence_readback(model)])
    from adaptive_content import fold_reader_markdown
    return fold_reader_markdown("\n".join(lines).rstrip() + "\n", model)
