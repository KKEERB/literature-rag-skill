#!/usr/bin/env python3
"""Shared helpers for the SXI-cusp literature workflow.

This module intentionally uses the Python standard library only. PDF text and
metadata extraction are delegated to Poppler command line tools when available.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from urllib.parse import unquote, urlparse
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


PROJECT_NAME = "SXI-cusp研究"
FORBIDDEN_PHRASE = "SXI-cusp" + "第一篇文章"

PROCESSING_COLUMNS = [
    "paper_id",
    "pdf_path",
    "title",
    "doi",
    "year",
    "page_count",
    "zotero_item_key",
    "zotero_match_confidence",
    "zotero_match_reason",
    "status",
    "paper_dir",
    "updated_at",
]

FAILED_COLUMNS = [
    "paper_id",
    "pdf_path",
    "failure_type",
    "message",
    "updated_at",
]

QUEUE_COLUMNS = [
    "paper_id",
    "project_id",
    "project_tag",
    "priority",
    "zotero_item_key",
    "zotero_attachment_key",
    "title",
    "note_kind",
    "note_title",
    "note_path",
    "zotero_note_key",
    "write_mode",
    "expected_content_sha256",
    "status",
    "match_confidence",
    "reason_if_blocked",
]

LITERATURE_COLUMNS = [
    "paper_id",
    "title",
    "doi",
    "year",
    "priority",
    "status",
    "sxi_cusp_relevance",
    "zotero_item_key",
    "zotero_match_confidence",
    "artifact_location",
    "artifact_dir",
    "topic_tags",
    "one_sentence_takeaway",
    "updated_at",
]

CANONICAL_REQUIRED_FILES = [
    "00_metadata.yaml",
    "01_reader_note.zh.md",
    "02_machine_evidence_pack.json",
    "02_rag_knowledge.md",
    "03_research_card.zh.md",
    "04_figures_index.md",
    "05_claims.jsonl",
    "06_evidence_spans.jsonl",
    "07_methods.jsonl",
    "08_reader_note.zotero.html",
    "09_research_card.zotero.html",
    "review_status.yaml",
]

STAGING_REQUIRED_FILES = [
    *CANONICAL_REQUIRED_FILES,
    "audit_log.jsonl",
]

# Backwards-compatible alias for producer/staging code. Canonical validation
# must use CANONICAL_REQUIRED_FILES explicitly because promotion deliberately
# does not copy the per-run audit log into Zotero storage.
EXPECTED_PAPER_FILES = STAGING_REQUIRED_FILES

LEGACY_READER_NOTE_REQUIRED_HEADINGS = [
    "## 第一层：读前地图",
    "## 第二层：分层中文精读",
    "## 第三层：方法、证据与争议",
    "## 第四层：关键图表精读",
    "## 第五层：主动回忆与自检",
    "## 答案键",
]

READER_NOTE_REQUIRED_HEADINGS = [
    "## 0. 阅读导航（Reading Guide）",
    "## 1. 论文身份卡（Paper Identity Card）",
    "## 2. 本文最核心的工作内容",
    "## 3. 科学背景与研究问题",
    "## 4. 物理机制理解",
    "## 5. 第一性原理物理审查（First-Principles Physics Analysis）",
    "## 6. 方法、数据、模型与实验设计",
    "## 7. Figure / Table 科学精读",
    "## 8. 科学贡献与发表价值分析",
    "## 9. 批判性审查",
    "## 10. 读者自测 QA",
    "## 11. 与",
    "## 12. 组会汇报版总结（10 分钟）",
    "## 13. 附录模块（按需）",
    "## 答案键",
    "## 人工审核记录",
]


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def today_iso() -> str:
    return dt.date.today().isoformat()


def ensure_base_dirs(root: Path | None = None) -> None:
    base = root or project_root()
    for name in ["inbox_pdf", "papers", "reports", "templates", "schemas", "scripts"]:
        (base / name).mkdir(parents=True, exist_ok=True)


def run_command(args: list[str], timeout: int = 30, *, encoding: str | None = None) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            args,
            check=False,
            text=True,
            encoding=encoding,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except Exception as exc:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": str(exc)}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in columns})


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def upsert_csv_row(path: Path, columns: list[str], key: str, row: dict[str, Any]) -> None:
    rows = read_csv(path)
    found = False
    for existing in rows:
        if existing.get(key) == str(row.get(key, "")):
            existing.update({col: str(row.get(col, "")) for col in columns})
            found = True
            break
    if not found:
        rows.append({col: str(row.get(col, "")) for col in columns})
    write_csv(path, columns, rows)


def command_status(name: str) -> dict[str, Any]:
    path = shutil.which(name)
    return {"name": name, "path": path, "available": path is not None}


def import_status(module_name: str) -> dict[str, Any]:
    try:
        __import__(module_name)
        return {"module": module_name, "available": True, "error": None}
    except Exception as exc:
        return {"module": module_name, "available": False, "error": type(exc).__name__}


def parse_pdfinfo_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        out[key.strip()] = value.strip()
    return out


def pdfinfo(pdf_path: Path) -> tuple[dict[str, str], list[str]]:
    result = run_command(["pdfinfo", "-enc", "UTF-8", str(pdf_path)], timeout=20, encoding="utf-8")
    warnings: list[str] = []
    if not result["ok"]:
        warnings.append(result["stderr"] or "pdfinfo failed")
        return {}, warnings
    return parse_pdfinfo_text(result["stdout"]), warnings


def pdftotext(pdf_path: Path, max_pages: int | None = None) -> tuple[str, list[str]]:
    args = ["pdftotext", "-enc", "UTF-8", "-layout"]
    if max_pages is not None:
        args.extend(["-f", "1", "-l", str(max_pages)])
    args.extend([str(pdf_path), "-"])
    result = run_command(args, timeout=60, encoding="utf-8")
    if not result["ok"]:
        return "", [result["stderr"] or "pdftotext failed"]
    return result["stdout"], []


def clean_doi(raw: str) -> str:
    doi = raw.strip().rstrip(".,;)]}>")
    doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
    return doi


def detect_doi(text: str) -> str:
    match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", text, flags=re.IGNORECASE)
    return clean_doi(match.group(0)) if match else ""


def detect_year(text: str) -> str:
    match = re.search(r"\b(19|20)\d{2}\b", text)
    return match.group(0) if match else ""


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_title(value: str) -> str:
    value = normalize_space(value).lower()
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", value)
    return normalize_space(value)


def safe_title_from_text(text: str) -> str:
    lines = [normalize_space(line) for line in text.splitlines()]
    candidates: list[str] = []
    blocked = {
        "abstract",
        "introduction",
        "references",
        "contents",
        "article",
        "research article",
    }
    for line in lines[:80]:
        if len(line) < 12 or len(line) > 220:
            continue
        if line.lower() in blocked:
            continue
        if re.match(r"^(doi|arxiv|http|www\.|page\s+\d+)", line, flags=re.IGNORECASE):
            continue
        candidates.append(line)
    return candidates[0] if candidates else ""


def slugify(value: str, fallback: str = "paper") -> str:
    value = normalize_space(value)
    value = re.sub(r"[^\w\s.-]+", "", value, flags=re.UNICODE)
    value = re.sub(r"[\s_.]+", "-", value)
    value = value.strip("-").lower()
    value = re.sub(r"-{2,}", "-", value)
    return value[:80] or fallback


def file_sha256(path: Path, limit: int | None = None) -> str:
    h = hashlib.sha256()
    remaining = limit
    with path.open("rb") as fh:
        while True:
            size = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
            if size <= 0:
                break
            chunk = fh.read(size)
            if not chunk:
                break
            h.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return h.hexdigest()


def metadata_from_pdf(pdf_path: Path, text_pages: int = 3) -> dict[str, Any]:
    info, info_warnings = pdfinfo(pdf_path)
    text, text_warnings = pdftotext(pdf_path, max_pages=text_pages)
    full_hint = "\n".join([info.get("Title", ""), info.get("Subject", ""), text])
    title = normalize_space(info.get("Title", "")) if info.get("Title") else ""
    if not title or title.lower() in {"untitled", "unknown"}:
        title = safe_title_from_text(text)
    doi = detect_doi(full_hint)
    year = detect_year(" ".join([info.get("CreationDate", ""), info.get("ModDate", ""), text]))
    page_count = info.get("Pages", "")
    stem = slugify(title or pdf_path.stem, fallback=pdf_path.stem)
    digest = file_sha256(pdf_path, limit=1024 * 1024)[:8]
    paper_id = f"{stem}-{digest}"
    return {
        "paper_id": paper_id,
        "pdf_path": str(pdf_path.resolve()),
        "pdf_filename": pdf_path.name,
        "title": title,
        "doi": doi,
        "year": year,
        "page_count": page_count,
        "pdfinfo": info,
        "text_preview": text[:8000],
        "warnings": info_warnings + text_warnings,
        "sha256_first_mb": digest,
    }


def yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    text = str(value).replace("\n", " ").strip()
    if text == "" or any(ch in text for ch in [":", "#", "{", "}", "[", "]", ",", '"', "'"]):
        return json.dumps(text, ensure_ascii=False)
    return text


def simple_yaml(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for key, value in data.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            if not value:
                lines.append("  []")
            else:
                for item in value:
                    lines.append(f"  - {yaml_scalar(item)}")
        elif isinstance(value, dict):
            lines.append(f"{key}:")
            for sub_key, sub_value in value.items():
                lines.append(f"  {sub_key}: {yaml_scalar(sub_value)}")
        else:
            lines.append(f"{key}: {yaml_scalar(value)}")
    return "\n".join(lines) + "\n"


def read_simple_yaml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or line.startswith(" "):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        raw = value.strip()
        if raw.lower() in {"true", "false"}:
            parsed: Any = raw.lower() == "true"
        elif raw in {"[]", ""}:
            parsed = [] if raw == "[]" else ""
        else:
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = raw
        data[key.strip()] = parsed
    return data


def reader_note_html_stub(metadata: dict[str, Any], review: dict[str, Any]) -> str:
    title = metadata.get("title") or metadata.get("paper_id") or "unknown title"
    priority = review.get("priority", "unknown")
    status = review.get("status", "draft")
    today = today_iso()
    return f"""<div class="zotero-note znv1">
<h1>Reader Note | {PROJECT_NAME} | {html.escape(str(priority))} | {html.escape(str(status))} | {today}</h1>
<p><strong>NOT HUMAN CHECKED</strong></p>
<p><code>zotero-note znv1</code></p>
<h2>元数据</h2>
<ul>
  <li>Title: {html.escape(str(title))}</li>
  <li>DOI: {html.escape(str(metadata.get("doi", "")))}</li>
  <li>Local paper dir: {html.escape(str(metadata.get("paper_dir", "")))}</li>
</ul>
<h2>用途</h2>
<p>辅助精读和人工复核。完整 Markdown 见 01_reader_note.zh.md。</p>
<h2>审核状态</h2>
<ul>
  <li>Status: {html.escape(str(status))}</li>
</ul>
</div>
"""


def group_brief_html_stub(metadata: dict[str, Any], review: dict[str, Any]) -> str:
    title = metadata.get("title") or metadata.get("paper_id") or "unknown title"
    priority = review.get("priority", "unknown")
    status = review.get("status", "draft")
    today = today_iso()
    return f"""<div class="zotero-note znv1">
<h1>LLM精读卡 | {PROJECT_NAME} | {html.escape(str(priority))} | {html.escape(str(status))} | {today}</h1>
<p><strong>NOT HUMAN CHECKED</strong></p>
<p><code>zotero-note znv1</code></p>
<h2>元数据</h2>
<ul>
  <li>Title: {html.escape(str(title))}</li>
  <li>DOI: {html.escape(str(metadata.get("doi", "")))}</li>
  <li>Local paper dir: {html.escape(str(metadata.get("paper_dir", "")))}</li>
</ul>
<h2>用途</h2>
<p>面向导师/同门和快速回顾。完整 Markdown 见 03_group_brief.zh.md。</p>
<h2>审核状态</h2>
<ul>
  <li>Status: {html.escape(str(status))}</li>
</ul>
</div>
"""


def boolish(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "allowed"}
