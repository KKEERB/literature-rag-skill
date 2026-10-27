#!/usr/bin/env python3
"""Core model, render, migration, and validation helpers for pipeline v2.

The knowledge model is the only LLM-authored/canonical content object. Reader
Note, RAG Markdown, Research Card, JSONL indexes, and Zotero HTML are rendered
deterministically from it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from common import append_jsonl, now_iso, read_json, read_simple_yaml, simple_yaml, write_json


SCHEMA_VERSION = "2.0"
PROMPT_VERSION = "pipeline-v2.3-daily-card-fulltext-cn-2026-08-19"
V22_PROMPT_VERSION = "pipeline-v2.2-research-training-reader-note-2026-08-19"
V21_PROMPT_VERSION = "pipeline-v2.1-deep-read-curated-2026-07-10"
LEGACY_PROMPT_VERSIONS = (V22_PROMPT_VERSION, V21_PROMPT_VERSION)
READER_NOTE_TEMPLATE_VERSION = "space_physics_research_training_v1"
EPISTEMIC_STATUSES = {
    "reported_result",
    "author_interpretation",
    "author_limitation",
    "external_finding",
    "codex_inference",
    "project_application",
}
SUPPORT_LEVELS = {"direct", "indirect", "speculative"}
VERIFICATION_STATUSES = {"anchored", "externally_verified", "needs_human_check"}
SOURCE_KINDS = {"paper", "external", "model", "project"}
FIGURE_STATUSES = {"visually_reviewed", "caption_only", "not_available"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_profile(path: Path) -> dict[str, Any]:
    data = read_json(path)
    required = {
        "schema_version",
        "project_id",
        "display_name",
        "reader_language",
        "translation_mode",
        "target_questions",
        "terminology",
        "relevance_dimensions",
        "claim_boundaries",
        "forbidden_phrases",
        "priority_policy",
        "external_research_policy",
    }
    missing = sorted(required - set(data))
    if missing:
        raise ValueError(f"project profile missing keys: {missing}")
    if data.get("translation_mode") != "layered_bilingual":
        raise ValueError("pipeline v2 currently requires translation_mode=layered_bilingual")
    if data.get("external_research_policy", {}).get("upload_local_pdf") is not False:
        raise ValueError("project profile must prohibit external PDF upload")
    return data


def first_int(value: Any) -> int | None:
    match = re.search(r"\d+", str(value or ""))
    return int(match.group(0)) if match else None


def clean_text(value: Any, limit: int | None = None) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit].rstrip() if limit and len(text) > limit else text


def has_cjk(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", value or ""))


def split_markdown_sections(markdown: str) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    current_title = ""
    current_lines: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            if current_title:
                sections.append({"title": current_title, "body": "\n".join(current_lines).strip()})
            current_title = line[3:].strip()
            current_lines = []
        elif current_title:
            current_lines.append(line)
    if current_title:
        sections.append({"title": current_title, "body": "\n".join(current_lines).strip()})
    return sections


def parse_qa(markdown: str) -> list[tuple[str, str]]:
    lines = markdown.splitlines()
    pairs: list[tuple[str, str]] = []
    for index, line in enumerate(lines):
        match = re.match(r"^Q\d+[:：]\s*(.+)", line.strip())
        if not match:
            continue
        answer = ""
        for candidate in lines[index + 1 : index + 5]:
            answer_match = re.match(r"^A\d+[:：]\s*(.*)", candidate.strip())
            if answer_match:
                answer = answer_match.group(1).strip()
                break
            if candidate.startswith("Q") or candidate.startswith("## "):
                break
        pairs.append((match.group(1).strip(), answer))
    return pairs


def figure_blocks(markdown: str) -> dict[str, dict[str, str]]:
    blocks: dict[str, dict[str, str]] = {}
    current = ""
    for line in markdown.splitlines():
        if line.startswith("### "):
            heading = line[4:].strip()
            if re.search(r"\b(Figure|Fig\.|Table)\s*\d+", heading, flags=re.I):
                current = heading
                blocks[current] = {}
            else:
                current = ""
            continue
        if current and line.lstrip().startswith("- ") and "：" in line:
            key, value = line.lstrip()[2:].split("：", 1)
            blocks[current][key.strip()] = value.strip()
    return blocks


def normalize_anchor(raw: dict[str, Any] | None, fallback_quote: str = "") -> dict[str, Any]:
    raw = raw or {}
    page_value = raw.get("pdf_page_index", raw.get("page"))
    quote = clean_text(raw.get("quote") or fallback_quote, 300)
    return {
        "pdf_page_index": first_int(page_value),
        "printed_page_label": clean_text(raw.get("printed_page_label") or raw.get("page")),
        "section": clean_text(raw.get("section")),
        "figure": clean_text(raw.get("figure")),
        "table": clean_text(raw.get("table")),
        "quote": quote,
    }


def anchor_present(anchor: dict[str, Any]) -> bool:
    return bool(
        anchor.get("pdf_page_index")
        or anchor.get("printed_page_label")
        or anchor.get("section")
        or anchor.get("figure")
        or anchor.get("table")
    )


def anchor_label(anchor: dict[str, Any]) -> str:
    parts: list[str] = []
    if anchor.get("pdf_page_index"):
        parts.append(f"PDF page {anchor['pdf_page_index']}")
    if anchor.get("printed_page_label"):
        parts.append(f"printed {anchor['printed_page_label']}")
    for field, label in [("section", "section"), ("figure", "figure"), ("table", "table")]:
        if anchor.get(field):
            parts.append(f"{label} {anchor[field]}")
    return "; ".join(parts) if parts else "unanchored"


def epistemic_from_v1(raw: dict[str, Any], item_kind: str) -> str:
    claim_type = str(raw.get("claim_type", "")).lower()
    if item_kind == "limitation" or "limitation" in claim_type:
        return "author_limitation"
    if "analogy" in claim_type or raw.get("support_level") in {"moderate", "weak"}:
        return "author_interpretation"
    return "reported_result"


def support_from_v1(value: Any) -> str:
    normalized = str(value or "").lower()
    if normalized in {"strong", "direct"}:
        return "direct"
    if normalized in {"weak", "speculative"}:
        return "speculative"
    return "indirect"


def retrieval_queries(text: str, text_zh: str, title: str, tags: list[str]) -> tuple[list[str], list[str]]:
    base_tags = [clean_text(tag) for tag in tags if clean_text(tag)][:4]
    zh = [f"{title} 核心证据", f"{title} 结论边界"]
    en = [f"{title} evidence", f"{title} limitations"]
    if has_cjk(text_zh):
        zh.append(clean_text(text_zh, 80))
    zh.extend(f"{tag} 论文证据" for tag in base_tags)
    en.extend(f"{tag} paper evidence" for tag in base_tags)
    return list(dict.fromkeys(zh))[:6], list(dict.fromkeys(en))[:6]


def knowledge_item(
    *,
    paper_id: str,
    prefix: str,
    index: int,
    text: str,
    text_zh: str,
    title: str,
    tags: list[str],
    source_kind: str,
    epistemic_status: str,
    support_level: str,
    evidence: dict[str, Any],
    verification_status: str | None = None,
    related_chunk_ids: list[str] | None = None,
    usable_for: list[str] | None = None,
    cannot_support: str = "",
) -> dict[str, Any]:
    item_id = f"{prefix}{index:03d}"
    chunk_id = f"{paper_id}:{prefix.lower()}:{item_id.lower()}"
    queries_zh, queries_en = retrieval_queries(text, text_zh, title, tags)
    status = verification_status or ("anchored" if anchor_present(evidence) else "needs_human_check")
    return {
        "id": item_id,
        "chunk_id": chunk_id,
        "source_kind": source_kind,
        "epistemic_status": epistemic_status,
        "support_level": support_level,
        "verification_status": status,
        "text": clean_text(text),
        "text_zh": clean_text(text_zh),
        "evidence": evidence,
        "retrieval_queries_zh": queries_zh,
        "retrieval_queries_en": queries_en,
        "related_chunk_ids": related_chunk_ids or [],
        "usable_for": usable_for or [],
        "cannot_support": clean_text(cannot_support),
    }


def find_pdf(metadata: dict[str, Any], paper_dir: Path) -> Path:
    candidates = [
        metadata.get("zotero_attachment_path"),
        metadata.get("pdf_path"),
    ]
    for value in candidates:
        if value and Path(str(value)).exists():
            return Path(str(value))
    pdfs = sorted(paper_dir.parent.glob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError(f"no source PDF found for {paper_dir}")
    return pdfs[0]


def paper_type_from_v1(v1: dict[str, Any], metadata: dict[str, Any]) -> str:
    known = {"observational", "simulation", "theory", "review", "mixed"}
    candidate = str(metadata.get("paper_type") or "").lower()
    if candidate in known:
        return candidate
    for claim in v1.get("core_claims", []):
        candidate = str(claim.get("claim_type", "")).lower()
        for value in known:
            if value in candidate:
                return value
    return "unknown"


def default_questions(model: dict[str, Any], existing: list[tuple[str, str]]) -> list[dict[str, Any]]:
    claims = model.get("core_claims", [])
    methods = model.get("methods", [])
    figures = model.get("figures", [])
    limitations = model.get("limitations", [])
    first_claim = claims[0] if claims else {}
    first_anchor = first_claim.get("evidence") or normalize_anchor({})
    seed: list[tuple[str, str, str, str, str]] = []
    categories = ["recall", "self_explanation", "evidence_location", "figure_reconstruction", "transfer", "critique"]
    for index, (question, answer) in enumerate(existing[:8]):
        seed.append((categories[index % len(categories)], question, answer or "需要回到对应证据锚点作答。", "只复述标题或关键词。", "用完整因果链或证据链回答。"))
    generated = [
        ("recall", "这篇论文的核心研究问题和一句话答案是什么？", clean_text(model.get("reading_map", {}).get("research_question")) or clean_text(first_claim.get("text_zh") or first_claim.get("text")), "只列主题词，没有说明作者真正回答的问题。", "写出问题、方法和主要答案三部分。"),
        ("self_explanation", "请不用原文措辞解释本文最重要的机制或方法链。", clean_text(first_claim.get("text_zh") or first_claim.get("text")), "逐句照抄，无法说明环节之间的关系。", "用“条件→过程→结果→边界”重建。"),
        ("evidence_location", "哪一个页码、图或段落是最强证据锚点？", anchor_label(first_anchor), "把摘要中的主张当作唯一证据。", "优先定位结果图、方法参数或结论对应段落。"),
        ("method", "本文的方法类型、关键输入和主要输出是什么？", clean_text((methods[0] if methods else {}).get("text_zh") or (methods[0] if methods else {}).get("text")) or "方法细节需要人工核查。", "只说 observational/simulation 而不说明数据流。", "列出输入、处理步骤、输出和误差来源。"),
        ("figure_reconstruction", "闭眼重建最关键图：横纵轴、颜色/符号和主要变化分别是什么？", clean_text((figures[0] if figures else {}).get("direct_observation")) or "图像尚未完成视觉复核；应先打开原图再作答。", "只复述 caption。", "分开写视觉编码、直接观察和作者解释。"),
        ("critique", "作者最重要的局限或假设是什么？", clean_text((limitations[0] if limitations else {}).get("text_zh") or (limitations[0] if limitations else {}).get("text")) or "需要回到论文 limitation/discussion 核查。", "把模型/数据缺口忽略为无关细节。", "说明局限会限制哪一个具体 claim。"),
        ("transfer", f"如果把本文用于{clean_text(model.get('project')) or '当前项目'}，最安全的表述是什么？", clean_text((model.get("project_relations") or [{}])[0].get("text_zh") or (model.get("project_relations") or [{}])[0].get("text")) or "只能作为背景或待核查支持。", "把 project_application 写成本文直接结论。", "保留 simulation/proxy/candidate/support 等证据层级。"),
        ("critique", "本文绝对不能单独支持哪一种结论？", "; ".join(model.get("cannot_support", [])[:3]), "用流畅叙述掩盖缺失验证层。", "明确列出需要补充的观测、forward model 或独立证据。"),
        ("evidence_location", "论文结果、作者解释和 Codex 项目迁移分别属于哪一证据层？", "reported_result 属于论文报告结果；author_interpretation 属于作者解释；project_application/codex_inference 属于项目迁移，三者不得互换。", "把所有内容都写成‘论文证明’。", "逐条检查 epistemic_status 与 source_kind。"),
        ("transfer", "下一步最小验证动作是什么，完成后会改变哪一个判断？", clean_text((model.get("reading_map", {}).get("next_validation") or "复核关键图与 claim anchor，并补独立项目证据。")), "提出宏大研究计划但没有可判别结果。", "选择一项可在一次检查中确认或否定的动作。"),
    ]
    for item in generated:
        if len(seed) >= 10:
            break
        seed.append(item)
    return [
        {
            "id": f"Q{index:02d}",
            "category": category,
            "question": question,
            "answer": answer,
            "evidence": first_anchor,
            "common_error": common_error,
            "correction": correction,
        }
        for index, (category, question, answer, common_error, correction) in enumerate(seed[:12], start=1)
    ]


def new_model_stub(
    paper_dir: Path,
    metadata: dict[str, Any],
    match: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Create a schema-v2 triage scaffold for a newly prepared PDF.

    The placeholder claim is deliberately a model inference with no evidence
    anchor.  It keeps the schema renderable while ensuring that the Research
    Card excludes it until the PDF has actually been read and anchored.
    """
    pdf_path = find_pdf(metadata, paper_dir)
    paper_id = clean_text(metadata.get("paper_id") or paper_dir.name)
    title = clean_text(metadata.get("title") or paper_id)
    storage_parts = paper_dir.resolve().parts
    inferred_attachment_key = ""
    if "storage" in storage_parts:
        storage_index = storage_parts.index("storage")
        if storage_index + 1 < len(storage_parts):
            inferred_attachment_key = storage_parts[storage_index + 1]
    attachment_key = clean_text(metadata.get("zotero_attachment_key") or inferred_attachment_key or "fallback")
    parent_key = clean_text(match.get("zotero_item_key") or metadata.get("zotero_item_key") or "unmatched")
    empty_anchor = normalize_anchor({})
    claim = knowledge_item(
        paper_id=paper_id,
        prefix="C",
        index=1,
        text="Pending full-text extraction and evidence anchoring.",
        text_zh="待完成全文提取、中文概念重建与证据锚定；本条不是论文结论。",
        title=title,
        tags=[],
        source_kind="model",
        epistemic_status="codex_inference",
        support_level="speculative",
        evidence=empty_anchor,
        verification_status="needs_human_check",
        usable_for=["triage only"],
        cannot_support="不得进入调研卡，不得表述为论文结论或已深读结果。",
    )
    model: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "project": profile["display_name"],
        "project_profile": profile["project_id"],
        "paper_id": paper_id,
        "title": title,
        "authors": clean_text(metadata.get("authors")),
        "year": clean_text(metadata.get("year")),
        "doi": clean_text(metadata.get("doi")),
        "citekey": "",
        "zotero_item_key": parent_key,
        "zotero_attachment_key": attachment_key,
        "source_pdf_path": str(pdf_path.resolve()),
        "source_pdf_sha256": sha256_file(pdf_path),
        "priority": "unknown",
        "status": "draft",
        "paper_type": "unknown",
        "translation_mode": "layered_bilingual",
        "topic_tags": [],
        "abstract_original": "",
        "abstract_zh": "尚未生成；不得把 metadata/text preview 当作摘要翻译。",
        "reading_map": {
            "research_question": f"待从 {title} 的摘要、引言和结论中定位研究问题。",
            "prerequisites": list(profile["terminology"].keys())[:8],
            "terms": profile["terminology"],
            "reading_route": ["核对摘要与研究问题", "提取方法与 claim–evidence", "渲染并实际查看关键图页", "先答自检题再看答案"],
            "next_validation": "阅读全文并用 PDF 页序号、印刷页码、section/figure/table 和短原文完成证据锚定。",
        },
        "section_guides": [
            {
                "id": "S001",
                "title": "待全文处理",
                "summary_zh": "当前仅完成 triage scaffold；尚未进行分章节中文概念重建。",
                "key_sentences": [],
                "formula_terms": [],
                "verification_status": "needs_human_check",
            }
        ],
        "core_claims": [claim],
        "methods": [],
        "figures": [],
        "limitations": [],
        "controversies": [
            {
                "id": "V001",
                "question": "当前 triage 信息能否视为深读结论？",
                "paper_position": "尚未提取。",
                "counter_position": "关键词或 metadata 只能用于路由，不能替代全文证据。",
                "resolution": "完成全文、图页和 claim anchor 检查后再升级状态。",
                "verification_status": "needs_human_check",
                "related_claim_ids": ["C001"],
            }
        ],
        "external_context": [],
        "external_research_status": "not_run",
        "project_relations": [],
        "cannot_support": [
            "当前 scaffold 不能支持任何论文实质结论。",
            "未实际查看的图页不得标记为 visually_reviewed。",
            "项目迁移推断不得替代论文原文证据。",
        ],
        "open_questions": ["本文的核心问题、方法、结果和最强限制分别是什么？"],
        "generation": {
            "generated_at": now_iso(),
            "generated_by": "Codex pipeline v2 prepare_paper",
            "prompt_version": PROMPT_VERSION,
            "mode": "triage_scaffold",
            "source_schema_version": "2.0",
        },
    }
    model["self_test"] = default_questions(model, [])
    return model


def upgrade_v1_model(paper_dir: Path, profile: dict[str, Any]) -> dict[str, Any]:
    metadata = read_simple_yaml(paper_dir / "00_metadata.yaml")
    v1 = read_json(paper_dir / "02_machine_evidence_pack.json")
    old_reader = (paper_dir / "01_reader_note.zh.md").read_text(encoding="utf-8", errors="replace")
    old_card_path = paper_dir / "03_group_brief.zh.md"
    old_card = old_card_path.read_text(encoding="utf-8", errors="replace") if old_card_path.exists() else ""
    pdf_path = find_pdf(metadata, paper_dir)
    paper_id = clean_text(v1.get("paper_id") or metadata.get("paper_id") or paper_dir.name)
    title = clean_text(metadata.get("title") or v1.get("title") or old_reader.splitlines()[0].replace("# Reader Note：", ""))
    tags = [clean_text(tag) for tag in v1.get("topic_tags", []) if clean_text(tag)]
    claims: list[dict[str, Any]] = []
    for index, raw in enumerate(v1.get("core_claims", []), start=1):
        text = clean_text(raw.get("claim") or raw.get("text"))
        text_zh = clean_text(raw.get("claim_zh") or raw.get("relation_to_project") or raw.get("relation_to_sxi_cusp_research"))
        claims.append(
            knowledge_item(
                paper_id=paper_id,
                prefix="C",
                index=index,
                text=text,
                text_zh=text_zh or (text if has_cjk(text) else f"原文核心结论：{text}"),
                title=title,
                tags=tags,
                source_kind="paper",
                epistemic_status=epistemic_from_v1(raw, "claim"),
                support_level=support_from_v1(raw.get("support_level")),
                evidence=normalize_anchor(raw.get("evidence_anchor"), raw.get("quote") or text),
                usable_for=list(raw.get("usable_for") or []),
                cannot_support=clean_text(raw.get("cannot_support")),
            )
        )
    methods: list[dict[str, Any]] = []
    for index, raw in enumerate(v1.get("methods", []), start=1):
        text = clean_text(raw.get("method") or raw.get("text"))
        text_zh = clean_text(raw.get("method_zh") or raw.get("transfer_to_project") or raw.get("transfer_to_sxi_cusp_research"))
        methods.append(
            knowledge_item(
                paper_id=paper_id,
                prefix="M",
                index=index,
                text=text,
                text_zh=text_zh or (text if has_cjk(text) else f"方法描述：{text}"),
                title=title,
                tags=tags,
                source_kind="paper",
                epistemic_status="reported_result",
                support_level="direct",
                evidence=normalize_anchor(raw.get("evidence_anchor"), text),
                usable_for=["method comparison", "reproducibility check"],
                cannot_support=clean_text(raw.get("limitations")),
            )
        )
    limitation_items: list[dict[str, Any]] = []
    limitation_anchor = claims[-1].get("evidence") if claims else normalize_anchor({})
    for index, raw in enumerate(v1.get("limitations", []), start=1):
        text = clean_text(raw.get("text") if isinstance(raw, dict) else raw)
        limitation_items.append(
            knowledge_item(
                paper_id=paper_id,
                prefix="L",
                index=index,
                text=text,
                text_zh=text if has_cjk(text) else f"局限：{text}",
                title=title,
                tags=tags,
                source_kind="paper",
                epistemic_status="author_limitation",
                support_level="direct",
                evidence=normalize_anchor((raw or {}).get("evidence") if isinstance(raw, dict) else limitation_anchor, text),
                verification_status="anchored" if anchor_present(limitation_anchor) else "needs_human_check",
                usable_for=["claim boundary", "reviewer-safe wording"],
                cannot_support="不得忽略该局限扩展结论。",
            )
        )
    figure_notes = figure_blocks(old_reader)
    figures: list[dict[str, Any]] = []
    for index, raw in enumerate(v1.get("figures", []), start=1):
        label = clean_text(raw.get("figure") or f"Figure {index}")
        note = next((value for key, value in figure_notes.items() if label.lower() in key.lower() or key.lower() in label.lower()), {})
        caption = clean_text(raw.get("caption_or_summary") or raw.get("summary"))
        evidence = normalize_anchor({"page": raw.get("page"), "figure": label, "quote": caption})
        figures.append(
            {
                "id": f"F{index:03d}",
                "chunk_id": f"{paper_id}:f:f{index:03d}",
                "label": label,
                "status": "caption_only",
                "verification_status": "needs_human_check",
                "visual_evidence_path": "",
                "evidence": evidence,
                "caption_original": caption,
                "caption_zh": clean_text(note.get("图或段落显示什么")) or (caption if has_cjk(caption) else f"图注摘要：{caption}"),
                "axes_encoding": clean_text(note.get("读者应该怎样读")) or "需打开原图核对坐标、颜色、符号和单位。",
                "direct_observation": clean_text(note.get("图中直接可见什么") or note.get("图或段落显示什么")),
                "author_interpretation": clean_text(note.get("作者如何解释")),
                "alternative_explanations": [clean_text(note.get("替代解释"))] if note.get("替代解释") else [],
                "cannot_conclude": clean_text(note.get("不能过度解读什么")) or "caption-only 状态不能替代视觉读图，也不能从 proxy/model output 跳到观测确认。",
                "related_claim_ids": [claim["id"] for claim in claims if label and label in claim.get("evidence", {}).get("figure", "")],
                "retrieval_queries_zh": [f"{title} {label} 图表解释", f"{label} 能支持什么"],
                "retrieval_queries_en": [f"{title} {label}", f"{label} evidence"],
            }
        )
    project_relations: list[dict[str, Any]] = []
    project_anchor = claims[0].get("evidence") if claims else normalize_anchor({})
    legacy_project_relations = v1.get("relations_to_project") or v1.get("relations_to_sxi_cusp_research", [])
    for index, raw in enumerate(legacy_project_relations, start=1):
        text = clean_text(raw)
        project_relations.append(
            knowledge_item(
                paper_id=paper_id,
                prefix="P",
                index=index,
                text=text,
                text_zh=text if has_cjk(text) else f"项目迁移：{text}",
                title=title,
                tags=tags,
                source_kind="project",
                epistemic_status="project_application",
                support_level="indirect",
                evidence=project_anchor,
                verification_status="needs_human_check",
                related_chunk_ids=[claims[0]["chunk_id"]] if claims else [],
                usable_for=["project relevance", "reading priority"],
                cannot_support="项目迁移解释不是论文原文结论。",
            )
        )
    excluded_fragments = {"Codex 自检", "人工审核", "读者思考", "审核状态", "答案键", "主动回忆"}
    section_guides = [
        {
            "id": f"S{index:03d}",
            "title": section["title"],
            "summary_zh": section["body"][:3500],
            "key_sentences": [],
            "formula_terms": [],
            "verification_status": "needs_human_check" if "unknown" in section["body"].lower() else "anchored",
        }
        for index, section in enumerate(split_markdown_sections(old_reader), start=1)
        if not any(fragment in section["title"] for fragment in excluded_fragments) and section["body"]
    ]
    research_question = next((section["summary_zh"] for section in section_guides if "研究问题" in section["title"]), "")
    model: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "project": profile["display_name"],
        "project_profile": profile["project_id"],
        "paper_id": paper_id,
        "title": title,
        "authors": clean_text(metadata.get("authors")),
        "year": clean_text(metadata.get("year")),
        "doi": clean_text(metadata.get("doi")),
        "citekey": clean_text(v1.get("citekey")),
        "zotero_item_key": clean_text(v1.get("zotero_item_key") or metadata.get("zotero_item_key")),
        "zotero_attachment_key": clean_text(metadata.get("zotero_attachment_key") or paper_dir.parent.name),
        "source_pdf_path": str(pdf_path),
        "source_pdf_sha256": sha256_file(pdf_path),
        "priority": clean_text(v1.get("priority") or metadata.get("priority") or "unknown"),
        "status": "draft",
        "paper_type": paper_type_from_v1(v1, metadata),
        "translation_mode": "layered_bilingual",
        "topic_tags": tags,
        "abstract_original": "",
        "abstract_zh": research_question[:1600],
        "reading_map": {
            "research_question": research_question[:1800] or f"本文围绕 {title} 的研究问题、方法、结果和证据边界展开。",
            "prerequisites": list(profile["terminology"].keys())[:8],
            "terms": profile["terminology"],
            "reading_route": ["先读摘要和研究问题", "再读方法与关键 claim", "随后逐图核对", "最后完成自检并回到证据锚点"],
            "next_validation": "打开关键图页并核对 claim–evidence 锚点；项目使用前补独立观测或 forward-model 证据。",
        },
        "section_guides": section_guides,
        "core_claims": claims,
        "methods": methods,
        "figures": figures,
        "limitations": limitation_items,
        "controversies": [
            {
                "id": "V001",
                "question": "论文证据与当前项目迁移之间是否存在证据层级跳跃？",
                "paper_position": "以论文直接报告的结果和作者解释为准。",
                "counter_position": "项目应用仍需独立观测、forward model 或多源验证。",
                "resolution": "保持 source_kind 与 epistemic_status 分层；未验证项标 needs_human_check。",
                "verification_status": "needs_human_check",
                "related_claim_ids": [claim["id"] for claim in claims[:3]],
            }
        ],
        "external_context": [],
        "external_research_status": "not_run",
        "project_relations": project_relations,
        "cannot_support": [clean_text(value) for value in v1.get("cannot_support", []) if clean_text(value)],
        "open_questions": [clean_text(value) for value in v1.get("open_questions", []) if clean_text(value)],
        "legacy_group_brief": old_card[:4000],
        "generation": {
            "generated_at": now_iso(),
            "generated_by": "Codex pipeline v2 migrator",
            "prompt_version": V21_PROMPT_VERSION,
            "mode": "migrated_deep_read_draft",
            "source_schema_version": clean_text(v1.get("schema_version") or "1.0"),
        },
    }
    model["self_test"] = default_questions(model, parse_qa(old_reader))
    return model


def markdown_table_cell(value: Any) -> str:
    # A literal pipe must be escaped inside a GFM table cell.  Replacing it
    # with another glyph corrupts scientific notation such as ``|B|`` and
    # ``J||/B`` in the reader-facing artifact.
    return re.sub(r"(?<!\\)\|", r"\\|", str(value or "")).replace("\n", "<br>")


def source_edition_details(model: dict[str, Any]) -> dict[str, Any]:
    """Normalize optional edition metadata without mutating the model."""
    value = model.get("source_edition")
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        return {
            "evidence_pdf_version": value.strip(),
            "final_publication_directly_verified": "",
            "boundary_note_zh": "",
        }
    return {}


def _render_reader_note_legacy(model: dict[str, Any], profile: dict[str, Any]) -> str:
    lines = [
        f"# Reader Note | {profile['display_name']} | {model['priority']} | {model['status']} | {dt.date.today().isoformat()}",
        "",
        f"**论文**：{model['title']}",
        f"**证据状态**：{model['status']}；机器生成内容保留 `NOT HUMAN CHECKED`，不等于人工确认。",
        "",
        "## 第一层：读前地图",
        "",
        "### 研究问题",
        model.get("reading_map", {}).get("research_question", ""),
        "",
        "### 前置知识与术语",
    ]
    edition = source_edition_details(model)
    if edition:
        lines.insert(5, f"**证据版本**：{edition.get('evidence_pdf_version', 'unknown')}；精确 quote/图值仅核对该版本。{edition.get('boundary_note_zh', '')}")
    for term, explanation in model.get("reading_map", {}).get("terms", {}).items():
        lines.append(f"- **{term}**：{explanation}")
    lines.extend(["", "### 推荐阅读路线"])
    for index, step in enumerate(model.get("reading_map", {}).get("reading_route", []), start=1):
        lines.append(f"{index}. {step}")
    lines.extend(["", "## 第二层：分层中文精读", "", "### 摘要英中对照"])
    lines.append(f"- **原文/英文锚点**：{model.get('abstract_original') or '未在迁移草稿中稳定抽取；请回原 PDF abstract。'}")
    lines.append(f"- **中文概念重建**：{model.get('abstract_zh') or '需要人工补充。'}")
    for section in model.get("section_guides", []):
        if any(fragment in section.get("title", "") for fragment in ["审核状态", "Codex 自检", "人工审核", "读者思考", "答案键", "主动回忆"]):
            continue
        lines.extend(["", f"### {section['title']}", section.get("summary_zh", "")])
        for sentence in section.get("key_sentences", []):
            if isinstance(sentence, dict):
                lines.append(f"- **EN**：{sentence.get('en', '')}")
                lines.append(f"- **ZH**：{sentence.get('zh', '')}")
        for term in section.get("formula_terms", []):
            lines.append(f"- **公式/变量**：{term}")
    lines.extend(
        [
            "",
            "## 第三层：方法、证据与争议",
            "",
            "### 方法链",
            "| ID | 方法说明 | 证据锚点 | 状态 |",
            "| --- | --- | --- | --- |",
        ]
    )
    for item in model.get("methods", []):
        lines.append(f"| {item['id']} | {markdown_table_cell(item.get('text_zh') or item.get('text'))} | {anchor_label(item['evidence'])} | {item['verification_status']} |")
    lines.extend(["", "### Claim–Evidence Map", "| ID | 结论 | 证据层 | 支持 | 锚点 | 不能支持 |", "| --- | --- | --- | --- | --- | --- |"])
    for item in model.get("core_claims", []):
        lines.append(
            f"| {item['id']} | {markdown_table_cell(item.get('text_zh') or item.get('text'))} | {item['epistemic_status']} | {item['support_level']} | {anchor_label(item['evidence'])} | {markdown_table_cell(item.get('cannot_support'))} |"
        )
    lines.extend(["", "### 局限与争议"])
    for item in model.get("limitations", []):
        lines.append(f"- **{item['id']} [{item['verification_status']}]**：{item.get('text_zh') or item.get('text')}（{anchor_label(item['evidence'])}）")
    for item in model.get("controversies", []):
        lines.extend(
            [
                f"#### {item.get('id')}: {item.get('question')}",
                f"- 论文立场：{item.get('paper_position')}",
                f"- 限制/反方：{item.get('counter_position')}",
                f"- 当前处理：{item.get('resolution')}",
            ]
        )
    lines.extend(["", "### 外部文献定位（与本文证据分层）"])
    if not model.get("external_context"):
        lines.append(f"- external_research_status: `{model.get('external_research_status', 'not_run')}`；未联网时不生成虚构争议。")
    for source in model.get("external_context", []):
        url = source.get("doi_or_url") or ""
        link = f"[{url}]({url})" if url else "未提供 URL"
        lines.append(f"- **[{source.get('role')}] {source.get('citation')}**：{source.get('finding_zh')}（{link}；{source.get('verification_status')}）")
    lines.extend(["", f"### 与{profile['display_name']}的关系"])
    for item in model.get("project_relations", []):
        lines.append(f"- **{item['id']} / {item['epistemic_status']}**：{item.get('text_zh') or item.get('text')}；不得当作论文直接结论。")
    lines.extend(["", "## 第四层：关键图表精读"])
    if not model.get("figures"):
        lines.append("- 未发现可稳定定位的关键图；状态为 `not_available`。")
    for figure in model.get("figures", []):
        lines.extend(
            [
                "",
                f"### {figure['label']} [{figure['status']}]",
                f"- **证据锚点**：{anchor_label(figure['evidence'])}",
                f"- **图注中文**：{figure.get('caption_zh')}",
                f"- **坐标/颜色/变量**：{figure.get('axes_encoding')}",
                f"- **图中直接可见**：{figure.get('direct_observation') or '尚未视觉复核；不得从 caption 补写。'}",
                f"- **作者解释**：{figure.get('author_interpretation')}",
                f"- **替代解释**：{'; '.join(figure.get('alternative_explanations', [])) or '待核查'}",
                f"- **不能推出**：{figure.get('cannot_conclude')}",
                f"- **关联 claims**：{', '.join(figure.get('related_claim_ids', [])) or 'none'}",
            ]
        )
    lines.extend(["", "## 第五层：主动回忆与自检", "", "请先在不看答案的情况下作答，再到文末核对。"])
    for question in model.get("self_test", []):
        lines.extend(["", f"### {question['id']} [{question['category']}]", question["question"], "", "我的回答："])
    lines.extend(["", "## 答案键"])
    for question in model.get("self_test", []):
        correction = question.get("correction")
        correction_layer = question.get("correction_epistemic_status")
        if correction_layer in {"codex_inference", "project_application"}:
            correction = f"[{correction_layer}] {correction}"
        lines.extend(
            [
                "",
                f"### {question['id']} 答案",
                f"- **参考答案**：{question.get('answer')}",
                f"- **证据锚点**：{anchor_label(question.get('evidence', {}))}",
                f"- **常见误答**：{question.get('common_error')}",
                f"- **纠正方式**：{correction}",
            ]
        )
    lines.extend(
        [
            "",
            "## 人工审核记录",
            "- 我确认可信的部分：",
            "- 我发现的问题：",
            "- 需要更新的 claim / figure / answer：",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def render_reader_note(model: dict[str, Any], profile: dict[str, Any], *, today: str | None = None) -> str:
    """Render the current space-physics research-training Reader Note."""
    from reader_note_v3 import render_research_training_reader_note

    return render_research_training_reader_note(
        model,
        profile,
        today=today or dt.date.today().isoformat(),
        template_version=READER_NOTE_TEMPLATE_VERSION,
    )


def _render_research_card_legacy(model: dict[str, Any], profile: dict[str, Any]) -> str:
    claims = [item for item in model.get("core_claims", []) if item.get("verification_status") == "anchored" and anchor_present(item.get("evidence", {}))]
    methods = [item for item in model.get("methods", []) if anchor_present(item.get("evidence", {}))]
    # A producer may nominate a specific anchored method for the quicklook
    # card when the first method is too broad for a one-line briefing.  The
    # default remains the historical first anchored method, so existing
    # models are unchanged; the field is intentionally model-driven and keeps
    # Reader Note/RAG method order intact.
    card_method_id = model.get("research_card_method_id", "")
    if card_method_id:
        selected_method = next((item for item in methods if item.get("id") == card_method_id), None)
    else:
        selected_method = None
    if selected_method is None:
        selected_method = methods[0] if methods else {}
    figures = model.get("figures", [])
    limitations = model.get("limitations", [])
    first = claims[0] if claims else {}
    lines = [
        f"# 调研卡 | {profile['display_name']} | {model['priority']} | {model['status']} | {dt.date.today().isoformat()}",
        "",
        f"**论文**：{model['title']}",
        f"**PDF SHA-256**：`{model['source_pdf_sha256'][:16]}…`",
        "",
        "## 30 秒结论",
        first.get("text_zh") or first.get("text") or "当前没有通过锚点校验的核心 claim，不能进入快速汇报。",
        "",
        "## 3 分钟说明",
        f"- **研究问题**：{clean_text(model.get('reading_map', {}).get('research_question'), 700)}",
        f"- **方法**：{clean_text(selected_method.get('text_zh') or selected_method.get('text'), 500) or '方法锚点待核查。'}",
        "- **三项发现**：",
    ]
    edition = source_edition_details(model)
    if edition:
        lines.insert(5, f"**证据版本边界**：{edition.get('evidence_pdf_version', 'unknown')}；{edition.get('boundary_note_zh', '')}")
    for item in claims[:3]:
        lines.append(f"  - `{item['id']}` {item.get('text_zh') or item.get('text')}（{anchor_label(item['evidence'])}）")
    lines.extend(
        [
            f"- **关键图**：{figures[0]['label'] + ' / ' + figures[0]['status'] if figures else 'not available'}",
            f"- **最强限制**：{clean_text((limitations[0] if limitations else {}).get('text_zh') or (limitations[0] if limitations else {}).get('text')) or '待核查'}",
            "",
            "## 主要争议与文献定位",
        ]
    )
    for item in model.get("controversies", [])[:2]:
        lines.append(f"- {item.get('question')}：{item.get('resolution')} [{item.get('verification_status')}] ")
    for source in model.get("external_context", [])[:3]:
        lines.append(f"- [{source.get('role')}] {source.get('citation')}：{source.get('finding_zh')} ({source.get('doi_or_url')})")
    lines.extend(["", "## 汇报安全边界", "", "### 可以说"])
    for item in claims[:2]:
        lines.append(f"- {item.get('text_zh') or item.get('text')}（`{item['id']}`）")
    lines.extend(["", "### 不能说"])
    for value in model.get("cannot_support", [])[:4]:
        lines.append(f"- {value}")
    lines.extend(["", "## 导师/同门可能追问"])
    for question in model.get("self_test", [])[:3]:
        lines.append(f"- **Q：{question['question']}**")
        lines.append(f"  - A：{question['answer']}（{anchor_label(question['evidence'])}）")
    lines.extend(["", "## 审计", f"- 使用的 claim IDs：{', '.join(item['id'] for item in claims[:3]) or 'none'}", "- 未锚定 claim 已从本卡排除。"])
    return "\n".join(lines).rstrip() + "\n"


def render_research_card(model: dict[str, Any], profile: dict[str, Any], *, today: str | None = None) -> str:
    """Dispatch cards by prompt version without rewriting legacy artifacts."""
    from research_card_daily import is_v23_model, render_daily_research_card

    if is_v23_model(model, PROMPT_VERSION):
        return render_daily_research_card(model, profile, anchor_label=anchor_label, today=today)
    return _render_research_card_legacy(model, profile)


def chunk_markdown(item: dict[str, Any], context: str) -> list[str]:
    anchor = item.get("evidence", {})
    return [
        f"<!-- chunk_id: {item['chunk_id']} -->",
        f"## [{item['id']}] {clean_text(item.get('text_zh') or item.get('text'), 120)}",
        f"- context: {context}",
        f"- source_kind: {item['source_kind']}",
        f"- epistemic_status: {item['epistemic_status']}",
        f"- support_level: {item['support_level']}",
        f"- verification_status: {item['verification_status']}",
        f"- evidence_anchor: {anchor_label(anchor)}",
        f"- evidence_quote: {anchor.get('quote', '')}",
        f"- retrieval_queries_zh: {json.dumps(item.get('retrieval_queries_zh', []), ensure_ascii=False)}",
        f"- retrieval_queries_en: {json.dumps(item.get('retrieval_queries_en', []), ensure_ascii=False)}",
        f"- related_chunk_ids: {json.dumps(item.get('related_chunk_ids', []), ensure_ascii=False)}",
        f"- usable_for: {json.dumps(item.get('usable_for', []), ensure_ascii=False)}",
        f"- cannot_support: {item.get('cannot_support', '')}",
        "",
        item.get("text_zh") or item.get("text") or "",
        "",
    ]


def render_rag(model: dict[str, Any], profile: dict[str, Any]) -> str:
    edition = source_edition_details(model)
    direct_source_label = (
        "专著章级"
        if model.get("document_mode") == "book_chapter_full"
        or isinstance(model.get("book_contract"), dict)
        else "论文内"
    )
    lines = [
        "---",
        f"schema_version: {json.dumps(model['schema_version'])}",
        f"paper_id: {json.dumps(model['paper_id'])}",
        f"title: {json.dumps(model['title'], ensure_ascii=False)}",
        f"project_profile: {json.dumps(model['project_profile'])}",
        f"zotero_item_key: {json.dumps(model['zotero_item_key'])}",
        f"zotero_attachment_key: {json.dumps(model['zotero_attachment_key'])}",
        f"doi: {json.dumps(model.get('doi', ''))}",
        f"priority: {json.dumps(model['priority'])}",
        f"status: {json.dumps(model['status'])}",
        f"source_pdf_sha256: {json.dumps(model['source_pdf_sha256'])}",
        f"evidence_pdf_version: {json.dumps(edition.get('evidence_pdf_version', ''), ensure_ascii=False)}",
        f"final_publication_directly_verified: {json.dumps(edition.get('final_publication_directly_verified', ''))}",
        f"prompt_version: {json.dumps(model.get('generation', {}).get('prompt_version', ''))}",
        f"generated_at: {json.dumps(model.get('generation', {}).get('generated_at', ''))}",
        "---",
        "",
        f"# RAG Knowledge：{model['title']}",
        "",
        "本文件是本地 GPT/LLM 索引入口。每个 chunk 自包含上下文、证据层级和来源锚点；模型不得把 `project_application` 或 `external_finding` 改写为本文直接结论。",
        "",
    ]
    if edition:
        lines.extend([f"**证据版本边界**：{edition.get('boundary_note_zh', '')}", ""])
    for item in model.get("core_claims", []):
        lines.extend(
            chunk_markdown(
                item,
                f"{model['title']} 的{direct_source_label}核心 claim",
            )
        )
    for item in model.get("methods", []):
        lines.extend(chunk_markdown(item, f"{model['title']} 的数据/模型/方法"))
    for figure in model.get("figures", []):
        item = {
            "id": figure["id"],
            "chunk_id": figure["chunk_id"],
            "source_kind": "paper",
            "epistemic_status": "author_interpretation",
            "support_level": "direct" if figure.get("status") == "visually_reviewed" else "indirect",
            "verification_status": figure.get("verification_status", "needs_human_check"),
            "evidence": figure.get("evidence", {}),
            "retrieval_queries_zh": figure.get("retrieval_queries_zh", []),
            "retrieval_queries_en": figure.get("retrieval_queries_en", []),
            "related_chunk_ids": [],
            "usable_for": ["figure interpretation", "claim evidence"],
            "cannot_support": figure.get("cannot_conclude", ""),
            "text": figure.get("caption_original", ""),
            "text_zh": "；".join(part for part in [figure.get("caption_zh", ""), figure.get("direct_observation", ""), figure.get("author_interpretation", "")] if part),
        }
        lines.extend(chunk_markdown(item, f"{model['title']} 的 {figure['label']} 图表证据"))
    for item in model.get("limitations", []):
        lines.extend(chunk_markdown(item, f"{model['title']} 的局限和适用边界"))
    for item in model.get("project_relations", []):
        lines.extend(chunk_markdown(item, f"{model['title']} 到 {profile['display_name']} 的项目迁移；不是论文原文结论"))
    for index, source in enumerate(model.get("external_context", []), start=1):
        chunk_id = source.get("chunk_id") or f"{model['paper_id']}:e:e{index:03d}"
        lines.extend(
            [
                f"<!-- chunk_id: {chunk_id} -->",
                f"## [E{index:03d}] {source.get('citation')}",
                f"- context: {model['title']} 的外部文献定位；不得视为本文内容",
                "- source_kind: external",
                "- epistemic_status: external_finding",
                f"- verification_status: {source.get('verification_status')}",
                f"- role: {source.get('role')}",
                f"- doi_or_url: {source.get('doi_or_url')}",
                f"- accessed_at: {source.get('accessed_at')}",
                "",
                source.get("finding_zh", ""),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_figures_index(model: dict[str, Any], profile: dict[str, Any]) -> str:
    lines = [
        f"# Figures Index：{model['title']}",
        "",
        "| Figure/Table | PDF page | Printed page | Status | Direct observation | Related claims |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for figure in model.get("figures", []):
        anchor = figure.get("evidence", {})
        lines.append(
            f"| {markdown_table_cell(figure.get('label'))} | {anchor.get('pdf_page_index') or ''} | {markdown_table_cell(anchor.get('printed_page_label'))} | {figure.get('status')} | {markdown_table_cell(figure.get('direct_observation') or 'not visually reviewed')} | {', '.join(figure.get('related_claim_ids', []))} |"
        )
    if not model.get("figures"):
        lines.append("| unknown | | | not_available | PDF 中未稳定定位 | |")
    lines.extend(["", f"图表状态受 `{profile['project_id']}` claim boundary 约束；`caption_only` 不等于已视觉读图。"])
    return "\n".join(lines) + "\n"


class AllowlistHTMLSanitizer(HTMLParser):
    allowed_tags = {
        "p", "h1", "h2", "h3", "h4", "ul", "ol", "li", "table", "thead", "tbody",
        "tr", "th", "td", "strong", "em", "code", "pre", "blockquote", "a", "br", "hr",
        "sup", "sub", "span", "details", "summary",
    }
    void_tags = {"br", "hr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.source_text = ""
        self.source_line_starts = [0]
        self.anchor_modes: list[str] = []

    def feed(self, data: str) -> None:
        start = len(self.source_text)
        self.source_text += data
        self.source_line_starts.extend(start + i + 1 for i, char in enumerate(data) if char == "\n")
        super().feed(data)

    def original_endtag(self) -> str:
        # HTMLParser normalizes tag names. Its public source position still
        # points to the exact token, including case and whitespace, across feeds.
        line, column = self.getpos()
        start = self.source_line_starts[line - 1] + column
        end = self.source_text.find(">", start)
        if end < 0 or not self.source_text.startswith("</", start):
            raise ValueError("original closing tag text unavailable")
        return self.source_text[start:end + 1]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        literal_anchor = tag == "a" and not attrs
        if tag == "a":
            self.anchor_modes.append("literal" if literal_anchor else "markup")
        if tag not in self.allowed_tags or literal_anchor:
            # GFM passes angle-bracket scientific text such as <g> through as
            # raw HTML. It is not an allowed element, but silently deleting it
            # changes the equation. Retain its literal spelling as safe text.
            self.parts.append(html.escape(self.get_starttag_text()))
            return
        safe_attrs: list[str] = []
        for key, value in attrs:
            if tag == "a" and key in {"href", "title"} and value:
                if key != "href" or value.startswith(("http://", "https://", "#")):
                    safe_attrs.append(f'{key}="{html.escape(value, quote=True)}"')
            elif tag in {"th", "td"} and key in {"colspan", "rowspan"} and value and value.isdigit():
                safe_attrs.append(f'{key}="{value}"')
        suffix = (" " + " ".join(safe_attrs)) if safe_attrs else ""
        self.parts.append(f"<{tag}{suffix}>")

    def handle_endtag(self, tag: str) -> None:
        literal_anchor = tag == "a" and (not self.anchor_modes or self.anchor_modes.pop() == "literal")
        if tag not in self.allowed_tags or literal_anchor:
            self.parts.append(html.escape(self.original_endtag()))
        elif tag not in self.void_tags:
            self.parts.append(f"</{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self.allowed_tags or (tag == "a" and not attrs):
            # HTMLParser's default emits start+end callbacks for <G/>; keep the
            # original literal once, without synthesizing an extra </g>.
            self.parts.append(html.escape(self.get_starttag_text()))
        else:
            super().handle_startendtag(tag, attrs)

    def handle_data(self, data: str) -> None:
        self.parts.append(html.escape(data))

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def html(self) -> str:
        return "".join(self.parts)


def preserve_unpaired_inline_paragraph_angles(markdown: str) -> str:
    """Protect the reproduced mean-pressure <p>/<P> case before GFM parsing.

    Paired paragraphs, block paragraphs and intentional inline HTML are kept.
    Code is already literal Markdown and must not acquire escaped-entity text.
    This is deliberately scoped to bare, unpaired, inline paragraph tokens.
    """
    excluded: list[tuple[int, int]] = []
    fence = None
    position = 0
    for line in markdown.splitlines(keepends=True):
        match = re.match(r" {0,3}(`{3,}|~{3,})", line)
        if fence is not None:
            excluded.append((position, position + len(line)))
            closing = r" {0,3}" + re.escape(fence[0]) + r"{" + str(len(fence)) + r",}[ \t]*(?:\r?\n)?$"
            if re.fullmatch(closing, line):
                fence = None
        elif match:
            fence = match[1]
            excluded.append((position, position + len(line)))
        position += len(line)
    excluded.extend((m.start(), m.end()) for m in re.finditer(r"(?<!`)(`+)(?!`)(.*?)(?<!`)\1(?!`)", markdown, re.S))
    tokens = [m for m in re.finditer(r"</?p\s*>", markdown, re.I)
              if not any(a <= m.start() < b for a, b in excluded)]
    stack = []
    for token in tokens:
        if token[0].startswith("</"):
            if stack:
                stack.pop()
        else:
            stack.append(token)
    for token in reversed(stack):
        line_start = markdown.rfind("\n", 0, token.start()) + 1
        if markdown[line_start:token.start()].strip():
            markdown = markdown[:token.start()] + html.escape(token[0]) + markdown[token.end():]
    return markdown


def markdown_to_zotero_html(markdown: str, draft: bool = True) -> str:
    proc = subprocess.run(
        ["pandoc", "--from=gfm", "--to=html", "--wrap=none"],
        input=preserve_unpaired_inline_paragraph_angles(markdown),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pandoc failed: {proc.stderr.strip()}")
    sanitizer = AllowlistHTMLSanitizer()
    sanitizer.feed(proc.stdout)
    markers = ['<p><code>zotero-note znv1</code></p>']
    if draft:
        markers.insert(0, '<p><strong>NOT HUMAN CHECKED</strong></p>')
    return '<div class="zotero-note znv1">\n' + "\n".join(markers) + "\n" + sanitizer.html() + "\n</div>\n"


def validate_model(model: dict[str, Any], profile: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    required = {
        "schema_version", "project", "project_profile", "paper_id", "title", "zotero_item_key",
        "zotero_attachment_key", "source_pdf_sha256", "priority", "status", "paper_type",
        "translation_mode", "reading_map", "section_guides", "core_claims", "methods", "figures",
        "limitations", "controversies", "external_context", "project_relations", "cannot_support",
        "self_test", "generation",
    }
    missing = sorted(required - set(model))
    if missing:
        errors.append(f"knowledge model missing keys: {missing}")
    if model.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if model.get("project") != profile.get("display_name"):
        generation = model.get("generation") if isinstance(model.get("generation"), dict) else {}
        if generation.get("prompt_version") in LEGACY_PROMPT_VERSIONS:
            warnings.append(
                "legacy knowledge model project label differs from profile display_name; "
                "retained without rewriting historical artifacts"
            )
        else:
            errors.append("knowledge model project does not match profile display_name")
    if not re.fullmatch(r"[0-9a-f]{64}", str(model.get("source_pdf_sha256", ""))):
        errors.append("source_pdf_sha256 must be a lowercase SHA-256")
    from adaptive_content import adaptive, validate_coverage
    errors.extend(validate_coverage(model))
    if not adaptive(model) and not 10 <= len(model.get("self_test", [])) <= 12:
        errors.append("self_test must contain 10-12 questions")
    item_ids: set[str] = set()
    chunk_ids: set[str] = set()
    for collection in ["core_claims", "methods", "limitations", "project_relations"]:
        for index, item in enumerate(model.get(collection, []), start=1):
            if item.get("id") in item_ids:
                errors.append(f"duplicate knowledge item id: {item.get('id')}")
            item_ids.add(item.get("id", ""))
            if item.get("chunk_id") in chunk_ids:
                errors.append(f"duplicate chunk_id: {item.get('chunk_id')}")
            chunk_ids.add(item.get("chunk_id", ""))
            if item.get("source_kind") not in SOURCE_KINDS:
                errors.append(f"{collection}[{index}] invalid source_kind")
            if item.get("epistemic_status") not in EPISTEMIC_STATUSES:
                errors.append(f"{collection}[{index}] invalid epistemic_status")
            if item.get("support_level") not in SUPPORT_LEVELS:
                errors.append(f"{collection}[{index}] invalid support_level")
            if item.get("verification_status") not in VERIFICATION_STATUSES:
                errors.append(f"{collection}[{index}] invalid verification_status")
            if not item.get("retrieval_queries_zh") or not item.get("retrieval_queries_en"):
                errors.append(f"{collection}[{index}] missing bilingual retrieval queries")
            if collection == "core_claims" and item.get("verification_status") == "anchored" and not anchor_present(item.get("evidence", {})):
                errors.append(f"{collection}[{index}] claims anchored status without locator")
            if item.get("source_kind") == "paper" and item.get("epistemic_status") in {"external_finding", "project_application"}:
                errors.append(f"{collection}[{index}] mixes paper source with external/project epistemic status")
    for figure in model.get("figures", []):
        if figure.get("chunk_id") in chunk_ids:
            errors.append(f"duplicate chunk_id: {figure.get('chunk_id')}")
        chunk_ids.add(figure.get("chunk_id", ""))
        if figure.get("status") not in FIGURE_STATUSES:
            errors.append(f"figure {figure.get('id')} invalid status")
        if figure.get("status") == "visually_reviewed":
            visual_path = Path(str(figure.get("visual_evidence_path", "")))
            if not visual_path.exists() or not clean_text(figure.get("direct_observation")):
                errors.append(f"figure {figure.get('id')} visually_reviewed without visual evidence file and observation")
        elif figure.get("direct_observation"):
            warnings.append(f"figure {figure.get('id')} has direct_observation but status is {figure.get('status')}")
    if len(model.get("external_context", [])) > profile.get("external_research_policy", {}).get("max_sources", 5):
        errors.append("external_context exceeds profile max_sources")
    for source in model.get("external_context", []):
        verification = source.get("verification_status")
        if verification == "needs_human_check":
            # Historical content corrections can retain an unresolved external
            # reference as explicit draft context. Rendering it must not force
            # a false external-verification declaration. Full intake's external
            # research requirement remains independently enforced by v21_quality.
            if model.get("status") != "draft":
                errors.append("pending external source is allowed only in draft artifacts")
            if (source.get("authority") != "context_only"
                    or source.get("supports_claim") is not False
                    or not clean_text(source.get("review_note_zh"))):
                errors.append(f"pending external source requires context_only, supports_claim=false and review_note_zh: {source.get('citation')}")
            if model.get("external_research_status") not in {"not_run", "failed"}:
                errors.append("pending external source conflicts with external_research_status")
            warnings.append(f"external source remains unverified draft context: {source.get('citation')}")
        elif verification != "externally_verified":
            errors.append(f"external source is not externally_verified: {source.get('citation')}")
        if not source.get("doi_or_url"):
            errors.append(f"external source lacks DOI/URL: {source.get('citation')}")
    if model.get("priority") == "P0" and model.get("external_research_status") not in {"completed", "not_run", "not_required", "failed"}:
        errors.append("P0 external_research_status invalid")
    from research_card_daily import is_v23_model, validate_research_card

    if is_v23_model(model, PROMPT_VERSION):
        card_errors, _card_checks = validate_research_card(model)
        errors.extend(card_errors)
    return errors, warnings


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.write_text("", encoding="utf-8")
    for row in rows:
        append_jsonl(path, row)


def project_relevance(model: dict[str, Any], profile: dict[str, Any]) -> str:
    """Return a project-neutral review relevance label.

    Deep-read producers may provide either a scalar relevance label or a
    structured relevance object.  Older cusp models provide neither, so their
    historical P0/high and non-P0/unknown mapping is retained.
    """
    raw = model.get("project_relevance", model.get("relevance"))
    if isinstance(raw, dict):
        raw = raw.get("level") or raw.get("label") or raw.get("status")
    value = clean_text(raw).lower()
    if value in {"high", "medium", "low", "unknown"}:
        return value
    priority = clean_text(model.get("priority")).upper()
    if profile.get("project_id") == "sxi_cusp":
        return "high" if priority == "P0" else "unknown"
    return {"P0": "high", "P1": "medium", "P2": "low"}.get(priority, "unknown")


def review_status_for(
    model: dict[str, Any],
    profile: dict[str, Any],
    warnings: list[str],
    paper_dir: Path | None = None,
) -> dict[str, Any]:
    """Build the generic review-status document while preserving cusp fields."""
    relevance = project_relevance(model, profile)
    review: dict[str, Any] = {
        "project": profile["display_name"],
        "project_id": profile["project_id"],
        "paper_id": model["paper_id"],
        "status": model["status"],
        "priority": model["priority"],
        "project_relevance": relevance,
        "translation_mode": model["translation_mode"],
        "research_card_template_version": (
            model.get("research_card", {}).get("template_version", "legacy")
            if isinstance(model.get("research_card"), dict)
            else "legacy"
        ),
        "fulltext_translation_status": "not_applicable",
        "fulltext_translation_path": "",
        "fulltext_translation_sha256": "",
        "external_research_status": model.get("external_research_status", "not_run"),
        "figure_review_status": "visually_reviewed" if model.get("figures") and all(item.get("status") == "visually_reviewed" for item in model["figures"]) else "partial_or_caption_only",
        "human_checked_by": "",
        "human_checked_at": "",
        "unresolved_critical_warnings": warnings,
        "notes": "Pipeline v2 deterministic render from schema v2 knowledge model.",
    }
    if profile.get("project_id") == "sxi_cusp":
        review["sxi_cusp_relevance"] = relevance
    if paper_dir is not None:
        translation_manifest = paper_dir / "fulltext_translation" / "manifest.json"
        if translation_manifest.is_file():
            try:
                manifest = read_json(translation_manifest)
                review["fulltext_translation_status"] = manifest.get("status", "blocked")
                if manifest.get("status") == "complete":
                    output_path = paper_dir / "10_fulltext_translation.zh-CN.pdf"
                    if output_path.is_file():
                        review["fulltext_translation_path"] = output_path.name
                        review["fulltext_translation_sha256"] = sha256_file(output_path)
            except Exception as exc:
                review["fulltext_translation_status"] = "blocked"
                review["unresolved_critical_warnings"].append(
                    f"translation manifest unreadable: {type(exc).__name__}: {exc}"
                )
    return review


def write_outputs(paper_dir: Path, model: dict[str, Any], profile: dict[str, Any]) -> dict[str, str]:
    errors, warnings = validate_model(model, profile)
    if errors:
        raise ValueError("; ".join(errors))
    reader = render_reader_note(model, profile)
    rag = render_rag(model, profile)
    card = render_research_card(model, profile)
    figures = render_figures_index(model, profile)
    # Reject PDF extraction controls before any artifact is overwritten or a
    # draft is sent to visual acceptance. Keep the evidence unchanged on failure.
    from note_html_integrity import validate_note_html
    reader_html = markdown_to_zotero_html(reader, draft=model.get("status") == "draft")
    card_html = markdown_to_zotero_html(card, draft=model.get("status") == "draft")
    validate_note_html(reader_html, label="08_reader_note.zotero.html")
    validate_note_html(card_html, label="09_research_card.zotero.html")
    write_json(paper_dir / "02_machine_evidence_pack.json", model)
    (paper_dir / "01_reader_note.zh.md").write_text(reader, encoding="utf-8")
    (paper_dir / "02_rag_knowledge.md").write_text(rag, encoding="utf-8")
    (paper_dir / "03_research_card.zh.md").write_text(card, encoding="utf-8")
    (paper_dir / "04_figures_index.md").write_text(figures, encoding="utf-8")
    write_jsonl(paper_dir / "05_claims.jsonl", model.get("core_claims", []))
    evidence_rows = []
    for item in [*model.get("core_claims", []), *model.get("methods", []), *model.get("limitations", [])]:
        evidence_rows.append({"span_id": item["id"], **item.get("evidence", {}), "chunk_id": item["chunk_id"], "verification_status": item["verification_status"]})
    write_jsonl(paper_dir / "06_evidence_spans.jsonl", evidence_rows)
    write_jsonl(paper_dir / "07_methods.jsonl", model.get("methods", []))
    (paper_dir / "08_reader_note.zotero.html").write_text(reader_html, encoding="utf-8")
    (paper_dir / "09_research_card.zotero.html").write_text(card_html, encoding="utf-8")
    review = review_status_for(model, profile, warnings, paper_dir)
    (paper_dir / "review_status.yaml").write_text(simple_yaml(review), encoding="utf-8")
    append_jsonl(
        paper_dir / "audit_log.jsonl",
        {
            "timestamp": now_iso(),
            "event": "pipeline_v2_render",
            "schema_version": model["schema_version"],
            "source_pdf_sha256": model["source_pdf_sha256"],
            "warnings": warnings,
        },
    )
    return {
        "reader_note": str(paper_dir / "01_reader_note.zh.md"),
        "rag_knowledge": str(paper_dir / "02_rag_knowledge.md"),
        "research_card": str(paper_dir / "03_research_card.zh.md"),
        "reader_html": str(paper_dir / "08_reader_note.zotero.html"),
        "research_card_html": str(paper_dir / "09_research_card.zotero.html"),
    }


def backup_artifacts(paper_dir: Path, backup_dir: Path) -> list[str]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for path in paper_dir.iterdir():
        if path.is_file() and path.name != "audit_log.jsonl":
            shutil.copy2(path, backup_dir / path.name)
            copied.append(path.name)
    return copied
