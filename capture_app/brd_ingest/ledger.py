"""BRD-specific persistence, checkpoints, and review state."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BrdLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._initialize()

    def _initialize(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS inventory (
          source_path TEXT PRIMARY KEY, file_hash TEXT NOT NULL, file_name TEXT NOT NULL,
          ext TEXT NOT NULL, tier TEXT NOT NULL, score INTEGER NOT NULL,
          reason_codes TEXT, pages INTEGER, metadata TEXT NOT NULL DEFAULT '{}',
          first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', raw_note_path TEXT
        );
        CREATE TABLE IF NOT EXISTS pages (
          id TEXT PRIMARY KEY, source_path TEXT NOT NULL, file_hash TEXT NOT NULL,
          page_number INTEGER NOT NULL, section_id TEXT, section_title TEXT,
          page_text_md TEXT, image_count INTEGER DEFAULT 0, table_count INTEGER DEFAULT 0,
          image_dominant INTEGER DEFAULT 0, repeated_header_footer TEXT, unique_hash TEXT,
          FOREIGN KEY(source_path) REFERENCES inventory(source_path)
        );
        CREATE TABLE IF NOT EXISTS sections (
          id TEXT PRIMARY KEY, source_path TEXT NOT NULL, file_hash TEXT NOT NULL,
          section_title TEXT NOT NULL, order_index INTEGER NOT NULL,
          page_start INTEGER NOT NULL, page_end INTEGER NOT NULL, section_hash TEXT NOT NULL,
          raw_note_path TEXT, status TEXT NOT NULL DEFAULT 'raw'
        );
        CREATE TABLE IF NOT EXISTS assets (
          id TEXT PRIMARY KEY, source_path TEXT NOT NULL, page_id TEXT,
          asset_type TEXT NOT NULL, relative_path TEXT NOT NULL, original_hash TEXT NOT NULL,
          dedupe_key TEXT, caption TEXT, width INTEGER, height INTEGER,
          has_figure_text INTEGER DEFAULT 0,
          FOREIGN KEY(source_path) REFERENCES inventory(source_path)
        );
        CREATE TABLE IF NOT EXISTS entries (
          id TEXT PRIMARY KEY, source_path TEXT NOT NULL, section_id TEXT,
          entry_type TEXT NOT NULL, title TEXT NOT NULL, content_json TEXT NOT NULL,
          source_anchor_json TEXT, status TEXT NOT NULL DEFAULT 'unverified',
          confidence REAL, version TEXT, hash TEXT NOT NULL, created_at TEXT NOT NULL,
          superseded_by TEXT
        );
        CREATE TABLE IF NOT EXISTS ledger (
          id INTEGER PRIMARY KEY AUTOINCREMENT, source_path TEXT NOT NULL,
          phase TEXT NOT NULL, step TEXT NOT NULL, status TEXT NOT NULL,
          message TEXT, created_at TEXT NOT NULL, details_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_entries_source ON entries(source_path, section_id);
        CREATE INDEX IF NOT EXISTS idx_pages_source ON pages(source_path, file_hash);
        """)
        # Upgrade databases created by the initial triage-only implementation.
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(inventory)")}
        if "raw_note_path" not in cols:
            self.conn.execute("ALTER TABLE inventory ADD COLUMN raw_note_path TEXT")
        self.conn.commit()

    def record(self, source: str, phase: str, step: str, status: str,
               message: str = "", details: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            "INSERT INTO ledger(source_path,phase,step,status,message,created_at,details_json) VALUES(?,?,?,?,?,?,?)",
            (source, phase, step, status, message, _now(), json.dumps(details or {}, ensure_ascii=False)),
        )
        self.conn.commit()

    def upsert_inventory(self, profile: Any) -> None:
        now = _now()
        self.conn.execute("""INSERT INTO inventory
          (source_path,file_hash,file_name,ext,tier,score,reason_codes,pages,metadata,first_seen,last_seen,status)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(source_path) DO UPDATE SET file_hash=excluded.file_hash,
          file_name=excluded.file_name,ext=excluded.ext,tier=excluded.tier,score=excluded.score,
          reason_codes=excluded.reason_codes,pages=excluded.pages,metadata=excluded.metadata,
          last_seen=excluded.last_seen,status=CASE WHEN inventory.file_hash!=excluded.file_hash THEN excluded.status
          WHEN excluded.status='manual' AND inventory.status NOT IN ('approved','rejected') THEN 'manual'
          ELSE inventory.status END""",
          (profile.source_path, profile.file_hash, profile.file_name, profile.ext, profile.tier,
           profile.score, json.dumps(profile.reason_codes), profile.pages,
           json.dumps(profile.metadata or {}, ensure_ascii=False), now, now, "manual" if profile.tier == "C" else "pending"))
        self.conn.commit()

    def inventory_row(self, source: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM inventory WHERE source_path=?", (source,)).fetchone()

    def set_review(self, source: str, status: str, note: str = "") -> None:
        allowed = {"approved", "rejected", "manual_review_required", "pending"}
        if status not in allowed:
            raise ValueError(f"Invalid review status: {status}")
        cur = self.conn.execute("UPDATE inventory SET status=?,last_seen=? WHERE source_path=?",
                                (status, _now(), source))
        if not cur.rowcount:
            raise KeyError(f"Unknown BRD source: {source}")
        self.record(source, "review", status, "complete", note)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "BrdLedger":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
