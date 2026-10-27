#!/usr/bin/env python3
"""Validate pipeline v2 model, rendered artifacts, and cross-file consistency."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from common import (
    CANONICAL_REQUIRED_FILES,
    LEGACY_READER_NOTE_REQUIRED_HEADINGS,
    READER_NOTE_REQUIRED_HEADINGS,
    STAGING_REQUIRED_FILES,
    project_root,
    read_json,
    write_json,
)
from v2_core import PROMPT_VERSION, anchor_present, load_profile, sha256_file, validate_model


def check_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"{path.name}:{line_no} JSON decode error: {exc}")
            continue
        if not isinstance(value, dict):
            errors.append(f"{path.name}:{line_no} row is not an object")
        else:
            rows.append(value)
    return rows, errors


def ordered_markers(text: str, markers: list[str]) -> bool:
    positions = [text.find(marker) for marker in markers]
    return all(position >= 0 for position in positions) and positions == sorted(positions)


def has_note_title_marker(name: str, note: str) -> bool:
    """Accept the approved v2.3 card title without weakening legacy checks."""
    if name == "09_research_card.zotero.html":
        return "调研卡 |" in note or "每日文献调研汇报 |" in note
    return "Reader Note |" in note


def required_paper_files(contract: str) -> list[str]:
    if contract == "canonical":
        return list(CANONICAL_REQUIRED_FILES)
    if contract == "staging":
        return list(STAGING_REQUIRED_FILES)
    raise ValueError(f"unknown artifact contract: {contract}")


def resolve_profile_path(value: Any) -> Path:
    """Resolve both legacy bare profile names and stored JSON paths."""
    raw = str(value or "sxi_cusp").strip()
    candidate = Path(raw)
    if candidate.suffix.lower() == ".json":
        if candidate.is_absolute():
            return candidate
        rooted = project_root() / candidate
        if rooted.exists():
            return rooted
        return project_root() / "profiles" / candidate.name
    return project_root() / "profiles" / f"{candidate.name}.json"


def load_provenance_manifest(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    if not path.exists():
        raise FileNotFoundError(f"provenance manifest does not exist: {path}")
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid provenance manifest line {line_no}: {exc}") from exc
        for key in (str(row.get("zotero_item_key", "")), str(row.get("zotero_attachment_key", ""))):
            if key:
                rows[key] = row
    return rows


def validate_manifest_entry(
    paper_dir: Path,
    model: dict[str, Any],
    provenance: dict[str, dict[str, Any]] | None,
) -> list[str]:
    if provenance is None:
        return ["canonical validation requires a provenance manifest"]
    key = str(model.get("zotero_item_key", ""))
    attachment = str(model.get("zotero_attachment_key", ""))
    entry = provenance.get(key) or provenance.get(attachment)
    if not entry:
        return [f"canonical provenance missing for parent/attachment: {key}/{attachment}"]
    errors: list[str] = []
    expected = {
        "model_sha256": sha256_file(paper_dir / "02_machine_evidence_pack.json"),
        "rag_sha256": sha256_file(paper_dir / "02_rag_knowledge.md"),
        "source_pdf_sha256": str(model.get("source_pdf_sha256", "")),
    }
    for field, value in expected.items():
        if str(entry.get(field, "")) != value:
            errors.append(f"canonical provenance {field} differs from current artifact")
    if not entry.get("provenance"):
        errors.append("canonical provenance lacks run/QA/promotion lineage")
    return errors


def resolve_source_pdf_path(
    paper_dir: Path,
    model: dict[str, Any],
    provenance: dict[str, dict[str, Any]] | None,
) -> Path | None:
    """Resolve a canonical PDF without rewriting legacy model artifacts."""
    candidates: list[Path] = []
    configured = str(model.get("source_pdf_path") or "")
    if configured:
        candidates.append(Path(configured))
    if provenance:
        key = str(model.get("zotero_item_key") or "")
        attachment = str(model.get("zotero_attachment_key") or "")
        entry = provenance.get(key) or provenance.get(attachment) or {}
        manifested = str(entry.get("source_pdf_path") or "")
        if manifested:
            candidates.append(Path(manifested))
    candidates.extend(sorted(paper_dir.parent.glob("*.pdf")))

    existing: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.expanduser()
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        existing.append(candidate)
    recorded_hash = str(model.get("source_pdf_sha256") or "")
    if recorded_hash:
        for candidate in existing:
            if sha256_file(candidate) == recorded_hash:
                return candidate
    return existing[0] if len(existing) == 1 else None


def validate_paper(
    paper_dir: Path,
    profile: dict[str, Any],
    write_report: bool = False,
    *,
    contract: str = "staging",
    provenance: dict[str, dict[str, Any]] | None = None,
    report_dir: Path | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not paper_dir.exists():
        errors.append(f"paper directory does not exist: {paper_dir}")
        return {"paper_dir": str(paper_dir), "error_count": 1, "warning_count": 0, "errors": errors, "warnings": warnings}
    for name in required_paper_files(contract):
        if not (paper_dir / name).exists():
            errors.append(f"missing required file: {name}")
    if errors:
        return {"paper_dir": str(paper_dir), "error_count": len(errors), "warning_count": 0, "errors": errors, "warnings": warnings}

    try:
        model = read_json(paper_dir / "02_machine_evidence_pack.json")
        model_errors, model_warnings = validate_model(model, profile)
        errors.extend(model_errors)
        warnings.extend(model_warnings)
        if contract == "canonical":
            errors.extend(validate_manifest_entry(paper_dir, model, provenance))
    except Exception as exc:
        errors.append(f"invalid knowledge model: {type(exc).__name__}: {exc}")
        model = {}

    pdf_path = resolve_source_pdf_path(paper_dir, model, provenance)
    if pdf_path is None:
        errors.append("source PDF missing or cannot be uniquely resolved")
    elif model.get("source_pdf_sha256") != sha256_file(pdf_path):
        errors.append("source PDF SHA-256 differs from knowledge model")

    reader = (paper_dir / "01_reader_note.zh.md").read_text(encoding="utf-8", errors="replace")
    current_reader_structure = ordered_markers(reader, READER_NOTE_REQUIRED_HEADINGS)
    legacy_reader_structure = ordered_markers(reader, LEGACY_READER_NOTE_REQUIRED_HEADINGS)
    if not current_reader_structure and not legacy_reader_structure:
        errors.append(
            "Reader Note research-training modules (or supported legacy five-layer structure) "
            "are missing or out of order"
        )
    question_prompts = re.findall(r"(?m)^### Q\d{2} \[", reader)
    answer_prompts = re.findall(r"(?m)^### Q\d{2} 答案$", reader)
    from adaptive_content import adaptive
    is_adaptive = adaptive(model)
    if (not is_adaptive and not 10 <= len(question_prompts) <= 12) or (is_adaptive and not question_prompts):
        errors.append(f"Reader Note must expose 10-12 questions; got {len(question_prompts)}")
    if len(answer_prompts) != len(question_prompts):
        errors.append("Reader Note question/answer counts differ")
    if reader.find("## 答案键") <= reader.find("### Q01 ["):
        errors.append("Reader Note answer key appears before questions")

    model_claim_ids = {item.get("id") for item in model.get("core_claims", [])}
    anchored_claim_ids = {
        item.get("id")
        for item in model.get("core_claims", [])
        if item.get("verification_status") == "anchored" and anchor_present(item.get("evidence", {}))
    }
    card = (paper_dir / "03_research_card.zh.md").read_text(encoding="utf-8", errors="replace")
    card_ids = set(re.findall(r"\bC\d{3}\b", card))
    unknown_card_ids = sorted(card_ids - model_claim_ids)
    if unknown_card_ids:
        errors.append(f"Research Card references unknown claim IDs: {unknown_card_ids}")
    unanchored_card_ids = sorted(card_ids - anchored_claim_ids)
    if unanchored_card_ids:
        errors.append(f"Research Card references unanchored claim IDs: {unanchored_card_ids}")
    if "未锚定 claim 已从本卡排除" not in card:
        errors.append("Research Card missing unanchored-claim exclusion marker")
    generation = model.get("generation", {}) if isinstance(model.get("generation"), dict) else {}
    is_v23 = generation.get("prompt_version") == PROMPT_VERSION and generation.get("mode") != "triage_scaffold"
    if is_v23:
        from research_card_daily import research_card_visible_chinese_count
        from v21_quality import load_quality_profile, validate_deep_read_model

        if contract == "staging":
            semantic_errors, semantic_warnings, _semantic_report = validate_deep_read_model(
                model,
                profile,
                load_quality_profile(project_root() / "profiles" / "content_quality_v21.json"),
                paper_dir,
                require_reviewer=False,
            )
            errors.extend(f"semantic quality: {error}" for error in semantic_errors)
            warnings.extend(f"semantic quality: {warning}" for warning in semantic_warnings)

        ordered_sections = [
            "## 今日日报摘要",
            "## 研究问题—方法—关键发现—关键图定位",
            "## 对当前项目的意义",
            "## 客观质疑",
            "## Q&A",
            "## 下一步最小行动",
            "## 证据边界与审计",
        ]
        if not ordered_markers(card, ordered_sections):
            errors.append("v2.3 Research Card sections are missing or out of order")
        visible_count = research_card_visible_chinese_count(card)
        if not is_adaptive and not 1000 <= visible_count <= 1800:
            errors.append(f"v2.3 Research Card visible Chinese body length is {visible_count}, expected 1000-1800")
        if not is_adaptive and len(re.findall(r"(?m)^### 质疑 \d+｜", card)) != 3:
            errors.append("v2.3 Research Card must render exactly three objective challenges")
        if not is_adaptive and len(re.findall(r"(?m)^### Q\d{2}｜", card)) != 4:
            errors.append("v2.3 Research Card must render exactly four Q&A groups")
        if re.search(r"!\[[^]]*\]\(|<img\b", card, flags=re.I):
            errors.append("v2.3 Research Card must not embed images")

        translation_manifest = paper_dir / "fulltext_translation" / "manifest.json"
        if not translation_manifest.is_file():
            warnings.append("v2.3 full-text translation stage has not been prepared")
        else:
            try:
                translation = read_json(translation_manifest)
                translation_status = translation.get("status")
                if translation_status == "complete":
                    if translation.get("template_version") != "academic_cn_journal_v1":
                        errors.append("new v2.3 complete translation must use academic_cn_journal_v1")
                    translation_pdf = paper_dir / "10_fulltext_translation.zh-CN.pdf"
                    if not translation_pdf.is_file():
                        errors.append("translation manifest is complete but formal PDF is missing")
                    elif sha256_file(translation_pdf) != translation.get("final_pdf_sha256"):
                        errors.append("translation PDF hash differs from its manifest")
                    translation_files = {
                        "block_ledger_sha256": paper_dir / "fulltext_translation" / "translation_blocks.jsonl",
                        "qa_sha256": paper_dir / "fulltext_translation" / "qa.json",
                        "rendered_pages_manifest_sha256": paper_dir / "fulltext_translation" / "rendered_pages_manifest.json",
                    }
                    for field, path in translation_files.items():
                        if not path.is_file():
                            errors.append(f"complete translation artifact is missing: {path.name}")
                        elif sha256_file(path) != translation.get(field):
                            errors.append(f"translation {path.name} hash differs from its manifest")
                elif translation_status in {"staging", "blocked"}:
                    warnings.append(f"translation stage is incomplete: {translation_status}")
                elif translation_status != "not_applicable":
                    errors.append(f"translation manifest has invalid status: {translation_status}")
            except Exception as exc:
                errors.append(f"invalid translation manifest: {type(exc).__name__}: {exc}")

    rag = (paper_dir / "02_rag_knowledge.md").read_text(encoding="utf-8", errors="replace")
    chunk_ids = re.findall(r"<!-- chunk_id: ([^ ]+) -->", rag)
    expected_chunks = {
        item.get("chunk_id")
        for collection in ["core_claims", "methods", "limitations", "project_relations"]
        for item in model.get(collection, [])
    }
    expected_chunks.update(figure.get("chunk_id") for figure in model.get("figures", []))
    if len(chunk_ids) != len(set(chunk_ids)):
        errors.append("RAG document contains duplicate chunk IDs")
    missing_chunks = sorted(value for value in expected_chunks if value and value not in chunk_ids)
    if missing_chunks:
        errors.append(f"RAG document missing model chunks: {missing_chunks}")
    for marker in ["retrieval_queries_zh:", "retrieval_queries_en:", "epistemic_status:", "evidence_anchor:"]:
        if marker not in rag:
            errors.append(f"RAG document missing marker: {marker}")

    for name, expected_marker in [
        ("08_reader_note.zotero.html", "Reader Note |"),
        ("09_research_card.zotero.html", "调研卡 |"),
    ]:
        note = (paper_dir / name).read_text(encoding="utf-8", errors="replace")
        if '<div class="zotero-note znv1">' not in note:
            errors.append(f"{name} missing zotero-note znv1 wrapper")
        if not has_note_title_marker(name, note):
            errors.append(f"{name} missing title marker: {expected_marker}")
        if model.get("status") == "draft" and "NOT HUMAN CHECKED" not in note:
            errors.append(f"{name} draft missing NOT HUMAN CHECKED")
        if "<script" in note.lower() or "javascript:" in note.lower():
            errors.append(f"{name} contains unsafe HTML")
        if name == "09_research_card.zotero.html" and "<img" in note.lower():
            errors.append(f"{name} must not embed images")

    claims_rows, claim_errors = check_jsonl(paper_dir / "05_claims.jsonl")
    evidence_rows, evidence_errors = check_jsonl(paper_dir / "06_evidence_spans.jsonl")
    methods_rows, method_errors = check_jsonl(paper_dir / "07_methods.jsonl")
    errors.extend(claim_errors + evidence_errors + method_errors)
    if {item.get("id") for item in claims_rows} != model_claim_ids:
        errors.append("05_claims.jsonl IDs differ from knowledge model")
    if {item.get("id") for item in methods_rows} != {item.get("id") for item in model.get("methods", [])}:
        errors.append("07_methods.jsonl IDs differ from knowledge model")
    if not evidence_rows:
        warnings.append("06_evidence_spans.jsonl is empty")

    report = {
        "paper_dir": str(paper_dir.resolve()),
        "paper_id": model.get("paper_id", paper_dir.name),
        "schema_version": model.get("schema_version"),
        "contract": contract,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "counts": {
            "claims": len(model.get("core_claims", [])),
            "figures": len(model.get("figures", [])),
            "visually_reviewed_figures": sum(figure.get("status") == "visually_reviewed" for figure in model.get("figures", [])),
            "self_test_questions": len(model.get("self_test", [])),
            "rag_chunks": len(chunk_ids),
        },
    }
    if write_report:
        if report_dir is None:
            raise ValueError("an explicit user report directory is required")
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(report['paper_id'])).strip('.') or 'paper'
        write_json(Path(report_dir) / f"validation_v2_{safe_id}.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--paper", help="Paper artifact directory.")
    parser.add_argument("--profile", help="Optional explicit project profile; otherwise infer it from each model.")
    parser.add_argument("--contract", choices=["staging", "canonical"])
    parser.add_argument("--provenance-manifest", help="Explicit source manifest; required for canonical validation.")
    parser.add_argument("--workspace", help="User workspace for validation reports; defaults to LITERATURE_RAG_HOME.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    contract = args.contract or "staging"
    workspace_text = args.workspace or os.environ.get('LITERATURE_RAG_HOME')
    if not workspace_text:
        parser.error("--workspace or LITERATURE_RAG_HOME is required for reports")
    workspace = Path(workspace_text).expanduser()
    if not workspace.is_absolute() or not workspace.is_dir():
        parser.error("workspace must be an existing absolute directory")
    workspace = workspace.resolve()
    report_dir = workspace / 'reports'
    if not report_dir.resolve().is_relative_to(workspace):
        parser.error("workspace reports must stay inside the workspace")
    if contract == 'canonical' and not args.provenance_manifest:
        parser.error("--provenance-manifest is required for canonical validation")
    provenance = load_provenance_manifest(Path(args.provenance_manifest).expanduser()) if contract == "canonical" else None
    targets = [Path(args.paper)]
    skipped_noncanonical = []
    reports = []
    for path in targets:
        if args.profile:
            profile_path = Path(args.profile)
        else:
            model = read_json(path / "02_machine_evidence_pack.json")
            profile_path = resolve_profile_path(model.get("project_profile"))
        reports.append(
            validate_paper(
                path,
                load_profile(profile_path),
                contract=contract,
                provenance=provenance,
                report_dir=report_dir,
                write_report=True,
            )
        )
    summary = {
        "contract": contract,
        "paper_count": len(reports),
        "error_count": sum(report["error_count"] for report in reports),
        "warning_count": sum(report["warning_count"] for report in reports),
        "skipped_noncanonical_directory_count": len(skipped_noncanonical),
        "skipped_noncanonical_directories": skipped_noncanonical,
        "reports": reports,
    }
    write_json(report_dir / "validation_v2_all.json", summary)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"Validation v2: {summary['paper_count']} papers, {summary['error_count']} errors, {summary['warning_count']} warnings")
    return 1 if summary["error_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
