#!/usr/bin/env python3
"""Local, rebuildable FTE/CUSP/SXI research knowledge system.

The canonical literature models and Zotero metadata are read-only inputs.  The
SQLite database is a derived index plus two deliberately lower-authority stores:
work memory and externally researched staging records.
"""

from __future__ import annotations

import csv
import datetime as dt
import glob
import hashlib
import html
import json
import math
import os
import re
import shutil
import sqlite3
import statistics
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Iterable

from common import now_iso, project_root, read_json
from v2_core import sha256_file
from knowledge_quality import (attach_reviews, consistent_backup, initialize_quality,
                               quality_decision, work_identity, workflow_lock, availability,
                               record_intake_quality, revision_lineage_ids, input_binding)
from source_edition_projection import (edition_projection, edition_provenance,
                                       resolve_edition_projection)


KNOWLEDGE_SCHEMA_VERSION = "1.0"
WORK_TYPES = {"hypothesis", "decision", "finding", "failure", "checklist"}
VERIFICATION_STATUSES = {"anchored", "externally_verified", "needs_human_check"}
PROMOTION_STATES = {"staged", "approved", "rejected", "tombstoned"}
DOMAIN_TERMS = {
    "fte": ["fte", "flux transfer event", "flux rope", "磁通量传输", "通量绳", "reconnection", "重联"],
    "cusp": ["cusp", "cleft", "llbl", "mantle", "极尖区", "尖区", "粒子 cusp"],
    "sxi": ["sxi", "swcx", "soft x-ray", "soft xray", "软 x", "软x", "x射线", "smile"],
    "magnetopause": ["magnetopause", "magnetosheath", "bow shock", "磁层顶", "磁鞘", "弓激波"],
    "simulation": ["ppmlr", "mhd", "hybrid", "pic", "simulation", "数值模拟", "混合模拟"],
}

SOURCE_COLUMNS = [
    "source_id", "source_type", "authority", "title", "title_normalized", "authors", "year", "doi",
    "zotero_item_key", "zotero_attachment_key", "collections_json", "tags_json", "priority", "status",
    "project_profile", "source_path", "source_pdf_path", "source_pdf_sha256", "model_sha256", "rag_sha256",
    "provenance_json", "content_hash", "updated_at", "tombstoned",
]
CHUNK_COLUMNS = [
    "chunk_id", "source_id", "kind", "item_id", "text", "text_zh", "context", "source_kind",
    "epistemic_status", "support_level", "verification_status", "evidence_json", "retrieval_queries_zh_json",
    "retrieval_queries_en_json", "related_chunk_ids_json", "usable_for_json", "cannot_support", "authority",
    "content_hash", "tombstoned",
]


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_mtime_iso(path: Path) -> str:
    return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc).replace(microsecond=0).isoformat()


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = re.sub(r"[^0-9a-z\u3400-\u9fff]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def cjk_bigrams(value: str) -> list[str]:
    sequences = re.findall(r"[\u3400-\u9fff]+", unicodedata.normalize("NFKC", value or ""))
    terms: list[str] = []
    for sequence in sequences:
        if len(sequence) == 1:
            terms.append(sequence)
        else:
            terms.extend(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return terms


def lexical_terms(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value or "").lower()
    terms = re.findall(r"[a-z0-9][a-z0-9_.+-]*", normalized)
    terms.extend(cjk_bigrams(normalized))
    result: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if term and term not in seen:
            seen.add(term)
            result.append(term)
    return result


def fts_match_query(value: str) -> str:
    terms = lexical_terms(value)[:32]
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def detect_domains(value: str) -> list[str]:
    lowered = unicodedata.normalize("NFKC", value or "").lower()
    return [domain for domain, terms in DOMAIN_TERMS.items() if any(term.lower() in lowered for term in terms)]


def default_config_path() -> Path:
    return project_root() / "configs" / "knowledge_system.json"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path).expanduser().resolve() if path else default_config_path()
    config = read_json(config_path)
    if config.get("schema_version") != KNOWLEDGE_SCHEMA_VERSION:
        raise ValueError(f"unsupported knowledge config schema: {config.get('schema_version')}")
    config["config_path"] = str(config_path)
    config["database_path"] = str(Path(str(config["database_path"])).expanduser())
    config["review_dir"] = str(Path(str(config["review_dir"])).expanduser())
    return config


def database_path(config: dict[str, Any], override: str | Path | None = None) -> Path:
    path = Path(override if override else str(config['database_path'])).expanduser()
    if path.is_symlink():
        raise ValueError('database path cannot be a symlink')
    return path.resolve()


def connect_database(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(path.expanduser().resolve().as_uri() + '?mode=ro', uri=True)
    else:
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=DELETE;
        CREATE TABLE sources (
            source_id TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            authority TEXT NOT NULL CHECK(authority IN ('evidence','context_only','staging')),
            title TEXT NOT NULL,
            title_normalized TEXT NOT NULL,
            authors TEXT NOT NULL DEFAULT '',
            year TEXT NOT NULL DEFAULT '',
            doi TEXT NOT NULL DEFAULT '',
            zotero_item_key TEXT NOT NULL DEFAULT '',
            zotero_attachment_key TEXT NOT NULL DEFAULT '',
            collections_json TEXT NOT NULL DEFAULT '[]',
            tags_json TEXT NOT NULL DEFAULT '[]',
            priority TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            project_profile TEXT NOT NULL DEFAULT '',
            source_path TEXT NOT NULL DEFAULT '',
            source_pdf_path TEXT NOT NULL DEFAULT '',
            source_pdf_sha256 TEXT NOT NULL DEFAULT '',
            model_sha256 TEXT NOT NULL DEFAULT '',
            rag_sha256 TEXT NOT NULL DEFAULT '',
            provenance_json TEXT NOT NULL DEFAULT '{}',
            content_hash TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            tombstoned INTEGER NOT NULL DEFAULT 0 CHECK(tombstoned IN (0,1))
        );
        CREATE TABLE chunks (
            chunk_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES sources(source_id),
            kind TEXT NOT NULL,
            item_id TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '',
            text_zh TEXT NOT NULL DEFAULT '',
            context TEXT NOT NULL DEFAULT '',
            source_kind TEXT NOT NULL DEFAULT '',
            epistemic_status TEXT NOT NULL DEFAULT '',
            support_level TEXT NOT NULL DEFAULT '',
            verification_status TEXT NOT NULL DEFAULT '',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            retrieval_queries_zh_json TEXT NOT NULL DEFAULT '[]',
            retrieval_queries_en_json TEXT NOT NULL DEFAULT '[]',
            related_chunk_ids_json TEXT NOT NULL DEFAULT '[]',
            usable_for_json TEXT NOT NULL DEFAULT '[]',
            cannot_support TEXT NOT NULL DEFAULT '',
            authority TEXT NOT NULL CHECK(authority IN ('evidence','context_only','staging')),
            content_hash TEXT NOT NULL,
            tombstoned INTEGER NOT NULL DEFAULT 0 CHECK(tombstoned IN (0,1))
        );
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            chunk_id UNINDEXED,
            title,
            body,
            aliases,
            ngrams,
            tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TABLE work_memory (
            record_id TEXT PRIMARY KEY,
            domain TEXT NOT NULL,
            origin_project TEXT NOT NULL,
            memory_type TEXT NOT NULL CHECK(memory_type IN ('hypothesis','decision','finding','failure','checklist')),
            statement TEXT NOT NULL,
            applicability TEXT NOT NULL DEFAULT '',
            dataset_version TEXT NOT NULL DEFAULT '',
            coordinate_context TEXT NOT NULL DEFAULT '',
            authority TEXT NOT NULL DEFAULT 'context_only' CHECK(authority='context_only'),
            evidence_chunk_ids_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'active',
            content_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE work_memory_fts USING fts5(
            record_id UNINDEXED,
            statement,
            context,
            ngrams,
            tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TABLE external_staging (
            record_id TEXT PRIMARY KEY,
            query TEXT NOT NULL,
            url TEXT NOT NULL DEFAULT '',
            doi TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            finding TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT '',
            accessed_at TEXT NOT NULL,
            domain TEXT NOT NULL DEFAULT '',
            origin_project TEXT NOT NULL DEFAULT '',
            verification_status TEXT NOT NULL DEFAULT 'needs_human_check',
            evidence_links_json TEXT NOT NULL DEFAULT '[]',
            state TEXT NOT NULL DEFAULT 'staged' CHECK(state IN ('staged','approved','rejected','tombstoned')),
            content_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE promotions (
            promotion_id TEXT PRIMARY KEY,
            record_id TEXT NOT NULL,
            from_authority TEXT NOT NULL,
            to_authority TEXT NOT NULL,
            result TEXT NOT NULL,
            reviewer TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            source_hash TEXT NOT NULL DEFAULT '',
            promoted_chunk_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE sync_runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            manifest_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            added_count INTEGER NOT NULL,
            updated_count INTEGER NOT NULL,
            unchanged_count INTEGER NOT NULL,
            tombstoned_count INTEGER NOT NULL,
            source_count INTEGER NOT NULL,
            chunk_count INTEGER NOT NULL,
            paper_source_count INTEGER NOT NULL,
            paper_chunk_count INTEGER NOT NULL,
            warnings_json TEXT NOT NULL DEFAULT '[]',
            validation_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX sources_type_idx ON sources(source_type, tombstoned);
        CREATE INDEX sources_zotero_idx ON sources(zotero_item_key, zotero_attachment_key);
        CREATE INDEX chunks_source_idx ON chunks(source_id, tombstoned);
        CREATE INDEX chunks_status_idx ON chunks(authority, verification_status, epistemic_status);
        CREATE INDEX staging_state_idx ON external_staging(state, verification_status);
        """
    )


def insert_row(connection: sqlite3.Connection, table: str, columns: list[str], row: dict[str, Any]) -> None:
    placeholders = ",".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
        [row.get(column, "") for column in columns],
    )


def copy_mutable_state(old_path: Path, new_connection: sqlite3.Connection) -> None:
    if not old_path.exists():
        return
    with connect_database(old_path, read_only=True) as old:
        tables = {row[0] for row in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        initialize_quality(new_connection)
        from knowledge_dependencies import initialize_dependencies
        initialize_dependencies(new_connection)
        from reading_sessions import initialize_reading
        initialize_reading(new_connection)
        for table in ("work_memory", "external_staging", "promotions",
                      "source_quality", "chunk_quality", "quality_history", "evidence_dependencies",
                      "reading_review_events", "reading_replacements", "reading_artifact_impacts", "reading_publications"):
            if table not in tables:
                continue
            columns = [row[1] for row in old.execute(f"PRAGMA table_info({table})")]
            target_columns = [row[1] for row in new_connection.execute(f"PRAGMA table_info({table})")]
            shared = [column for column in target_columns if column in columns]
            for row in old.execute(f"SELECT {','.join(shared)} FROM {table}"):
                placeholders = ",".join("?" for _ in shared)
                new_connection.execute(
                    f"INSERT OR IGNORE INTO {table} ({','.join(shared)}) VALUES ({placeholders})",
                    list(row),
                )


def reindex_work_memory(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM work_memory_fts")
    for row in connection.execute("SELECT * FROM work_memory WHERE status='active'"):
        context = " ".join(
            str(row[key] or "")
            for key in ("domain", "origin_project", "memory_type", "applicability", "dataset_version", "coordinate_context")
        )
        text = str(row["statement"])
        connection.execute(
            "INSERT INTO work_memory_fts(record_id,statement,context,ngrams) VALUES (?,?,?,?)",
            (row["record_id"], text, context, " ".join(cjk_bigrams(text + " " + context))),
        )


def run_provenance_map(root: Path) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for manifest_path in sorted((root / "rebuild_runs").glob("*/run_manifest.json")):
        try:
            manifest = read_json(manifest_path)
        except Exception:
            continue
        run_root = manifest_path.parent
        qa_path = run_root / "qa" / "full_library_qa.json"
        promotion_path = run_root / "promotion_report.json"
        common = {
            "run_id": manifest.get("run_id") or run_root.name,
            "run_manifest_path": str(manifest_path),
            "run_manifest_sha256": sha256_file(manifest_path),
            "qa_path": str(qa_path) if qa_path.exists() else "",
            "qa_sha256": sha256_file(qa_path) if qa_path.exists() else "",
            "promotion_path": str(promotion_path) if promotion_path.exists() else "",
            "promotion_sha256": sha256_file(promotion_path) if promotion_path.exists() else "",
        }
        for paper in manifest.get("papers", []):
            parent = str(paper.get("parent_key") or paper.get("zotero_item_key") or "")
            if not parent:
                continue
            result.setdefault(parent, []).append(
                {
                    **common,
                    "stage_dir": paper.get("stage_dir", ""),
                    "canonical_artifact_dir": paper.get("canonical_artifact_dir", ""),
                    "pdf_path": paper.get("pdf_path", ""),
                    "model_content_sha256": paper.get("model_content_sha256", ""),
                    "revision_round": paper.get("revision_round", ""),
                }
            )
    return result


def walk_chunk_items(value: Any, path: tuple[Any, ...] = ()) -> Iterable[tuple[str, tuple[Any, ...], dict[str, Any]]]:
    # Generation/revision provenance can retain exact old/new scientific objects
    # for review. Those historical copies are audit data, never indexed evidence.
    if path and path[0] == "generation":
        return
    if isinstance(value, dict):
        chunk_id = value.get("chunk_id")
        if chunk_id:
            yield str(chunk_id), path, value
        for key, child in value.items():
            yield from walk_chunk_items(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_chunk_items(child, path + (index,))


def paper_context(kind: str, model: dict[str, Any], item: dict[str, Any]) -> str:
    title = str(model.get("title", ""))
    profile = str(model.get("project_profile", ""))
    source_label = (
        "专著章级"
        if model.get("document_mode") == "book_chapter_full"
        or isinstance(model.get("book_contract"), dict)
        else "论文内"
    )
    labels = {
        "core_claims": f"{title} 的{source_label}核心 claim",
        "methods": f"{title} 的数据、模型或方法",
        "figures": f"{title} 的 {item.get('label', '图表')} 图表证据",
        "limitations": f"{title} 的局限和适用边界",
        "project_relations": f"{title} 到 {profile} 的项目迁移；不是论文原文结论",
        "external_context": f"{title} 的外部文献定位；不得视为本文内容",
    }
    return labels.get(kind, f"{title} 的结构化知识项")


def external_context_chunk(model: dict[str, Any], source: dict[str, Any], index: int) -> dict[str, Any]:
    chunk_id = str(source.get("chunk_id") or f"{model['paper_id']}:e:e{index:03d}")
    return {
        "chunk_id": chunk_id,
        "id": str(source.get("id") or f"E{index:03d}"),
        "source_kind": "external",
        "epistemic_status": "external_finding",
        "support_level": "indirect",
        "verification_status": str(source.get("verification_status") or "needs_human_check"),
        "text": str(source.get("finding") or ""),
        "text_zh": str(source.get("finding_zh") or source.get("finding") or ""),
        "evidence": {
            "pdf_page_index": None,
            "printed_page_label": "",
            "section": str(source.get("citation") or ""),
            "figure": "",
            "table": "",
            "quote": "",
            "doi_or_url": str(source.get("doi_or_url") or ""),
            "accessed_at": str(source.get("accessed_at") or ""),
        },
        "retrieval_queries_zh": [str(source.get("citation") or ""), str(source.get("finding_zh") or "")[:100]],
        "retrieval_queries_en": [str(source.get("citation") or "")],
        "related_chunk_ids": [],
        "usable_for": [str(source.get("role") or "external context")],
        "cannot_support": "该条目是论文外部定位，不能改写为本文直接结论或本地项目事实。",
    }


def normalized_paper_chunk(
    source_id: str,
    model: dict[str, Any],
    kind: str,
    item: dict[str, Any],
) -> dict[str, Any]:
    if kind == "figures":
        normalized = {
            "chunk_id": item["chunk_id"],
            "id": item.get("id", ""),
            "source_kind": "paper",
            "epistemic_status": "author_interpretation",
            "support_level": "direct" if item.get("status") == "visually_reviewed" else "indirect",
            "verification_status": item.get("verification_status", "needs_human_check"),
            "text": item.get("caption_original", ""),
            "text_zh": "；".join(
                str(value) for value in (item.get("caption_zh"), item.get("direct_observation"), item.get("author_interpretation")) if value
            ),
            "evidence": item.get("evidence", {}),
            "retrieval_queries_zh": item.get("retrieval_queries_zh", []),
            "retrieval_queries_en": item.get("retrieval_queries_en", []),
            "related_chunk_ids": item.get("related_chunk_ids", []),
            "usable_for": ["figure interpretation", "claim evidence"],
            "cannot_support": item.get("cannot_conclude", ""),
        }
    else:
        normalized = dict(item)
    source_kind = str(normalized.get("source_kind") or "paper")
    epistemic = str(normalized.get("epistemic_status") or "")
    explicit_source_context_only = (
        str(model.get("claim_policy") or "") == "context_only"
        or ("supports_claim" in model and model.get("supports_claim") is False)
    )
    context_only = (
        explicit_source_context_only
        or
        kind in {"project_relations", "external_context"}
        or source_kind in {"project", "external"}
        or epistemic in {"project_application", "codex_inference"}
    )
    authority = "context_only" if context_only else "evidence"
    row = {
        "chunk_id": str(normalized["chunk_id"]),
        "source_id": source_id,
        "kind": kind.rstrip("s") if kind != "core_claims" else "claim",
        "item_id": str(normalized.get("id") or ""),
        "text": str(normalized.get("text") or ""),
        "text_zh": str(normalized.get("text_zh") or ""),
        "context": paper_context(kind, model, normalized),
        "source_kind": source_kind,
        "epistemic_status": epistemic,
        "support_level": str(normalized.get("support_level") or "indirect"),
        "verification_status": str(normalized.get("verification_status") or "needs_human_check"),
        "evidence_json": json_text(normalized.get("evidence") or {}),
        "retrieval_queries_zh_json": json_text(normalized.get("retrieval_queries_zh") or []),
        "retrieval_queries_en_json": json_text(normalized.get("retrieval_queries_en") or []),
        "related_chunk_ids_json": json_text(normalized.get("related_chunk_ids") or []),
        "usable_for_json": json_text(normalized.get("usable_for") or []),
        "cannot_support": str(normalized.get("cannot_support") or ""),
        "authority": authority,
        "tombstoned": 0,
    }
    row["content_hash"] = sha256_text(json_text({key: row[key] for key in CHUNK_COLUMNS if key not in {"content_hash", "tombstoned"}}))
    return row


def resolve_model_pdf_path(
    model: dict[str, Any],
    model_path: Path,
    provenance_records: list[dict[str, Any]],
) -> Path:
    candidates: list[Path] = []
    configured = str(model.get("source_pdf_path") or "")
    if configured:
        candidates.append(Path(configured))
    candidates.extend(Path(str(record.get("pdf_path"))) for record in reversed(provenance_records) if record.get("pdf_path"))
    candidates.extend(sorted(model_path.parent.parent.glob("*.pdf")))
    existing = []
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.expanduser()
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        existing.append(candidate)
    if not existing:
        raise RuntimeError(f"source PDF missing for canonical model: {model_path}")
    recorded_hash = str(model.get("source_pdf_sha256") or "")
    if recorded_hash:
        for candidate in existing:
            if sha256_file(candidate) == recorded_hash:
                return candidate
    if len(existing) == 1:
        return existing[0]
    raise RuntimeError(f"cannot deterministically resolve source PDF for canonical model: {model_path}")


def discover_paper_sources(config: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    root = project_root()
    provenance_map = run_provenance_map(root)
    model_paths: list[Path] = []
    for pattern in config.get("paper_model_globs", []):
        model_paths.extend(Path(value) for value in glob.glob(str(pattern)))
    model_paths = sorted(set(path.resolve() for path in model_paths))
    sources: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    warnings: list[str] = []
    parent_keys: set[str] = set()
    chunk_ids: set[str] = set()
    for model_path in model_paths:
        model = read_json(model_path)
        parent = str(model.get("zotero_item_key") or "")
        attachment = str(model.get("zotero_attachment_key") or "")
        if not parent or not attachment:
            raise RuntimeError(f"model lacks Zotero parent/attachment keys: {model_path}")
        if parent in parent_keys:
            raise RuntimeError(f"duplicate canonical model for Zotero parent: {parent}")
        parent_keys.add(parent)
        source_id = f"zotero:{parent}"
        rag_path = model_path.with_name("02_rag_knowledge.md")
        if not rag_path.exists():
            raise RuntimeError(f"RAG artifact missing beside model: {rag_path}")
        rag_text = rag_path.read_text(encoding="utf-8", errors="replace")
        rag_ids = re.findall(r"<!-- chunk_id: ([^ ]+) -->", rag_text)
        if len(rag_ids) != len(set(rag_ids)):
            raise RuntimeError(f"duplicate RAG chunk IDs in {rag_path}")
        explicit: dict[str, tuple[str, dict[str, Any]]] = {}
        for chunk_id, location, item in walk_chunk_items(model):
            kind = str(location[0]) if location else "unknown"
            if chunk_id in explicit:
                raise RuntimeError(f"duplicate model chunk ID {chunk_id}: {model_path}")
            explicit[chunk_id] = (kind, item)
        derived_external = {
            str(source.get("chunk_id") or f"{model['paper_id']}:e:e{index:03d}"): external_context_chunk(model, source, index)
            for index, source in enumerate(model.get("external_context", []), start=1)
        }
        missing_from_rag = [chunk_id for chunk_id, (kind, _item) in explicit.items() if chunk_id not in rag_ids and kind != "controversies"]
        if missing_from_rag:
            raise RuntimeError(f"RAG omits model-backed chunks in {model_path}: {missing_from_rag[:5]}")
        controversy_only = [chunk_id for chunk_id, (kind, _item) in explicit.items() if chunk_id not in rag_ids and kind == "controversies"]
        if controversy_only:
            warnings.append(f"{parent}：{len(controversy_only)} 条 controversy 记录保留为 source metadata，以保持现有 RAG 契约")
        for chunk_id in rag_ids:
            if chunk_id in explicit:
                kind, item = explicit[chunk_id]
                if kind == "external_context":
                    item = derived_external[chunk_id]
                chunk = normalized_paper_chunk(source_id, model, kind, item)
            elif chunk_id in derived_external:
                chunk = normalized_paper_chunk(source_id, model, "external_context", derived_external[chunk_id])
            else:
                raise RuntimeError(f"RAG chunk has no model lineage: {chunk_id} in {rag_path}")
            if chunk_id in chunk_ids:
                raise RuntimeError(f"duplicate global paper chunk ID: {chunk_id}")
            chunk_ids.add(chunk_id)
            chunks.append(chunk)
        provenance_records = provenance_map.get(parent, [])
        pdf_path = resolve_model_pdf_path(model, model_path, provenance_records)
        recorded_pdf_hash = str(model.get("source_pdf_sha256") or "")
        if config.get("verify_pdf_hashes", True):
            actual_pdf_hash = sha256_file(pdf_path)
            if actual_pdf_hash != recorded_pdf_hash:
                raise RuntimeError(f"source PDF hash differs from model: {pdf_path}")
        provenance = {
            "canonical_artifact_dir": str(model_path.parent),
            "lineage": "canonical model + deterministic RAG + hash manifest",
            "run_records": provenance_records[-5:],
        }
        model_sha = sha256_file(model_path)
        provenance.update(edition_provenance(edition_projection(model, recorded_pdf_hash, model_sha)))
        source_context_only = (
            str(model.get("claim_policy") or "") == "context_only"
            or ("supports_claim" in model and model.get("supports_claim") is False)
        )
        provenance.update({
            "claim_policy": str(model.get("claim_policy") or ""),
            "supports_claim": bool(model.get("supports_claim", True)),
        })
        source = {
            "source_id": source_id,
            "source_type": "paper_evidence",
            "authority": "context_only" if source_context_only else "evidence",
            "title": str(model.get("title") or ""),
            "title_normalized": normalize_text(str(model.get("title") or "")),
            "authors": str(model.get("authors") or ""),
            "year": str(model.get("year") or ""),
            "doi": str(model.get("doi") or ""),
            "zotero_item_key": parent,
            "zotero_attachment_key": attachment,
            "collections_json": "[]",
            "tags_json": json_text(model.get("topic_tags") or []),
            "priority": str(model.get("priority") or ""),
            "status": str(model.get("status") or ""),
            "project_profile": str(model.get("project_profile") or ""),
            "source_path": str(model_path.parent),
            "source_pdf_path": str(pdf_path),
            "source_pdf_sha256": recorded_pdf_hash,
            "model_sha256": model_sha,
            "rag_sha256": sha256_file(rag_path),
            "provenance_json": json_text(provenance),
            "updated_at": str(model.get("generation", {}).get("generated_at") or file_mtime_iso(model_path)),
            "tombstoned": 0,
        }
        sources.append(source)
    baseline = config.get("baseline", {})
    baseline_mode = str(baseline.get("validation_mode") or "exact")
    if baseline_mode not in {"exact", "dynamic"}:
        raise RuntimeError(f"unsupported knowledge baseline validation mode: {baseline_mode}")
    expected_parents = int(baseline.get("processed_parent_count") or 0)
    expected_chunks = int(baseline.get("paper_chunk_count") or 0)
    if baseline_mode == "exact" and expected_parents and len(sources) != expected_parents:
        raise RuntimeError(f"processed parent baseline differs: expected {expected_parents}, got {len(sources)}")
    if baseline_mode == "exact" and expected_chunks and len(chunks) != expected_chunks:
        raise RuntimeError(f"paper chunk baseline differs: expected {expected_chunks}, got {len(chunks)}")
    return sources, chunks, warnings


def split_paragraphs(text: str, limit: int = 2600) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) + 2 > limit:
            chunks.append(current)
            current = ""
        if len(paragraph) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(paragraph[index : index + limit] for index in range(0, len(paragraph), limit))
        else:
            current = f"{current}\n\n{paragraph}".strip()
    if current:
        chunks.append(current)
    return chunks or [text.strip()]


def markdown_section_chunks(source: dict[str, Any], text: str) -> list[dict[str, Any]]:
    headings = list(re.finditer(r"(?m)^(#{2,3})\s+(.+?)\s*$", text))
    chunks: list[dict[str, Any]] = []
    for index, match in enumerate(headings):
        heading = match.group(2).strip()
        body_end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[match.end() : body_end].strip()
        for part_index, part in enumerate(split_paragraphs(body), start=1):
            identifier = hashlib.sha1(heading.encode("utf-8")).hexdigest()[:10]
            chunks.append(
                {
                    "chunk_id": f"handbook:{identifier}:{part_index:02d}",
                    "kind": "handbook_section",
                    "item_id": heading,
                    "text": part,
                    "text_zh": part,
                    "context": f"领域综合手册章节：{heading}",
                    "evidence": {"section": heading, "research_cutoff": source.get("research_cutoff", "")},
                    "queries": [heading],
                }
            )
    return chunks


def redbook_page_chunks(text: str) -> list[dict[str, Any]]:
    headings = list(re.finditer(r"(?m)^## PDF page (\d+) / report page ([^\s]+) - (.+?)\s*$", text))
    chunks: list[dict[str, Any]] = []
    for index, match in enumerate(headings):
        body_end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[match.end() : body_end].strip()
        page = int(match.group(1))
        section = match.group(3).strip()
        chunks.append(
            {
                "chunk_id": f"smile-redbook:page:{page:03d}",
                "kind": "mission_page",
                "item_id": f"PDF page {page}",
                "text": body,
                "text_zh": body,
                "context": f"SMILE RedBook PDF page {page}；{section}",
                "evidence": {"pdf_page_index": page, "printed_page_label": match.group(2), "section": section},
                "queries": ["SMILE", "SXI", section],
            }
        )
    return chunks


def structured_mission_document_chunks(spec: dict[str, Any], text: str) -> list[dict[str, Any]]:
    """Parse a curated, page-anchored engineering/mission document sidecar.

    The sidecar is deliberately separate from the source PDF: it records a
    conservative machine reading while the PDF hash and page anchors preserve
    the route back to the technical source.  These chunks remain context-only.
    """

    payload = json.loads(text)
    if payload.get("schema_version") != "1.0":
        raise RuntimeError(f"unsupported structured mission document schema: {spec.get('path')}")
    items = payload.get("chunks")
    if not isinstance(items, list) or not items:
        raise RuntimeError(f"structured mission document has no chunks: {spec.get('path')}")

    required = {"chunk_id", "kind", "item_id", "text", "text_zh", "context", "evidence", "queries", "usable_for"}
    prefix = str(spec.get("chunk_id_prefix") or "")
    seen: set[str] = set()
    chunks: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError(f"structured mission document chunk is not an object: {spec.get('path')}")
        missing = sorted(required - set(item))
        if missing:
            raise RuntimeError(f"structured mission document chunk missing {missing}: {spec.get('path')}")
        chunk_id = str(item["chunk_id"])
        if not chunk_id or chunk_id in seen:
            raise RuntimeError(f"duplicate or empty structured mission document chunk id: {chunk_id!r}")
        if prefix and not chunk_id.startswith(prefix):
            raise RuntimeError(f"structured mission document chunk id lacks prefix {prefix!r}: {chunk_id}")
        evidence = item["evidence"]
        if not isinstance(evidence, dict) or not evidence.get("pdf_page_indices"):
            raise RuntimeError(f"structured mission document chunk lacks PDF page anchors: {chunk_id}")
        queries = item["queries"]
        usable_for = item["usable_for"]
        if not isinstance(queries, list) or not all(isinstance(value, str) and value.strip() for value in queries):
            raise RuntimeError(f"structured mission document chunk has invalid queries: {chunk_id}")
        if not isinstance(usable_for, list) or not all(isinstance(value, str) and value.strip() for value in usable_for):
            raise RuntimeError(f"structured mission document chunk has invalid usable_for: {chunk_id}")
        seen.add(chunk_id)
        chunks.append(
            {
                "chunk_id": chunk_id,
                "kind": str(item["kind"]),
                "item_id": str(item["item_id"]),
                "text": str(item["text"]),
                "text_zh": str(item["text_zh"]),
                "context": str(item["context"]),
                "evidence": evidence,
                "queries": queries,
                "usable_for": usable_for,
                "epistemic_status": str(item.get("epistemic_status") or "reported_definition"),
                "support_level": str(item.get("support_level") or "direct"),
                "verification_status": str(item.get("verification_status") or "anchored"),
                "cannot_support": str(item.get("cannot_support") or spec.get("cannot_support") or ""),
            }
        )
    return chunks


def discover_additional_sources(config: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    sources: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    warnings: list[str] = []
    for spec in config.get("additional_sources", []):
        path = Path(str(spec["path"])).expanduser()
        if not path.exists():
            warnings.append(f"additional source missing: {path}")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        parser = str(spec.get("parser") or "markdown_sections")
        if parser == "redbook_pages":
            parsed = redbook_page_chunks(text)
        elif parser == "structured_mission_document":
            parsed = structured_mission_document_chunks(spec, text)
        else:
            parsed = markdown_section_chunks(spec, text)
        source_pdf_path = Path(str(spec.get("source_pdf_path") or "")).expanduser() if spec.get("source_pdf_path") else None
        expected_pdf_hash = str(spec.get("source_pdf_sha256") or "")
        if source_pdf_path is not None:
            if not source_pdf_path.is_file():
                raise RuntimeError(f"additional source PDF missing: {source_pdf_path}")
            actual_pdf_hash = sha256_file(source_pdf_path)
            if not expected_pdf_hash:
                raise RuntimeError(f"additional source PDF hash not configured: {source_pdf_path}")
            if actual_pdf_hash != expected_pdf_hash:
                raise RuntimeError(
                    f"additional source PDF hash mismatch: {source_pdf_path}; expected {expected_pdf_hash}, got {actual_pdf_hash}"
                )
        source_id = str(spec["source_id"])
        provenance = {
            "lineage": str(spec.get("lineage") or "configured local reference"),
            "parser": parser,
            "research_cutoff": spec.get("research_cutoff", ""),
        }
        provenance.update(spec.get("provenance") or {})
        source = {
            "source_id": source_id,
            "source_type": str(spec["source_type"]),
            "authority": str(spec["authority"]),
            "title": str(spec["title"]),
            "title_normalized": normalize_text(str(spec["title"])),
            "authors": str(spec.get("authors") or ""),
            "year": str(spec.get("year") or spec.get("research_cutoff") or ""),
            "doi": "",
            "zotero_item_key": "",
            "zotero_attachment_key": "",
            "collections_json": "[]",
            "tags_json": json_text(spec.get("tags") or detect_domains(text[:10000])),
            "priority": "",
            "status": str(spec.get("status") or "reference_context"),
            "project_profile": str(spec.get("project_profile") or "fte_cusp_sxi_domain"),
            "source_path": str(path),
            "source_pdf_path": str(source_pdf_path) if source_pdf_path is not None else "",
            "source_pdf_sha256": expected_pdf_hash,
            "model_sha256": sha256_file(path),
            "rag_sha256": "",
            "provenance_json": json_text(provenance),
            "updated_at": file_mtime_iso(path),
            "tombstoned": 0,
        }
        sources.append(source)
        for item in parsed:
            row = {
                "chunk_id": item["chunk_id"],
                "source_id": source_id,
                "kind": item["kind"],
                "item_id": item["item_id"],
                "text": item["text"],
                "text_zh": item["text_zh"],
                "context": item["context"],
                "source_kind": "external" if spec["source_type"] == "external_synthesis" else "mission_document",
                "epistemic_status": str(item.get("epistemic_status") or "external_finding"),
                "support_level": str(item.get("support_level") or "indirect"),
                "verification_status": str(item.get("verification_status") or "needs_human_check"),
                "evidence_json": json_text(item["evidence"]),
                "retrieval_queries_zh_json": json_text(item["queries"]),
                "retrieval_queries_en_json": json_text(item["queries"]),
                "related_chunk_ids_json": "[]",
                "usable_for_json": json_text(item.get("usable_for") or ["domain context", "method guidance"]),
                "cannot_support": str(item.get("cannot_support") or spec["cannot_support"]),
                "authority": str(spec["authority"]),
                "tombstoned": 0,
            }
            row["content_hash"] = sha256_text(json_text({key: row[key] for key in CHUNK_COLUMNS if key not in {"content_hash", "tombstoned"}}))
            chunks.append(row)
    return sources, chunks, warnings


def finalize_source_hashes(sources: list[dict[str, Any]], chunks: list[dict[str, Any]]) -> None:
    by_source: dict[str, list[str]] = {}
    for chunk in chunks:
        by_source.setdefault(str(chunk["source_id"]), []).append(str(chunk["content_hash"]))
    for source in sources:
        payload = {
            key: source.get(key, "")
            for key in SOURCE_COLUMNS
            if key not in {"content_hash", "updated_at", "tombstoned"}
        }
        payload["chunk_hashes"] = sorted(by_source.get(str(source["source_id"]), []))
        source["content_hash"] = sha256_text(json_text(payload))


def apply_human_review_policy(config: dict[str, Any], sources: list[dict[str, Any]]) -> int:
    """Apply an auditable user attestation without rewriting canonical artifacts.

    The policy closes only the human-check gate for matching source records. It
    deliberately does not modify chunk authority, claim support, verification
    status, or the promotions table.
    """

    policy = config.get("human_review_policy") or {}
    if not policy.get("enabled"):
        return 0
    if str(policy.get("mode") or "") != "user_attested_default":
        raise RuntimeError(f"unsupported human review policy mode: {policy.get('mode')}")
    attested_at = str(policy.get("attested_at") or "")
    attested_by = str(policy.get("attested_by") or "")
    review_scope = str(policy.get("review_scope") or "")
    if not attested_at or not attested_by or not review_scope:
        raise RuntimeError("human review policy requires attested_at, attested_by, and review_scope")
    source_types = {str(value) for value in policy.get("applies_to_source_types", [])}
    source_statuses = {str(value) for value in policy.get("applies_to_statuses", [])}
    generated_at_lte = str(policy.get("canonical_generated_at_lte") or "")
    try:
        cutoff = dt.datetime.fromisoformat(generated_at_lte.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("human review policy requires a valid timezone-aware cutoff") from exc
    if cutoff.tzinfo is None:
        raise RuntimeError("human review policy requires a timezone-aware cutoff")
    target_status = str(policy.get("target_status") or "human_checked")
    applied = 0
    for source in sources:
        # The historical batch attestation is scoped to its original pipeline.
        # A local two-round intake never inherits human acceptance merely by
        # using an older PDF or an old timestamp supplied in its metadata.
        if decode_json(str(source.get("provenance_json") or "{}"), {}).get("ingestion_mode") == "fast_pdf_two_round":
            continue
        if source_types and str(source.get("source_type") or "") not in source_types:
            continue
        original_status = str(source.get("status") or "")
        if source_statuses and original_status not in source_statuses:
            continue
        canonical_generated_at = str(source.get("updated_at") or "")
        try:
            generated = dt.datetime.fromisoformat(canonical_generated_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if generated.tzinfo is None or generated > cutoff:
            continue
        try:
            provenance = json.loads(str(source.get("provenance_json") or "{}"))
        except json.JSONDecodeError:
            provenance = {}
        if not isinstance(provenance, dict):
            provenance = {}
        provenance["human_review"] = {
            "status": target_status,
            "original_canonical_status": original_status,
            "mode": "user_attested_default",
            "attested_by": attested_by,
            "attested_at": attested_at,
            "review_scope": review_scope,
            "canonical_generated_at": canonical_generated_at,
            "canonical_generated_at_lte": generated_at_lte,
            "per_paper_checklist_reconstructed": bool(policy.get("per_paper_checklist_reconstructed", False)),
            "scientific_promotion_authorized": bool(policy.get("scientific_promotion_authorized", False)),
        }
        source["status"] = target_status
        source["provenance_json"] = json_text(provenance)
        source["updated_at"] = attested_at
        applied += 1
    return applied


def build_current_state(config: dict[str, Any]) -> dict[str, Any]:
    paper_sources, paper_chunks, warnings = discover_paper_sources(config)
    paper_sources, paper_chunks, warnings = list(paper_sources), list(paper_chunks), list(warnings)
    fast_sources, fast_chunks = [], []
    if config.get("fast_pdf_model_globs"):
        # Keep the regular library independent of this optional intake module.
        # A configured but invalid final artifact must fail discovery rather
        # than quietly disappear during a future complete index rebuild.
        from fast_pdf_intake import discover_fast_pdf_sources
        fast_sources, fast_chunks, fast_warnings = discover_fast_pdf_sources(config)
        existing_pdf_hashes = {source["source_pdf_sha256"] for source in paper_sources}
        existing_dois = {
            re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "",
                   urllib.parse.unquote(str(source.get("doi") or "")).strip().casefold()).rstrip(" .")
            for source in paper_sources if source.get("doi")
        }
        for source in fast_sources:
            if source["source_pdf_sha256"] in existing_pdf_hashes:
                raise RuntimeError("fast PDF duplicates an existing paper source SHA: " + source["source_pdf_sha256"])
            existing_pdf_hashes.add(source["source_pdf_sha256"])
            doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "",
                         urllib.parse.unquote(str(source.get("doi") or "")).strip().casefold()).rstrip(" .")
            if doi and doi in existing_dois:
                raise RuntimeError("fast PDF has an unresolved DOI/version collision: " + doi)
            if doi:
                existing_dois.add(doi)
        paper_sources.extend(fast_sources)
        paper_chunks.extend(fast_chunks)
        warnings.extend(fast_warnings)
    additional_sources, additional_chunks, additional_warnings = discover_additional_sources(config)
    warnings.extend(additional_warnings)
    sources = paper_sources + additional_sources
    chunks = paper_chunks + additional_chunks
    human_review_applied_count = apply_human_review_policy(config, sources)
    if len({source["source_id"] for source in sources}) != len(sources):
        raise RuntimeError("duplicate source IDs in current state")
    if len({chunk["chunk_id"] for chunk in chunks}) != len(chunks):
        raise RuntimeError("duplicate chunk IDs in current state")
    finalize_source_hashes(sources, chunks)
    manifest_hash = sha256_text(
        json_text(
            {
                "schema": KNOWLEDGE_SCHEMA_VERSION,
                "sources": sorted((source["source_id"], source["content_hash"]) for source in sources),
                "chunks": sorted((chunk["chunk_id"], chunk["content_hash"]) for chunk in chunks),
            }
        )
    )
    return {
        "sources": sources,
        "chunks": chunks,
        "warnings": warnings,
        "manifest_hash": manifest_hash,
        "paper_source_count": len(paper_sources),
        "paper_chunk_count": len(paper_chunks),
        "fast_pdf_source_count": len(fast_sources),
        "fast_pdf_chunk_count": len(fast_chunks),
        "additional_source_count": len(additional_sources),
        "human_review_applied_count": human_review_applied_count,
    }


def old_source_state(path: Path) -> tuple[dict[str, str], str]:
    if not path.exists():
        return {}, ""
    with connect_database(path, read_only=True) as connection:
        sources = {
            str(row["source_id"]): str(row["content_hash"])
            for row in connection.execute("SELECT source_id,content_hash FROM sources WHERE tombstoned=0")
        }
        last = connection.execute(
            "SELECT manifest_hash FROM sync_runs WHERE status='pass' ORDER BY completed_at DESC LIMIT 1"
        ).fetchone()
        return sources, str(last[0]) if last else ""


def insert_source_and_chunks(
    connection: sqlite3.Connection,
    source: dict[str, Any],
    chunks: list[dict[str, Any]],
) -> None:
    insert_row(connection, "sources", SOURCE_COLUMNS, source)
    for chunk in chunks:
        insert_row(connection, "chunks", CHUNK_COLUMNS, chunk)
        if chunk.get("tombstoned"):
            continue
        title = str(source.get("title") or "")
        aliases = " ".join(
            [
                str(source.get("doi") or ""),
                str(source.get("zotero_item_key") or ""),
                str(chunk.get("item_id") or ""),
                " ".join(json.loads(chunk.get("retrieval_queries_zh_json") or "[]")),
                " ".join(json.loads(chunk.get("retrieval_queries_en_json") or "[]")),
                " ".join(json.loads(source.get("tags_json") or "[]")),
            ]
        )
        body = "\n".join(
            str(chunk.get(field) or "")
            for field in ("text_zh", "text", "context", "cannot_support")
        )
        connection.execute(
            "INSERT INTO chunks_fts(chunk_id,title,body,aliases,ngrams) VALUES (?,?,?,?,?)",
            (chunk["chunk_id"], title, body, aliases, " ".join(cjk_bigrams(title + " " + body + " " + aliases))),
        )


def copy_retained_sources(old_path: Path, connection: sqlite3.Connection, active_ids: set[str]) -> set[str]:
    """Keep explicitly indexed revisions and quarantined missing-source history.

    Canonical discovery does not enumerate additive project/revision records.
    Absence from that glob is not a withdrawal of an indexed version.
    """
    retained = set()
    if not old_path.exists():
        return retained
    with connect_database(old_path, read_only=True) as old:
        tables = {row[0] for row in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'sources', 'chunks'} <= tables:
            return retained
        quality = dict(old.execute('SELECT source_id,record_json FROM source_quality')) if 'source_quality' in tables else {}
        for source_row in old.execute("SELECT * FROM sources WHERE tombstoned=0 AND source_type='paper_evidence'"):
            source = dict(source_row)
            sid = source['source_id']
            if sid in active_ids:
                continue
            provenance = decode_json(source['provenance_json'], {})
            known_version = (isinstance(provenance, dict) and ':version:' in sid
                             and provenance.get('independent_scientific_source') is False
                             and isinstance(provenance.get('original_to_indexed_chunk_ids'), dict)
                             and bool(provenance['original_to_indexed_chunk_ids']))
            record = decode_json(quality.get(sid, '{}'), {})
            missing_history = (record.get('state') == 'quarantined'
                               and availability(source)['state'] == 'unavailable')
            if known_version or missing_history:
                chunks = [dict(row) for row in old.execute('SELECT * FROM chunks WHERE source_id=? AND tombstoned=0', (sid,))]
                insert_source_and_chunks(connection, source, chunks)
                retained.add(sid)
    return retained


def copy_tombstones(old_path: Path, connection: sqlite3.Connection, active_ids: set[str]) -> int:
    if not old_path.exists():
        return 0
    count = 0
    with connect_database(old_path, read_only=True) as old:
        tables = {row[0] for row in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"sources", "chunks"}.issubset(tables):
            return 0
        for source_row in old.execute("SELECT * FROM sources WHERE tombstoned=0"):
            source = dict(source_row)
            source_id = str(source["source_id"])
            if source_id in active_ids or source.get("source_type") == "external_promoted":
                continue
            source["tombstoned"] = 1
            insert_row(connection, "sources", SOURCE_COLUMNS, source)
            for chunk_row in old.execute("SELECT * FROM chunks WHERE source_id=?", (source_id,)):
                chunk = dict(chunk_row)
                chunk["tombstoned"] = 1
                insert_row(connection, "chunks", CHUNK_COLUMNS, chunk)
            count += 1
    return count


def materialize_approved_external(connection: sqlite3.Connection) -> int:
    count = 0
    for row in connection.execute("SELECT * FROM external_staging WHERE state='approved'").fetchall():
        source_id = f"external-promoted:{row['record_id']}"
        chunk_id = f"external:{row['record_id']}"
        if connection.execute("SELECT 1 FROM sources WHERE source_id=?", (source_id,)).fetchone():
            continue
        source = {
            "source_id": source_id,
            "source_type": "external_promoted",
            "authority": "evidence",
            "title": str(row["title"] or row["url"] or row["doi"] or row["query"]),
            "title_normalized": normalize_text(str(row["title"] or row["query"])),
            "authors": "",
            "year": str(row["accessed_at"])[:4],
            "doi": str(row["doi"]),
            "zotero_item_key": "",
            "zotero_attachment_key": "",
            "collections_json": "[]",
            "tags_json": json_text(detect_domains(str(row["finding"]))),
            "priority": "",
            "status": "human_promoted",
            "project_profile": str(row["domain"]),
            "source_path": str(row["url"]),
            "source_pdf_path": "",
            "source_pdf_sha256": "",
            "model_sha256": "",
            "rag_sha256": "",
            "provenance_json": json_text({"staging_record_id": row["record_id"], "promotion": "explicitly confirmed"}),
            "updated_at": str(row["updated_at"]),
            "tombstoned": 0,
        }
        chunk = {
            "chunk_id": chunk_id,
            "source_id": source_id,
            "kind": "external_promoted",
            "item_id": str(row["record_id"]),
            "text": str(row["finding"]),
            "text_zh": str(row["finding"]),
            "context": f"经人工确认后从联网暂存层提升；原始查询：{row['query']}",
            "source_kind": "external",
            "epistemic_status": "external_finding",
            "support_level": "indirect",
            "verification_status": "externally_verified",
            "evidence_json": json_text({"url": row["url"], "doi": row["doi"], "accessed_at": row["accessed_at"]}),
            "retrieval_queries_zh_json": json_text([row["query"], row["title"]]),
            "retrieval_queries_en_json": json_text([row["query"], row["title"]]),
            "related_chunk_ids_json": str(row["evidence_links_json"]),
            "usable_for_json": json_text([row["role"] or "external evidence"]),
            "cannot_support": "该联网来源只在其原始范围内有效，不能覆盖当前项目 truth sources。",
            "authority": "evidence",
            "tombstoned": 0,
        }
        chunk["content_hash"] = sha256_text(json_text({key: chunk[key] for key in CHUNK_COLUMNS if key not in {"content_hash", "tombstoned"}}))
        finalize_source_hashes([source], [chunk])
        insert_source_and_chunks(connection, source, [chunk])
        count += 1
    return count


def validate_database(connection: sqlite3.Connection, config: dict[str, Any]) -> dict[str, Any]:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    source_count = connection.execute("SELECT count(*) FROM sources WHERE tombstoned=0").fetchone()[0]
    chunk_count = connection.execute("SELECT count(*) FROM chunks WHERE tombstoned=0").fetchone()[0]
    paper_sources = connection.execute("SELECT count(*) FROM sources WHERE source_type='paper_evidence' AND tombstoned=0").fetchone()[0]
    paper_chunks = connection.execute(
        "SELECT count(*) FROM chunks c JOIN sources s ON s.source_id=c.source_id WHERE s.source_type='paper_evidence' AND c.tombstoned=0 AND s.tombstoned=0"
    ).fetchone()[0]
    fast_source_ids = {
        row[0] for row in connection.execute(
            "SELECT source_id,provenance_json FROM sources WHERE source_type='paper_evidence' AND tombstoned=0")
        if decode_json(row[1], {}).get("ingestion_mode") == "fast_pdf_two_round"
    }
    fast_chunks = sum(row[0] in fast_source_ids for row in connection.execute(
        "SELECT source_id FROM chunks WHERE tombstoned=0"))
    duplicate_chunks = connection.execute(
        "SELECT count(*) FROM (SELECT chunk_id,count(*) n FROM chunks GROUP BY chunk_id HAVING n>1)"
    ).fetchone()[0]
    fts_count = connection.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
    fts_missing = connection.execute(
        "SELECT count(*) FROM (SELECT chunk_id FROM chunks WHERE tombstoned=0 "
        "EXCEPT SELECT chunk_id FROM chunks_fts)"
    ).fetchone()[0]
    fts_extra = connection.execute(
        "SELECT count(*) FROM chunks_fts f LEFT JOIN chunks c ON c.chunk_id=f.chunk_id "
        "WHERE c.chunk_id IS NULL OR c.tombstoned!=0"
    ).fetchone()[0]
    fts_duplicates = connection.execute(
        "SELECT count(*) FROM (SELECT chunk_id FROM chunks_fts GROUP BY chunk_id HAVING count(*)>1)"
    ).fetchone()[0]
    orphan_chunks = connection.execute(
        "SELECT count(*) FROM chunks c LEFT JOIN sources s ON s.source_id=c.source_id "
        "WHERE c.tombstoned=0 AND (s.source_id IS NULL OR s.tombstoned!=0)"
    ).fetchone()[0]
    baseline = config.get("baseline", {})
    baseline_mode = str(baseline.get("validation_mode") or "exact")
    if baseline_mode not in {"exact", "dynamic"}:
        raise RuntimeError(f"unsupported knowledge baseline validation mode: {baseline_mode}")
    errors = []
    if integrity != "ok":
        errors.append(f"sqlite integrity_check: {integrity}")
    canonical_papers, canonical_chunks = paper_sources - len(fast_source_ids), paper_chunks - fast_chunks
    if baseline_mode == "exact" and canonical_papers != int(baseline.get("processed_parent_count") or canonical_papers):
        errors.append(f"paper source baseline mismatch: {paper_sources}")
    if baseline_mode == "exact" and canonical_chunks != int(baseline.get("paper_chunk_count") or canonical_chunks):
        errors.append(f"paper chunk baseline mismatch: {paper_chunks}")
    if duplicate_chunks:
        errors.append(f"duplicate chunks: {duplicate_chunks}")
    if fts_count != chunk_count:
        errors.append(f"FTS row mismatch: {fts_count} != {chunk_count}")
    if fts_missing or fts_extra or fts_duplicates:
        errors.append(f"FTS identity mismatch: missing={fts_missing}, extra={fts_extra}, duplicates={fts_duplicates}")
    if orphan_chunks:
        errors.append(f"active chunks with inactive or missing source: {orphan_chunks}")
    result = {
        "integrity": integrity,
        "source_count": source_count,
        "chunk_count": chunk_count,
        "paper_source_count": paper_sources,
        "paper_chunk_count": paper_chunks,
        "fast_pdf_source_count": len(fast_source_ids),
        "fast_pdf_chunk_count": fast_chunks,
        "duplicate_chunk_count": duplicate_chunks,
        "fts_count": fts_count,
        "fts_missing_count": fts_missing,
        "fts_extra_count": fts_extra,
        "fts_duplicate_count": fts_duplicates,
        "orphan_chunk_count": orphan_chunks,
        "errors": errors,
    }
    if errors:
        raise RuntimeError("; ".join(errors))
    return result


def write_sync_artifacts(config: dict[str, Any], state: dict[str, Any], report: dict[str, Any]) -> None:
    review_dir = Path(str(config["review_dir"]))
    review_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = review_dir / "source_manifest.jsonl"
    with manifest_path.open("w", encoding="utf-8") as handle:
        for source in sorted(state["sources"], key=lambda row: str(row["source_id"])):
            output = dict(source)
            output["collections"] = json.loads(output.pop("collections_json"))
            output["tags"] = json.loads(output.pop("tags_json"))
            output["provenance"] = json.loads(output.pop("provenance_json"))
            handle.write(json.dumps(output, ensure_ascii=False, sort_keys=True) + "\n")
    (review_dir / "sync_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    policy = config.get("human_review_policy") or {}
    if policy.get("enabled"):
        attestation = {
            "schema_version": "1.0",
            "status": "active",
            "policy": policy,
            "applied_source_count": state.get("human_review_applied_count", 0),
            "paper_source_count": state.get("paper_source_count", 0),
            "manifest_hash": state.get("manifest_hash", ""),
            "claim_authority_changed": False,
            "scientific_promotion_count_changed": False,
        }
        (review_dir / "human_review_attestation.json").write_text(
            json.dumps(attestation, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def sync_database(
    config: dict[str, Any],
    *,
    db_override: str | Path | None = None,
    dry_run: bool = False,
    if_stale: bool = False,
) -> dict[str, Any]:
    if dry_run:
        return _sync_database(config, db_override=db_override, dry_run=True, if_stale=if_stale)
    db_path = database_path(config, db_override)
    # A database-specific lock coordinates derived-index rebuilds and additive
    # quality writes even when a writeback runner already owns its outer lock.
    with workflow_lock(db_path.with_suffix(db_path.suffix + '.write.lock')):
        return _sync_database(config, db_override=db_override, dry_run=False, if_stale=if_stale)


def _sync_database(config, *, db_override=None, dry_run=False, if_stale=False):
    started = now_iso()
    db_path = database_path(config, db_override)
    # Publication authority belongs to the actual target, including isolated
    # rebuilds selected through --database; never inherit another DB's marker.
    state = build_current_state({**config, 'database_path': str(db_path)})
    old_hashes, old_manifest = old_source_state(db_path)
    new_hashes = {str(source["source_id"]): str(source["content_hash"]) for source in state["sources"]}
    added = sorted(set(new_hashes) - set(old_hashes))
    updated = sorted(source_id for source_id in set(new_hashes) & set(old_hashes) if new_hashes[source_id] != old_hashes[source_id])
    unchanged = sorted(source_id for source_id in set(new_hashes) & set(old_hashes) if new_hashes[source_id] == old_hashes[source_id])
    removed = sorted(set(old_hashes) - set(new_hashes))
    if if_stale and db_path.exists() and old_manifest == state["manifest_hash"]:
        with connect_database(db_path, read_only=True) as connection:
            validation = validate_database(connection, config)
        report = {
            "status": "dry_run" if dry_run else "pass",
            "mode": "if_stale",
            "no_op": True,
            "database_path": str(db_path),
            "manifest_hash": state["manifest_hash"],
            "source_count": len(state["sources"]),
            "chunk_count": len(state["chunks"]),
            "paper_source_count": state["paper_source_count"],
            "paper_chunk_count": state["paper_chunk_count"],
            "warnings": state["warnings"],
            "validation": validation,
            "completed_at": now_iso(),
        }
        if not dry_run:
            write_sync_artifacts(config, state, report)
        return report
    preview = {
        "status": "dry_run" if dry_run else "building",
        "no_op": False,
        "database_path": str(db_path),
        "manifest_hash": state["manifest_hash"],
        "added": added,
        "updated": updated,
        "unchanged_count": len(unchanged),
        "tombstoned": removed,
        "source_count": len(state["sources"]),
        "chunk_count": len(state["chunks"]),
        "paper_source_count": state["paper_source_count"],
        "paper_chunk_count": state["paper_chunk_count"],
        "warnings": state["warnings"],
    }
    if dry_run:
        preview["completed_at"] = now_iso()
        return preview
    db_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = db_path.with_name(f"{db_path.name}.tmp.{os.getpid()}")
    if temp_path.exists():
        temp_path.unlink()
    run_id = dt.datetime.now(dt.timezone.utc).strftime("sync-%Y%m%dT%H%M%SZ")
    try:
        with connect_database(temp_path) as connection:
            initialize_database(connection)
            copy_mutable_state(db_path, connection)
            chunks_by_source: dict[str, list[dict[str, Any]]] = {}
            for chunk in state["chunks"]:
                chunks_by_source.setdefault(str(chunk["source_id"]), []).append(chunk)
            for source in state["sources"]:
                insert_source_and_chunks(connection, source, chunks_by_source.get(str(source["source_id"]), []))
            retained_ids = copy_retained_sources(db_path, connection, set(new_hashes))
            tombstoned_count = copy_tombstones(db_path, connection, set(new_hashes) | retained_ids)
            for source_row in connection.execute('SELECT * FROM sources WHERE tombstoned=0').fetchall():
                source = dict(source_row)
                current_chunks = [dict(row) for row in connection.execute('SELECT * FROM chunks WHERE source_id=? AND tombstoned=0',
                                                                          (source['source_id'],))]
                record_intake_quality(connection, source, current_chunks)
            materialize_approved_external(connection)
            reindex_work_memory(connection)
            validation = validate_database(connection, config)
            connection.execute(
                """INSERT INTO sync_runs(
                    run_id,started_at,completed_at,manifest_hash,status,added_count,updated_count,unchanged_count,
                    tombstoned_count,source_count,chunk_count,paper_source_count,paper_chunk_count,warnings_json,validation_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, started, now_iso(), state["manifest_hash"], "pass", len(added), len(updated), len(unchanged),
                    tombstoned_count, validation["source_count"], validation["chunk_count"],
                    validation["paper_source_count"], validation["paper_chunk_count"],
                    json_text(state["warnings"]), json_text(validation),
                ),
            )
            connection.commit()
        if db_path.exists():
            backup_path = db_path.with_name(db_path.name + '.backup.' + uuid.uuid4().hex)
            consistent_backup(db_path, backup_path)
            os.replace(backup_path, db_path.with_suffix(db_path.suffix + '.previous'))
        os.replace(temp_path, db_path)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise
    report = {
        **preview,
        "status": "pass",
        "run_id": run_id,
        "added_count": len(added),
        "updated_count": len(updated),
        "unchanged_count": len(unchanged),
        "tombstoned_count": tombstoned_count,
        "tombstoned": sorted(set(removed) - retained_ids),
        "retained_source_count": len(retained_ids),
        "source_count": validation['source_count'],
        "chunk_count": validation['chunk_count'],
        "validation": validation,
        "completed_at": now_iso(),
    }
    write_sync_artifacts(config, state, report)
    return report


def decode_json(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return fallback


def has_usable_evidence_locator(evidence: Any) -> bool:
    """Check locator structure only; original-text support needs a separate audit."""
    if not isinstance(evidence, dict):
        return False
    page = evidence.get("pdf_page_index")
    if type(page) is int and page >= 1:
        return True
    pages = evidence.get("pdf_page_indices")
    if isinstance(pages, list) and pages and all(type(value) is int and value >= 1 for value in pages):
        return True
    for key in ("printed_page_label", "section", "figure", "table"):
        value = evidence.get(key)
        if isinstance(value, str) and value.strip():
            return True
    for key in ("printed_page_labels", "sections"):
        values = evidence.get(key)
        if isinstance(values, list) and values and all(isinstance(value, str) and value.strip() for value in values):
            return True
    return False


def chunk_supports_claim(row: sqlite3.Row | dict[str, Any]) -> bool:
    return (
        str(row["authority"]) == "evidence"
        and dict(row).get('source_authority') == 'evidence'
        and str(row["verification_status"]) in {"anchored", "externally_verified"}
        and str(row["epistemic_status"]) not in {"project_application", "codex_inference"}
        and str(row["source_kind"]) not in {"project", "external", "zotero_metadata", "mission_document", "translation_style", "translation_reference"}
        and has_usable_evidence_locator(decode_json(str(row["evidence_json"]), {}))
        and quality_decision(row)["eligible"]
    )


def mission_document_query_boost(row: sqlite3.Row | dict[str, Any], query: str) -> float:
    """Make a mission document discoverable only for document/format-specific queries.

    Paper evidence should continue to dominate ordinary scientific searches.  A
    draft engineering document gets a boost only when the user names its title
    or document identifier, or when several of its curated aliases overlap an
    explicitly format-oriented query.
    """

    if str(row["source_type"]) != "mission_document":
        return 0.0
    query_norm = normalize_text(query)
    title_norm = normalize_text(str(row["title"]))
    if title_norm and title_norm in query_norm:
        return 180.0

    target_identity = " ".join(
        [
            str(row["source_id"]),
            str(row["chunk_id"]),
        ]
    ).lower()
    identifier_terms = [
        term for term in lexical_terms(query)
        if len(term) >= 8 and (term.count("-") >= 2 or term.startswith("ul-"))
    ]
    if any(term in target_identity for term in identifier_terms):
        return 180.0

    lowered = unicodedata.normalize("NFKC", query or "").lower()
    engineering_markers = {
        "fits", "hdu", "keyword", "header", "column", "field", "file", "format",
        "gti", "badpix", "wmap", "rmf", "arf", "timezero", "tstart", "tstop",
        "fracexp", "ccd", "coordinate", "event list", "light curve", "spectrum",
        "格式", "关键字", "文件", "字段", "数据列", "坐标", "时标", "光变曲线", "光谱",
    }
    if not any(marker in lowered for marker in engineering_markers):
        return 0.0
    aliases = decode_json(str(row["retrieval_queries_zh_json"]), []) + decode_json(
        str(row["retrieval_queries_en_json"]), []
    )
    query_terms = set(lexical_terms(query))
    alias_terms = set(lexical_terms(" ".join(str(value) for value in aliases)))
    overlap = query_terms & alias_terms
    if len(overlap) < 3:
        return 0.0
    return min(165.0, 90.0 + 12.0 * len(overlap))


def score_chunk(row: sqlite3.Row, rank_index: int, query: str, domain: str) -> float:
    score = max(1.0, 100.0 - rank_index * 0.75)
    query_norm = normalize_text(query)
    if query_norm and query_norm in {
        normalize_text(str(row["doi"])),
        normalize_text(str(row["chunk_id"])),
        normalize_text(str(row["title"])),
    }:
        score += 140.0
    elif query_norm and query_norm in normalize_text(str(row["title"])):
        score += 70.0
    chunk_text_norm = normalize_text(str(row["text_zh"] or row["text"]))
    curated_queries = decode_json(str(row["retrieval_queries_zh_json"]), [])
    if query_norm and (
        query_norm == chunk_text_norm
        or query_norm in {normalize_text(str(value)) for value in curated_queries if value}
    ):
        # Exact curated semantic queries should recover their own source even
        # when an explicit context-only policy correctly applies a rank penalty.
        score += 140.0
    source_type = str(row["source_type"])
    score += {
        "paper_evidence": 55.0,
        "external_promoted": 28.0,
        "mission_document": 16.0,
        "external_synthesis": 12.0,
        "raw_source": 2.0,
    }.get(source_type, 0.0)
    verification = str(row["verification_status"])
    score += {"anchored": 28.0, "externally_verified": 16.0, "needs_human_check": 0.0}.get(verification, 0.0)
    score += {"direct": 10.0, "indirect": 4.0, "speculative": -8.0}.get(str(row["support_level"]), 0.0)
    if str(row["authority"]) == "context_only":
        score -= 45.0
    if str(row["epistemic_status"]) in {"project_application", "codex_inference"}:
        score -= 25.0
    if domain and domain in str(row["project_profile"]):
        score += 8.0
    score += mission_document_query_boost(row, query)
    return round(score, 3)


def evidence_result(row: sqlite3.Row | dict, score: float, availability_cache: dict | None = None) -> dict[str, Any]:
    evidence = decode_json(str(row["evidence_json"]), {})
    source_provenance = decode_json(str(row["provenance_json"]), {})
    human_review_attestation = (
        source_provenance.get("human_review", {})
        if isinstance(source_provenance, dict)
        else {}
    )
    quality = quality_decision(row, availability_cache)
    edition_resolution = resolve_edition_projection(
        row, quality['source_availability'] == 'valid', availability_cache)
    if edition_resolution['state'] == 'projection_mismatch':
        # A valid semantic review does not authorize silently losing the actual
        # evidence edition while retaining its parent's DOI. Keep the result
        # searchable, but close direct support without rewriting review history.
        quality = quality | {'eligible': False, 'reason': 'source_edition_projection_mismatch',
                             'source_edition_status': 'projection_mismatch'}
    source_quality = decode_json(dict(row).get('source_quality_json', '{}'), {})
    if not isinstance(source_quality, dict):
        source_quality = {}
    relation = source_quality.get('revision_relation', {})
    if not isinstance(relation, dict):
        relation = {}
    relation_inputs = relation.get('inputs', {})
    if not isinstance(relation_inputs, dict):
        relation_inputs = {}
    preferred = (relation.get('preferred_source_id') == row['source_id']
                 and relation.get('work_id') == work_identity(row)
                 and relation_inputs.get(row['source_id']) == {
                     k: row[k] for k in ('source_pdf_sha256', 'model_sha256', 'rag_sha256')})
    supersedes = []
    if preferred:
        try:
            ancestors = set(revision_lineage_ids(relation)[:-1])
        except (KeyError, ValueError, TypeError):
            ancestors = set()
        # A paper/project view may reuse C001 for a different assertion. Only
        # the explicitly reviewed predecessor chunk is superseded, never all
        # chunks sharing an item ID or DOI.
        for pair in relation.get('superseded_assertions', []):
            if (isinstance(pair, dict) and pair.get('new_chunk_id') == row['chunk_id']
                    and pair.get('kind') == row['kind'] and pair.get('item_id') == row['item_id']
                    and pair.get('original_source_id') in ancestors
                    and pair.get('new_inputs') == input_binding(row)):
                supersedes.append(pair.get('original_chunk_id', ''))
        figures = relation.get('figure_revision', {})
        if isinstance(figures, dict):
            for name, value in figures.items():
                if (name.startswith('new_') and isinstance(value, dict)
                        and value.get('chunk_id') == row['chunk_id']):
                    predecessor = figures.get('old_' + name[4:], {})
                    if isinstance(predecessor, dict):
                        supersedes.append(predecessor.get('chunk_id', ''))
    supersedes = [value for value in supersedes if value and value != row['chunk_id']]
    supports_claim = (
        str(row['authority']) == 'evidence'
        and dict(row).get('source_authority') == 'evidence'
        and str(row['verification_status']) in {'anchored', 'externally_verified'}
        and str(row['epistemic_status']) not in {'project_application', 'codex_inference'}
        and str(row['source_kind']) not in {'project', 'external', 'zotero_metadata', 'mission_document', 'translation_style', 'translation_reference'}
        and has_usable_evidence_locator(evidence) and quality['eligible']
    )
    item_human_review = dict(row).get('item_human_review', {'state': 'not_reviewed', 'current': None, 'scope': 'this_assertion_only'})
    replacement = dict(row).get('reading_replacement')
    invalid_human_review = bool(item_human_review.get('invalid_record_count'))
    machine_citable = bool(supports_claim and not replacement and not invalid_human_review)
    disputed = item_human_review['state'] in {'needs_revision', 'unresolved'}
    human_comment = (item_human_review.get('current') or {}).get('rationale_zh', '')
    citation_qualification = ('人工审阅：不接受／有争议。' + ('用户意见：' + human_comment if human_comment else '用户未附意见。')
                              + '引用须同时呈现此异议；仅用于说明论文表述及争议，不能称为无争议或已获人工认可。') if disputed else ''
    if invalid_human_review:
        citation_qualification = '人工审阅记录与原始事件绑定失效，须核对后才能引用。'
    if replacement or invalid_human_review or item_human_review['state'] in {'needs_revision', 'unresolved'}:
        supports_claim = False
    page = (
        evidence.get("pdf_page_index")
        or evidence.get("pdf_page_indices")
        or evidence.get("printed_page_label")
        or evidence.get("printed_page_labels")
        or ""
    )
    section = evidence.get("section") or evidence.get("sections") or ""
    original_evidence = evidence
    review_record = decode_json(dict(row).get('content_review_json', '{}'), {})
    review = review_record.get('review', {}) if isinstance(review_record, dict) else {}
    if quality['eligible'] and review.get('source_pages'):
        # An independently checked expanded passage can repair the returned
        # locator without silently overwriting the historical canonical anchor.
        page = review['source_pages']
        evidence = {'pdf_page_indices': page, 'section': section,
                    'anchor_kind': 'independent_original_review',
                    'reviewer_id': review['reviewer_id']}
    citation = {
        "qualification": citation_qualification,
        "qualification_required": disputed or invalid_human_review,
        "zotero_item_key": row["zotero_item_key"],
        "zotero_attachment_key": row["zotero_attachment_key"],
        "doi": row["doi"],
        "pdf_page": page,
        "section": section,
        "source_path": row["source_path"],
        "source_pdf_path": row['source_pdf_path'],
        "source_pdf_sha256": row['source_pdf_sha256'],
    }
    intake = {
        key: source_provenance.get(key, default)
        for key, default in (("ingestion_mode", "canonical"), ("coverage_status", "not_recorded"),
                             ("coverage_gaps", []), ("review_provenance", {}))
    }
    if intake["ingestion_mode"] == "fast_pdf_two_round":
        citation.update({"local_pdf_identity": "pdf:" + row['source_pdf_sha256'],
                         "work_id": work_identity(row), **intake})
    source_edition = edition_resolution['projection']
    if source_edition is not None:
        citation.update(edition_provenance(source_edition))
        citation.update({
            'pdf_page_basis': 'attached_evidence_pdf',
            'doi_role': ('local_pdf_bibliographic_metadata' if intake['ingestion_mode'] == 'fast_pdf_two_round'
                         else 'zotero_parent_bibliographic_metadata'),
            'bibliographic_parent': {key: row[key] for key in ('title', 'authors', 'year', 'doi')},
            'source_edition_origin': (
                'hash_bound_model_reconstruction_missing_provenance'
                if edition_resolution['state'] == 'reconstructed_from_hash_bound_model'
                else 'stored_input_bound_projection'),
        })
    elif edition_resolution['state'] == 'projection_mismatch':
        citation['source_edition_status'] = 'projection_mismatch'
    return {
        "result_type": "knowledge_chunk",
        "chunk_id": row["chunk_id"],
        "source_id": row["source_id"],
        "source_type": row["source_type"],
        "title": row["title"],
        "kind": row["kind"],
        "text": row["text_zh"] or row["text"],
        "context": row["context"],
        "authority": row["authority"],
        "verification_status": row["verification_status"],
        "epistemic_status": row["epistemic_status"],
        "support_level": row["support_level"],
        "source_status": row["status"],
        "human_reviewed": row["status"] in {"human_checked", "human_promoted"},
        "human_review_attestation": human_review_attestation,
        "item_human_review": item_human_review,
        "item_human_reviewed": item_human_review.get('current') is not None,
        "item_human_accepted": item_human_review['state'] == 'faithful',
        "citation_allowed": machine_citable,
        "citation_mode": ('with_human_dissent' if disputed else 'standard') if machine_citable else 'blocked',
        "citation_qualification": citation_qualification,
        "answer_text": (row['text_zh'] or row['text']) + ('\n' + citation_qualification if citation_qualification else ''),
        "human_review_scope": "historical_source_declaration" if human_review_attestation else "explicit_assertions_only",
        "superseded_by": replacement.get('new_chunk_id') if replacement else None,
        "reading_artifact_impacts": dict(row).get('reading_artifact_impacts', []),
        **intake,
        "supports_claim": supports_claim,
        "content_quality": quality,
        "work_id": work_identity(row),
        "preferred_version": bool(preferred),
        "supersedes_chunk_ids": supersedes,
        "item_id": row['item_id'],
        "requires_original_review": (row['source_type'] == 'paper_evidence'
                                     and row['source_kind'] not in {'project', 'external', 'translation_style', 'translation_reference'}
                                     and row['epistemic_status'] not in {'codex_inference', 'project_application'}
                                     and not supports_claim and quality['state'] != 'quarantined'),
        "usable_for": decode_json(str(row["usable_for_json"]), []),
        "cannot_support": row["cannot_support"],
        "evidence_anchor": evidence,
        "original_evidence_anchor": original_evidence,
        "citation": citation,
        "score": score,
    }


def search_database(
    db_path: Path,
    query: str,
    *,
    domain: str = "",
    top_k: int = 10,
    include_quarantined: bool = False,
) -> dict[str, Any]:
    if not db_path.exists():
        raise FileNotFoundError(f"knowledge database does not exist: {db_path}")
    match = fts_match_query(query)
    started = time.perf_counter()
    with connect_database(db_path, read_only=True) as connection:
        last = connection.execute(
            "SELECT run_id,manifest_hash,completed_at FROM sync_runs WHERE status='pass' ORDER BY completed_at DESC LIMIT 1"
        ).fetchone()
        rows: list[sqlite3.Row] = []
        if match:
            rows = connection.execute(
                """
                SELECT c.*,s.title,s.authors,s.year,s.source_type,s.status,s.project_profile,s.doi,s.zotero_item_key,
                       s.zotero_attachment_key,s.source_path,s.source_pdf_path,s.source_pdf_sha256,
                       s.model_sha256,s.rag_sha256,s.authority AS source_authority,s.provenance_json,bm25(chunks_fts,0.0,5.0,2.0,3.0,1.5) AS fts_rank
                FROM chunks_fts
                JOIN chunks c ON c.chunk_id=chunks_fts.chunk_id
                JOIN sources s ON s.source_id=c.source_id
                WHERE chunks_fts MATCH ? AND c.tombstoned=0 AND s.tombstoned=0
                ORDER BY fts_rank LIMIT 250
                """,
                (match,),
            ).fetchall()
        query_like = f"%{query.strip()}%"
        exact_rows = connection.execute(
            """
            SELECT c.*,s.title,s.authors,s.year,s.source_type,s.status,s.project_profile,s.doi,s.zotero_item_key,
                   s.zotero_attachment_key,s.source_path,s.source_pdf_path,s.source_pdf_sha256,
                   s.model_sha256,s.rag_sha256,s.authority AS source_authority,s.provenance_json,0.0 AS fts_rank
            FROM chunks c JOIN sources s ON s.source_id=c.source_id
            WHERE c.tombstoned=0 AND s.tombstoned=0 AND (
                lower(c.chunk_id)=lower(?) OR lower(s.doi)=lower(?) OR s.title LIKE ?
            ) LIMIT 250
            """,
            (query.strip(), query.strip(), query_like),
        ).fetchall()
        candidates: dict[str, sqlite3.Row] = {}
        for row in exact_rows + rows:
            candidates.setdefault(str(row["chunk_id"]), row)
        available = {}
        from reading_sessions import resolve_replacement_candidates
        reviewed_rows = attach_reviews(connection, resolve_replacement_candidates(connection, list(candidates.values())))
        scored = [evidence_result(row, score_chunk(row, index, query, domain), available)
                  for index, row in enumerate(reviewed_rows)]
        if not include_quarantined:
            scored = [item for item in scored if item['content_quality']['state'] != 'quarantined' and not item.get('superseded_by')]
        scored.sort(key=lambda item: (-float(item["score"]), str(item["chunk_id"])))
        # IDs such as C001 can be reused for different assertions in another
        # project view. Collapse only identical statements; count independent
        # evidence by work below, regardless of how many assertions are returned.
        grouped = {}
        for item in scored:
            key = (item['work_id'], item['kind'], item['text'],
                   item['epistemic_status'], json_text(item['evidence_anchor']),
                   item['item_human_review']['state'], item['citation_qualification'])
            previous = grouped.get(key)
            if (previous is None or (item['supports_claim'] and not previous['supports_claim'])
                    or (item['supports_claim'] == previous['supports_claim'] and item['preferred_version']
                        and not previous['preferred_version'])):
                grouped[key] = item
        scored = sorted(grouped.values(), key=lambda item: (-float(item['score']), item['chunk_id']))
        if not include_quarantined:
            superseded_chunks = {identifier for item in scored if item['citation_allowed']
                                 for identifier in item['supersedes_chunk_ids']}
            scored = [item for item in scored if item['chunk_id'] not in superseded_chunks]
        # An explicit DOI/title lookup asks for a paper. SQLite insertion order
        # must not put a previously published version ahead of a reviewed,
        # explicitly preferred successor. Keep every distinct assertion and
        # preserve the ranks occupied by other works; preference is not a new
        # support decision and is never inferred from a timestamp.
        query_identity = normalize_text(query)
        identity_works = {work_identity(row) for row in reviewed_rows
                          if query_identity and query_identity in {
                              normalize_text(str(row['doi'])),
                              normalize_text(str(row['title']))}}
        for identity_work in identity_works:
            positions = [i for i, item in enumerate(scored) if item['work_id'] == identity_work]
            preferred_first = sorted((scored[i] for i in positions),
                                     key=lambda item: (not (item['preferred_version'] and item['citation_allowed']),
                                                       -float(item['score']), item['chunk_id']))
            for position, item in zip(positions, preferred_first):
                scored[position] = item
        memory_results: list[dict[str, Any]] = []
        if match:
            memory_rows = connection.execute(
                """SELECT m.*,bm25(work_memory_fts,0.0,3.0,2.0,1.0) AS fts_rank
                   FROM work_memory_fts JOIN work_memory m ON m.record_id=work_memory_fts.record_id
                   WHERE work_memory_fts MATCH ? AND m.status='active' ORDER BY fts_rank LIMIT 20""",
                (match,),
            ).fetchall()
            for index, row in enumerate(memory_rows):
                memory_results.append(
                    {
                        "result_type": "work_memory",
                        "record_id": row["record_id"],
                        "memory_type": row["memory_type"],
                        "domain": row["domain"],
                        "origin_project": row["origin_project"],
                        "text": row["statement"],
                        "applicability": row["applicability"],
                        "authority": "context_only",
                        "verification_status": "needs_human_check",
                        "supports_claim": False,
                        "cannot_support": "工作记忆只能用于查询扩展、检查清单或待验证假设，不能直接支持科研 claim。",
                        "evidence_chunk_ids": decode_json(str(row["evidence_chunk_ids_json"]), []),
                        "score": round(max(1.0, 30.0 - index), 3),
                    }
                )
        evidence_results = scored[:top_k]
        from knowledge_dependencies import annotate_dependencies
        dependency_summary = annotate_dependencies(connection, evidence_results, available)
        anchored = [item for item in evidence_results if item["supports_claim"]]
        gaps = []
        if not anchored:
            gaps.append("检索结果可用于定位；当前陈述仍需核验原 PDF 的支持范围。")
        context_violations = sum(item["authority"] == "context_only" and item["supports_claim"] for item in evidence_results)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return {
            "query": query,
            "domain": domain or ",".join(detect_domains(query)),
            "sync_run_id": last["run_id"] if last else "",
            "manifest_hash": last["manifest_hash"] if last else "",
            "results": evidence_results,
            "context_results": memory_results[:5],
            "query_expansions": [item["text"] for item in memory_results[:3]],
            "gaps": gaps,
            "network_recommended": not any(item['content_quality']['source_availability'] == 'valid'
                                           for item in evidence_results),
            "independent_work_count": len({item['work_id'] for item in anchored}),
            "independent_count_unit": "distinct_paper_identity; experimental independence is not inferred",
            **dependency_summary,
            "context_authority_violations": context_violations,
            "elapsed_ms": round(elapsed_ms, 3),
        }


def get_chunk(db_path: Path, chunk_id: str) -> dict[str, Any] | None:
    with connect_database(db_path, read_only=True) as connection:
        row = connection.execute(
            """SELECT c.*,s.title,s.authors,s.year,s.source_type,s.status,s.project_profile,s.doi,s.zotero_item_key,
                      s.zotero_attachment_key,s.source_path,s.source_pdf_path,s.source_pdf_sha256,
                      s.model_sha256,s.rag_sha256,s.authority AS source_authority,s.provenance_json
               FROM chunks c JOIN sources s ON s.source_id=c.source_id
               WHERE c.chunk_id=? AND c.tombstoned=0 AND s.tombstoned=0""",
            (chunk_id,),
        ).fetchone()
        if not row:
            return None
        item = evidence_result(attach_reviews(connection, [row])[0], 0.0)
        from knowledge_dependencies import annotate_dependencies
        item.update(annotate_dependencies(connection, [item]))
        return item


def database_status(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {"status": "missing", "database_path": str(db_path)}
    with connect_database(db_path, read_only=True) as connection:
        last = connection.execute("SELECT * FROM sync_runs ORDER BY completed_at DESC LIMIT 1").fetchone()
        source_types = {row[0]: row[1] for row in connection.execute("SELECT source_type,count(*) FROM sources WHERE tombstoned=0 GROUP BY source_type")}
        authorities = {row[0]: row[1] for row in connection.execute("SELECT authority,count(*) FROM chunks WHERE tombstoned=0 GROUP BY authority")}
        source_statuses = {row[0]: row[1] for row in connection.execute("SELECT status,count(*) FROM sources WHERE tombstoned=0 GROUP BY status")}
        paper_statuses = {row[0]: row[1] for row in connection.execute("SELECT status,count(*) FROM sources WHERE tombstoned=0 AND source_type='paper_evidence' GROUP BY status")}
        paper_rows = [dict(row) for row in connection.execute(
            "SELECT source_id,doi,zotero_item_key,provenance_json FROM sources WHERE tombstoned=0 AND source_type='paper_evidence'")]
        fast_rows = [row for row in paper_rows if decode_json(row['provenance_json'], {}).get('ingestion_mode') == 'fast_pdf_two_round']
        return {
            "status": "ready",
            "database_path": str(db_path),
            "database_size_bytes": db_path.stat().st_size,
            "source_count": sum(source_types.values()),
            "source_types": source_types,
            "chunk_count": connection.execute("SELECT count(*) FROM chunks WHERE tombstoned=0").fetchone()[0],
            "chunk_authorities": authorities,
            "source_statuses": source_statuses,
            "paper_statuses": paper_statuses,
            "paper_work_identity_count": len({work_identity(row) for row in paper_rows}),
            "fast_pdf_source_count": len(fast_rows),
            "fast_pdf_work_identity_count": len({work_identity(row) for row in fast_rows}),
            "work_memory_count": connection.execute("SELECT count(*) FROM work_memory WHERE status='active'").fetchone()[0],
            "external_staging_count": connection.execute("SELECT count(*) FROM external_staging WHERE state='staged'").fetchone()[0],
            "promotion_count": connection.execute("SELECT count(*) FROM promotions").fetchone()[0],
            "last_sync": dict(last) if last else None,
        }


def capture_work_memory(
    db_path: Path,
    *,
    project_id: str,
    memory_type: str,
    statement: str,
    domain: str = "",
    applicability: str = "",
    dataset_version: str = "",
    coordinate_context: str = "",
    evidence_chunk_ids: list[str] | None = None,
) -> dict[str, Any]:
    if memory_type not in WORK_TYPES:
        raise ValueError(f"memory type must be one of {sorted(WORK_TYPES)}")
    evidence_chunk_ids = evidence_chunk_ids or []
    content_hash = sha256_text(json_text({"project_id": project_id, "type": memory_type, "statement": normalize_text(statement), "applicability": applicability, "dataset_version": dataset_version, "coordinate_context": coordinate_context}))
    timestamp = now_iso()
    with connect_database(db_path) as connection:
        for chunk_id in evidence_chunk_ids:
            if not connection.execute("SELECT 1 FROM chunks WHERE chunk_id=? AND tombstoned=0", (chunk_id,)).fetchone():
                raise ValueError(f"evidence chunk does not exist: {chunk_id}")
        existing = connection.execute("SELECT record_id FROM work_memory WHERE content_hash=?", (content_hash,)).fetchone()
        if existing:
            connection.execute("UPDATE work_memory SET updated_at=? WHERE record_id=?", (timestamp, existing[0]))
            record_id = str(existing[0])
            deduplicated = True
        else:
            record_id = f"wm-{uuid.uuid4().hex[:16]}"
            connection.execute(
                """INSERT INTO work_memory(record_id,domain,origin_project,memory_type,statement,applicability,dataset_version,
                   coordinate_context,authority,evidence_chunk_ids_json,status,content_hash,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record_id, domain or ",".join(detect_domains(statement)), project_id, memory_type, statement, applicability,
                    dataset_version, coordinate_context, "context_only", json_text(evidence_chunk_ids), "active", content_hash,
                    timestamp, timestamp,
                ),
            )
            deduplicated = False
        reindex_work_memory(connection)
        connection.commit()
    return {"status": "pass", "record_id": record_id, "deduplicated": deduplicated, "authority": "context_only", "supports_claim": False}


def stage_external_record(
    db_path: Path,
    *,
    query: str,
    finding: str,
    url: str = "",
    doi: str = "",
    title: str = "",
    role: str = "",
    domain: str = "",
    origin_project: str = "",
    accessed_at: str = "",
    evidence_links: list[str] | None = None,
) -> dict[str, Any]:
    if not url and not doi:
        raise ValueError("external staging requires a URL or DOI")
    evidence_links = evidence_links or []
    content_hash = sha256_text(json_text({"url": url, "doi": doi, "finding": normalize_text(finding)}))
    timestamp = now_iso()
    with connect_database(db_path) as connection:
        existing = connection.execute("SELECT record_id,state FROM external_staging WHERE content_hash=?", (content_hash,)).fetchone()
        if existing:
            return {"status": "pass", "record_id": existing[0], "state": existing[1], "deduplicated": True, "authority": "staging"}
        record_id = f"ext-{uuid.uuid4().hex[:16]}"
        connection.execute(
            """INSERT INTO external_staging(record_id,query,url,doi,title,finding,role,accessed_at,domain,origin_project,
               verification_status,evidence_links_json,state,content_hash,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record_id, query, url, doi, title, finding, role, accessed_at or timestamp, domain or ",".join(detect_domains(query + " " + finding)),
                origin_project, "needs_human_check", json_text(evidence_links), "staged", content_hash, timestamp, timestamp,
            ),
        )
        connection.commit()
    return {"status": "pass", "record_id": record_id, "state": "staged", "deduplicated": False, "authority": "staging"}


def promote_external_record(
    db_path: Path,
    *,
    record_id: str,
    confirmed: bool,
    reason: str = "",
) -> dict[str, Any]:
    timestamp = now_iso()
    promotion_id = f"promotion-{uuid.uuid4().hex[:16]}"
    with connect_database(db_path) as connection:
        row = connection.execute("SELECT * FROM external_staging WHERE record_id=?", (record_id,)).fetchone()
        if not row:
            raise KeyError(f"external staging record not found: {record_id}")
        if not confirmed:
            connection.execute(
                "INSERT INTO promotions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (promotion_id, record_id, "staging", "evidence", "blocked_unconfirmed", "", reason, row["content_hash"], "", timestamp),
            )
            connection.commit()
            raise PermissionError("promotion requires --confirmed")
        connection.execute(
            "UPDATE external_staging SET state='approved',verification_status='externally_verified',updated_at=? WHERE record_id=?",
            (timestamp, record_id),
        )
        connection.execute(
            "INSERT INTO promotions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (promotion_id, record_id, "staging", "evidence", "approved", "human_confirmed_cli", reason, row["content_hash"], f"external:{record_id}", timestamp),
        )
        materialize_approved_external(connection)
        connection.commit()
    return {"status": "pass", "promotion_id": promotion_id, "record_id": record_id, "state": "approved", "promoted_chunk_id": f"external:{record_id}"}


def select_benchmark_cases(connection: sqlite3.Connection, count: int = 40) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT c.chunk_id,c.kind,c.text,c.text_zh,c.context,c.retrieval_queries_zh_json,c.retrieval_queries_en_json,
                  s.source_id,s.project_profile,s.title
           FROM chunks c JOIN sources s ON s.source_id=c.source_id
           WHERE s.source_type='paper_evidence' AND c.verification_status='anchored' AND c.authority='evidence'
             AND c.tombstoned=0 AND s.tombstoned=0
           ORDER BY s.title,c.kind,c.chunk_id"""
    ).fetchall()
    categories = [
        ("fte", ["fte", "flux transfer", "flux rope", "通量传输", "通量绳", "bipolar b"]),
        ("cusp", ["cusp", "cleft", "llbl", "mantle", "极尖", "尖区"]),
        ("magnetopause", ["magnetopause", "magnetosheath", "磁层顶", "磁鞘", "boundary fitting", "tangent fitting"]),
        ("sxi", ["sxi", "swcx", "soft x-ray", "soft xray", "软 x", "软x", "x射线"]),
        ("simulation", ["simulation", "numerical simulation", "mhd", "hybrid-vlasov", "hybrid-pic", "particle-in-cell", "数值模拟", "全局模拟", "混合模拟"]),
    ]
    cases: list[dict[str, Any]] = []
    used_chunks: set[str] = set()
    used_sources: set[str] = set()
    per_category = count // len(categories)
    kind_cycle = ["claim", "method", "figure", "limitation", "claim", "method", "figure", "limitation"]
    for category, terms in categories:
        matching = []
        for row in rows:
            haystack = normalize_text(
                " ".join(
                    str(row[field] or "")
                    for field in ("title", "text", "text_zh", "context", "retrieval_queries_zh_json", "retrieval_queries_en_json")
                )
            )
            if any(normalize_text(term) in haystack for term in terms):
                matching.append(row)
        for local_index in range(per_category):
            language = "zh" if local_index % 2 == 0 else "en"
            desired_kind = kind_cycle[local_index % len(kind_cycle)]
            selected = None
            selected_query = ""
            for strict_kind in (True, False):
                for require_new_source in (True, False):
                    for row in matching:
                        if row["chunk_id"] in used_chunks:
                            continue
                        if strict_kind and row["kind"] != desired_kind:
                            continue
                        if require_new_source and row["source_id"] in used_sources:
                            continue
                        queries = decode_json(str(row[f"retrieval_queries_{language}_json"]), [])
                        query = next((str(value).strip() for value in queries if str(value).strip()), "")
                        if query:
                            selected = row
                            selected_query = query
                            break
                    if selected:
                        break
                if selected:
                    break
            if not selected:
                raise RuntimeError(f"could not construct benchmark case for {category}/{language}/{desired_kind}")
            used_chunks.add(str(selected["chunk_id"]))
            used_sources.add(str(selected["source_id"]))
            cases.append(
                {
                    "id": f"Q{len(cases)+1:02d}",
                    "language": language,
                    "domain": category,
                    "kind": selected["kind"],
                    "query": selected_query,
                    "expected_chunk_id": selected["chunk_id"],
                    "source_title": selected["title"],
                }
            )
    if len(cases) != count:
        raise RuntimeError(f"could not construct {count} benchmark cases; got {len(cases)}")
    return cases


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def run_benchmark(
    config: dict[str, Any],
    *,
    db_override: str | Path | None = None,
    refresh_spec: bool = False,
) -> dict[str, Any]:
    db_path = database_path(config, db_override)
    review_dir = Path(str(config["review_dir"]))
    review_dir.mkdir(parents=True, exist_ok=True)
    spec_path = review_dir / "knowledge_benchmark.json"
    with connect_database(db_path, read_only=True) as connection:
        if spec_path.exists() and not refresh_spec:
            stored = read_json(spec_path)
            cases = stored.get("cases", []) if isinstance(stored, dict) else stored
        else:
            cases = select_benchmark_cases(connection, 40)
    if refresh_spec or not spec_path.exists():
        spec = {
            "schema_version": "2.0",
            "description": "五个主题各八条、中文英文各半、优先跨论文与跨知识项类型的固定检索基准。",
            "cases": cases,
        }
        spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    case_results: list[dict[str, Any]] = []
    latencies: list[float] = []
    context_violations = 0
    for case in cases:
        result = search_database(db_path, case["query"], domain=case.get("domain", ""), top_k=10)
        ids = [item["chunk_id"] for item in result["results"]]
        hit = case["expected_chunk_id"] in ids
        latencies.append(float(result["elapsed_ms"]))
        context_violations += int(result["context_authority_violations"])
        case_results.append({**case, "hit": hit, "rank": ids.index(case["expected_chunk_id"]) + 1 if hit else None, "elapsed_ms": result["elapsed_ms"]})
    adversarial_results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="knowledge-adversarial-") as tmp:
        temp_db = Path(tmp) / "knowledge.sqlite3"
        consistent_backup(db_path, temp_db)
        for case in cases[:10]:
            captured = capture_work_memory(
                temp_db,
                project_id="adversarial-smoke",
                memory_type="hypothesis",
                statement=case["query"],
                domain=case.get("domain", ""),
                applicability="对抗测试：不得覆盖论文证据",
            )
            result = search_database(temp_db, case["query"], domain=case.get("domain", ""), top_k=10)
            paper = next((item for item in result["results"] if item.get("chunk_id") == case["expected_chunk_id"]), None)
            memory = next((item for item in result["context_results"] if item.get("record_id") == captured["record_id"]), None)
            passed = bool(paper and memory and paper["score"] > memory["score"] and paper["supports_claim"] and not memory["supports_claim"])
            adversarial_results.append({"id": case["id"], "passed": passed, "paper_score": paper["score"] if paper else None, "memory_score": memory["score"] if memory else None})
    routing_prompts = [
        "分析一个新的 FTE 观测候选", "flux rope 的 bipolar B_N 能证明什么", "cusp 粒子沉降如何解释",
        "magnetopause 边界拟合方法", "SXI 软 X 射线亮化", "SWCX LOS integration", "SMILE SXI 观测设计",
        "PPMLR-MHD 的 magnetopause", "hybrid simulation 中的重联", "PIC 模拟的 flux transfer event",
    ]
    routing_results = []
    for prompt in routing_prompts:
        domains = detect_domains(prompt)
        result = search_database(db_path, prompt, domain=domains[0] if domains else "", top_k=3)
        cited = any(item.get("citation", {}).get("source_path") for item in result["results"])
        routing_results.append({"prompt": prompt, "domains": domains, "passed": bool(domains and result["results"] and cited)})
    recall = sum(item["hit"] for item in case_results) / len(case_results)
    p95 = percentile(latencies, 0.95)
    report = {
        "status": "pass" if recall >= 0.90 and p95 < 500 and all(item["passed"] for item in adversarial_results) and all(item["passed"] for item in routing_results) and context_violations == 0 else "fail",
        "generated_at": now_iso(),
        "database_path": str(db_path),
        "query_count": len(case_results),
        "coverage": {
            "domains": {domain: sum(case["domain"] == domain for case in cases) for domain in sorted({case["domain"] for case in cases})},
            "languages": {language: sum(case["language"] == language for case in cases) for language in ("zh", "en")},
            "unique_source_count": len({case["source_title"] for case in cases}),
        },
        "recall_at_10": recall,
        "latency_ms": {"mean": statistics.fmean(latencies), "p95": p95, "max": max(latencies)},
        "context_authority_violations": context_violations,
        "cases": case_results,
        "adversarial": {"count": len(adversarial_results), "pass_count": sum(item["passed"] for item in adversarial_results), "cases": adversarial_results},
        "cross_project_routing": {"count": len(routing_results), "pass_count": sum(item["passed"] for item in routing_results), "cases": routing_results},
    }
    (review_dir / "benchmark_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def html_table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{html.escape(str(value))}</th>" for value in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in row) + "</tr>"
        for row in rows
    )
    return f"<div class='table-wrap'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def build_review(config: dict[str, Any], *, db_override: str | Path | None = None) -> Path:
    db_path = database_path(config, db_override)
    review_dir = Path(str(config["review_dir"]))
    review_dir.mkdir(parents=True, exist_ok=True)
    status = database_status(db_path)
    benchmark_path = review_dir / "benchmark_report.json"
    benchmark = read_json(benchmark_path) if benchmark_path.exists() else {}
    sync_report_path = review_dir / "sync_report.json"
    sync_report = read_json(sync_report_path) if sync_report_path.exists() else {}
    with connect_database(db_path, read_only=True) as connection:
        source_rows = connection.execute(
            "SELECT source_type,authority,status,count(*) n FROM sources WHERE tombstoned=0 GROUP BY source_type,authority,status ORDER BY source_type"
        ).fetchall()
        verification_rows = connection.execute(
            "SELECT verification_status,authority,count(*) n FROM chunks WHERE tombstoned=0 GROUP BY verification_status,authority ORDER BY verification_status"
        ).fetchall()
        staging_rows = connection.execute(
            "SELECT record_id,title,url,verification_status,state,accessed_at FROM external_staging ORDER BY updated_at DESC LIMIT 50"
        ).fetchall()
        memory_rows = connection.execute(
            "SELECT record_id,memory_type,domain,origin_project,statement,updated_at FROM work_memory WHERE status='active' ORDER BY updated_at DESC LIMIT 50"
        ).fetchall()
        promotion_rows = connection.execute(
            "SELECT promotion_id,record_id,result,reviewer,created_at FROM promotions ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        boundary_violations = connection.execute(
            """SELECT count(*)
               FROM chunks c JOIN sources s ON s.source_id=c.source_id
               WHERE c.tombstoned=0 AND s.tombstoned=0 AND c.authority!='context_only'
                 AND (s.source_type IN ('raw_source','external_synthesis','mission_document')
                      OR c.kind IN ('project_relation','external_context')
                      OR c.source_kind IN ('project','external','zotero_metadata','mission_document')
                      OR c.epistemic_status IN ('project_application','codex_inference'))"""
        ).fetchone()[0]
        provenance_missing = connection.execute(
            "SELECT count(*) FROM sources WHERE tombstoned=0 AND source_type='paper_evidence' AND (model_sha256='' OR rag_sha256='' OR source_pdf_sha256='' OR provenance_json='{}')"
        ).fetchone()[0]
        translation_source_rows = connection.execute(
            "SELECT title,source_path FROM sources WHERE tombstoned=0 AND source_type='paper_evidence' ORDER BY title"
        ).fetchall()
        benchmark_samples: list[dict[str, Any]] = []
        sample_counts: dict[str, int] = {}
        for case in benchmark.get("cases", []):
            domain = str(case.get("domain") or "未分类")
            if sample_counts.get(domain, 0) >= 2:
                continue
            chunk_row = connection.execute(
                """SELECT c.text,c.text_zh,c.evidence_json,s.title
                   FROM chunks c JOIN sources s ON s.source_id=c.source_id
                   WHERE c.chunk_id=? AND c.tombstoned=0 AND s.tombstoned=0""",
                (str(case.get("expected_chunk_id") or ""),),
            ).fetchone()
            if not chunk_row:
                continue
            benchmark_samples.append(
                {
                    **case,
                    "resolved_title": chunk_row["title"],
                    "chunk_text": chunk_row["text_zh"] or chunk_row["text"],
                    "evidence": json.loads(chunk_row["evidence_json"] or "{}"),
                }
            )
            sample_counts[domain] = sample_counts.get(domain, 0) + 1
    source_table = html_table(
        ["来源类型", "权限", "状态", "数量"],
        [[row["source_type"], row["authority"], row["status"], row["n"]] for row in source_rows],
    )
    verification_table = html_table(
        ["核验状态", "权限", "chunk 数量"],
        [[row["verification_status"], row["authority"], row["n"]] for row in verification_rows],
    )
    staging_table = html_table(
        ["record_id", "标题", "URL", "核验", "状态", "访问时间"],
        [[row["record_id"], row["title"], row["url"], row["verification_status"], row["state"], row["accessed_at"]] for row in staging_rows] or [["—", "当前无暂存记录", "", "", "", ""]],
    )
    memory_table = html_table(
        ["record_id", "类型", "领域", "来源项目", "内容", "更新时间"],
        [[row["record_id"], row["memory_type"], row["domain"], row["origin_project"], str(row["statement"])[:180], row["updated_at"]] for row in memory_rows] or [["—", "当前无工作记忆", "", "", "", ""]],
    )
    promotion_table = html_table(
        ["promotion_id", "record_id", "结果", "审核者", "时间"],
        [[row["promotion_id"], row["record_id"], row["result"], row["reviewer"], row["created_at"]] for row in promotion_rows] or [["—", "当前无 promotion 记录", "", "", ""]],
    )
    benchmark_status = benchmark.get("status", "not_run")
    recall = benchmark.get("recall_at_10", "—")
    p95 = benchmark.get("latency_ms", {}).get("p95", "—")
    human_review_policy = config.get("human_review_policy") or {}
    human_checked_papers = status.get("paper_statuses", {}).get("human_checked", 0)
    draft_papers = status.get("paper_statuses", {}).get("draft", 0)
    warning_rows = sync_report.get("warnings", [])
    warning_list = (
        "<ul>" + "".join(f"<li>{html.escape(str(item))}</li>" for item in warning_rows) + "</ul>"
        if warning_rows
        else '<p class="pass">无同步警告。</p>'
    )
    sample_blocks: list[str] = []
    for case in benchmark_samples:
        evidence = case.get("evidence") or {}
        anchor = {
            key: evidence.get(key)
            for key in ("pdf_page_index", "printed_page_label", "section", "figure", "table", "quote", "doi_or_url")
            if evidence.get(key) not in (None, "")
        }
        sample_blocks.append(
            '<details><summary><strong>{id}</strong> [{domain}/{language}] {query}</summary>'
            '<p><strong>命中来源：</strong>{title}<br><strong>expected chunk：</strong><code>{chunk}</code>；'
            '<strong>rank：</strong>{rank}</p><p><strong>chunk 摘要：</strong>{text}</p>'
            '<pre>{anchor}</pre></details>'.format(
                id=html.escape(str(case.get("id") or "")),
                domain=html.escape(str(case.get("domain") or "")),
                language=html.escape(str(case.get("language") or "")),
                query=html.escape(str(case.get("query") or "")),
                title=html.escape(str(case.get("resolved_title") or case.get("source_title") or "")),
                chunk=html.escape(str(case.get("expected_chunk_id") or "")),
                rank=html.escape(str(case.get("rank") or "—")),
                text=html.escape(str(case.get("chunk_text") or "")[:400]),
                anchor=html.escape(json.dumps(anchor, ensure_ascii=False, indent=2)),
            )
        )
    sample_html = "".join(sample_blocks) or "<p>尚未生成检索样例。</p>"
    translation_links: list[str] = []
    translation_candidates = [
        {"title": str(row["title"] or ""), "artifact_dir": str(row["source_path"] or "")}
        for row in translation_source_rows
    ]
    translation_candidates.extend(
        {
            "title": str(spec.get("title") or "未命名全文翻译"),
            "artifact_dir": str(spec.get("artifact_dir") or ""),
        }
        for spec in config.get("translation_review_sidecars", [])
        if isinstance(spec, dict)
    )
    seen_translation_dirs: set[Path] = set()
    for candidate in translation_candidates:
        artifact_dir = Path(candidate["artifact_dir"]).expanduser()
        try:
            artifact_dir = artifact_dir.resolve()
        except OSError:
            continue
        if artifact_dir in seen_translation_dirs:
            continue
        seen_translation_dirs.add(artifact_dir)
        manifest_path = artifact_dir / "fulltext_translation" / "manifest.json"
        qa_path = artifact_dir / "fulltext_translation" / "qa.json"
        rendered_manifest_path = artifact_dir / "fulltext_translation" / "rendered_pages_manifest.json"
        output_path = artifact_dir / "10_fulltext_translation.zh-CN.pdf"
        if not manifest_path.is_file() or not qa_path.is_file() or not output_path.is_file():
            continue
        try:
            translation_manifest = read_json(manifest_path)
            translation_qa = read_json(qa_path)
        except Exception:
            continue
        if translation_manifest.get("status") != "complete":
            continue
        if translation_manifest.get("final_pdf_sha256") != sha256_file(output_path):
            continue
        if translation_manifest.get("qa_sha256") != sha256_file(qa_path):
            continue
        if translation_manifest.get("template_version") == "academic_cn_journal_v1":
            if not rendered_manifest_path.is_file():
                continue
            if (
                translation_manifest.get("rendered_pages_manifest_sha256")
                != sha256_file(rendered_manifest_path)
            ):
                continue
        if translation_qa.get("status") != "pass":
            continue
        visual_inspection = translation_qa.get("visual_inspection")
        render_checks = translation_qa.get("render_checks", {}).get("checks", {})
        if not isinstance(visual_inspection, dict) or visual_inspection.get("status") != "pass":
            continue
        output_page_count = int(render_checks.get("output_page_count") or 0)
        if output_page_count <= 0:
            continue
        if int(visual_inspection.get("inspected_page_count") or 0) != output_page_count:
            continue
        if int(visual_inspection.get("expected_page_count") or 0) != output_page_count:
            continue
        if visual_inspection.get("final_pdf_sha256") != translation_manifest.get("final_pdf_sha256"):
            continue
        if render_checks.get("final_pdf_sha256") != translation_manifest.get("final_pdf_sha256"):
            continue
        if (
            visual_inspection.get("rendered_pages_manifest_sha256")
            != render_checks.get("rendered_pages_manifest_sha256")
        ):
            continue
        translation_links.append(
            f'<li><a href="{html.escape(output_path.resolve().as_uri(), quote=True)}">'
            f'{html.escape(candidate["title"])}｜全文中文翻译 PDF</a> '
            '<span class="warn">AI辅助学术翻译 / NOT HUMAN CHECKED</span></li>'
        )
    translation_html = (
        "<ul>" + "".join(translation_links) + "</ul>"
        if translation_links
        else "<p>当前没有通过完整性门的全文中文翻译 PDF；staging/blocked 产物不会显示。</p>"
    )
    html_text = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,">
<title>FTE/CUSP/SXI 本地科研知识系统审查</title>
<style>
:root{{--bg:#f4f1e9;--panel:#fffdf8;--ink:#17211b;--muted:#657067;--line:#d9d3c6;--green:#1f6b4f;--amber:#9a5b13;--red:#9f2f2f}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Noto Sans CJK SC",sans-serif}}
main{{max-width:1180px;margin:0 auto;padding:36px 24px 80px}} h1{{font-size:32px;margin:0 0 8px}} h2{{margin-top:36px;border-bottom:1px solid var(--line);padding-bottom:8px}}
.lede{{color:var(--muted);max-width:900px}} .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin:24px 0}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}} .metric{{font-size:28px;font-weight:700}} .label{{color:var(--muted)}}
.pass{{color:var(--green)}} .warn{{color:var(--amber)}} .fail{{color:var(--red)}} .callout{{background:#fff7df;border-left:4px solid var(--amber);padding:14px 18px;margin:18px 0}}
.table-wrap{{overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:10px}} table{{border-collapse:collapse;width:100%;min-width:650px}} th,td{{padding:9px 11px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}} th{{background:#ece8de}}
code{{background:#ece8de;padding:2px 5px;border-radius:4px}} a{{color:var(--green)}} footer{{margin-top:42px;color:var(--muted)}} code,footer,li,summary{{overflow-wrap:anywhere;word-break:break-word}}
details{{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:10px 13px;margin:10px 0}} summary{{cursor:pointer}} pre{{white-space:pre-wrap;background:#252c28;color:#f7f3e8;border-radius:8px;padding:12px;overflow:auto}}
</style></head><body><main>
<h1>FTE/CUSP/SXI 本地科研知识系统</h1>
<p class="lede">本页用于判断索引是否完整、检索是否把论文证据与工作记忆分开、联网结果是否仍在暂存层。它不是新的科学证据，也不替代当前项目 truth sources。</p>
<div class="cards">
  <div class="card"><div class="metric">{status.get('source_count','—')}</div><div class="label">活动来源</div></div>
  <div class="card"><div class="metric">{status.get('chunk_count','—')}</div><div class="label">活动 chunks</div></div>
  <div class="card"><div class="metric">{human_checked_papers}</div><div class="label">human_checked 论文</div></div>
  <div class="card"><div class="metric">{draft_papers}</div><div class="label">未完成人工检查论文</div></div>
  <div class="card"><div class="metric {('pass' if benchmark_status=='pass' else 'warn')}">{benchmark_status}</div><div class="label">检索基准</div></div>
</div>
<div class="callout"><strong>人工审查声明：</strong>当前 <code>paper_evidence</code> 依据用户声明按 <code>{html.escape(str(human_review_policy.get('mode','未启用')))}</code> 管理；适用论文数为 {human_checked_papers}，仍为 draft 的论文数为 {draft_papers}。审查范围：{html.escape(str(human_review_policy.get('review_scope','—')))}。该声明不改变 chunk authority、<code>supports_claim</code>、验证等级或 scientific promotion。</div>
<div class="callout"><strong>Claim boundary：</strong><code>human_checked</code> 只表示人工检查门已关闭；局部 <code>anchored</code> 只表示存在来源锚点。<code>context_only</code>、<code>metadata_only</code> 与 <code>staging</code> 不得直接支持科研结论。</div>
<h2>一、来源权威与状态</h2>{source_table}
<h2>二、证据与核验分布</h2>{verification_table}
<p>Provenance 缺失的强证据来源：<strong class="{('pass' if provenance_missing==0 else 'fail')}">{provenance_missing}</strong>；context-only 越权记录：<strong class="{('pass' if boundary_violations==0 else 'fail')}">{boundary_violations}</strong>。</p>
<p>最近同步：<strong>{html.escape(str(sync_report.get('status','—')))}</strong>；unchanged no-op：<strong>{html.escape(str(sync_report.get('no_op','—')))}</strong>；manifest hash：<code>{html.escape(str(sync_report.get('manifest_hash','—')))}</code>。</p>
<h3>同步告警与保留边界</h3>{warning_list}
<h2>三、检索验收</h2>
<div class="cards"><div class="card"><div class="metric">{recall}</div><div class="label">Recall@10</div></div><div class="card"><div class="metric">{p95}</div><div class="label">p95 latency (ms)</div></div><div class="card"><div class="metric">{benchmark.get('adversarial',{}).get('pass_count','—')}/{benchmark.get('adversarial',{}).get('count','—')}</div><div class="label">对抗排序通过</div></div><div class="card"><div class="metric">{benchmark.get('cross_project_routing',{}).get('pass_count','—')}/{benchmark.get('cross_project_routing',{}).get('count','—')}</div><div class="label">跨目录路由 smoke</div></div></div>
<h3>中英双语检索样例与原文锚点</h3>{sample_html}
<p>完整证据：<a href="benchmark_report.json">benchmark_report.json</a>；固定查询集：<a href="knowledge_benchmark.json">knowledge_benchmark.json</a>。</p>
<h2>四、联网暂存</h2>{staging_table}
<h2>五、跨项目工作记忆</h2>{memory_table}
<h2>六、Promotion 审计</h2>{promotion_table}
<h2>七、全文中文翻译 PDF（阅读辅助 sidecar）</h2>{translation_html}
<p>译文不进入数据库 chunk，不支持 claim promotion，也不能代替英文原文引用。</p>
<h2>八、机器证据附件</h2><ul><li><a href="source_manifest.jsonl">source_manifest.jsonl</a></li><li><a href="sync_report.json">sync_report.json</a></li><li><a href="human_review_attestation.json">human_review_attestation.json</a></li><li><a href="source_authority_matrix.csv">source_authority_matrix.csv</a></li></ul>
<footer>生成时间：{html.escape(now_iso())}；数据库：{html.escape(str(db_path))}</footer>
</main></body></html>"""
    review_path = review_dir / "review_index.html"
    review_path.write_text(html_text, encoding="utf-8")
    with (review_dir / "source_authority_matrix.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["source_type", "authority", "status", "count", "may_support_claim"])
        for row in source_rows:
            writer.writerow([row["source_type"], row["authority"], row["status"], row["n"], row["authority"] == "evidence"])
    return review_path
