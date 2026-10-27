from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from v2_core import (  # noqa: E402
    PROMPT_VERSION,
    V21_PROMPT_VERSION,
    load_profile,
    markdown_to_zotero_html,
    new_model_stub,
    render_rag,
    render_reader_note,
    render_research_card,
    review_status_for,
    validate_model,
)
from common import READER_NOTE_REQUIRED_HEADINGS  # noqa: E402


def anchor(page: int | None = 1) -> dict:
    return {
        "pdf_page_index": page,
        "printed_page_label": str(page or ""),
        "section": "Results" if page else "",
        "figure": "",
        "table": "",
        "quote": "short source phrase",
    }


def item(item_id: str, *, anchored: bool = True, source_kind: str = "paper", epistemic: str = "reported_result") -> dict:
    return {
        "id": item_id,
        "chunk_id": f"paper:c:{item_id.lower()}",
        "source_kind": source_kind,
        "epistemic_status": epistemic,
        "support_level": "direct" if anchored else "indirect",
        "verification_status": "anchored" if anchored else "needs_human_check",
        "text": f"Claim {item_id}",
        "text_zh": f"结论 {item_id}",
        "evidence": anchor(1 if anchored else None),
        "retrieval_queries_zh": [f"查询 {item_id}"],
        "retrieval_queries_en": [f"query {item_id}"],
        "related_chunk_ids": [],
        "usable_for": ["test"],
        "cannot_support": "不能过度解读",
    }


def model() -> dict:
    questions = []
    for index in range(1, 11):
        questions.append(
            {
                "id": f"Q{index:02d}",
                "category": "recall",
                "question": f"问题 {index}？",
                "answer": f"答案 {index}",
                "evidence": anchor(),
                "common_error": "误答",
                "correction": "纠正",
            }
        )
    return {
        "schema_version": "2.0",
        "project": "SXI-cusp研究",
        "project_profile": "sxi_cusp",
        "paper_id": "paper",
        "title": "Test paper",
        "authors": "A. Author",
        "year": "2026",
        "doi": "10.0000/test",
        "zotero_item_key": "ABCDEFGH",
        "zotero_attachment_key": "HGFEDCBA",
        "source_pdf_path": "/tmp/test.pdf",
        "source_pdf_sha256": "a" * 64,
        "priority": "P0",
        "status": "draft",
        "paper_type": "simulation",
        "translation_mode": "layered_bilingual",
        "topic_tags": ["FTE", "cusp"],
        "abstract_original": "Abstract",
        "abstract_zh": "摘要",
        "reading_map": {
            "research_question": "研究问题",
            "terms": {"FTE": "磁通量传输事件"},
            "reading_route": ["读摘要", "核对证据"],
            "next_validation": "核对图",
        },
        "section_guides": [{"id": "S001", "title": "结果", "summary_zh": "结果概述", "key_sentences": [], "formula_terms": []}],
        "core_claims": [item("C001")],
        "methods": [dict(item("M001"), chunk_id="paper:m:m001")],
        "figures": [],
        "limitations": [dict(item("L001"), chunk_id="paper:l:l001", epistemic_status="author_limitation")],
        "controversies": [],
        "external_context": [],
        "external_research_status": "not_run",
        "project_relations": [dict(item("P001", anchored=False, source_kind="project", epistemic="project_application"), chunk_id="paper:p:p001")],
        "cannot_support": ["不能证明观测事件"],
        "self_test": questions,
        "generation": {"generated_at": "2026-07-10T00:00:00Z", "prompt_version": "test"},
    }


class PipelineV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.profile = load_profile(ROOT / "profiles" / "sxi_cusp.json")

    def test_profile_forbids_pdf_upload(self) -> None:
        self.assertFalse(self.profile["external_research_policy"]["upload_local_pdf"])
        self.assertEqual(self.profile["translation_mode"], "layered_bilingual")

    def test_local_review_status_does_not_advertise_zotero_writeback(self) -> None:
        from jsonschema import Draft202012Validator
        status = review_status_for(model(), self.profile, [])
        self.assertNotIn("zotero_writeback_policy", status)
        schema = json.loads((ROOT / "schemas" / "review_status.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(status)), [])

    def test_valid_model_and_stable_rag(self) -> None:
        value = model()
        errors, _ = validate_model(value, self.profile)
        self.assertEqual(errors, [])
        self.assertEqual(render_rag(value, self.profile), render_rag(value, self.profile))
        self.assertIn("<!-- chunk_id: paper:c:c001 -->", render_rag(value, self.profile))

    def test_project_label_mismatch_is_strict_for_v23_but_legacy_compatible(self) -> None:
        fte_profile = load_profile(ROOT / "profiles" / "fte_knowledge.json")
        value = model()
        value["project_profile"] = "fte_knowledge"
        value["generation"]["prompt_version"] = PROMPT_VERSION
        current_errors, _ = validate_model(value, fte_profile)
        self.assertIn("knowledge model project does not match profile display_name", current_errors)

        value["generation"]["prompt_version"] = V21_PROMPT_VERSION
        legacy_errors, legacy_warnings = validate_model(value, fte_profile)
        self.assertNotIn("knowledge model project does not match profile display_name", legacy_errors)
        self.assertTrue(any("retained without rewriting historical artifacts" in warning for warning in legacy_warnings))

    def test_source_epistemic_mixing_is_rejected(self) -> None:
        value = model()
        value["core_claims"][0]["epistemic_status"] = "external_finding"
        errors, _ = validate_model(value, self.profile)
        self.assertTrue(any("mixes paper source" in error for error in errors))

    def test_reader_questions_precede_answers(self) -> None:
        rendered = render_reader_note(model(), self.profile)
        self.assertLess(rendered.index("### Q01 ["), rendered.index("## 答案键"))
        self.assertEqual(rendered.count("### Q01 ["), 1)
        self.assertEqual(rendered.count("### Q01 答案"), 1)

    def test_reader_uses_research_training_modules_with_conservative_legacy_fallback(self) -> None:
        rendered = render_reader_note(model(), self.profile)
        positions = [rendered.index(heading) for heading in READER_NOTE_REQUIRED_HEADINGS]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("旧模型未按新版模板记录适用性", rendered)
        self.assertIn("本节是对论文证据的物理重审，不是新增观测、模拟或拓扑证据", rendered)

    def test_reader_table_preserves_scientific_absolute_value_notation(self) -> None:
        value = model()
        value["methods"][0]["text_zh"] = "作者比较磁场模长 |B| 与归一化电流 J||/B。"
        markdown = render_reader_note(value, self.profile)
        self.assertIn("\\|B\\|", markdown)
        self.assertIn("J\\|\\|/B", markdown)
        html = markdown_to_zotero_html(markdown)
        self.assertIn("|B|", html)
        self.assertIn("J||/B", html)
        self.assertNotIn("/B/", html)

    def test_renderers_accept_string_source_edition(self) -> None:
        value = model()
        value["source_edition"] = "Publisher PDF, 14 pages"
        self.assertIn("Publisher PDF, 14 pages", render_reader_note(value, self.profile))
        self.assertIn("Publisher PDF, 14 pages", render_research_card(value, self.profile))
        self.assertIn('evidence_pdf_version: "Publisher PDF, 14 pages"', render_rag(value, self.profile))

    def test_research_card_excludes_unanchored_claim(self) -> None:
        value = model()
        value["core_claims"].append(dict(item("C002", anchored=False), chunk_id="paper:c:c002"))
        rendered = render_research_card(value, self.profile)
        self.assertIn("C001", rendered)
        self.assertNotIn("`C002`", rendered)

    def test_zotero_html_allowlist(self) -> None:
        rendered = markdown_to_zotero_html("# Title\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(2))")
        self.assertIn('class="zotero-note znv1"', rendered)
        self.assertNotIn("<script", rendered.lower())
        self.assertNotIn("javascript:", rendered.lower())

    def test_new_pdf_starts_as_unanchored_v2_triage_scaffold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paper_dir = Path(tmp) / "ATTACH01" / "sxi_cusp_literature"
            paper_dir.mkdir(parents=True)
            pdf = paper_dir.parent / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\n% test fixture\n")
            metadata = {
                "paper_id": "new-paper",
                "title": "New paper",
                "pdf_path": str(pdf),
                "zotero_attachment_key": "ATTACH01",
            }
            value = new_model_stub(paper_dir, metadata, {"zotero_item_key": "PARENT01"}, self.profile)
            errors, _ = validate_model(value, self.profile)
            self.assertEqual(errors, [])
            self.assertEqual(value["schema_version"], "2.0")
            self.assertEqual(value["generation"]["mode"], "triage_scaffold")
            self.assertEqual(value["core_claims"][0]["verification_status"], "needs_human_check")
            self.assertNotIn("`C001`", render_research_card(value, self.profile))


if __name__ == "__main__":
    unittest.main()
