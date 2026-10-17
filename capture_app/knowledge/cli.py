"""CLI for Knowledge ingestion plus reviewed ticket-driven contract changes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .changes import apply_change, edit_change, get_change, list_changes, propose, review_change
from .llm import provider_from_args
from .service import build_reports, ingest, review_inbox
from .validation import migrate_requirement_ids, validate_vault, write_validation_report


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Ingest QC notes and manage reviewed API/parameter contract changes")
    commands = root.add_subparsers(dest="command", required=True)

    ingest_cmd = commands.add_parser("ingest", help="convert raw notes into reviewed-ready Knowledge drafts")
    ingest_cmd.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    ingest_cmd.add_argument("--notes", type=Path, required=True, help="Notes-raw folder or a single .md/.txt file")
    ingest_cmd.add_argument("--provider", choices=("local", "openrouter"), required=True)
    ingest_cmd.add_argument("--model", help="OpenRouter model override")
    ingest_cmd.add_argument("--local-url", help="OpenAI-compatible local chat completions URL")
    ingest_cmd.add_argument("--local-model", help="Local model identifier")
    ingest_cmd.add_argument("--force", action="store_true", help="reprocess unchanged note blocks")

    review_cmd = commands.add_parser("review", help="approve drafts into Knowledge/Reviewed")
    review_cmd.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    review_cmd.add_argument("--reviewer", help="reviewer name for entries explicitly confirmed")

    report_cmd = commands.add_parser("report", help="write BA gap, stale-link, and vault validation reports")
    report_cmd.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    report_cmd.add_argument("--include-third-party", action="store_true",
                             help="include captured API entities outside the site's primary domain")

    validate_cmd = commands.add_parser("validate", help="validate links and Knowledge/BRD invariants")
    validate_cmd.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    validate_cmd.add_argument("--write-report", action="store_true",
                              help="write Knowledge/Reports/vault-validation.md")

    migrate_cmd = commands.add_parser("migrate-requirement-ids", help="namespace BRD requirement IDs by source document")
    migrate_cmd.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))

    propose_cmd = commands.add_parser("propose-change", help="analyze a ticket/error note and save a reviewable API/parameter proposal")
    propose_cmd.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    source = propose_cmd.add_mutually_exclusive_group(required=True)
    source.add_argument("--note", help="ticket/error note text")
    source.add_argument("--note-file", type=Path, help="UTF-8 text or Markdown note file")
    propose_cmd.add_argument("--ticket-id", default="")
    propose_cmd.add_argument("--provider", choices=("local", "openrouter"), required=True)
    propose_cmd.add_argument("--model")
    propose_cmd.add_argument("--local-url")
    propose_cmd.add_argument("--local-model")
    propose_cmd.add_argument("--include-third-party", action="store_true",
                             help="allow third-party captured APIs in proposal context")

    list_cmd = commands.add_parser("list-changes", help="list ticket-driven contract proposals")
    list_cmd.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    list_cmd.add_argument("--status")

    show_cmd = commands.add_parser("show-change", help="show a proposal and its traceability")
    show_cmd.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    show_cmd.add_argument("proposal_id")

    edit_cmd = commands.add_parser("edit-change", help="replace proposal changes with a reviewer-edited JSON patch")
    edit_cmd.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    edit_cmd.add_argument("proposal_id")
    edit_cmd.add_argument("--changes-file", type=Path, required=True, help="UTF-8 JSON array of proposed changes")
    edit_cmd.add_argument("--reviewer", required=True)
    edit_cmd.add_argument("--rationale", required=True)
    edit_cmd.add_argument("--resolve-question", action="append", default=[], help="exact open question resolved by this edit; repeat as needed")

    review_cmd = commands.add_parser("review-change", help="approve, reject, or request information on a proposal")
    review_cmd.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    review_cmd.add_argument("proposal_id")
    review_cmd.add_argument("--decision", choices=("approve", "reject", "needs_info"), required=True)
    review_cmd.add_argument("--reviewer", required=True)
    review_cmd.add_argument("--rationale", required=True)
    review_cmd.add_argument("--po-approved-by", help="authorized PO resolving unverified/conflicting requirement behavior")

    apply_cmd = commands.add_parser("apply-change", help="apply an approved expected-contract proposal")
    apply_cmd.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    apply_cmd.add_argument("proposal_id")
    apply_cmd.add_argument("--reviewer", required=True)
    apply_cmd.add_argument("--rationale", required=True)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "ingest":
        provider = provider_from_args(args.provider, args.model, args.local_url, args.local_model)
        result = ingest(args.vault, args.notes, provider, force=args.force)
    elif args.command == "review":
        result = review_inbox(args.vault, reviewer=args.reviewer)
    elif args.command == "report":
        result = build_reports(args.vault, include_third_party=args.include_third_party)
    elif args.command == "validate":
        result = validate_vault(args.vault)
        if args.write_report:
            result["report"] = str(write_validation_report(args.vault, result))
    elif args.command == "migrate-requirement-ids":
        result = {"requirements_migrated": migrate_requirement_ids(args.vault)}
    elif args.command == "propose-change":
        note = args.note if args.note is not None else args.note_file.read_text(encoding="utf-8")
        result = propose(args.vault, note, args.ticket_id, args.provider, args.model,
                         args.local_url, args.local_model,
                         include_third_party=args.include_third_party)
    elif args.command == "list-changes":
        result = list_changes(args.vault, args.status)
    elif args.command == "show-change":
        result = get_change(args.vault, args.proposal_id)
    elif args.command == "edit-change":
        changes = json.loads(args.changes_file.read_text(encoding="utf-8"))
        if not isinstance(changes, list):
            raise ValueError("--changes-file must contain a JSON array")
        result = edit_change(args.vault, args.proposal_id, changes, args.reviewer, args.rationale,
                             args.resolve_question)
    elif args.command == "review-change":
        result = review_change(args.vault, args.proposal_id, args.decision, args.reviewer,
                               args.rationale, args.po_approved_by)
    elif args.command == "apply-change":
        result = apply_change(args.vault, args.proposal_id, args.reviewer, args.rationale)
    else:
        raise ValueError(f"Unsupported command: {args.command}")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if args.command == "validate" and result.get("finding_count", 0):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
