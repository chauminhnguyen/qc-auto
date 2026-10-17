from __future__ import annotations

import json

from .db import ChatDB


class SearchEngine:
    def __init__(self, chat_db: ChatDB):
        self.db = chat_db

    def search(self, query: str, limit: int = 10, note_type: str | None = None) -> list[dict]:
        results = self.db.search(query, limit=limit, note_type=note_type)
        alias_hits = self.db.search_by_alias(query, limit=limit)
        merged = {row["id"]: row for row in results}
        for row in alias_hits:
            merged.setdefault(row["id"], row)
        ordered = list(merged.values())
        if not ordered:
            return []
        return ordered[:limit]

    def expand_related(self, matches: list[dict], limit: int = 10) -> list[dict]:
        if not matches:
            return []
        targets = set()
        for row in matches:
            if row.get("source_path"):
                targets.add(str(row["source_path"]))
            try:
                linked = json.loads(row.get("linked_entities") or "[]")
            except (TypeError, ValueError):
                linked = []
            for item in linked:
                if item:
                    targets.add(str(item))
        if not targets:
            return matches
        merged = {row["id"]: dict(row) for row in matches}
        rows = self.db.conn.execute("SELECT * FROM chunks").fetchall()
        for row in rows:
            row_obj = dict(row)
            try:
                linked = json.loads(row_obj.get("linked_entities") or "[]")
            except (TypeError, ValueError):
                linked = []
            if row_obj.get("source_path") in targets or any(item in targets for item in linked):
                merged.setdefault(row_obj["id"], row_obj)
        return list(merged.values())[:limit]
