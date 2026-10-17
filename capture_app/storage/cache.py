"""
Persistent cache for AI screen classifications, keyed by ScreenIndex.

Lives in the SAME sqlite file as vault.py's own tables (vault.conn) -- one
more table alongside apis/steps/sessions, rather than a second stray
.sqlite file. Classifying a screen once, ever, is what keeps API usage
bounded: repeat runs against the same site should cost ~0 extra calls (see
the quota discussion this pipeline came out of).
"""
from __future__ import annotations

import json
from typing import Optional

from capture_app.storage.vault import VaultWriter

SCHEMA = """
CREATE TABLE IF NOT EXISTS screen_classifications (
    screen_index TEXT PRIMARY KEY,
    interactables_hash TEXT NOT NULL,
    classification_json TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


class ClassificationCache:
    def __init__(self, vault: VaultWriter):
        self.conn = vault.conn
        self.conn.execute(SCHEMA)
        self.conn.commit()

    def get(self, screen_index: str, interactables_hash: str) -> Optional[list]:
        row = self.conn.execute(
            "SELECT interactables_hash, classification_json "
            "FROM screen_classifications WHERE screen_index = ?",
            (screen_index,),
        ).fetchone()
        if row is None:
            return None
        cached_hash, payload = row
        if cached_hash != interactables_hash:
            # the screen's untested-candidate shape changed since last time
            # (e.g. a new promo banner appeared) -- treat as a cache miss
            return None
        return json.loads(payload)

    def set(self, screen_index: str, interactables_hash: str, classifications: list) -> None:
        self.conn.execute(
            "INSERT INTO screen_classifications "
            "(screen_index, interactables_hash, classification_json) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(screen_index) DO UPDATE SET "
            "interactables_hash = excluded.interactables_hash, "
            "classification_json = excluded.classification_json, "
            "created_at = datetime('now')",
            (screen_index, interactables_hash, json.dumps(classifications)),
        )
        self.conn.commit()
