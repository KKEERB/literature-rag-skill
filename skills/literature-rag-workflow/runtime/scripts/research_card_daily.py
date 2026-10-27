#!/usr/bin/env python3
"""Versioned daily-report Research Card contract and deterministic renderer.

The card is deliberately a view over ``02_machine_evidence_pack.json``.  It
never invents claims, questions, evidence anchors, or project relevance during
rendering.  The validator is therefore also the boundary between an authored
v2.3 model and its Markdown/Zotero display layers.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Callable


TEMPLATE_VERSION = "research_daily_brief_v1"
CHALLENGE_ROLES = {
    "method_or_data_limit",
    "alternative_or_internal_consistency",
    "project_transfer_boundary",
}
CHALLENGE_STATUSES = {
    "author_limitation",
    "codex_inference",
    "project_application",
}
QA_CAPABILITY_BY_CATEGORY = {
    "recall": "fact_or_evidence",
    "evidence_location": "fact_or_evidence",
    "figure_reconstruction": "fact_or_evidence",
    "self_explanation": "mechanism_or_method",
    "method": "mechanism_or_method",
    "critique": "critical_judgment",
    "transfer": "project_transfer",
}
REQUIRED_QA_CAPABILITIES = {
    "fact_or_evidence",
    "mechanism_or_method",
    "critical_judgment",
    "project_transfer",
}
GENERIC_CHALLENGE_PATTERNS = {
    "样本可能不足",
    "仍需更多研究",
    "需要更多研究",
    "存在一定局限",
    "可能存在误差",
}


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _cjk_count(value: Any) -> int:
    return len(re.findall(r"[\u3400-\u9fff]", str(value or "")))


def _substantive_chinese(value: Any, minimum: int = 8) -> bool:
    return _cjk_count(value) >= minimum


def _anchor_present(anchor: dict[str, Any] | None) -> bool:
    anchor = anchor or {}
    return bool(
        anchor.get("pdf_page_index")
        or anchor.get("printed_page_label")
        or anchor.get("section")
        or anchor.get("figure")
        or anchor.get("table")
    )


def _anchor_label(anchor: dict[str, Any] | None) -> str:
    anchor = anchor or {}
    parts: list[str] = []
    if anchor.get("pdf_page_index"):
        parts.append(f"PDF page {anchor['pdf_page_index']}")
    if anchor.get("printed_page_label"):
        parts.append(f"printed {anchor['printed_page_label']}")
    for field, label in (("section", "section"), ("figure", "figure"), ("table", "table")):
        if anchor.get(field):
            parts.append(f"{label} {anchor[field]}")
    return "; ".join(parts) if parts else "unanchored"


def is_v23_model(model: dict[str, Any], prompt_version: str) -> bool:
    """Return true only for the v2.3 authoring prompt, not triage scaffolds."""
    generation = model.get("generation") if isinstance(model.get("generation"), dict) else {}
    return generation.get("prompt_version") == prompt_version and generation.get("mode") != "triage_scaffold"


def _id_indexes(model: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    objects: dict[str, dict[str, Any]] = {}
    kinds: dict[str, str] = {}
    for collection in ("core_claims", "methods", "limitations", "project_relations"):
        for item in model.get(collection, []) or []:
            if isinstance(item, dict) and item.get("id"):
                objects[str(item["id"])] = item
                kinds[str(item["id"])] = collection
    for figure in model.get("figures", []) or []:
        if isinstance(figure, dict) and figure.get("id"):
            objects[str(figure["id"])] = figure
            kinds[str(figure["id"])] = "figures"
    return objects, kinds


def validate_research_card(model: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Validate the semantic v2.3 card contract without rendering it."""
    errors: list[str] = []
    from adaptive_content import adaptive
    is_adaptive = adaptive(model)
    card = model.get("research_card")
    if not isinstance(card, dict):
        return ["research_card is required by the v2.3 prompt version"], {}
    if card.get("template_version") != TEMPLATE_VERSION:
        errors.append(f"research_card.template_version must be {TEMPLATE_VERSION}")

    for field, minimum in (
        ("daily_summary_zh", 20),
        ("project_value_zh", 15),
        ("next_action_zh", 12),
    ):
        if not _substantive_chinese(card.get(field), minimum):
            errors.append(f"research_card.{field} lacks substantive Chinese content")

    objects, kinds = _id_indexes(model)
    claim_map = {
        str(item.get("id")): item
        for item in model.get("core_claims", []) or []
        if isinstance(item, dict) and item.get("id")
    }
    method_map = {
        str(item.get("id")): item
        for item in model.get("methods", []) or []
        if isinstance(item, dict) and item.get("id")
    }
    figure_map = {
        str(item.get("id")): item
        for item in model.get("figures", []) or []
        if isinstance(item, dict) and item.get("id")
    }
    relation_map = {
        str(item.get("id")): item
        for item in model.get("project_relations", []) or []
        if isinstance(item, dict) and item.get("id")
    }

    method_id = str(card.get("method_id") or "")
    method = method_map.get(method_id)
    modules = model.get('generation', {}).get('coverage', {}).get('modules', {})
    method_na = (is_adaptive and not method_map and not method_id
                 and modules.get('method_audit', {}).get('applicable') is False
                 and modules.get('method_audit', {}).get('reason_zh'))
    if not method and not method_na:
        errors.append("research_card.method_id must reference a method in the same evidence pack")
    elif method and (method.get("verification_status") != "anchored" or not _anchor_present(method.get("evidence"))):
        errors.append("research_card.method_id must reference an anchored method with a PDF locator")

    findings = card.get("finding_items")
    findings = findings if isinstance(findings, list) else []
    if not findings or (not is_adaptive and len(findings) > 2):
        errors.append("research_card.finding_items must contain 1-2 items")
    for index, finding in enumerate(findings, start=1):
        owner = f"research_card.finding_items[{index}]"
        if not isinstance(finding, dict):
            errors.append(f"{owner} must be an object")
            continue
        if not _substantive_chinese(finding.get("summary_zh"), 12):
            errors.append(f"{owner}.summary_zh lacks substantive Chinese content")
        claim_ids = finding.get("claim_ids")
        claim_ids = claim_ids if isinstance(claim_ids, list) else []
        if not claim_ids:
            errors.append(f"{owner}.claim_ids must not be empty")
        for claim_id in claim_ids:
            claim = claim_map.get(str(claim_id))
            if not claim:
                errors.append(f"{owner}.claim_ids contains unknown claim ID: {claim_id}")
            elif claim.get("verification_status") != "anchored" or not _anchor_present(claim.get("evidence")):
                errors.append(f"{owner}.claim_ids references an unanchored claim: {claim_id}")

    figure_id = str(card.get("key_figure_id") or "")
    figure = figure_map.get(figure_id)
    figure_na = (is_adaptive and not figure_map and not figure_id
                 and model.get('generation', {}).get('coverage', {}).get('no_substantive_figures_reason_zh'))
    if not figure and not figure_na:
        errors.append("research_card.key_figure_id must reference a figure in the same evidence pack")
    elif figure and not _anchor_present(figure.get("evidence")):
        errors.append("research_card.key_figure_id must reference a figure with a PDF locator")

    relation_ids = card.get("project_relation_ids")
    relation_ids = relation_ids if isinstance(relation_ids, list) else []
    if not relation_ids:
        errors.append("research_card.project_relation_ids must not be empty")
    for relation_id in relation_ids:
        relation = relation_map.get(str(relation_id))
        if not relation:
            errors.append(f"research_card.project_relation_ids contains unknown ID: {relation_id}")
        elif relation.get("epistemic_status") != "project_application":
            errors.append(f"research_card project relation must retain project_application: {relation_id}")

    challenges = card.get("objective_challenges")
    challenges = challenges if isinstance(challenges, list) else []
    if not is_adaptive and len(challenges) != 3:
        errors.append("research_card.objective_challenges must contain exactly 3 items")
    roles = [str(item.get("role") or "") for item in challenges if isinstance(item, dict)]
    if (not is_adaptive and set(roles) != CHALLENGE_ROLES) or not set(roles) <= CHALLENGE_ROLES or len(roles) != len(set(roles)):
        errors.append("research_card.objective_challenges must contain each required role exactly once")
    for index, challenge in enumerate(challenges, start=1):
        owner = f"research_card.objective_challenges[{index}]"
        if not isinstance(challenge, dict):
            errors.append(f"{owner} must be an object")
            continue
        status = challenge.get("epistemic_status")
        if status not in CHALLENGE_STATUSES:
            errors.append(f"{owner}.epistemic_status is invalid")
        if challenge.get("role") == "project_transfer_boundary" and status != "project_application":
            errors.append(f"{owner} project transfer challenge must use project_application")
        for field, minimum in (("question_zh", 10), ("impact_zh", 12), ("minimum_validation_zh", 12)):
            value = _text(challenge.get(field))
            if not _substantive_chinese(value, minimum):
                errors.append(f"{owner}.{field} lacks a specific Chinese statement")
            if any(pattern in value for pattern in GENERIC_CHALLENGE_PATTERNS) and _cjk_count(value) < 28:
                errors.append(f"{owner}.{field} is an object-free generic challenge")
        basis_ids = challenge.get("basis_ids")
        basis_ids = basis_ids if isinstance(basis_ids, list) else []
        if not basis_ids:
            errors.append(f"{owner}.basis_ids must not be empty")
        for basis_id in basis_ids:
            basis_id = str(basis_id)
            basis = objects.get(basis_id)
            if not basis:
                errors.append(f"{owner}.basis_ids contains unknown ID: {basis_id}")
                continue
            if kinds.get(basis_id) in {"core_claims", "methods", "limitations", "figures"}:
                contextual = (basis.get('source_kind') == 'model' or
                              basis.get('epistemic_status') in {'codex_inference', 'project_application'})
                if contextual and status == 'author_limitation':
                    errors.append(f"{owner}.basis_ids presents analyst/project context as an author limitation: {basis_id}")
                elif not contextual and (basis.get("verification_status") != "anchored" or not _anchor_present(basis.get("evidence"))):
                    errors.append(f"{owner}.basis_ids references paper evidence without a valid anchor: {basis_id}")

    questions = {
        str(item.get("id")): item
        for item in model.get("self_test", []) or []
        if isinstance(item, dict) and item.get("id")
    }
    qa_ids = card.get("qa_ids")
    qa_ids = qa_ids if isinstance(qa_ids, list) else []
    if (not is_adaptive and len(qa_ids) != 4) or len(set(map(str, qa_ids))) != len(qa_ids) or not qa_ids:
        errors.append("research_card.qa_ids must contain exactly 4 distinct self_test IDs")
    capabilities: set[str] = set()
    for qa_id in qa_ids:
        qa_id = str(qa_id)
        question = questions.get(qa_id)
        if not question:
            errors.append(f"research_card.qa_ids contains unknown self_test ID: {qa_id}")
            continue
        capability = QA_CAPABILITY_BY_CATEGORY.get(str(question.get("category") or ""))
        if not capability:
            errors.append(f"self_test {qa_id} has an unsupported category for the daily card")
        else:
            capabilities.add(capability)
        if not _anchor_present(question.get("evidence")):
            errors.append(f"self_test {qa_id} lacks a PDF evidence locator")
        for field in ("question", "answer", "common_error", "correction"):
            if not _text(question.get(field)):
                errors.append(f"self_test {qa_id}.{field} is empty")
    if not is_adaptive and capabilities != REQUIRED_QA_CAPABILITIES:
        errors.append(
            "research_card.qa_ids must cover fact/evidence, mechanism/method, "
            "critical judgment, and project transfer exactly once"
        )

    return errors, {
        "template_version": card.get("template_version"),
        "finding_count": len(findings),
        "challenge_roles": roles,
        "qa_ids": [str(value) for value in qa_ids],
        "qa_capabilities": sorted(capabilities),
    }


def _citation(model: dict[str, Any]) -> str:
    citation = model.get("citation_metadata")
    citation = citation if isinstance(citation, dict) else {}
    authors = _text(citation.get("authors") or model.get("authors")) or "作者字段缺失"
    year = _text(citation.get("year") or model.get("year")) or "年份字段缺失"
    title = _text(citation.get("title") or model.get("title")) or "题名字段缺失"
    journal = _text(
        citation.get("journal")
        or citation.get("publication_title")
        or model.get("journal")
        or model.get("publication_title")
    ) or "期刊字段缺失"
    doi = _text(citation.get("doi") or model.get("doi")) or "DOI 字段缺失"
    return f"{authors}（{year}）. {title}. {journal}. DOI: {doi}."


def _audit_ids(card: dict[str, Any]) -> dict[str, list[str]]:
    claims = [str(value) for item in card.get("finding_items", []) for value in item.get("claim_ids", [])]
    methods = [str(card.get("method_id") or "")]
    figures = [str(card.get("key_figure_id") or "")]
    limitations: list[str] = []
    project_relations = [str(value) for value in card.get("project_relation_ids", [])]
    for challenge in card.get("objective_challenges", []):
        for value in challenge.get("basis_ids", []):
            value = str(value)
            if value.startswith("L"):
                limitations.append(value)
            elif value.startswith("M"):
                methods.append(value)
            elif value.startswith("F"):
                figures.append(value)
            elif value.startswith("C"):
                claims.append(value)
            elif value.startswith("P"):
                project_relations.append(value)
    return {
        "claim": sorted(set(filter(None, claims))),
        "method": sorted(set(filter(None, methods))),
        "figure": sorted(set(filter(None, figures))),
        "limitation": sorted(set(filter(None, limitations))),
        "project_relation": sorted(set(filter(None, project_relations))),
    }


def research_card_visible_chinese_count(markdown: str) -> int:
    """Count CJK body characters after metadata and audit sections are removed."""
    body = markdown
    if "## 今日日报摘要" in body:
        body = body.split("## 今日日报摘要", 1)[1]
    if "## 证据边界与审计" in body:
        body = body.split("## 证据边界与审计", 1)[0]
    body = re.sub(r"(?m)^#{1,6}\s+.*$", "", body)
    body = re.sub(r"[`*_>\[\]()|#-]", "", body)
    return _cjk_count(body)


def render_daily_research_card(
    model: dict[str, Any],
    profile: dict[str, Any],
    *,
    today: str | None = None,
    anchor_label: Callable[[dict[str, Any]], str] | None = None,
) -> str:
    """Render a validated daily brief and reject out-of-contract length."""
    errors, _checks = validate_research_card(model)
    if errors:
        raise ValueError("; ".join(errors))
    anchor_label = anchor_label or _anchor_label
    today = today or dt.date.today().isoformat()
    card = model["research_card"]
    objects, _kinds = _id_indexes(model)
    questions = {str(item["id"]): item for item in model.get("self_test", [])}
    status = str(model.get("status") or "draft")
    marker = "NOT HUMAN CHECKED" if status == "draft" else "HUMAN CHECKED"
    lines = [
        f"# 每日文献调研汇报 | {profile['display_name']} | {model.get('priority', 'unknown')} | {status} | {today}",
        "",
        f"**完整引文**：{_citation(model)}",
        f"**项目 profile**：`{profile['project_id']}` / {profile['display_name']}",
        f"**优先级与状态**：{model.get('priority', 'unknown')} / {status} / `{marker}`",
        "",
        "## 今日日报摘要",
        "",
        _text(card.get("daily_summary_zh")),
        "",
        "## 研究问题—方法—关键发现—关键图定位",
        "",
        f"- **研究问题**：{_text(model.get('reading_map', {}).get('research_question'))}",
    ]
    method = objects.get(str(card["method_id"]))
    if method:
        lines.append(
        f"- **方法（`{method['id']}`）**：{_text(method.get('text_zh') or method.get('text'))}"
        f"（{anchor_label(method.get('evidence', {}))}）"
        )
    else:
        lines.append('- **方法**：本篇不适用；' + model['generation']['coverage']['modules']['method_audit']['reason_zh'])
    for index, finding in enumerate(card.get("finding_items", []), start=1):
        evidence_labels = []
        for claim_id in finding["claim_ids"]:
            claim = objects[str(claim_id)]
            evidence_labels.append(f"`{claim_id}` {anchor_label(claim.get('evidence', {}))}")
        lines.append(f"- **关键发现 {index}**：{_text(finding['summary_zh'])}（{'；'.join(evidence_labels)}）")
    figure = objects.get(str(card["key_figure_id"]))
    if figure:
        figure_meaning = _text(figure.get("direct_observation") or figure.get("caption_zh") or figure.get("author_interpretation"))
        lines.append(
        f"- **关键图定位（`{figure['id']}` / {figure.get('label', figure['id'])}）**："
        f"{anchor_label(figure.get('evidence', {}))}；证据意义：{figure_meaning}"
        )
    else:
        lines.append('- **关键图定位**：本篇没有实质图表；' + model['generation']['coverage']['no_substantive_figures_reason_zh'])
    lines.extend(
        [
            "",
            "## 对当前项目的意义",
            "",
            f"**项目应用判断（project_application）**：{_text(card.get('project_value_zh'))}",
        ]
    )
    for relation_id in card.get("project_relation_ids", []):
        relation = objects[str(relation_id)]
        lines.append(f"- `{relation_id}`：{_text(relation.get('text_zh') or relation.get('text'))}；这不是论文直接结论。")
    role_titles = {
        "method_or_data_limit": "方法或数据基础",
        "alternative_or_internal_consistency": "替代解释或内部一致性",
        "project_transfer_boundary": "项目迁移边界",
    }
    lines.extend(["", "## 客观质疑", ""])
    for index, challenge in enumerate(card.get("objective_challenges", []), start=1):
        basis_labels: list[str] = []
        for basis_id in challenge.get("basis_ids", []):
            item = objects[str(basis_id)]
            locator = anchor_label(item.get("evidence", {})) if item.get("evidence") else "project relation"
            basis_labels.append(f"`{basis_id}` {locator}")
        lines.extend(
            [
                f"### 质疑 {index}｜{role_titles[challenge['role']]}｜`{challenge['epistemic_status']}`",
                "",
                f"- **问题**：{_text(challenge['question_zh'])}",
                f"- **证据基础**：{'；'.join(basis_labels)}",
                f"- **实际影响**：{_text(challenge['impact_zh'])}",
                f"- **最小可判别验证**：{_text(challenge['minimum_validation_zh'])}",
                "",
            ]
        )
    lines.extend(["## Q&A", "", "以下问答逐字复用 Reader Note 的 `self_test`。", ""])
    for qa_id in card.get("qa_ids", []):
        question = questions[str(qa_id)]
        lines.extend(
            [
                f"### {qa_id}｜{question.get('category')}",
                "",
                f"- **Q**：{_text(question.get('question'))}",
                f"- **A**：{_text(question.get('answer'))}",
                f"- **证据锚点**：{anchor_label(question.get('evidence', {}))}",
                f"- **常见误答**：{_text(question.get('common_error'))}",
                f"- **纠正方式**：{_text(question.get('correction'))}",
                "",
            ]
        )
    lines.extend(
        [
            "## 下一步最小行动",
            "",
            _text(card.get("next_action_zh")),
            "",
            "## 证据边界与审计",
            "",
            "- 本卡只复用同一 `02_machine_evidence_pack.json` 中的已锚定论文证据和明确标识的项目应用判断；未锚定 claim 已从本卡排除。",
            "- 译文、项目兴趣和流畅叙述均不得替代英文原文、PDF 锚点或 claim promotion 人工门。",
        ]
    )
    audit = _audit_ids(card)
    for label, values in audit.items():
        lines.append(f"- {label} IDs：{', '.join(values) or 'none'}")
    markdown = "\n".join(lines).rstrip() + "\n"
    count = research_card_visible_chinese_count(markdown)
    from adaptive_content import adaptive
    if not adaptive(model) and not 1000 <= count <= 1800:
        raise ValueError(
            "v2.3 Research Card body must contain 1000-1800 visible Chinese "
            f"characters after metadata/audit removal; got {count}. Revise research_card fields instead of truncating."
        )
    return markdown

