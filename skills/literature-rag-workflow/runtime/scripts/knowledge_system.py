#!/usr/bin/env python3
"""CLI for the local FTE/CUSP/SXI research knowledge system."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from knowledge_system_core import (
    build_review,
    capture_work_memory,
    database_path,
    database_status,
    get_chunk,
    load_config,
    promote_external_record,
    run_benchmark,
    search_database,
    stage_external_record,
    sync_database,
)


def emit(value: Any, as_json: bool = False) -> None:
    if as_json or isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print(value)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", help="Knowledge-system JSON config.")
    result.add_argument("--db", help="Override the configured SQLite database path.")
    sub = result.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="Report index and staging status.")
    status.add_argument("--json", action="store_true")

    sync = sub.add_parser("sync", help="Rebuild the derived index atomically.")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--if-stale", action="store_true")
    sync.add_argument("--json", action="store_true")

    search = sub.add_parser("search", help="Search evidence and lower-authority context.")
    search.add_argument("--query", required=True)
    search.add_argument("--domain", default="")
    search.add_argument("--top-k", type=int, default=10)
    search.add_argument("--json", action="store_true")
    search.add_argument("--include-quarantined", action="store_true", help="Audit view; quarantined results never support claims.")

    get = sub.add_parser("get", help="Read one chunk by stable ID.")
    get.add_argument("--chunk-id", required=True)
    get.add_argument("--json", action="store_true")

    capture = sub.add_parser("capture-work", help="Save reusable domain work as context-only memory.")
    capture.add_argument("--project-id", required=True)
    capture.add_argument("--type", required=True, choices=["hypothesis", "decision", "finding", "failure", "checklist"])
    capture.add_argument("--text", required=True)
    capture.add_argument("--domain", default="")
    capture.add_argument("--applicability", default="")
    capture.add_argument("--dataset-version", default="")
    capture.add_argument("--coordinate-context", default="")
    capture.add_argument("--evidence-chunk", action="append", default=[])
    capture.add_argument("--json", action="store_true")

    stage = sub.add_parser("stage-external", help="Save a web result in the unapproved staging layer.")
    stage.add_argument("--query", required=True)
    stage.add_argument("--finding", required=True)
    stage.add_argument("--url", default="")
    stage.add_argument("--doi", default="")
    stage.add_argument("--title", default="")
    stage.add_argument("--role", default="")
    stage.add_argument("--domain", default="")
    stage.add_argument("--origin-project", default="")
    stage.add_argument("--accessed-at", default="")
    stage.add_argument("--evidence-link", action="append", default=[])
    stage.add_argument("--json", action="store_true")

    promote = sub.add_parser("promote", help="Explicitly promote one reviewed external record.")
    promote.add_argument("--record-id", required=True)
    promote.add_argument("--confirmed", action="store_true")
    promote.add_argument("--reason", default="")
    promote.add_argument("--json", action="store_true")

    benchmark = sub.add_parser("benchmark", help="Run the 40-query and adversarial acceptance suite.")
    benchmark.add_argument("--refresh-spec", action="store_true", help="Regenerate the fixed stratified query set.")
    benchmark.add_argument("--json", action="store_true")

    review = sub.add_parser("build-review", help="Build the Chinese human-review HTML entry.")
    review.add_argument("--json", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    config = load_config(args.config)
    db = database_path(config, args.db)
    try:
        if args.command == "status":
            emit(database_status(db), args.json)
        elif args.command == "sync":
            emit(sync_database(config, db_override=db, dry_run=args.dry_run, if_stale=args.if_stale), args.json)
        elif args.command == "search":
            emit(search_database(db, args.query, domain=args.domain, top_k=args.top_k,
                                 include_quarantined=args.include_quarantined), args.json)
        elif args.command == "get":
            value = get_chunk(db, args.chunk_id)
            if value is None:
                raise KeyError(f"chunk not found: {args.chunk_id}")
            emit(value, args.json)
        elif args.command == "capture-work":
            emit(
                capture_work_memory(
                    db,
                    project_id=args.project_id,
                    memory_type=args.type,
                    statement=args.text,
                    domain=args.domain,
                    applicability=args.applicability,
                    dataset_version=args.dataset_version,
                    coordinate_context=args.coordinate_context,
                    evidence_chunk_ids=args.evidence_chunk,
                ),
                args.json,
            )
        elif args.command == "stage-external":
            emit(
                stage_external_record(
                    db,
                    query=args.query,
                    finding=args.finding,
                    url=args.url,
                    doi=args.doi,
                    title=args.title,
                    role=args.role,
                    domain=args.domain,
                    origin_project=args.origin_project,
                    accessed_at=args.accessed_at,
                    evidence_links=args.evidence_link,
                ),
                args.json,
            )
        elif args.command == "promote":
            emit(promote_external_record(db, record_id=args.record_id, confirmed=args.confirmed, reason=args.reason), args.json)
        elif args.command == "benchmark":
            report = run_benchmark(config, db_override=db, refresh_spec=args.refresh_spec)
            emit(report, args.json)
            return 0 if report.get("status") == "pass" else 1
        elif args.command == "build-review":
            review_path = build_review(config, db_override=db)
            emit({"status": "pass", "review_path": str(review_path)}, args.json)
        return 0
    except PermissionError as exc:
        emit({"status": "blocked", "error": str(exc)}, True)
        return 2
    except Exception as exc:
        emit({"status": "error", "error_type": type(exc).__name__, "error": str(exc)}, True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
