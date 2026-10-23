"""CLI for Knowledge ingestion plus reviewed ticket-driven contract changes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .jira import normalize_issue_key
from .jira_config import JiraIngestConfig


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Ingest QC notes and manage reviewed API/parameter contract changes")
    commands = root.add_subparsers(dest="command", required=True)

    ingest_cmd = commands.add_parser("ingest", help="ingest raw notes and/or retrieve Jira tickets")
    ingest_cmd.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    ingest_cmd.add_argument("--notes", type=Path, help="Notes-raw folder or a single .md/.txt file")
    ingest_cmd.add_argument("--jira", action="append", default=[], metavar="KEY_OR_URL",
                            help="Jira issue key or URL; may be repeated")
    ingest_cmd.add_argument("--provider", choices=("local", "openrouter", "claude"),
                            help="generative provider used by the --notes workflow")
    ingest_cmd.add_argument("--model", help="OpenRouter model override")
    ingest_cmd.add_argument("--local-url", help="OpenAI-compatible local chat completions URL")
    ingest_cmd.add_argument("--local-model", help="Local model identifier")
    ingest_cmd.add_argument("--claude-model", help="Claude model override (defaults to ANTHROPIC_MODEL or claude-sonnet-5-5)")
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
    propose_cmd.add_argument("--provider", choices=("local", "openrouter", "claude"), required=True)
    propose_cmd.add_argument("--model")
    propose_cmd.add_argument("--local-url")
    propose_cmd.add_argument("--local-model")
    propose_cmd.add_argument("--claude-model")
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
    root = parser()
    args = root.parse_args(argv)
    if args.command == "ingest":
        try:
            validate_ingest_sources(args.notes, args.jira, args.provider)
            jira_roots = [normalize_issue_key(value) for value in args.jira]
            if args.notes and not args.notes.exists():
                raise ValueError(f"Notes source does not exist: {args.notes}")
            jira_config = JiraIngestConfig.load() if jira_roots else None
        except ValueError as error:
            root.error(str(error))
        jira_result = {"written": [], "errors": []}
        if jira_roots:
            try:
                from .jira_pipeline import ingest_jira_roots
                jira_result = ingest_jira_roots(args.vault, jira_roots, jira_config)
            except Exception as error:
                root.error(str(error))
        notes_result = None
        if args.notes:
            from .llm import provider_from_args
            from .service import ingest
            provider = provider_from_args(args.provider, args.model, args.local_url,
                                          args.local_model, args.claude_model)
            notes_result = ingest(args.vault, args.notes, provider, force=args.force)
        try:
            from capture_app.chat.ingest import refresh_index
            refresh_index(args.vault)
        except Exception as error:
            print(f"WARNING: chat index refresh failed after knowledge ingest: {error}")
        result = {"jira": jira_result, "notes": notes_result}
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 1 if jira_result.get("errors") else 0
    elif args.command == "review":
        from .service import review_inbox
        result = review_inbox(args.vault, reviewer=args.reviewer)
    elif args.command == "report":
        from .service import build_reports
        result = build_reports(args.vault, include_third_party=args.include_third_party)
    elif args.command == "validate":
        from .validation import validate_vault, write_validation_report
        result = validate_vault(args.vault)
        if args.write_report:
            result["report"] = str(write_validation_report(args.vault, result))
    elif args.command == "migrate-requirement-ids":
        from .validation import migrate_requirement_ids
        result = {"requirements_migrated": migrate_requirement_ids(args.vault)}
    elif args.command == "propose-change":
        from .changes import propose
        note = args.note if args.note is not None else args.note_file.read_text(encoding="utf-8")
        result = propose(args.vault, note, args.ticket_id, args.provider, args.model,
                         args.local_url, args.local_model,
                         claude_model=args.claude_model,
                         include_third_party=args.include_third_party)
    elif args.command == "list-changes":
        from .changes import list_changes
        result = list_changes(args.vault, args.status)
    elif args.command == "show-change":
        from .changes import get_change
        result = get_change(args.vault, args.proposal_id)
    elif args.command == "edit-change":
        from .changes import edit_change
        changes = json.loads(args.changes_file.read_text(encoding="utf-8"))
        if not isinstance(changes, list):
            raise ValueError("--changes-file must contain a JSON array")
        result = edit_change(args.vault, args.proposal_id, changes, args.reviewer, args.rationale,
                             args.resolve_question)
    elif args.command == "review-change":
        from .changes import review_change
        result = review_change(args.vault, args.proposal_id, args.decision, args.reviewer,
                               args.rationale, args.po_approved_by)
    elif args.command == "apply-change":
        from .changes import apply_change
        result = apply_change(args.vault, args.proposal_id, args.reviewer, args.rationale)
    else:
        raise ValueError(f"Unsupported command: {args.command}")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if args.command == "validate" and result.get("finding_count", 0):
        return 1
    return 0


def validate_ingest_sources(notes: Path | None, jira: list[str] | None,
                            provider: str | None = None) -> None:
    """Validate source/provider combinations before any ingest side effects."""
    if notes is None and not jira:
        raise ValueError("provide --notes and/or --jira")
    if notes is not None and provider is None:
        raise ValueError("--provider is required when using --notes")
    if notes is None and jira and provider is not None:
        raise ValueError("Jira-only ingestion does not accept --provider; configure matching in config.yaml")


if __name__ == "__main__":
    raise SystemExit(main())
