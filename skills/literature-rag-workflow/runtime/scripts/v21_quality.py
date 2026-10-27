#!/usr/bin/env python3
"""Semantic quality gates for the isolated pipeline-v2.1 rebuild.

The v2 structural validator intentionally remains backwards compatible.  This
module is the promotion gate for deep-read-curated staging models: it verifies
language, specificity, page-local evidence, figure review, external research,
and independent reviewer provenance.
"""

from __future__ import annotations

import hashlib
import json
import copy
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from common import read_json
from v2_core import clean_text, has_cjk, validate_model


FTE_GRADING_DIMENSIONS = {
    "fte_directness",
    "mechanism_topology_value",
    "method_reusability",
    "quantitative_evidence_strength",
    "cross_observation_simulation_validation",
}
PRIORITIES = {"P0", "P1", "P2"}
ITEM_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FIRST_PRINCIPLES_GRADES = {
    "strong_support",
    "reasonable_inference",
    "model_dependent",
    "not_demonstrated",
}
FIRST_PRINCIPLES_APPLICABILITY = {"required", "optional", "not_applicable"}


def load_run_config(path: Path) -> dict[str, Any]:
    """Load and validate a project-scoped rebuild/promotion configuration."""
    raw = read_json(path)
    data = dict(raw)
    required = {
        "schema_version", "run_id", "collection_key", "project_profile",
        "project_profile_path", "quality_profile_path", "artifact_dir_name",
        "project_tag", "display_name", "expected_parent_count",
        "expected_pdf_attachment_count", "expected_note_count",
        "promotion_policy", "include_subcollections", "queue_filename",
        "multi_pdf_overrides", "reuse_overrides", "note_key_overrides",
    }
    missing = sorted(required - set(data))
    if missing:
        raise ValueError(f"run config missing keys: {missing}")
    if data.get("schema_version") != "1.0":
        raise ValueError("run config schema_version must be 1.0")
    document_mode = raw.get("document_mode", "standard")
    if not isinstance(document_mode, str) or document_mode not in {"standard", "book_chapter_full"}:
        raise ValueError("run config document_mode must be standard or book_chapter_full")
    data["document_mode"] = document_mode
    if document_mode == "book_chapter_full":
        expected_chapter_count = data.get("expected_chapter_count")
        if isinstance(expected_chapter_count, bool) or not isinstance(expected_chapter_count, int) or expected_chapter_count < 1:
            raise ValueError("book_chapter_full run config requires a positive expected_chapter_count")
    elif "expected_chapter_count" in raw:
        raise ValueError("standard run config must not define expected_chapter_count")
    selected_parent_keys = raw.get("selected_parent_keys")
    if selected_parent_keys is None:
        data["selected_parent_keys"] = []
    else:
        if not isinstance(selected_parent_keys, list) or not selected_parent_keys:
            raise ValueError("run config selected_parent_keys must be a non-empty array when provided")
        if any(not isinstance(value, str) or not ITEM_KEY_RE.fullmatch(value) for value in selected_parent_keys):
            raise ValueError("run config selected_parent_keys must contain Zotero parent keys")
        normalized_parent_keys = list(selected_parent_keys)
        if len(set(normalized_parent_keys)) != len(normalized_parent_keys):
            raise ValueError("run config selected_parent_keys must not contain duplicates")
        data["selected_parent_keys"] = normalized_parent_keys
    for field in ("run_id", "project_profile", "artifact_dir_name", "project_tag", "display_name"):
        if not clean_text(data.get(field)):
            raise ValueError(f"run config {field} must be non-empty")
    if not ITEM_KEY_RE.fullmatch(str(data.get("collection_key", ""))):
        raise ValueError("run config collection_key must be an eight-character Zotero key")
    for field in ("expected_parent_count", "expected_pdf_attachment_count", "expected_note_count"):
        value = data.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"run config {field} must be a positive integer")
    if data["expected_note_count"] != data["expected_parent_count"] * 2:
        raise ValueError("run config expected_note_count must equal two notes per parent")
    if data["selected_parent_keys"] and len(data["selected_parent_keys"]) != data["expected_parent_count"]:
        raise ValueError("run config selected_parent_keys count must equal expected_parent_count")
    if data["expected_pdf_attachment_count"] < data["expected_parent_count"]:
        raise ValueError("run config cannot expect fewer PDF attachments than parents")
    if data.get("promotion_policy") not in {"qa_auto", "manual_after_sample_review"}:
        raise ValueError("run config promotion_policy is invalid")
    if not isinstance(data.get("include_subcollections"), bool):
        raise ValueError("run config include_subcollections must be boolean")
    queue_name = str(data.get("queue_filename", ""))
    if Path(queue_name).name != queue_name or not queue_name.endswith(".csv"):
        raise ValueError("run config queue_filename must be a project-scoped CSV basename")
    if queue_name == "zotero_writeback_queue.csv":
        raise ValueError("run config must not use the legacy global Zotero queue")

    config_root = path.resolve().parent.parent if path.resolve().parent.name == "profiles" else path.resolve().parent
    for field in ("project_profile_path", "quality_profile_path"):
        relpath = Path(str(data.get(field, "")))
        if relpath.is_absolute() or ".." in relpath.parts or not (config_root / relpath).is_file():
            raise ValueError(f"run config {field} must resolve to an existing project file")
    project = read_json(config_root / data["project_profile_path"])
    if project.get("project_id") != data.get("project_profile"):
        raise ValueError("run config project_profile does not match project_profile_path")
    if project.get("display_name") != data.get("display_name") or data.get("project_tag") != data.get("display_name"):
        raise ValueError("run config project_tag/display_name must match the project profile")

    multi_pdf = data.get("multi_pdf_overrides")
    reuse = data.get("reuse_overrides")
    note_keys = data.get("note_key_overrides")
    if not all(isinstance(value, dict) for value in (multi_pdf, reuse, note_keys)):
        raise ValueError("run config override collections must be objects")
    for parent_key, attachment_key in multi_pdf.items():
        if not ITEM_KEY_RE.fullmatch(str(parent_key)) or not ITEM_KEY_RE.fullmatch(str(attachment_key)):
            raise ValueError("multi_pdf_overrides must map Zotero parent keys to attachment keys")
    seen_note_keys: set[str] = set()
    for parent_key, override in reuse.items():
        if not ITEM_KEY_RE.fullmatch(str(parent_key)) or not isinstance(override, dict):
            raise ValueError("reuse_overrides contains an invalid parent key or value")
        if override.get("write_mode") != "tag_only":
            raise ValueError(f"reuse_overrides.{parent_key} must use write_mode=tag_only")
        if override.get("priority") not in PRIORITIES:
            raise ValueError(f"reuse_overrides.{parent_key} has invalid priority")
        if not clean_text(override.get("artifact_dir_name")):
            raise ValueError(f"reuse_overrides.{parent_key} lacks artifact_dir_name")
        hashes = override.get("expected_content_sha256")
        if not isinstance(hashes, dict) or set(hashes) != {"reader_note", "research_card"}:
            raise ValueError(f"reuse_overrides.{parent_key} requires both expected content hashes")
        if any(not SHA256_RE.fullmatch(str(value)) for value in hashes.values()):
            raise ValueError(f"reuse_overrides.{parent_key} contains an invalid expected content hash")
        for field in ("reader_note_key", "research_card_key"):
            note_key = str(override.get(field, ""))
            if not ITEM_KEY_RE.fullmatch(note_key):
                raise ValueError(f"reuse_overrides.{parent_key}.{field} must preserve an existing note key")
            if note_key in seen_note_keys:
                raise ValueError(f"run config reuses note key more than once: {note_key}")
            seen_note_keys.add(note_key)
    for parent_key, override in note_keys.items():
        if not ITEM_KEY_RE.fullmatch(str(parent_key)) or not isinstance(override, dict):
            raise ValueError("note_key_overrides contains an invalid parent key or value")
        if set(override) != {"reader_note_key", "research_card_key"}:
            raise ValueError(f"note_key_overrides.{parent_key} must define exactly two note keys")
        for field in ("reader_note_key", "research_card_key"):
            note_key = str(override.get(field, ""))
            if note_key and not ITEM_KEY_RE.fullmatch(note_key):
                raise ValueError(f"note_key_overrides.{parent_key}.{field} is invalid")
            if note_key in seen_note_keys:
                raise ValueError(f"run config reuses note key more than once: {note_key}")
            if note_key:
                seen_note_keys.add(note_key)
    if set(reuse).intersection(note_keys):
        raise ValueError("a parent cannot appear in both reuse_overrides and note_key_overrides")
    return data


def load_quality_profile(path: Path) -> dict[str, Any]:
    data = read_json(path)
    required = {
        "schema_version", "prompt_version", "generation_mode", "quality_level",
        "legacy_prompt_versions", "reader_note_template_version", "reader_note_contract",
        "research_card_template_version", "research_card_contract",
        "claim_count", "method_count", "section_count", "bilingual_key_sentence_count", "figure_count", "question_count",
        "quote_word_count", "known_placeholder_phrases", "generic_method_phrases",
        "generic_anchor_patterns", "external_source_count", "allowed_extraction_routes",
    }
    missing = sorted(required - set(data))
    if missing:
        raise ValueError(f"content quality profile missing keys: {missing}")
    return data


def normalize_evidence_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("\u00ad", "")
    value = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip().casefold()


def text_checksum(value: str) -> str:
    return hashlib.sha256(normalize_evidence_text(value).encode("utf-8")).hexdigest()


def model_content_hash(model: dict[str, Any]) -> str:
    """Hash producer-authored content while ignoring reviewer attestation fields."""
    value = copy.deepcopy(model)
    generation = value.setdefault("generation", {})
    generation["reviewer_task_id"] = ""
    generation.pop("reviewed_at", None)
    grading = value.get("grading")
    if isinstance(grading, dict):
        grading["reviewer_confirmed"] = False
        grading["reviewer_task_id"] = ""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def word_count(value: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*|[\u3400-\u9fff]", value or ""))


def effective_chinese(value: str) -> bool:
    compact = re.sub(r"\s+", "", value or "")
    cjk = re.findall(r"[\u3400-\u9fff]", compact)
    latin_words = re.findall(r"[A-Za-z]{3,}", compact)
    return len(cjk) >= 12 and len(cjk) >= len(latin_words) * 2


def concise_chinese(value: Any, minimum: int = 5) -> bool:
    return len(re.findall(r"[\u3400-\u9fff]", str(value or ""))) >= minimum


def validate_reader_note_training_contract(
    model: dict[str, Any],
    quality: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    """Validate the v2.2 reader-facing synthesis without replacing evidence gates."""
    errors: list[str] = []
    note = model.get("reader_note")
    if not isinstance(note, dict):
        return ["reader_note is required by the current prompt version"], {}
    expected_template = quality["reader_note_template_version"]
    if note.get("template_version") != expected_template:
        errors.append(f"reader_note.template_version must be {expected_template}")

    if not effective_chinese(str(note.get("one_sentence_summary", ""))):
        errors.append("reader_note.one_sentence_summary lacks a substantive Chinese research summary")
    contract = quality["reader_note_contract"]
    guiding = note.get("guiding_questions")
    guiding = guiding if isinstance(guiding, list) else []
    gmin = contract["guiding_questions"]["min"]
    gmax = contract["guiding_questions"]["max"]
    if not gmin <= len(guiding) <= gmax:
        errors.append(f"reader_note.guiding_questions requires {gmin}-{gmax} items")
    for index, value in enumerate(guiding, start=1):
        if not concise_chinese(value):
            errors.append(f"reader_note.guiding_questions[{index}] is not a substantive Chinese question")

    claim_ids = {str(item.get("id")) for item in model.get("core_claims", [])}
    method_ids = {str(item.get("id")) for item in model.get("methods", [])}
    limitation_ids = {str(item.get("id")) for item in model.get("limitations", [])}
    relation_ids = {str(item.get("id")) for item in model.get("project_relations", [])}
    evidence_ids = claim_ids | method_ids | limitation_ids

    core_work = note.get("core_work")
    core_work = core_work if isinstance(core_work, list) else []
    wmin = contract["core_work"]["min"]
    wmax = contract["core_work"]["max"]
    if not wmin <= len(core_work) <= wmax:
        errors.append(f"reader_note.core_work requires {wmin}-{wmax} items")
    for index, work in enumerate(core_work, start=1):
        owner = f"reader_note.core_work[{index}]"
        if not isinstance(work, dict):
            errors.append(f"{owner} must be an object")
            continue
        for field in ("title", "what_zh", "why_zh", "how_zh", "result_zh"):
            if not concise_chinese(work.get(field)):
                errors.append(f"{owner}.{field} lacks substantive Chinese content")
        linked_claims = work.get("claim_ids")
        linked_methods = work.get("method_ids")
        if not isinstance(linked_claims, list) or not linked_claims:
            errors.append(f"{owner}.claim_ids must reference at least one canonical claim")
        elif not set(linked_claims).issubset(claim_ids):
            errors.append(f"{owner}.claim_ids contains unknown claim IDs")
        if not isinstance(linked_methods, list) or not linked_methods:
            errors.append(f"{owner}.method_ids must reference at least one canonical method")
        elif not set(linked_methods).issubset(method_ids):
            errors.append(f"{owner}.method_ids contains unknown method IDs")

    background = note.get("background")
    background = background if isinstance(background, dict) else {}
    for field in ("known_zh", "gap_zh", "paper_response_zh"):
        if not effective_chinese(str(background.get(field, ""))):
            errors.append(f"reader_note.background.{field} lacks substantive Chinese content")

    mechanism = note.get("physical_mechanism")
    mechanism = mechanism if isinstance(mechanism, dict) else {}
    if not effective_chinese(str(mechanism.get("phenomenon_zh", ""))):
        errors.append("reader_note.physical_mechanism.phenomenon_zh lacks substantive Chinese content")
    cause_chain = mechanism.get("cause_chain")
    if not isinstance(cause_chain, list) or len(cause_chain) < 2:
        errors.append("reader_note.physical_mechanism.cause_chain requires at least two causal steps")
    elif not all(concise_chinese(value) for value in cause_chain):
        errors.append("reader_note.physical_mechanism.cause_chain contains a non-substantive step")
    for field in ("equations_or_constraints", "observables"):
        values = mechanism.get(field)
        if not isinstance(values, list) or not values:
            errors.append(f"reader_note.physical_mechanism.{field} requires at least one item")
        elif not all(clean_text(value) for value in values):
            errors.append(f"reader_note.physical_mechanism.{field} contains an empty item")

    first_principles = note.get("first_principles")
    first_principles = first_principles if isinstance(first_principles, dict) else {}
    applicability = first_principles.get("applicability")
    if applicability not in FIRST_PRINCIPLES_APPLICABILITY:
        errors.append("reader_note.first_principles.applicability is invalid")
    if not effective_chinese(str(first_principles.get("reason_zh", ""))):
        errors.append("reader_note.first_principles.reason_zh lacks substantive Chinese rationale")
    applicable = applicability in {"required", "optional"}
    if applicable:
        if not first_principles.get("starting_points"):
            errors.append("applicable first-principles review requires starting_points")
        if len(first_principles.get("derivation_chain", [])) < 2:
            errors.append("applicable first-principles review requires at least two derivation steps")
        assumptions = first_principles.get("assumptions")
        if not isinstance(assumptions, list) or not assumptions:
            errors.append("applicable first-principles review requires an assumption audit")
        else:
            for index, assumption in enumerate(assumptions, start=1):
                owner = f"reader_note.first_principles.assumptions[{index}]"
                if not isinstance(assumption, dict):
                    errors.append(f"{owner} must be an object")
                    continue
                for field in ("assumption_zh", "physical_basis_zh", "risk_zh"):
                    if not concise_chinese(assumption.get(field)):
                        errors.append(f"{owner}.{field} lacks substantive Chinese content")
                if assumption.get("confidence") not in {"high", "medium", "low"}:
                    errors.append(f"{owner}.confidence is invalid")
                related = assumption.get("related_claim_ids", [])
                if not related or not set(related).issubset(claim_ids):
                    errors.append(f"{owner}.related_claim_ids must reference canonical claims")
        if not effective_chinese(str(first_principles.get("method_rationale_zh", ""))):
            errors.append("applicable first-principles review requires method_rationale_zh")
        assessments = first_principles.get("claim_assessments")
        minimum = contract["first_principles_claim_assessments"]["min_when_applicable"]
        if not isinstance(assessments, list) or len(assessments) < minimum:
            errors.append("applicable first-principles review requires claim assessments")
        else:
            for index, assessment in enumerate(assessments, start=1):
                owner = f"reader_note.first_principles.claim_assessments[{index}]"
                if not isinstance(assessment, dict):
                    errors.append(f"{owner} must be an object")
                    continue
                if assessment.get("claim_id") not in claim_ids:
                    errors.append(f"{owner}.claim_id is unknown")
                if assessment.get("grade") not in FIRST_PRINCIPLES_GRADES:
                    errors.append(f"{owner}.grade is invalid")
                if not effective_chinese(str(assessment.get("rationale_zh", ""))):
                    errors.append(f"{owner}.rationale_zh lacks substantive Chinese content")
        if not first_principles.get("new_questions"):
            errors.append("applicable first-principles review requires at least one new physical question")

    method_audit = note.get("method_audit")
    method_audit = method_audit if isinstance(method_audit, dict) else {}
    if not effective_chinese(str(method_audit.get("design_summary_zh", ""))):
        errors.append("reader_note.method_audit.design_summary_zh lacks substantive Chinese content")
    checks = method_audit.get("checks")
    checks = checks if isinstance(checks, list) else []
    if len(checks) < contract["method_checks"]["min"]:
        errors.append("reader_note.method_audit.checks has too few type-specific checks")
    for index, check in enumerate(checks, start=1):
        owner = f"reader_note.method_audit.checks[{index}]"
        if not isinstance(check, dict):
            errors.append(f"{owner} must be an object")
            continue
        for field in ("item_zh", "finding_zh", "risk_zh"):
            if not concise_chinese(check.get(field)):
                errors.append(f"{owner}.{field} lacks substantive Chinese content")
        linked = check.get("evidence_ids", [])
        if not linked or not set(linked).issubset(evidence_ids):
            errors.append(f"{owner}.evidence_ids must reference canonical evidence objects")

    contribution = note.get("contribution_audit")
    contribution = contribution if isinstance(contribution, dict) else {}
    for field in ("previous_state_zh", "publication_value_zh", "remaining_uncertainty_zh"):
        if not effective_chinese(str(contribution.get(field, ""))):
            errors.append(f"reader_note.contribution_audit.{field} lacks substantive Chinese content")
    contributions = contribution.get("contributions")
    contributions = contributions if isinstance(contributions, list) else []
    if not contributions:
        errors.append("reader_note.contribution_audit.contributions requires at least one item")
    for index, item in enumerate(contributions, start=1):
        owner = f"reader_note.contribution_audit.contributions[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{owner} must be an object")
            continue
        if item.get("type") not in {
            "scientific_question", "method", "physical_understanding",
            "observation_window", "constraint",
        }:
            errors.append(f"{owner}.type is invalid")
        if not effective_chinese(str(item.get("text_zh", ""))):
            errors.append(f"{owner}.text_zh lacks substantive Chinese content")
        linked = item.get("claim_ids", [])
        if not linked or not set(linked).issubset(claim_ids):
            errors.append(f"{owner}.claim_ids must reference canonical claims")

    critical = note.get("critical_audit")
    critical = critical if isinstance(critical, dict) else {}
    if not effective_chinese(str(critical.get("evidence_strength_zh", ""))):
        errors.append("reader_note.critical_audit.evidence_strength_zh lacks substantive Chinese content")
    for field in ("method_limits", "alternative_explanations", "future_directions"):
        values = critical.get(field)
        if not isinstance(values, list) or not values:
            errors.append(f"reader_note.critical_audit.{field} requires at least one item")

    transfer = note.get("research_transfer")
    transfer = transfer if isinstance(transfer, dict) else {}
    direct = transfer.get("direct_relation_ids", [])
    adaptable = transfer.get("adaptable_relation_ids", [])
    if not isinstance(direct, list) or not isinstance(adaptable, list):
        errors.append("reader_note.research_transfer relation ID fields must be arrays")
    elif not set(direct + adaptable).issubset(relation_ids):
        errors.append("reader_note.research_transfer contains unknown project relation IDs")
    if not transfer.get("not_applicable_zh"):
        errors.append("reader_note.research_transfer.not_applicable_zh requires at least one boundary")
    if not transfer.get("new_questions"):
        errors.append("reader_note.research_transfer.new_questions requires at least one question")

    meeting = note.get("group_meeting_summary")
    meeting = meeting if isinstance(meeting, dict) else {}
    for field in (
        "background_zh", "research_question_zh", "main_contribution_zh",
        "main_limitation_zh", "research_takeaway_zh",
    ):
        if not effective_chinese(str(meeting.get(field, ""))):
            errors.append(f"reader_note.group_meeting_summary.{field} lacks substantive Chinese content")
    meeting_work = meeting.get("core_work_zh")
    meeting_work = meeting_work if isinstance(meeting_work, list) else []
    mmin = contract["group_meeting_core_work"]["min"]
    mmax = contract["group_meeting_core_work"]["max"]
    if not mmin <= len(meeting_work) <= mmax:
        errors.append(f"reader_note.group_meeting_summary.core_work_zh requires {mmin}-{mmax} items")

    checks_out = {
        "template_version": note.get("template_version"),
        "guiding_question_count": len(guiding),
        "core_work_count": len(core_work),
        "first_principles_applicability": applicability,
        "method_check_count": len(checks),
        "contribution_count": len(contributions),
        "group_meeting_core_work_count": len(meeting_work),
    }
    return errors, checks_out


def validate_grading(
    model: dict[str, Any],
    project_profile: dict[str, Any],
    *,
    require_reviewer: bool,
) -> list[str]:
    """Validate evidence-based P0/P1/P2 grading when a profile opts in."""
    policy = project_profile.get("grading_policy")
    if not policy:
        return []
    errors: list[str] = []
    configured_dimensions = {str(item.get("id", "")) for item in policy.get("dimensions", []) if isinstance(item, dict)}
    if configured_dimensions != FTE_GRADING_DIMENSIONS:
        errors.append("grading_policy must define the five FTE grading dimensions")
    grading = model.get("grading")
    if not isinstance(grading, dict):
        return errors + ["FTE project model requires grading"]
    dimensions = grading.get("dimensions")
    if not isinstance(dimensions, dict) or set(dimensions) != configured_dimensions:
        errors.append("grading.dimensions must contain exactly the five configured dimensions")
        dimensions = dimensions if isinstance(dimensions, dict) else {}
    scores: list[int] = []
    for name in configured_dimensions:
        score = dimensions.get(name)
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 2:
            errors.append(f"grading.dimensions.{name} must be an integer from 0 to 2")
        else:
            scores.append(score)
    total = grading.get("total_score")
    if isinstance(total, bool) or not isinstance(total, int) or not 0 <= total <= 10:
        errors.append("grading.total_score must be an integer from 0 to 10")
    elif len(scores) == len(configured_dimensions) and total != sum(scores):
        errors.append(f"grading.total_score must equal the dimension sum {sum(scores)}")
    assigned = grading.get("assigned_priority")
    if assigned not in PRIORITIES:
        errors.append("grading.assigned_priority must be P0, P1, or P2")
    if model.get("priority") != assigned:
        errors.append("model.priority must match grading.assigned_priority")
    if grading.get("core_support") not in {"claim", "method", "validation", "none"}:
        errors.append("grading.core_support is invalid")
    if not isinstance(grading.get("scope_limited"), bool):
        errors.append("grading.scope_limited must be boolean")
    if not isinstance(grading.get("foundational_exception"), bool):
        errors.append("grading.foundational_exception must be boolean")
    if not effective_chinese(str(grading.get("rationale_zh", ""))):
        errors.append("grading.rationale_zh must be a substantive Chinese grading rationale")

    thresholds = policy.get("thresholds", {})
    directness = dimensions.get("fte_directness")
    foundational_exception = grading.get("foundational_exception") is True
    scope_limited = grading.get("scope_limited") is True
    if assigned == "P0":
        p0 = thresholds.get("P0", {})
        standard_p0 = (
            isinstance(total, int)
            and p0.get("min_total", 8) <= total <= p0.get("max_total", 10)
            and directness == p0.get("required_fte_directness", 2)
            and grading.get("core_support") in {"claim", "method", "validation"}
        )
        if not standard_p0 and not foundational_exception:
            errors.append("P0 requires score 8-10, FTE directness 2, and direct core claim/method/validation support")
        if foundational_exception:
            if not p0.get("foundational_exception_allowed"):
                errors.append("grading policy does not allow a foundational P0 exception")
            if not effective_chinese(str(grading.get("exception_rationale_zh", ""))):
                errors.append("foundational P0 exception requires a substantive Chinese rationale")
    elif assigned == "P1":
        p1 = thresholds.get("P1", {})
        standard_p1 = isinstance(total, int) and p1.get("min_total", 4) <= total <= p1.get("max_total", 7)
        narrow_p1 = p1.get("scope_limited_exception_allowed") and scope_limited and directness == 2
        if not standard_p1 and not narrow_p1:
            errors.append("P1 requires score 4-7 or a scope-limited direct FTE study")
        if foundational_exception:
            errors.append("foundational_exception is only valid for P0")
    elif assigned == "P2":
        p2 = thresholds.get("P2", {})
        if not (isinstance(total, int) and p2.get("min_total", 0) <= total <= p2.get("max_total", 3)):
            errors.append("P2 requires score 0-3")
        if foundational_exception:
            errors.append("foundational_exception is only valid for P0")

    generation = model.get("generation", {})
    grading_reviewer = clean_text(grading.get("reviewer_task_id"))
    generation_reviewer = clean_text(generation.get("reviewer_task_id"))
    if grading.get("reviewer_confirmed") is True and grading_reviewer != generation_reviewer:
        errors.append("confirmed grading reviewer task ID does not match generation provenance")
    return errors


def semantic_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", value)


def duplicate_pairs(items: list[dict[str, Any]], field: str = "text_zh") -> list[tuple[str, str]]:
    duplicates: list[tuple[str, str]] = []
    for left_index, left in enumerate(items):
        left_key = semantic_key(str(left.get(field, "")))
        for right in items[left_index + 1 :]:
            right_key = semantic_key(str(right.get(field, "")))
            if not left_key or not right_key:
                continue
            shorter = min(len(left_key), len(right_key))
            containment = shorter >= 30 and (left_key in right_key or right_key in left_key)
            similar = SequenceMatcher(None, left_key, right_key).ratio() >= 0.86
            if containment or similar:
                duplicates.append((str(left.get("id", "?")), str(right.get("id", "?"))))
    return duplicates


def load_page_record(extraction: dict[str, Any], page: int) -> dict[str, Any] | None:
    for record in extraction.get("pages", []):
        if record.get("pdf_page_index") == page:
            return record
    return None


def page_text(stage_dir: Path, record: dict[str, Any], route: str) -> str:
    routes = record.get("routes", {})
    route_record = routes.get(route, {})
    relpath = route_record.get("path", "")
    if not relpath:
        return ""
    path = stage_dir / relpath
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def validate_anchor(
    *,
    owner: str,
    anchor: dict[str, Any],
    stage_dir: Path,
    extraction: dict[str, Any],
    quality: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    page = anchor.get("pdf_page_index")
    if not isinstance(page, int) or page < 1 or page > int(extraction.get("page_count") or 0):
        return [f"{owner} anchored evidence lacks an exact valid pdf_page_index"]
    locators = [clean_text(anchor.get(name)) for name in ("section", "figure", "table")]
    if not any(locators):
        errors.append(f"{owner} anchored evidence lacks an actual section/figure/table locator")
    locator_text = " / ".join(value.casefold() for value in locators if value)
    for pattern in quality["generic_anchor_patterns"]:
        if pattern.casefold() in locator_text:
            errors.append(f"{owner} uses generic anchor locator: {locator_text}")
            break
    quote = clean_text(anchor.get("quote"))
    qcount = word_count(quote)
    qmin = quality["quote_word_count"]["min"]
    qmax = quality["quote_word_count"]["max"]
    if not qmin <= qcount <= qmax:
        errors.append(f"{owner} evidence quote must contain {qmin}-{qmax} words; got {qcount}")
    route = anchor.get("extraction_route")
    if route not in quality["allowed_extraction_routes"]:
        errors.append(f"{owner} missing/invalid extraction_route")
        return errors
    record = load_page_record(extraction, page)
    if not record:
        errors.append(f"{owner} has no extraction record for PDF page {page}")
        return errors
    route_record = record.get("routes", {}).get(route, {})
    text = page_text(stage_dir, record, route)
    if not text:
        errors.append(f"{owner} extraction route {route} has no readable page text")
        return errors
    expected_checksum = route_record.get("normalized_sha256")
    if anchor.get("page_checksum") != expected_checksum or text_checksum(text) != expected_checksum:
        errors.append(f"{owner} page_checksum does not match {route} extraction record")
    if normalize_evidence_text(quote) not in normalize_evidence_text(text):
        errors.append(f"{owner} quote cannot be found on PDF page {page} via {route}")
    if route == "ocr" and not route_record.get("image_sha256"):
        errors.append(f"{owner} OCR anchor lacks rendered-page checksum provenance")
    return errors


def validate_deep_read_model(
    model: dict[str, Any],
    project_profile: dict[str, Any],
    quality: dict[str, Any],
    stage_dir: Path,
    *,
    require_reviewer: bool = True,
    document_mode: str | None = None,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Reuse deterministic checks only with every read dependency bound.

    Transaction/writeback checks are outside this cache and always run fresh.
    Models passed in memory without an identical saved model use the direct path.
    """
    from adaptive_content import cached_check
    from knowledge_quality import object_digest
    stage_dir = Path(stage_dir)
    model_path = stage_dir / '02_machine_evidence_pack.json'
    extraction_path = stage_dir / 'extraction/extraction_manifest.json'
    if not model_path.is_file() or not extraction_path.is_file() or read_json(model_path) != model:
        return _validate_deep_read_model(model, project_profile, quality, stage_dir,
                                        require_reviewer=require_reviewer, document_mode=document_mode)
    dependencies = {'model': model_path, 'extraction_manifest': extraction_path}
    for name in ('paper_manifest.json', 'producer_report.json', 'review/review_report.json'):
        path = stage_dir / name
        if path.is_file():
            dependencies[name] = path
    extraction = read_json(extraction_path)
    for page in extraction.get('pages', []):
        for route, item in page.get('routes', {}).items():
            path = stage_dir / item.get('path', '')
            if path.is_file():
                dependencies['page:' + str(page.get('pdf_page_index')) + ':' + route] = path
    for figure in model.get('figures', []):
        path = Path(str(figure.get('visual_evidence_path') or ''))
        if not path.is_absolute():
            path = stage_dir / path
        if path.is_file():
            dependencies['figure:' + figure['id']] = path
    paper_manifest = read_json(stage_dir / 'paper_manifest.json') if (stage_dir / 'paper_manifest.json').is_file() else {}
    source = model.get('source_pdf_path') or paper_manifest.get('pdf_path')
    if source and Path(source).is_file():
        dependencies['original_pdf'] = Path(source)
    for name in ('v2_core.py', 'reader_note_v3.py', 'research_card_daily.py', 'adaptive_content.py',
                 'knowledge_quality.py', 'artifact_attribution.py', 'portable_identity.py', 'common.py'):
        dependencies['checker:' + name] = Path(__file__).with_name(name)
    scope = object_digest({'profile': project_profile, 'quality': quality,
                           'reviewer_required': require_reviewer, 'document_mode': document_mode})
    result, reused = cached_check(stage_dir / '.quality_check_cache', 'deep_read:' + scope,
                                 dependencies, Path(__file__), lambda: _validate_deep_read_model(
                                     model, project_profile, quality, stage_dir,
                                     require_reviewer=require_reviewer, document_mode=document_mode))
    errors, warnings, report = result
    report = dict(report)
    report['check_cache'] = {'reused': reused, 'input_count': len(dependencies),
                             'writer_and_concurrency_checks_cached': False}
    return errors, warnings, report


def _validate_deep_read_model(model, project_profile, quality, stage_dir, *,
                              require_reviewer=True, document_mode=None):
    from adaptive_content import adaptive, folded_fields
    from artifact_attribution import attribution_risks, attribution_review_errors
    import copy
    is_adaptive = adaptive(model)
    quality = copy.deepcopy(quality)
    if is_adaptive:
        for field in ('claim_count', 'method_count', 'section_count', 'question_count',
                      'figure_count', 'bilingual_key_sentence_count'):
            quality[field] = {'min': 0, 'max': 1000000}
        for field in ('guiding_questions', 'core_work', 'method_checks', 'group_meeting_core_work'):
            quality['reader_note_contract'][field] = {'min': 0, 'max': 1000000}
    errors, warnings = validate_model(model, project_profile)
    paper_manifest_path = stage_dir / "paper_manifest.json"
    paper_manifest = read_json(paper_manifest_path) if paper_manifest_path.exists() else {}
    if paper_manifest.get('schema') == 'portable-deep-reading-source/1':
        from portable_identity import validate_portable_identity
        try:
            validate_portable_identity(paper_manifest, model)
        except ValueError as exc:
            errors.append('portable source identity: ' + str(exc))
    effective_document_mode = str(document_mode or paper_manifest.get("document_mode") or "standard")
    expected_chapter_count = paper_manifest.get("expected_chapter_count")
    if effective_document_mode not in {"standard", "book_chapter_full"}:
        errors.append("document_mode must be standard or book_chapter_full")
        effective_document_mode = "standard"
    if effective_document_mode == "book_chapter_full":
        if isinstance(expected_chapter_count, bool) or not isinstance(expected_chapter_count, int) or expected_chapter_count < 1:
            errors.append("book_chapter_full paper manifest requires a positive expected_chapter_count")
    elif expected_chapter_count is not None:
        errors.append("standard paper manifest must not define expected_chapter_count")
    extraction_path = stage_dir / "extraction" / "extraction_manifest.json"
    if not extraction_path.exists():
        errors.append("missing extraction/extraction_manifest.json")
        return errors, warnings, {"checks": {}, "errors": errors, "warnings": warnings}
    extraction = read_json(extraction_path)
    if extraction.get("status") != "pass":
        errors.append(f"PDF extraction did not pass: {extraction.get('status')}")

    generation = model.get("generation", {})
    artifact_risks = attribution_risks(model)
    if artifact_risks:
        warnings.append('Paper-contribution fields copy project-use text; independent original-source attribution review required.')
    expected_generation = {
        "mode": quality["generation_mode"],
        "quality_level": quality["quality_level"],
    }
    for field, expected in expected_generation.items():
        if generation.get(field) != expected:
            errors.append(f"generation.{field} must be {expected}")
    prompt_version = str(generation.get("prompt_version") or "")
    current_prompt_version = quality["prompt_version"]
    allowed_prompt_versions = {
        current_prompt_version,
        *quality.get("legacy_prompt_versions", []),
    }
    if prompt_version not in allowed_prompt_versions:
        errors.append(
            "generation.prompt_version must be the current research-training "
            "version or an explicitly supported legacy version"
        )
    reader_note_contract_required = prompt_version == current_prompt_version
    reader_note_checks: dict[str, Any] = {}
    if reader_note_contract_required:
        reader_note_errors, reader_note_checks = validate_reader_note_training_contract(model, quality)
        errors.extend(reader_note_errors)
        from research_card_daily import (
            render_daily_research_card,
            research_card_visible_chinese_count,
            validate_research_card,
        )

        research_card_errors, research_card_checks = validate_research_card(model)
        errors.extend(research_card_errors)
        if not research_card_errors:
            try:
                rendered_card = render_daily_research_card(model, project_profile)
                research_card_checks["visible_chinese_count"] = research_card_visible_chinese_count(rendered_card)
            except ValueError as exc:
                errors.append(str(exc))
    else:
        research_card_checks = {"template_version": "legacy"}
    if not clean_text(generation.get("producer_task_id")):
        errors.append("generation.producer_task_id is required")
    if require_reviewer:
        review_path = stage_dir / "review" / "review_report.json"
        if not review_path.exists():
            errors.append("independent review report is missing")
        else:
            review = read_json(review_path)
            if paper_manifest.get('schema') == 'portable-deep-reading-source/1':
                try:
                    validate_portable_identity(paper_manifest, model, review)
                except ValueError as exc:
                    errors.append('portable review identity: ' + str(exc))
            if review.get("decision") != "pass":
                errors.append(f"independent reviewer decision is not pass: {review.get('decision')}")
            review_reviewer = clean_text(review.get("reviewer_task_id"))
            review_producer = clean_text(review.get("producer_task_id"))
            if not review_reviewer or not review_producer:
                errors.append("review report must identify both producer and reviewer tasks")
            if review_reviewer == review_producer:
                errors.append("producer and reviewer task IDs must be independent")
            model_producer = clean_text(generation.get("producer_task_id"))
            if review_producer != model_producer:
                producer_report_path = stage_dir / "producer_report.json"
                if not producer_report_path.exists():
                    errors.append("review producer differs from model generation without a producer report")
                else:
                    producer_report = read_json(producer_report_path)
                    if clean_text(producer_report.get("producer_task_id")) != review_producer:
                        errors.append("review producer does not match producer report")
                    if producer_report.get("model_content_sha256") != model_content_hash(model):
                        errors.append("producer report does not attest to current producer-authored model content")
            if review.get("model_content_sha256") != model_content_hash(model):
                errors.append("review report does not attest to the current producer-authored model content")
            revision_round = review.get("revision_round")
            if isinstance(revision_round, bool) or not isinstance(revision_round, int) or revision_round < 0:
                errors.append("review revision_round must be a nonnegative integer")
            if review.get("blocking_findings"):
                errors.append("review pass cannot retain blocking_findings")
            if len(review.get("checks", [])) < 6:
                errors.append("review pass requires at least six explicit quality checks")
            attribution_text_cache = {}
            for risk in artifact_risks:
                decisions = [item for item in review.get('artifact_attribution_reviews', [])
                             if isinstance(item, dict) and item.get('paper_field') == risk['paper_field']
                             and item.get('field_sha256') == risk['field_sha256']]
                receipt_errors = (attribution_review_errors(model, risk, decisions[0], attribution_text_cache)
                                  if len(decisions) == 1 else ['one bound original-source field review required'])
                if receipt_errors:
                    errors.append('Unresolved project-text attribution in ' + risk['paper_field'])
                    errors.extend(risk['paper_field'] + ': ' + message for message in receipt_errors)
            if project_profile.get("grading_policy") and not any(
                "grading" in str(check.get("id", "")).casefold()
                for check in review.get("checks", [])
                if isinstance(check, dict)
            ):
                errors.append("FTE reviewer report requires an explicit grading check")

    errors.extend(validate_grading(model, project_profile, require_reviewer=require_reviewer))

    claims = model.get("core_claims", [])
    methods = model.get("methods", [])
    sections = model.get("section_guides", [])
    figures = model.get("figures", [])
    questions = model.get("self_test", [])
    bounds = [
        ("core_claims", len(claims), quality["claim_count"].get("min"), quality["claim_count"].get("max")),
        ("section_guides", len(sections), quality["section_count"].get("min"), quality["section_count"].get("max")),
        ("self_test", len(questions), quality["question_count"].get("min"), quality["question_count"].get("max")),
    ]
    if effective_document_mode == "standard":
        bounds.insert(
            1,
            ("methods", len(methods), quality["method_count"].get("min"), quality["method_count"].get("max")),
        )
    for label, count, minimum, maximum in bounds:
        if minimum is not None and count < minimum:
            errors.append(f"{label} requires at least {minimum}; got {count}")
        if maximum is not None and count > maximum:
            errors.append(f"{label} allows at most {maximum}; got {count}")

    all_text_fields: list[tuple[str, str]] = []
    for collection_name in ("core_claims", "methods", "limitations", "project_relations"):
        for item in model.get(collection_name, []):
            owner = f"{collection_name}.{item.get('id')}"
            text_zh = str(item.get("text_zh", ""))
            all_text_fields.append((owner, str(item.get("text", ""))))
            all_text_fields.append((owner, text_zh))
            if not effective_chinese(text_zh):
                errors.append(f"{owner}.text_zh is not substantive Chinese concept reconstruction")
            if semantic_key(text_zh) == semantic_key(str(item.get("text", ""))):
                errors.append(f"{owner}.text_zh copies the source text")
            if item.get("verification_status") == "anchored":
                errors.extend(validate_anchor(owner=owner, anchor=item.get("evidence", {}), stage_dir=stage_dir, extraction=extraction, quality=quality))
            if collection_name in {"core_claims", "methods", "limitations"} and item.get("verification_status") != "anchored":
                errors.append(f"{owner} must be anchored in deep_read_curated mode")
    for item in model.get("controversies", []):
        owner = f"controversies.{item.get('id')}"
        if item.get("verification_status") == "anchored":
            errors.extend(
                validate_anchor(
                    owner=owner,
                    anchor=item.get("evidence", {}),
                    stage_dir=stage_dir,
                    extraction=extraction,
                    quality=quality,
                )
            )
    if not effective_chinese(str(model.get("reading_map", {}).get("research_question", ""))):
        errors.append("reading_map.research_question lacks substantive Chinese reconstruction")
    if word_count(str(model.get("abstract_original", ""))) < quality["quote_word_count"]["min"]:
        errors.append("abstract_original is missing or too short")
    if not effective_chinese(str(model.get("abstract_zh", ""))):
        errors.append("abstract_zh lacks substantive Chinese reconstruction")
    bilingual_sentences: list[tuple[str, dict[str, Any]]] = []
    for section in sections:
        owner = f"section_guides.{section.get('id')}"
        if not effective_chinese(str(section.get("summary_zh", ""))):
            errors.append(f"{owner}.summary_zh lacks substantive Chinese concept reconstruction")
        for sentence in section.get("key_sentences", []):
            if isinstance(sentence, dict):
                bilingual_sentences.append((owner, sentence))
    if len(bilingual_sentences) < quality["bilingual_key_sentence_count"]["min"]:
        errors.append(f"section_guides require at least {quality['bilingual_key_sentence_count']['min']} bilingual key sentences")
    for owner, sentence in bilingual_sentences:
        if not quality["quote_word_count"]["min"] <= word_count(str(sentence.get("en", ""))) <= quality["quote_word_count"]["max"]:
            errors.append(f"{owner} bilingual key sentence EN must contain 8-40 words")
        if not effective_chinese(str(sentence.get("zh", ""))):
            errors.append(f"{owner} bilingual key sentence ZH lacks substantive Chinese reconstruction")
        errors.extend(validate_anchor(owner=f"{owner}.key_sentence", anchor=sentence.get("evidence", {}), stage_dir=stage_dir, extraction=extraction, quality=quality))
    for owner, value in all_text_fields:
        lowered = value.casefold()
        for phrase in quality["known_placeholder_phrases"]:
            if phrase.replace("{title}", model.get("title", "")).casefold() in lowered:
                errors.append(f"{owner} contains known placeholder phrase")
                break
    for method in methods:
        combined = f"{method.get('text', '')} {method.get('text_zh', '')}".casefold()
        if any(phrase.casefold() in combined for phrase in quality["generic_method_phrases"]):
            errors.append(f"methods.{method.get('id')} is a generic method placeholder")
    for left, right in duplicate_pairs(claims):
        errors.append(f"duplicate/near-duplicate Chinese claims: {left}, {right}")
    anchored_claim_pages = [item.get("evidence", {}).get("pdf_page_index") for item in claims if item.get("verification_status") == "anchored"]
    if int(extraction.get("page_count") or 0) > 1 and anchored_claim_pages and set(anchored_claim_pages) == {1}:
        if not clean_text(generation.get("page1_only_justification")):
            errors.append("all anchored claims fall on PDF page 1 without explicit justification")

    if effective_document_mode == "standard":
        substantive_count = generation.get("substantive_figure_count")
        fmin = quality["figure_count"]["min"]
        fmax = quality["figure_count"]["max"]
        all_substantive_allowed = (
            isinstance(substantive_count, int)
            and substantive_count >= 0
            and len(figures) == substantive_count
            and clean_text(generation.get("figure_selection_reason"))
        )
        if not all_substantive_allowed and not fmin <= len(figures) <= fmax:
            errors.append(f"figures must contain {fmin}-{fmax} visual reviews, or all substantive figures with a reason; got {len(figures)}")
        page_count = int(extraction.get("page_count") or 0)
        selection_reason = clean_text(generation.get("figure_selection_reason"))
        declared_pages: list[int] = []
        for pattern in (
            r"PDF\s*第([^页]{1,120})页",
            r"PDF\s+pages?\s+([^.;；。]{1,120})",
        ):
            for match in re.finditer(pattern, selection_reason, flags=re.I):
                declared_pages.extend(
                    int(value) for value in re.findall(r"(?<!\d)(\d{1,4})(?!\d)", match.group(1))
                )
        invalid_pages = sorted({page for page in declared_pages if page < 1 or page > page_count})
        if invalid_pages:
            errors.append(
                "generation.figure_selection_reason declares PDF pages outside the source: "
                f"{invalid_pages}; source page_count={page_count}"
            )
    claim_ids = {item.get("id") for item in claims}
    for figure in figures:
        owner = f"figures.{figure.get('id')}"
        required_schema_fields = [
            "id", "chunk_id", "label", "verification_status", "visual_evidence_path",
            "evidence", "caption_original", "caption_zh", "axes_encoding",
            "direct_observation", "author_interpretation", "alternative_explanations",
            "cannot_conclude", "related_claim_ids", "retrieval_queries_zh", "retrieval_queries_en",
        ]
        missing_figure_fields = [field for field in required_schema_fields if field not in figure]
        if missing_figure_fields:
            errors.append(f"{owner} missing renderer-required fields: {missing_figure_fields}")
        if figure.get("verification_status") != "anchored":
            errors.append(f"{owner}.verification_status must be anchored")
        if figure.get("status") != "visually_reviewed":
            errors.append(f"{owner} must be visually_reviewed in deep_read_curated mode")
        visual_path = Path(str(figure.get("visual_evidence_path", "")))
        if not visual_path.is_absolute():
            visual_path = stage_dir / visual_path
        required_figure_fields = ["axes_encoding", "direct_observation", "author_interpretation", "cannot_conclude"]
        if not visual_path.exists():
            errors.append(f"{owner} visual evidence file does not exist")
        for field in required_figure_fields:
            if not effective_chinese(str(figure.get(field, ""))):
                errors.append(f"{owner}.{field} lacks a specific Chinese visual analysis")
        if not any(effective_chinese(str(value)) for value in figure.get("alternative_explanations", [])):
            errors.append(f"{owner} lacks a substantive alternative explanation")
        related = figure.get("related_claim_ids")
        explicit_no_claim = (related == [] and
                             figure.get("claim_relation_status") == "no_direct_canonical_claim" and
                             effective_chinese(str(figure.get("claim_relation_reason_zh", ""))))
        if not explicit_no_claim and (not isinstance(related, list) or not related or
                                      not set(related).issubset(claim_ids)):
            errors.append(f"{owner} has missing/invalid related_claim_ids")
        errors.extend(validate_anchor(owner=owner, anchor=figure.get("evidence", {}), stage_dir=stage_dir, extraction=extraction, quality=quality))

    for left, right in duplicate_pairs(questions, field="question"):
        errors.append(f"duplicate/near-duplicate self-test questions: {left}, {right}")
    question_ids = [str(question.get("id", "")) for question in questions]
    expected_question_ids = [f"Q{index:02d}" for index in range(1, len(questions) + 1)]
    if question_ids != expected_question_ids:
        errors.append(
            "self_test IDs must be contiguous Q01..QNN in display order; "
            f"got {question_ids}"
        )
    for question in questions:
        owner = f"self_test.{question.get('id')}"
        if not effective_chinese(str(question.get("question", ""))):
            errors.append(f"{owner} is not paper-specific Chinese")
        if not effective_chinese(str(question.get("answer", ""))):
            errors.append(f"{owner}.answer is not substantive Chinese")
        errors.extend(validate_anchor(owner=owner, anchor=question.get("evidence", {}), stage_dir=stage_dir, extraction=extraction, quality=quality))
    required_categories = {"recall", "self_explanation", "evidence_location", "figure_reconstruction", "transfer", "critique"}
    present_categories = {str(question.get("category", "")) for question in questions}
    if not is_adaptive and not required_categories.issubset(present_categories):
        errors.append(f"self_test lacks required learning categories: {sorted(required_categories - present_categories)}")
    if not model.get("limitations"):
        errors.append("deep_read_curated model requires at least one anchored limitation")
    if not model.get("project_relations"):
        errors.append("deep_read_curated model requires at least one project_application item")
    if not is_adaptive and len(model.get("cannot_support", [])) < 2:
        errors.append("deep_read_curated model requires at least two explicit cannot_support boundaries")

    external_required = model.get("priority") == "P0" or any(item.get("substantive") is True for item in model.get("controversies", []))
    external = model.get("external_context", [])
    if external_required:
        emin = quality["external_source_count"]["min"]
        emax = quality["external_source_count"]["max"]
        if model.get("external_research_status") != "completed":
            errors.append("P0/substantive-controversy paper requires completed external research")
        if not emin <= len(external) <= emax:
            errors.append(f"external_context requires {emin}-{emax} verified sources; got {len(external)}")
        roles = {str(source.get("role", "")) for source in external}
        if "background" not in roles:
            errors.append("external research requires a background source")
        if not roles.intersection({"independent_support", "limitation_or_counterevidence"}):
            errors.append("external research requires independent support or limitation/counterevidence")

    checks = {
        "document_mode": effective_document_mode,
        "expected_chapter_count": expected_chapter_count,
        "method_figure_count_policy": "actual_anchored_content" if effective_document_mode == "book_chapter_full" else "standard_bounds",
        "claim_count": len(claims),
        "method_count": len(methods),
        "section_count": len(sections),
        "bilingual_key_sentence_count": len(bilingual_sentences),
        "figure_count": len(figures),
        "question_count": len(questions),
        "external_source_count": len(external),
        "anchored_claim_pages": anchored_claim_pages,
        "review_required": require_reviewer,
        "grading_required": bool(project_profile.get("grading_policy")),
        "grading_priority": model.get("grading", {}).get("assigned_priority"),
        "grading_total_score": model.get("grading", {}).get("total_score"),
        "grading_reviewer_confirmed": model.get("grading", {}).get("reviewer_confirmed"),
        "prompt_version": prompt_version,
        "reader_note_contract_required": reader_note_contract_required,
        "reader_note": reader_note_checks,
        "research_card": research_card_checks,
        "artifact_attribution_risks": artifact_risks,
    }
    if is_adaptive:
        folded = folded_fields(model)
        errors = [error for error in errors if not any(
            error.startswith('reader_note.' + name + '.') for name in folded)]
        checks['content_policy'] = 'adaptive_coverage_v1'
        checks['folded_modules'] = sorted(folded)
        warnings.append('Adaptive content policy is an opt-in candidate; default rollout requires independent evaluation.')
    return errors, warnings, {"checks": checks, "errors": errors, "warnings": warnings}
