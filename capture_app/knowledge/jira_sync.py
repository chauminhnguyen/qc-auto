"""SQLite sync ledger for generated Jira issue notes and comments."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def payload_hash(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def open_jira_ledger(vault_path: Path) -> sqlite3.Connection:
    ledger_path = Path(vault_path) / "Knowledge" / "_state" / "ledger.sqlite"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ledger_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS jira_issues (
        issue_key TEXT PRIMARY KEY,
        updated_at TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        note_path TEXT NOT NULL,
        match_input_hash TEXT NOT NULL,
        config_hash TEXT NOT NULL,
        synced_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS jira_comments (
        issue_key TEXT NOT NULL,
        comment_id TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        deleted INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(issue_key, comment_id))""")
    conn.commit()
    return conn


def get_issue_state(conn: sqlite3.Connection, issue_key: str) -> dict | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM jira_issues WHERE issue_key=?", (issue_key,)).fetchone()
    return dict(row) if row else None


def issue_payload_hash(issue_row: dict, parent_keys: list[str], child_keys: list[str]) -> str:
    return payload_hash({
        "issue": issue_row.get("issue", {}),
        "comments": issue_row.get("comments", []),
        "parent_keys": sorted(set(parent_keys)),
        "child_keys": sorted(set(child_keys)),
    })


def commit_root_sync(conn: sqlite3.Connection, records: list[dict]) -> None:
    """Commit sync state after all notes for a root have been written successfully."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        for record in records:
            key = record["issue_key"]
            conn.execute(
                """INSERT INTO jira_issues
                   (issue_key,updated_at,content_hash,note_path,match_input_hash,config_hash,synced_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(issue_key) DO UPDATE SET
                     updated_at=excluded.updated_at,
                     content_hash=excluded.content_hash,
                     note_path=excluded.note_path,
                     match_input_hash=excluded.match_input_hash,
                     config_hash=excluded.config_hash,
                     synced_at=excluded.synced_at""",
                (key, record.get("updated_at", ""), record["content_hash"],
                 record["note_path"], record["match_input_hash"], record["config_hash"], now),
            )
            current: set[str] = set()
            for comment in record.get("comments", []):
                comment_id = str(comment.get("id", ""))
                if not comment_id:
                    continue
                current.add(comment_id)
                conn.execute(
                    """INSERT INTO jira_comments
                       (issue_key,comment_id,updated_at,content_hash,deleted)
                       VALUES (?,?,?,?,0)
                       ON CONFLICT(issue_key,comment_id) DO UPDATE SET
                         updated_at=excluded.updated_at,
                         content_hash=excluded.content_hash,
                         deleted=0""",
                    (key, comment_id, str(comment.get("updated") or comment.get("created") or ""),
                     payload_hash(comment)),
                )
            previous = conn.execute(
                "SELECT comment_id FROM jira_comments WHERE issue_key=? AND deleted=0", (key,)
            ).fetchall()
            for row in previous:
                if row[0] not in current:
                    conn.execute(
                        "UPDATE jira_comments SET deleted=1 WHERE issue_key=? AND comment_id=?",
                        (key, row[0]),
                    )
