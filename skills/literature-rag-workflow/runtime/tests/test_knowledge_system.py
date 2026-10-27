from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from common import CANONICAL_REQUIRED_FILES, STAGING_REQUIRED_FILES  # noqa: E402
from knowledge_system_core import (  # noqa: E402
    apply_human_review_policy,
    build_review,
    capture_work_memory,
    chunk_supports_claim,
    database_status,
    detect_domains,
    discover_additional_sources,
    finalize_source_hashes,
    get_chunk,
    has_usable_evidence_locator,
    initialize_database,
    insert_source_and_chunks,
    mission_document_query_boost,
    normalized_paper_chunk,
    paper_context,
    promote_external_record,
    search_database,
    sha256_text,
    stage_external_record,
    structured_mission_document_chunks,
    sync_database,
    validate_database,
)
from validate_outputs import required_paper_files, resolve_profile_path, resolve_source_pdf_path  # noqa: E402


def source(source_id: str = "zotero:PARENT01", content_hash: str = "") -> dict:
    row = {
        "source_id": source_id,
        "source_type": "paper_evidence",
        "authority": "evidence",
        "title": "Flux transfer event cusp soft X-ray imaging",
        "title_normalized": "flux transfer event cusp soft x ray imaging",
        "authors": "A. Researcher",
        "year": "2026",
        "doi": "10.0000/test",
        "zotero_item_key": "PARENT01",
        "zotero_attachment_key": "ATTACH01",
        "collections_json": "[]",
        "tags_json": json.dumps(["FTE", "SXI"]),
        "priority": "P0",
        "status": "draft",
        "project_profile": "sxi_cusp",
        "source_path": "/tmp/artifact",
        "source_pdf_path": "/tmp/paper.pdf",
        "source_pdf_sha256": "a" * 64,
        "model_sha256": "b" * 64,
        "rag_sha256": "c" * 64,
        "provenance_json": json.dumps({"lineage": "fixture"}),
        "content_hash": content_hash,
        "updated_at": "2026-07-14T00:00:00+00:00",
        "tombstoned": 0,
    }
    return row


def chunk(source_id: str = "zotero:PARENT01") -> dict:
    row = {
        "chunk_id": "paper:c:c001",
        "source_id": source_id,
        "kind": "claim",
        "item_id": "C001",
        "text": "A flux transfer event requires multiple discriminators.",
        "text_zh": "磁通量传输事件需要多个独立判据。",
        "context": "论文内核心 claim",
        "source_kind": "paper",
        "epistemic_status": "reported_result",
        "support_level": "direct",
        "verification_status": "anchored",
        "evidence_json": json.dumps({"pdf_page_index": 3, "section": "Results"}),
        "retrieval_queries_zh_json": json.dumps(["磁通量传输事件 独立判据"]),
        "retrieval_queries_en_json": json.dumps(["flux transfer event discriminators"]),
        "related_chunk_ids_json": "[]",
        "usable_for_json": json.dumps(["FTE identification"]),
        "cannot_support": "单一 bipolar B_N 不能唯一确认 FTE。",
        "authority": "evidence",
        "content_hash": "",
        "tombstoned": 0,
    }
    row["content_hash"] = sha256_text(json.dumps({key: value for key, value in row.items() if key not in {"content_hash", "tombstoned"}}, sort_keys=True))
    return row


def state(source_row: dict | None = None, chunk_row: dict | None = None) -> dict:
    sources = [source_row] if source_row else []
    chunks = [chunk_row] if chunk_row else []
    if sources:
        finalize_source_hashes(sources, chunks)
    manifest = sha256_text(json.dumps([(row["source_id"], row["content_hash"]) for row in sources]))
    return {
        "sources": sources,
        "chunks": chunks,
        "warnings": [],
        "manifest_hash": manifest,
        "paper_source_count": len(sources),
        "paper_chunk_count": len(chunks),
        "raw_source_count": 0,
        "additional_source_count": 0,
    }


class KnowledgeSystemTests(unittest.TestCase):
    def test_book_model_context_is_not_labeled_as_paper_only(self):
        context = paper_context(
            "core_claims",
            {
                "title": "Magnetospheric Current Systems",
                "project_profile": "magnetosphere_ionosphere",
                "document_mode": "book_chapter_full",
            },
            {},
        )
        self.assertIn("专著章级核心 claim", context)
        self.assertNotIn("论文内核心 claim", context)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "knowledge.sqlite3"
        self.config = {
            "schema_version": "1.0",
            "database_path": str(self.db),
            "review_dir": str(self.root / "review"),
            "baseline": {"processed_parent_count": 1, "paper_chunk_count": 1},
        }

    def test_profile_path_accepts_bare_name_and_stored_relative_json_path(self) -> None:
        expected = ROOT / "profiles" / "sxi_cusp.json"
        self.assertEqual(resolve_profile_path("sxi_cusp"), expected)
        self.assertEqual(resolve_profile_path("profiles/sxi_cusp.json"), expected)

    def test_canonical_pdf_resolution_uses_adjacent_hash_matched_attachment(self) -> None:
        attachment = self.root / "ATTACH01"
        paper_dir = attachment / "fte_literature"
        paper_dir.mkdir(parents=True)
        pdf_path = attachment / "paper.pdf"
        pdf_path.write_bytes(b"test-pdf")
        from v2_core import sha256_file

        model = {"source_pdf_path": None, "source_pdf_sha256": sha256_file(pdf_path)}
        self.assertEqual(resolve_source_pdf_path(paper_dir, model, None), pdf_path)

    def test_structured_mission_document_is_page_anchored_and_context_only(self) -> None:
        from v2_core import sha256_file

        pdf_path = self.root / "mission.pdf"
        pdf_path.write_bytes(b"draft-mission-document")
        artifact_path = self.root / "knowledge_chunks.json"
        payload = {
            "schema_version": "1.0",
            "chunks": [
                {
                    "chunk_id": "mission:test:scope",
                    "kind": "format_definition",
                    "item_id": "SCOPE",
                    "text": "A draft FITS format definition.",
                    "text_zh": "一条草案 FITS 格式定义。",
                    "context": "测试任务文档。",
                    "evidence": {"pdf_page_indices": [3], "section": "Scope"},
                    "queries": ["draft FITS format", "草案 FITS 格式"],
                    "usable_for": ["format checks"],
                }
            ],
        }
        artifact_path.write_text(json.dumps(payload), encoding="utf-8")
        spec = {
            "source_id": "local:mission-test",
            "source_type": "mission_document",
            "authority": "context_only",
            "title": "Mission Test",
            "status": "draft",
            "path": str(artifact_path),
            "parser": "structured_mission_document",
            "chunk_id_prefix": "mission:test:",
            "source_pdf_path": str(pdf_path),
            "source_pdf_sha256": sha256_file(pdf_path),
            "cannot_support": "Cannot support scientific claims.",
        }

        parsed = structured_mission_document_chunks(spec, artifact_path.read_text(encoding="utf-8"))
        self.assertEqual(parsed[0]["evidence"]["pdf_page_indices"], [3])
        sources, chunks, warnings = discover_additional_sources({"additional_sources": [spec]})
        self.assertEqual(warnings, [])
        self.assertEqual(sources[0]["source_pdf_sha256"], sha256_file(pdf_path))
        self.assertEqual(sources[0]["status"], "draft")
        self.assertEqual(chunks[0]["authority"], "context_only")
        self.assertEqual(chunks[0]["verification_status"], "anchored")

    def test_structured_mission_document_fails_closed_on_pdf_hash_mismatch(self) -> None:
        pdf_path = self.root / "mission.pdf"
        pdf_path.write_bytes(b"draft-mission-document")
        artifact_path = self.root / "knowledge_chunks.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "chunks": [
                        {
                            "chunk_id": "mission:test:scope",
                            "kind": "format_definition",
                            "item_id": "SCOPE",
                            "text": "A draft FITS format definition.",
                            "text_zh": "一条草案 FITS 格式定义。",
                            "context": "测试任务文档。",
                            "evidence": {"pdf_page_indices": [3]},
                            "queries": ["draft FITS format"],
                            "usable_for": ["format checks"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        spec = {
            "source_id": "local:mission-test",
            "source_type": "mission_document",
            "authority": "context_only",
            "title": "Mission Test",
            "path": str(artifact_path),
            "parser": "structured_mission_document",
            "chunk_id_prefix": "mission:test:",
            "source_pdf_path": str(pdf_path),
            "source_pdf_sha256": "0" * 64,
            "cannot_support": "Cannot support scientific claims.",
        }
        with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
            discover_additional_sources({"additional_sources": [spec]})

    def test_mission_document_boost_requires_identity_or_engineering_query(self) -> None:
        row = {
            "source_type": "mission_document",
            "source_id": "local:example-data-001-v1.0",
            "chunk_id": "example-data-001-v1.0:event-columns",
            "title": "Example Science Data Formats",
            "retrieval_queries_zh_json": json.dumps(["example EVENTS columns", "example level one columns"]),
            "retrieval_queries_en_json": json.dumps(["example event file fields", "FRAME COLUMN_A COLUMN_B VALUE"]),
        }
        self.assertEqual(
            mission_document_query_boost(row, "EXAMPLE-DATA-001 Example Science Data Formats Issue 1 Revision 0"),
            180.0,
        )
        self.assertGreater(
            mission_document_query_boost(row, "example event file EVENTS columns FRAME VALUE"),
            0.0,
        )
        self.assertEqual(mission_document_query_boost(row, "cusp imaging science opportunity"), 0.0)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def seed(self) -> None:
        # Search fixtures represent available, unchecked sources. Missing sources
        # are now quarantined by the intake hook and have separate tests.
        from knowledge_quality import digest
        available = source()
        for name, field in (('source.pdf', 'source_pdf_sha256'),
                            ('02_machine_evidence_pack.json', 'model_sha256'),
                            ('02_rag_knowledge.md', 'rag_sha256')):
            path = self.root / name
            path.write_text('synthetic available search fixture ' + name)
            available[field] = digest(path)
        available.update(source_path=str(self.root), source_pdf_path=str(self.root / 'source.pdf'))
        current = state(available, chunk())
        self.available_fixture_state = current
        with patch("knowledge_system_core.build_current_state", return_value=current):
            report = sync_database(self.config)
        self.assertEqual(report["status"], "pass")

    def test_contracts_split_staging_audit_from_canonical(self) -> None:
        self.assertIn("audit_log.jsonl", STAGING_REQUIRED_FILES)
        self.assertNotIn("audit_log.jsonl", CANONICAL_REQUIRED_FILES)
        self.assertEqual(required_paper_files("canonical"), CANONICAL_REQUIRED_FILES)
        self.assertEqual(required_paper_files("staging"), STAGING_REQUIRED_FILES)

    def test_atomic_sync_and_unchanged_noop(self) -> None:
        current = state(source(), chunk())
        with patch("knowledge_system_core.build_current_state", return_value=current):
            first = sync_database(self.config)
            second = sync_database(self.config, if_stale=True)
        self.assertEqual(first["validation"]["paper_chunk_count"], 1)
        self.assertTrue(second["no_op"])

    def test_noop_rejects_missing_or_misbound_fts_rows(self) -> None:
        self.seed()
        current = self.available_fixture_state
        for mutation in (
            "UPDATE chunks_fts SET chunk_id='wrong-id'",
            "DELETE FROM chunks_fts",
        ):
            with self.subTest(mutation=mutation):
                with sqlite3.connect(self.db) as connection:
                    connection.execute(mutation)
                with patch("knowledge_system_core.build_current_state", return_value=current):
                    with self.assertRaisesRegex(RuntimeError, "FTS"):
                        sync_database(self.config, if_stale=True)
                    sync_database(self.config)

    def test_dry_run_noop_does_not_write_database_or_reports(self) -> None:
        self.seed()
        before_db = self.db.read_bytes()
        review_dir = Path(self.config["review_dir"])
        before_reports = {p.name: p.read_bytes() for p in review_dir.iterdir()}
        with patch("knowledge_system_core.build_current_state", return_value=state(source(), chunk())):
            report = sync_database(self.config, dry_run=True, if_stale=True)
        self.assertEqual(report["status"], "dry_run")
        self.assertEqual(self.db.read_bytes(), before_db)
        self.assertEqual({p.name: p.read_bytes() for p in review_dir.iterdir()}, before_reports)

    def test_validation_rejects_chunk_with_inactive_parent(self) -> None:
        self.seed()
        with sqlite3.connect(self.db) as connection:
            connection.execute("UPDATE sources SET tombstoned=1")
            with self.assertRaisesRegex(RuntimeError, "inactive or missing source"):
                validate_database(connection, {"baseline": {"validation_mode": "dynamic"}})

    def test_claim_gate_requires_usable_locator(self) -> None:
        for anchor in ({}, [], {"pdf_page_index": 0}, {"pdf_page_index": -1},
                       {"pdf_page_index": True}, {"pdf_page_indices": [0]},
                       {"section": "   "}, {"quote": "A quote without a locator"}):
            with self.subTest(anchor=anchor):
                row = chunk()
                row["evidence_json"] = json.dumps(anchor)
                self.assertFalse(chunk_supports_claim(row))
        for anchor in ({"pdf_page_index": 1}, {"pdf_page_indices": [1, 2]},
                       {"section": "Results"}, {"printed_page_label": "iv"}):
            with self.subTest(anchor=anchor):
                row = chunk()
                row["evidence_json"] = json.dumps(anchor)
                self.assertTrue(has_usable_evidence_locator(anchor))
                self.assertFalse(chunk_supports_claim(row))

    def test_unknown_source_status_is_not_human_reviewed(self) -> None:
        self.seed()
        for status, expected in (("", False), ("unknown", False), ("draft", False),
                                 ("human_checked", True), ("human_promoted", True)):
            with self.subTest(status=status):
                with sqlite3.connect(self.db) as connection:
                    connection.execute("UPDATE sources SET status=?", (status,))
                self.assertEqual(get_chunk(self.db, chunk()["chunk_id"])["human_reviewed"], expected)

    def test_human_review_attestation_requires_bounded_timezone_aware_date(self) -> None:
        policy = {
            "enabled": True, "mode": "user_attested_default", "attested_by": "user",
            "attested_at": "2026-08-27T12:39:28Z", "review_scope": "existing sources",
            "canonical_generated_at_lte": "2026-08-27T12:39:28Z",
            "applies_to_source_types": ["paper_evidence"], "applies_to_statuses": ["draft"],
        }
        for timestamp, expected in (("", 0), ("invalid", 0), ("2026-08-01T00:00:00", 0),
                                    ("2026-09-01T00:00:00Z", 0),
                                    ("2026-08-01T00:00:00Z", 1)):
            with self.subTest(timestamp=timestamp):
                row = source()
                row["updated_at"] = timestamp
                self.assertEqual(apply_human_review_policy({"human_review_policy": policy}, [row]), expected)
                self.assertEqual(row["status"], "human_checked" if expected else "draft")
        for cutoff in ("", "invalid", "2026-08-27T12:39:28"):
            with self.subTest(cutoff=cutoff), self.assertRaisesRegex(RuntimeError, "cutoff"):
                apply_human_review_policy({"human_review_policy": {**policy, "canonical_generated_at_lte": cutoff}}, [source()])

    def test_dynamic_baseline_accepts_sealed_backfill_growth(self) -> None:
        self.config["baseline"] = {
            "validation_mode": "dynamic",
            "reference_processed_parent_count_before_backfill": 183,
            "reference_paper_chunk_count_before_backfill": 4929,
        }
        current = state(source(), chunk())
        with patch("knowledge_system_core.build_current_state", return_value=current):
            report = sync_database(self.config)
        self.assertEqual(report["validation"]["paper_source_count"], 1)
        self.assertEqual(report["validation"]["paper_chunk_count"], 1)
        self.assertEqual(report["validation"]["duplicate_chunk_count"], 0)

    def test_single_source_change_is_the_only_logical_update(self) -> None:
        self.seed()
        changed = source()
        changed["title"] = "Changed title"
        current = state(changed, chunk())
        with patch("knowledge_system_core.build_current_state", return_value=current):
            report = sync_database(self.config)
        self.assertEqual(report["updated"], ["zotero:PARENT01"])
        self.assertEqual(report["added"], [])
        self.assertEqual(report["tombstoned"], [])

    def test_removed_context_source_is_preserved_only_as_tombstone(self) -> None:
        paper_source = source()
        paper_chunk = chunk()
        raw_source = source("zotero:RAW01")
        raw_source.update({"source_type": "raw_source", "authority": "context_only", "status": "metadata_only"})
        raw_chunk = chunk("zotero:RAW01")
        raw_chunk.update(
            {
                "chunk_id": "raw:RAW01:metadata",
                "authority": "context_only",
                "source_kind": "zotero_metadata",
                "epistemic_status": "codex_inference",
                "verification_status": "needs_human_check",
            }
        )
        initial = state()
        initial["sources"] = [paper_source, raw_source]
        initial["chunks"] = [paper_chunk, raw_chunk]
        finalize_source_hashes(initial["sources"], initial["chunks"])
        initial["manifest_hash"] = sha256_text(
            json.dumps(sorted((row["source_id"], row["content_hash"]) for row in initial["sources"]))
        )
        with patch("knowledge_system_core.build_current_state", return_value=initial):
            sync_database(self.config)
        with patch("knowledge_system_core.build_current_state", return_value=state(paper_source, paper_chunk)):
            report = sync_database(self.config)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("SELECT tombstoned FROM sources WHERE source_id='zotero:RAW01'").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT tombstoned FROM chunks WHERE chunk_id='raw:RAW01:metadata'").fetchone()[0], 1)
        self.assertEqual(report["tombstoned"], ["zotero:RAW01"])
        self.assertEqual(report["tombstoned_count"], 1)

    def test_independent_rebuilds_have_identical_active_content(self) -> None:
        current = state(source(), chunk())
        first_db = self.root / "first.sqlite3"
        second_db = self.root / "second.sqlite3"
        with patch("knowledge_system_core.build_current_state", return_value=current):
            first = sync_database(self.config, db_override=first_db)
            second = sync_database(self.config, db_override=second_db)
        self.assertEqual(first["manifest_hash"], second["manifest_hash"])
        snapshots = []
        for path in (first_db, second_db):
            with sqlite3.connect(path) as connection:
                snapshots.append(
                    (
                        connection.execute("SELECT source_id,content_hash FROM sources WHERE tombstoned=0 ORDER BY source_id").fetchall(),
                        connection.execute("SELECT chunk_id,content_hash FROM chunks WHERE tombstoned=0 ORDER BY chunk_id").fetchall(),
                    )
                )
        self.assertEqual(snapshots[0], snapshots[1])

    def test_failed_rebuild_retains_active_database(self) -> None:
        self.seed()
        before = self.db.read_bytes()
        changed = source()
        changed["title"] = "Changed title"
        current = state(changed, chunk())
        with patch("knowledge_system_core.build_current_state", return_value=current), patch(
            "knowledge_system_core.validate_database", side_effect=RuntimeError("fixture failure")
        ):
            with self.assertRaises(RuntimeError):
                sync_database(self.config)
        self.assertEqual(self.db.read_bytes(), before)

    def test_search_returns_stable_citation_and_claim_boundary(self) -> None:
        self.seed()
        result = search_database(self.db, "flux transfer event discriminators", top_k=10)
        self.assertEqual(result["results"][0]["chunk_id"], "paper:c:c001")
        self.assertFalse(result["results"][0]["supports_claim"])
        self.assertTrue(result["results"][0]["requires_original_review"])
        self.assertEqual(result["results"][0]["citation"]["zotero_item_key"], "PARENT01")
        self.assertEqual(get_chunk(self.db, "paper:c:c001")["evidence_anchor"]["pdf_page_index"], 3)

    def test_work_memory_is_context_only_and_deduplicated(self) -> None:
        self.seed()
        first = capture_work_memory(
            self.db,
            project_id="new-sxi-project",
            memory_type="hypothesis",
            statement="flux transfer event discriminators",
        )
        second = capture_work_memory(
            self.db,
            project_id="new-sxi-project",
            memory_type="hypothesis",
            statement="flux transfer event discriminators",
        )
        result = search_database(self.db, "flux transfer event discriminators")
        self.assertFalse(first["supports_claim"])
        self.assertEqual(first["record_id"], second["record_id"])
        self.assertTrue(second["deduplicated"])
        self.assertGreater(result["results"][0]["score"], result["context_results"][0]["score"])
        self.assertFalse(result["context_results"][0]["supports_claim"])

    def test_external_staging_requires_confirmed_promotion_and_audits_denial(self) -> None:
        self.seed()
        staged = stage_external_record(
            self.db,
            query="latest FTE review",
            finding="A review statement requiring human verification.",
            url="https://example.test/review",
        )
        with self.assertRaises(PermissionError):
            promote_external_record(self.db, record_id=staged["record_id"], confirmed=False)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("SELECT result FROM promotions").fetchone()[0], "blocked_unconfirmed")
            self.assertEqual(connection.execute("SELECT state FROM external_staging").fetchone()[0], "staged")
        promoted = promote_external_record(self.db, record_id=staged["record_id"], confirmed=True, reason="fixture review")
        self.assertEqual(promoted["state"], "approved")
        self.assertIsNotNone(get_chunk(self.db, promoted["promoted_chunk_id"]))

    def test_review_page_is_chinese_human_entry(self) -> None:
        self.seed()
        path = build_review(self.config)
        text = path.read_text(encoding="utf-8")
        self.assertIn("FTE/CUSP/SXI 本地科研知识系统", text)
        self.assertIn("Claim boundary", text)
        self.assertTrue((path.parent / "source_authority_matrix.csv").exists())

    def test_review_links_only_complete_hash_matched_translation_without_ingesting_it(self) -> None:
        artifact = self.root / "artifact"
        sidecar = artifact / "fulltext_translation"
        sidecar.mkdir(parents=True)
        output = artifact / "10_fulltext_translation.zh-CN.pdf"
        output.write_bytes(b"pdf")
        manifest_path = sidecar / "manifest.json"
        qa_path = sidecar / "qa.json"
        pdf_sha256 = sha256_text("pdf")
        rendered_pages_sha256 = "d" * 64
        qa = {
            "status": "pass",
            "render_checks": {
                "checks": {
                    "output_page_count": 2,
                    "final_pdf_sha256": pdf_sha256,
                    "rendered_pages_manifest_sha256": rendered_pages_sha256,
                }
            },
        }
        qa_path.write_text(json.dumps(qa), encoding="utf-8")
        manifest_path.write_text(
            json.dumps(
                {
                    "status": "complete",
                    "final_pdf_sha256": pdf_sha256,
                    "qa_sha256": sha256_text(qa_path.read_text(encoding="utf-8")),
                }
            ),
            encoding="utf-8",
        )
        paper_source = source()
        paper_source["source_path"] = str(artifact)
        current = state(paper_source, chunk())
        with patch("knowledge_system_core.build_current_state", return_value=current):
            sync_database(self.config)
        # Machine PDF QA without the all-page visual gate is not review-ready.
        rendered = build_review(self.config).read_text(encoding="utf-8")
        self.assertNotIn(output.resolve().as_uri(), rendered)

        qa["visual_inspection"] = {
            "status": "pass",
            "inspected_page_count": 2,
            "expected_page_count": 2,
            "final_pdf_sha256": pdf_sha256,
            "rendered_pages_manifest_sha256": rendered_pages_sha256,
        }
        qa_path.write_text(json.dumps(qa), encoding="utf-8")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["qa_sha256"] = sha256_text(qa_path.read_text(encoding="utf-8"))
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        review = build_review(self.config)
        rendered = review.read_text(encoding="utf-8")
        self.assertIn("全文中文翻译 PDF", rendered)
        self.assertIn(output.resolve().as_uri(), rendered)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM chunks").fetchone()[0], 1)

        manifest_path.write_text(
            json.dumps(
                {
                    "status": "blocked",
                    "final_pdf_sha256": sha256_text("pdf"),
                    "qa_sha256": sha256_text(qa_path.read_text(encoding="utf-8")),
                }
            ),
            encoding="utf-8",
        )
        rendered = build_review(self.config).read_text(encoding="utf-8")
        self.assertNotIn(output.resolve().as_uri(), rendered)
        self.assertIn("staging/blocked 产物不会显示", rendered)

    def test_domain_route_smoke(self) -> None:
        prompts = [
            "FTE 观测", "flux rope", "cusp 粒子", "magnetopause", "SXI 亮化",
            "SWCX", "SMILE", "PPMLR-MHD", "hybrid simulation", "PIC 重联",
        ]
        self.assertTrue(all(detect_domains(prompt) for prompt in prompts))

    def test_status_preserves_draft_boundary(self) -> None:
        self.seed()
        status = database_status(self.db)
        self.assertEqual(status["source_statuses"]["draft"], 1)
        self.assertEqual(status["chunk_authorities"]["evidence"], 1)

    def test_paper_model_results_are_evidence_but_external_context_is_not(self) -> None:
        base = {
            "chunk_id": "paper:test:c001",
            "id": "C001",
            "source_kind": "model",
            "epistemic_status": "reported_result",
            "verification_status": "anchored",
            "text": "A result reported by a numerical model in the paper.",
            "evidence": {"pdf_page_index": 3, "section": "Results"},
        }
        reported = normalized_paper_chunk("zotero:PARENT01", {}, "core_claims", base)
        external = normalized_paper_chunk(
            "zotero:PARENT01",
            {},
            "external_context",
            {**base, "chunk_id": "paper:test:e001", "source_kind": "external", "epistemic_status": "external_finding"},
        )
        self.assertEqual(reported["authority"], "evidence")
        self.assertEqual(external["authority"], "context_only")
        self.assertFalse(chunk_supports_claim(reported))  # locator alone is not semantic review
        self.assertFalse(chunk_supports_claim(external))


if __name__ == "__main__":
    unittest.main()
