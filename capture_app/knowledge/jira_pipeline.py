"""Fetch Jira issues, match each ticket, and write a connected note graph."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
import sqlite3

from .jira import (
    JiraClient,
    JiraError,
    format_jira_issue,
    jira_issue_text,
    normalize_issue_key,
)
from .jira_config import JiraIngestConfig
from .jira_sync import (
    commit_root_sync,
    get_issue_state,
    issue_payload_hash,
    open_jira_ledger,
    payload_hash,
)
from .retrieval import index_vault, rank_issue_nodes, similar_jira_tickets


def _updated_at(row: dict) -> str:
    return str(((row.get("issue", {}).get("fields") or {}).get("updated")) or "")


def _merge_rows(results: dict[str, dict]) -> tuple[dict[str, dict], dict[str, set[str]], dict[str, set[str]]]:
    """Deduplicate issue keys while retaining all root membership and parent links."""
    rows_by_key: dict[str, dict] = {}
    root_members: dict[str, set[str]] = defaultdict(set)
    parents_by_key: dict[str, set[str]] = defaultdict(set)
    for root, result in results.items():
        for row in result.get("issues", []):
            key = str(row["key"]).upper()
            root_members[root].add(key)
            existing = rows_by_key.get(key)
            if existing is None or _updated_at(row) >= _updated_at(existing):
                rows_by_key[key] = dict(row)
            parents = row.get("parent_keys") or ([row["parent_key"]] if row.get("parent_key") else [])
            parents_by_key[key].update(str(parent).upper() for parent in parents if parent)
    return rows_by_key, root_members, parents_by_key


def _print_match_report(issue_key: str, matches: list[dict]) -> None:
    if not matches:
        print(f"  {issue_key}: no confident match")
        return
    print(f"  {issue_key}: {len(matches)} related node(s)")
    for rank, match in enumerate(matches, 1):
        evidence = ", ".join(match.get("matched_anchors", []) or match.get("matched_terms", []))
        print(
            f"    {rank}. {match['path']} score={match['score']:.3f} "
            f"(anchor={match['anchor_part']:.3f}, BM25={match['bm25_part']:.3f}, "
            f"graph={match['graph_part']:.3f}) evidence={evidence or '(none)'}"
        )
        if match.get("snippet"):
            print(f"       {match['snippet']}")


def ingest_jira_roots(vault_path: Path, roots: list[str],
                      config: JiraIngestConfig | None = None) -> dict:
    config = config or JiraIngestConfig.load()
    vault_path = Path(vault_path).resolve()
    normalized_roots = list(dict.fromkeys(normalize_issue_key(root) for root in roots))
    written: list[dict] = []
    errors: list[dict] = []
    try:
        client = JiraClient.from_env(max_issues=config.max_issues)
    except JiraError as error:
        return {"written": written,
                "errors": [{"root": root, "error": str(error)} for root in normalized_roots]}

    fetched: dict[str, dict] = {}
    truncation: dict[str, bool] = {}
    for root in normalized_roots:
        try:
            result = client.fetch(
                [root], depth=config.depth, include_bugs=config.include_bugs,
                link_types=list(config.link_types), include_noise=True,
            )
            fetched[root] = result
            truncation[root] = bool(result.get("truncated"))
        except JiraError as error:
            errors.append({"root": root, "error": str(error)})
            print(f"Jira {root}: ERROR: {error}")
    if not fetched:
        return {"written": written, "errors": errors}

    rows_by_key, root_members, discovered_parents = _merge_rows(fetched)
    issue_keys = set(rows_by_key)
    parent_map: dict[str, list[str]] = {}
    child_map: dict[str, set[str]] = defaultdict(set)
    for key in issue_keys:
        parents = sorted(parent for parent in discovered_parents.get(key, set()) if parent in issue_keys)
        parent_map[key] = parents
        for parent in parents:
            child_map[parent].add(key)

    try:
        index_stats = index_vault(vault_path, chunk_chars=config.chunk_chars)
        ledger = open_jira_ledger(vault_path)
    except (OSError, ValueError, RuntimeError) as error:
        for root in fetched:
            errors.append({"root": root, "error": f"Could not prepare Jira retrieval index: {error}"})
        return {"written": written, "errors": errors}

    try:
        config_hash = payload_hash(asdict(config))
        indexed_rows = ledger.execute(
            "SELECT path,content_hash FROM vault_files WHERE path NOT LIKE 'Jira/%' ORDER BY path"
        ).fetchall()
        vault_hash = payload_hash(indexed_rows)
        changed_records: dict[str, dict] = {}
        note_contents: dict[str, str] = {}
        note_paths: dict[str, str] = {}
        write_errors: dict[str, str] = {}

        for key in sorted(issue_keys):
            row = rows_by_key[key]
            parents = parent_map.get(key, [])
            children = sorted(child_map.get(key, set()))
            source_hash = issue_payload_hash(row, parents, children)
            text = jira_issue_text(row, include_noise=not config.filter_noise)
            match_input_hash = payload_hash({
                "issue_hash": source_hash,
                "config_hash": config_hash,
                "vault_hash": vault_hash,
                "parents": parents,
                "children": children,
            })
            note_path = f"Jira/{key}.md"
            prior = get_issue_state(ledger, key)
            if (prior and prior["content_hash"] == source_hash
                    and prior["match_input_hash"] == match_input_hash
                    and (vault_path / note_path).is_file()):
                note_paths[key] = note_path
                continue

            matches = rank_issue_nodes(
                vault_path, text, config, exclude_path=note_path,
                ledger_path=vault_path / "Knowledge" / "_state" / "ledger.sqlite",
                refresh_index=False,
            )
            similar = similar_jira_tickets(
                vault_path, text, exclude_keys=issue_keys, limit=5,
                ledger_path=vault_path / "Knowledge" / "_state" / "ledger.sqlite",
            )
            _print_match_report(key, matches)
            note_contents[key] = format_jira_issue(
                row, parent_keys=parents, child_keys=children,
                related_nodes=matches, similar_tickets=similar,
                include_noise=not config.filter_noise,
            )
            note_paths[key] = note_path
            changed_records[key] = {
                "issue_key": key,
                "updated_at": _updated_at(row),
                "content_hash": source_hash,
                "note_path": note_path,
                "match_input_hash": match_input_hash,
                "config_hash": config_hash,
                "comments": row.get("comments", []),
            }

        from capture_app.storage.vault import VaultWriter
        writer = VaultWriter(vault_path)
        try:
            for key, markdown in note_contents.items():
                try:
                    writer.write_jira_note(key, markdown)
                except (OSError, ValueError) as error:
                    write_errors[key] = str(error)
                    print(f"Jira {key}: ERROR writing note: {error}")
        finally:
            writer.close()

        index_failed = False
        successful_writes = set(note_contents) - set(write_errors)
        if successful_writes:
            # Index the newly written Jira notes for future similar-ticket searches.
            try:
                index_vault(vault_path, chunk_chars=config.chunk_chars)
            except (OSError, ValueError, sqlite3.Error) as error:
                for root in fetched:
                    errors.append({"root": root, "error": f"Could not refresh Jira retrieval index: {error}"})
                index_failed = True

        for root, result in fetched.items():
            affected_write_errors = [key for key in root_members[root] if key in write_errors]
            if affected_write_errors:
                for key in affected_write_errors:
                    errors.append({"root": root, "error": f"{key}: {write_errors[key]}"})
            elif not index_failed:
                records = [changed_records[key] for key in sorted(root_members[root])
                           if key in changed_records]
                if records:
                    try:
                        commit_root_sync(ledger, records)
                    except sqlite3.Error as error:
                        errors.append({"root": root, "error": f"Could not commit Jira sync state: {error}"})
            issue_count = len(root_members[root])
            comment_count = sum(len(rows_by_key[key].get("comments", [])) for key in root_members[root])
            root_notes = [note_paths[key] for key in sorted(root_members[root]) if key in note_paths]
            written.append({
                "root": root,
                "issues": issue_count,
                "comments_fetched": comment_count,
                "notes_written": sum(key in note_contents and key not in write_errors for key in root_members[root]),
                "notes_unchanged": sum(key not in note_contents for key in root_members[root]),
                "notes": root_notes,
                "truncated": truncation[root],
                "max_issues": config.max_issues,
            })
            if truncation[root]:
                print(f"WARNING: Jira traversal for {root} reached the {config.max_issues} issue cap; results may be incomplete.")
            print(f"Jira {root}: fetched {issue_count} issue(s) and {comment_count} comment(s); "
                  f"wrote {len(root_notes)} note path(s)")
    finally:
        ledger.close()
    return {"written": written, "errors": errors,
            "index": index_stats}
