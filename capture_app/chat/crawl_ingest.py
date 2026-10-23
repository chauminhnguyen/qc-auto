from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import ChatDB


def _chunk_id(source_key: str) -> str:
    return hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:24]


def _content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _decode_json(value: Any, field: str, row_id: Any) -> Any:
    try:
        return json.loads(value or "[]")
    except (TypeError, ValueError) as error:
        raise ValueError(f"Malformed {field} JSON in ui_transitions row {row_id}") from error


def _render_steps(steps: list[dict[str, Any]]) -> list[str]:
    rendered = []
    for index, step in enumerate(steps, start=1):
        kind = str(step.get("kind") or "action")
        role = str(step.get("role") or "").strip()
        name = str(step.get("name") or "").strip()
        value = step.get("value")
        duplicate_index = int(step.get("dup_index") or 0)
        duplicate_note = f" (item {duplicate_index + 1})" if duplicate_index else ""
        target = f"{role} {name}".strip()
        if kind == "goto":
            description = f"Navigate to {name or value}"
        elif kind == "fill":
            description = f"Fill {target}{duplicate_note} with {value!r}"
        elif kind == "select":
            description = f"Select {value!r} in {target}{duplicate_note}"
        elif kind == "set":
            description = f"Set {target}{duplicate_note} to {value!r}"
        else:
            description = f"{kind.title()} {target}{duplicate_note}".strip()
            if value not in (None, ""):
                description += f" with value {value!r}"
        rendered.append(f"{index}. {description}")
    return rendered


def _insert_transition(db: ChatDB, row: sqlite3.Row) -> None:
    transition_id = row["id"]
    steps = _decode_json(row["path_json"], "path", transition_id)
    apis = _decode_json(row["triggered_apis_json"], "triggered APIs", transition_id)
    if not isinstance(steps, list) or not isinstance(apis, list):
        raise ValueError(f"Invalid transition data shape in ui_transitions row {transition_id}")
    steps = [
        *steps,
        {
            "kind": row["action_kind"],
            "role": row["action_role"],
            "name": row["action_name"],
            "value": row["action_value"],
            "dup_index": row["action_dup_index"],
        },
    ]

    action = " ".join(
        str(row[key]).strip()
        for key in ("action_kind", "action_role", "action_name")
        if row[key]
    )
    if row["action_value"] not in (None, ""):
        action += f" with value {row['action_value']!r}"
    duplicate_index = int(row["action_dup_index"] or 0)
    if duplicate_index:
        action += f" (item {duplicate_index + 1})"

    api_lines = []
    api_aliases = []
    linked_entities = []
    for api in apis:
        method = str(api.get("method") or "")
        url = str(api.get("url") or "")
        ref_stem = str(api.get("ref_stem") or "")
        status = api.get("status")
        api_lines.append(f"- {method} {url}; status={status}; ref={ref_stem}")
        api_aliases.extend(value for value in (ref_stem, url, f"{method} {url}") if value)
        if ref_stem:
            linked_entities.append(ref_stem)

    ok = bool(row["ok"])
    body_lines = [
        f"Title: UI transition {row['crawl_id']} #{row['seq']}",
        "Type: crawler-transition",
        f"Feature: {row['domain'] or row['start_url'] or 'UI crawl'}",
        f"Status: {'succeeded' if ok else 'failed'}",
        f"Crawl ID: {row['crawl_id']}",
        f"Transition sequence: {row['seq']}",
        f"Start URL: {row['start_url'] or ''}",
        f"From UI state: {row['from_fp'] or ''}",
        f"To UI state: {row['to_fp'] or ''}",
        f"Destination URL: {row['to_url'] or ''}",
        f"Action: {action or row['action_label'] or 'unknown'}",
        f"Outcome detail: {row['detail'] or 'none'}",
        "Action path:",
        *(_render_steps(steps) or ["(no action path recorded)"]),
        "Triggered APIs:",
        *(api_lines or ["- none recorded"]),
    ]
    body = "\n".join(body_lines)
    source_path = f"ui_transitions/{row['crawl_id']}/{transition_id}"
    title = f"UI transition {row['crawl_id']} #{row['seq']}"
    feature = str(row["domain"] or row["start_url"] or "UI crawl")
    db.upsert_chunk({
        "id": _chunk_id(f"ui-transition:{transition_id}"),
        "source_path": source_path,
        "note_type": "crawler-transition",
        "title": title,
        "feature": feature,
        "status": "succeeded" if ok else "failed",
        "owner": "crawler",
        "last_verified": None,
        "confirmed_by": [],
        "linked_entities": linked_entities,
        "content_hash": _content_hash(body),
        "body": body,
    })
    for alias in {title, feature, str(row["crawl_id"]), *api_aliases}:
        if alias:
            db.add_alias(alias, source_path, "crawler-transition", source_path)


def index_ui_transitions(vault_path: str | Path, chat_db: ChatDB) -> int:
    """Copy persisted crawler paths into chat search without writing to capture DB."""
    capture_db_path = Path(vault_path).resolve() / ".capture_index" / "index.sqlite"
    if not capture_db_path.is_file():
        _prune_ui_transitions(chat_db, set())
        return 0
    uri = capture_db_path.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as source:
        source.row_factory = sqlite3.Row
        try:
            exists = source.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ui_transitions'"
            ).fetchone()
            if exists is None:
                _prune_ui_transitions(chat_db, set())
                return 0
            columns = {
                row["name"] for row in source.execute("PRAGMA table_info(ui_transitions)")
            }
            duplicate_index = (
                "COALESCE(action_dup_index, 0)"
                if "action_dup_index" in columns
                else "0"
            )
            query = f"""
                SELECT id, crawl_id, seq, from_fp, to_fp, to_url,
                       action_kind, action_role, action_name, action_value,
                       action_label, {duplicate_index} AS action_dup_index,
                       path_json, triggered_apis_json, ok, detail, ts,
                       start_url, domain
                FROM ui_transitions
                ORDER BY ts, id
            """
            rows = source.execute(query).fetchall()
        except sqlite3.Error as error:
            raise RuntimeError(f"Unable to read crawler transitions from {capture_db_path}: {error}") from error
    for row in rows:
        _insert_transition(chat_db, row)
    _prune_ui_transitions(chat_db, {
        f"ui_transitions/{row['crawl_id']}/{row['id']}" for row in rows
    })
    return len(rows)


def _prune_ui_transitions(chat_db: ChatDB, expected: set[str]) -> None:
    existing = chat_db.conn.execute(
        "SELECT id,path FROM files WHERE path LIKE 'ui_transitions/%'"
    ).fetchall()
    for row in existing:
        if row["path"] not in expected:
            chat_db.conn.execute("DELETE FROM chunks WHERE file_id=?", (row["id"],))
            chat_db.conn.execute("DELETE FROM files WHERE id=?", (row["id"],))


def index_live_runner_run(
    vault_path: str | Path,
    *,
    run_id: str,
    session_id: str,
    transitions: list[Any],
    discoveries: list[dict[str, Any]],
    summary: dict[str, Any],
    report_text: str,
) -> int:
    """Persist live_runner's pre-visualization findings in the separate chat DB."""
    vault = Path(vault_path).resolve()
    db = ChatDB(vault / ".chat_index" / "index.sqlite")
    inserted = 0
    try:
        for seq, transition in enumerate(transitions, start=1):
            action = transition.action
            api_lines = []
            aliases = set()
            for api in transition.apis:
                method = str(api.get("method") or "")
                url = str(api.get("url") or "")
                status = api.get("status")
                api_lines.append(f"- {method} {url}; status={status}")
                aliases.update(value for value in (url, f"{method} {url}") if value)
            path_lines = _render_steps([
                {
                    "kind": step.kind,
                    "role": step.role,
                    "name": step.name,
                    "value": step.value,
                    "dup_index": getattr(step, "dup_index", 0),
                }
                for step in [*transition.path, action]
            ])
            action_label = getattr(action, "label", str(action.name))
            title = f"Live runner {run_id} transition {seq}: {action_label}"
            body = "\n".join([
                f"Title: {title}",
                "Type: live-run-transition",
                "Feature: live crawler",
                f"Status: {'succeeded' if transition.ok else 'failed'}",
                f"Session: {session_id}",
                f"From UI state: {transition.from_fp}",
                f"To UI state: {transition.to_fp or ''}",
                f"Destination URL: {transition.to_url or ''}",
                f"Action: {action_label}",
                f"Outcome detail: {transition.detail or 'none'}",
                "Action path:",
                *(path_lines or ["(no action path recorded)"]),
                "Triggered APIs:",
                *(api_lines or ["- none recorded"]),
            ])
            source_path = f"live_runner/{run_id}/transition-{seq}"
            db.upsert_chunk({
                "id": _chunk_id(f"live-run:{run_id}:transition:{seq}"),
                "source_path": source_path,
                "note_type": "live-run-transition",
                "title": title,
                "feature": "live crawler",
                "status": "succeeded" if transition.ok else "failed",
                "owner": "crawler",
                "last_verified": datetime.now(timezone.utc).isoformat(),
                "confirmed_by": [],
                "linked_entities": [],
                "content_hash": _content_hash(body),
                "body": body,
            })
            for alias in {title, action_label, *aliases}:
                if alias:
                    db.add_alias(alias, source_path, "live-run-transition", source_path)
            inserted += 1

        discovery_lines = [
            f"- {item.get('action_label')} -> {item.get('api_method')} "
            f"{item.get('api_url')} (status {item.get('api_status')})"
            for item in discoveries
        ]
        summary_body = "\n".join([
            f"Title: Live runner summary {run_id}",
            "Type: live-run-summary",
            "Feature: live crawl results",
            "Status: completed",
            f"Session: {session_id}",
            *(f"{key}: {value}" for key, value in sorted(summary.items())),
            "Discoveries (API calls not in the standard flow):",
            *(discovery_lines or ["- none"]),
        ])
        summary_path = f"live_runner/{run_id}/summary"
        db.upsert_chunk({
            "id": _chunk_id(f"live-run:{run_id}:summary"),
            "source_path": summary_path,
            "note_type": "live-run-summary",
            "title": f"Live runner summary {run_id}",
            "feature": "live crawl results",
            "status": "completed",
            "owner": "crawler",
            "last_verified": datetime.now(timezone.utc).isoformat(),
            "confirmed_by": [],
            "linked_entities": [],
            "content_hash": _content_hash(summary_body),
            "body": summary_body,
        })
        for item in discoveries:
            for alias in (item.get("action_label"), item.get("api_url"), item.get("api_method")):
                if alias:
                    db.add_alias(alias, summary_path, "live-discovery", summary_path)
        inserted += 1

        if report_text.strip():
            report_path = f"live_runner/{run_id}/report"
            report_body = "\n".join([
                f"Title: Live runner diagnostic report {run_id}",
                "Type: live-run-report",
                "Feature: live crawl results",
                "Status: generated",
                f"Session: {session_id}",
                report_text.strip(),
            ])
            db.upsert_chunk({
                "id": _chunk_id(f"live-run:{run_id}:report"),
                "source_path": report_path,
                "note_type": "live-run-report",
                "title": f"Live runner diagnostic report {run_id}",
                "feature": "live crawl results",
                "status": "generated",
                "owner": "crawler",
                "last_verified": datetime.now(timezone.utc).isoformat(),
                "confirmed_by": [],
                "linked_entities": [],
                "content_hash": _content_hash(report_body),
                "body": report_body,
            })
            inserted += 1
    finally:
        db.conn.commit()
        db.close()
    return inserted
