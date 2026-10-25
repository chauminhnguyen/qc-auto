from __future__ import annotations

import re

from . import graph_config
from .db import ChatDB


_TOKEN = re.compile(r"[a-z0-9]+", re.I)


def _is_hub(path: str) -> bool:
    folded = path.casefold()
    return folded.startswith("brds/index/") or "brd-master-index" in folded


def _seed_scores(seeds: list[dict]) -> dict[int, float]:
    scores = [float(row.get("score", 0.0)) for row in seeds]
    if not scores:
        return {}
    best, worst = min(scores), max(scores)
    if best == worst:
        return {int(row["id"]): max(graph_config.BM25_SEED_SCORE_FLOOR, 1.0 - i * 0.01)
                for i, row in enumerate(seeds)}
    return {int(row["id"]): max(
        graph_config.BM25_SEED_SCORE_FLOOR,
        (worst - float(row.get("score", worst))) / (worst - best),
    ) for row in seeds}


def _row_cost(row: dict) -> int:
    return len(str(row.get("text", row.get("body", ""))))


def _tokens(text: str) -> set[str]:
    return {token.casefold() for token in _TOKEN.findall(text or "") if len(token) > 1}


class SearchEngine:
    def __init__(self, chat_db: ChatDB):
        self.db = chat_db

    def search(self, query: str, limit: int = 10, note_type: str | None = None) -> list[dict]:
        results = self.db.search(query, limit=limit, note_type=note_type)
        per_note: dict[str, int] = {}
        limited = []
        for row in results:
            path = str(row.get("source_path") or "")
            if per_note.get(path, 0) >= 2:
                continue
            per_note[path] = per_note.get(path, 0) + 1
            limited.append(row)
            if len(limited) >= limit:
                break
        return limited

    def expand_context(self, seeds: list[dict], max_hops: int = graph_config.MAX_HOPS,
                       budget_chars: int = graph_config.EXPAND_BUDGET_CHARS,
                       min_score: float = graph_config.MIN_EXPANSION_SCORE) -> list[dict]:
        """Return seed passages followed by the best connected context passages."""
        if not seeds:
            return []
        seed_score = _seed_scores(seeds)
        selected: list[dict] = []
        used_ids: set[int] = set()
        used_chars = 0
        for seed in seeds:
            item = dict(seed)
            item.update(origin="seed", edge_kind="matched", edge_weight=1.0,
                        final_score=seed_score.get(int(seed["id"]), 1.0),
                        context_label="matched", seed_path=str(seed.get("source_path", "")))
            cost = _row_cost(item)
            if used_chars + cost > max(0, budget_chars):
                continue
            selected.append(item)
            used_ids.add(int(item["id"]))
            used_chars += cost
        if not selected or max_hops <= 0:
            return selected

        candidates: dict[int, dict] = {}
        frontier: list[tuple[str, str, float, str, float, int]] = []
        for seed in seeds:
            path = str(seed.get("source_path") or "")
            sid = int(seed["id"])
            score = seed_score.get(sid, 1.0)
            if not _is_hub(path):
                frontier.append((path, path, score, "matched", 1.0, 0))
            neighbors = self.db.conn.execute("""
                SELECT c.id,f.path AS source_path,f.title,c.heading_path,c.text,c.ord
                FROM chunks c JOIN files f ON f.id=c.file_id
                WHERE f.path=? AND c.ord IN (?,?)
                ORDER BY c.ord
            """, (path, int(seed.get("ord", 0)) - 1, int(seed.get("ord", 0)) + 1)).fetchall()
            for neighbor in neighbors:
                row = dict(neighbor)
                if int(row["id"]) in used_ids:
                    continue
                neighbor_score = score * graph_config.EDGE_SAME_NOTE_NEIGHBOR
                if neighbor_score < min_score:
                    continue
                self._offer(candidates, row, path, "same_note_neighbor",
                            graph_config.EDGE_SAME_NOTE_NEIGHBOR, neighbor_score)

        visited: dict[tuple[str, str], float] = {}
        while frontier:
            next_frontier = []
            for source_path, seed_path, base_score, _last_kind, _last_weight, hop in frontier:
                if hop >= max_hops:
                    continue
                for edge in self.db.conn.execute(
                        "SELECT dst,kind,weight FROM edges WHERE src=? "
                        "UNION ALL SELECT src AS dst,kind,weight FROM edges WHERE dst=? "
                        "ORDER BY weight DESC,dst,kind",
                        (source_path, source_path)).fetchall():
                    target, kind, weight = str(edge["dst"]), str(edge["kind"]), float(edge["weight"])
                    if target == source_path or _is_hub(target) or _is_hub(source_path):
                        continue
                    decay = graph_config.HOP_DECAY ** hop
                    score = base_score * weight * decay
                    if score < min_score:
                        continue
                    visit_key = (target, seed_path)
                    if visited.get(visit_key, -1.0) >= score:
                        continue
                    visited[visit_key] = score
                    chunks = self._target_chunks(target, seeds, seed_path)
                    for row in chunks:
                        self._offer(candidates, row, seed_path, kind, weight, score)
                    if hop + 1 < max_hops:
                        next_frontier.append((target, seed_path, score, kind, weight, hop + 1))
            frontier = next_frontier

        ordered = sorted(candidates.values(), key=lambda row: (
            -float(row["final_score"]), str(row["source_path"]).casefold(), int(row.get("ord", 0))))
        for candidate in ordered:
            if int(candidate["id"]) in used_ids:
                continue
            cost = _row_cost(candidate)
            if used_chars + cost > max(0, budget_chars):
                continue
            selected.append(candidate)
            used_ids.add(int(candidate["id"]))
            used_chars += cost
        return self._merge_adjacent(selected)

    @staticmethod
    def _offer(candidates: dict[int, dict], row: dict, seed_path: str, kind: str,
               weight: float, score: float) -> None:
        row = dict(row)
        row.update(origin="expansion", edge_kind=kind, edge_weight=weight,
                   final_score=score, context_label=f"context: {kind} of {seed_path}",
                   seed_path=seed_path)
        chunk_id = int(row["id"])
        prior = candidates.get(chunk_id)
        if prior is None or score > float(prior["final_score"]):
            candidates[chunk_id] = row

    def _target_chunks(self, target: str, seeds: list[dict], seed_path: str) -> list[dict]:
        rows = [dict(row) for row in self.db.conn.execute("""
            SELECT c.id,f.path AS source_path,f.title,c.heading_path,c.text,c.ord
            FROM chunks c JOIN files f ON f.id=c.file_id
            WHERE f.path=? ORDER BY c.ord
        """, (target,)).fetchall()]
        if not rows:
            return []
        seed_text = " ".join(str(row.get("text") or row.get("body") or "")
                              for row in seeds if row.get("source_path") == seed_path)
        query_terms = _tokens(seed_text)
        rows.sort(key=lambda row: (-len(query_terms & _tokens(row["text"])), int(row["ord"])))
        return rows[:graph_config.MAX_EXPANDED_CHUNKS_PER_NOTE]

    @staticmethod
    def _merge_adjacent(rows: list[dict]) -> list[dict]:
        groups: dict[tuple, list[dict]] = {}
        seed_order: dict[str, int] = {}
        for index, row in enumerate(rows):
            key = (row.get("origin"), row.get("source_path"), row.get("context_label"))
            groups.setdefault(key, []).append(dict(row))
            if row.get("origin") == "seed":
                seed_order[str(row.get("source_path"))] = min(
                    index, seed_order.get(str(row.get("source_path")), index))
        merged_seeds: list[dict] = []
        merged_expansions: list[dict] = []
        for (origin, _path, _label), group in groups.items():
            group.sort(key=lambda item: int(item.get("ord", 0)))
            runs: list[list[dict]] = []
            for row in group:
                if runs and int(row.get("ord", 0)) == int(runs[-1][-1].get("ord", 0)) + 1:
                    runs[-1].append(row)
                else:
                    runs.append([row])
            for run in runs:
                head = dict(run[0])
                head["chunk_ids"] = [part["id"] for part in run]
                if len(run) > 1:
                    head["text"] = "\n\n".join(str(part.get("text", "")) for part in run)
                    head["body"] = head["text"]
                if origin == "seed":
                    merged_seeds.append(head)
                else:
                    merged_expansions.append(head)
        merged_seeds.sort(key=lambda row: seed_order.get(str(row.get("source_path")), 0))
        merged_expansions.sort(key=lambda row: (-float(row.get("final_score", 0.0)),
                                                 str(row.get("source_path", "")).casefold(),
                                                 int(row.get("ord", 0))))
        return merged_seeds + merged_expansions
