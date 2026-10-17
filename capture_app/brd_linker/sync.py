"""Planning and applying safe source-to-vault synchronization."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from .adapters import BrdAdapter, EvidenceAdapter, content_hash, normalize_path
from .generated import (AUTO_END, AUTO_START, REGION_RE, compose_note, current_region_hash,
                       generated_body, parse_frontmatter, replace_auto_links,
                       replace_generated, set_managed_frontmatter, sha256)
from .store import SyncStore


def _embedding_vectors(texts: list[str], model: str) -> list[list[float]]:
    import httpx
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise SyncError("OPENROUTER_API_KEY is required when sync.embed.model is configured")
    response = httpx.post("https://openrouter.ai/api/v1/embeddings",
                          headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                          json={"model": model, "input": texts}, timeout=60)
    if response.status_code != 200:
        raise SyncError(f"OpenRouter embeddings failed ({response.status_code})")
    payload = response.json()
    try:
        data = sorted(payload["data"], key=lambda row: row.get("index", 0))
        return [[float(value) for value in row["embedding"]] for row in data]
    except (KeyError, TypeError, ValueError) as exc:
        raise SyncError("OpenRouter embeddings returned an invalid response") from exc


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_l = sum(a * a for a in left) ** 0.5
    norm_r = sum(b * b for b in right) ** 0.5
    return dot / (norm_l * norm_r) if norm_l and norm_r else 0.0


class SyncError(RuntimeError):
    pass


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _fingerprint(paths: list[Path], *, vault: Path, state_db: Path, include_state: bool = True) -> str:
    items = []
    for path in sorted({p.resolve() for p in paths if p.exists()}, key=lambda p: str(p).casefold()):
        try:
            rel = path.relative_to(vault.resolve()).as_posix()
        except ValueError:
            rel = str(path)
        if path.is_file():
            items.append((rel, _hash_file(path)))
        elif path.is_dir():
            items.append((rel, "directory"))
            for child in sorted(path.rglob("*")):
                if not child.is_file() or any(part in {".linker_backup", "__pycache__"} for part in child.parts):
                    continue
                if child.resolve() == state_db.resolve():
                    continue
                try:
                    child_key = child.relative_to(vault.resolve()).as_posix()
                except ValueError:
                    child_key = f"{rel.rstrip('/')}/{child.relative_to(path).as_posix()}"
                items.append((child_key, _hash_file(child)))
    if include_state and state_db.is_file():
        items.append((".sync_state_db", _hash_file(state_db)))
    return hashlib.sha256(_json(items).encode()).hexdigest()


def _slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return (value or "entity")[:150]


def _note_path(vault: Path, kind: str, source_id: str, source: dict) -> Path:
    leaf = _slug(source_id) + ".md"
    if kind == "segment":
        return vault / "BRDs" / "segments" / leaf
    if kind == "api":
        return vault / "APIs" / "_sync" / leaf
    return vault / "Params" / "_sync" / leaf


def _source_dict(item) -> dict:
    if hasattr(item, "to_dict"):
        value = item.to_dict()
    else:
        value = dict(item)
    value.pop("source_id", None)
    return value


def _source_hash(kind: str, source: dict) -> str:
    if kind == "segment":
        content = source.get("text", "")
    else:
        content = _json({k: v for k, v in source.items() if k not in {"seen_in"}})
    return content_hash(content)


def _jaccard(left: str, right: str) -> float:
    tokens_a, tokens_b = _tokens(left), _tokens(right)
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def _tokens(text: str) -> set[str]:
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])", " ", text)
    return set(re.findall(r"[a-z0-9]+", text.casefold()))


def _retrieval_score(left: str, right: str, stop_words: set[str]) -> float:
    aliases = {"registration": "register", "registrations": "register", "accounts": "account",
               "customers": "customer", "payments": "payment", "products": "product",
               "orders": "order", "addresses": "address", "browsing": "browse", "searching": "search"}
    tokens_a = {aliases.get(token, token) for token in _tokens(left)} - stop_words
    tokens_b = {aliases.get(token, token) for token in _tokens(right)} - stop_words
    if not tokens_a or not tokens_b:
        return 0.0
    # Entity descriptions are short; coverage is more useful than Jaccard
    # against a long BRD segment and still requires meaningful shared terms.
    return len(tokens_a & tokens_b) / min(len(tokens_a), len(tokens_b))


def _entity_terms(target: dict) -> str:
    attrs = target["source"].get("attrs", {})
    if target["kind"] == "api":
        return f"{attrs.get('path', '')} {target['name']}"
    api_key = str(attrs.get("api_key") or "")
    return f"{target.get('name', '')} {api_key}"


def _legacy_entity_notes(vault: Path, item: dict, key_normalization: dict) -> list[Path]:
    """Locate an existing capture-generated API/param note by its source identity."""
    if item["kind"] == "api":
        matches = []
        method = str(item["source"].get("attrs", {}).get("method") or "").upper()
        wanted_path = str(item["source"].get("attrs", {}).get("path") or "")
        wanted_domain = str(item["source"].get("attrs", {}).get("domain") or "").casefold()
        for path in sorted((vault / "APIs").rglob("*.md")):
            if "_sync" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            fields, _ = parse_frontmatter(text)
            if str(fields.get("method") or "").upper() != method:
                continue
            candidate_url = str(fields.get("url") or fields.get("path_template") or "")
            candidate_path = normalize_path(candidate_url, key_normalization)
            if candidate_path != wanted_path:
                continue
            if wanted_domain and str(fields.get("domain") or "").casefold() != wanted_domain:
                continue
            matches.append(path)
        return matches
    if item["kind"] != "param":
        return []
    attrs = item["source"].get("attrs", {})
    wanted_api = str(attrs.get("api_key") or "")
    wanted_name = str(item.get("name") or "").casefold()
    api_by_stem = {}
    for api_path in sorted((vault / "APIs").rglob("*.md")):
        if "_sync" in api_path.parts:
            continue
        api_fields, _ = parse_frontmatter(api_path.read_text(encoding="utf-8", errors="replace"))
        method = str(api_fields.get("method") or "").upper()
        path_value = str(api_fields.get("url") or api_fields.get("path_template") or "")
        api_by_stem[api_path.stem.casefold()] = f"{method} {normalize_path(path_value, key_normalization)}"
    matches = []
    for path in sorted((vault / "Params").rglob("*.md")):
        if "_sync" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        fields, _ = parse_frontmatter(text)
        canonical = str(fields.get("canonical_name") or "").casefold()
        wire_names = fields.get("wire_names") or []
        if isinstance(wire_names, str):
            wire_names = [wire_names]
        if wanted_name != canonical and wanted_name not in {str(x).casefold() for x in wire_names}:
            continue
        targets = re.findall(r"\[\[([^\]|#]+)", text)
        linked_apis = {api_by_stem.get(Path(target).stem.casefold()) for target in targets}
        if wanted_api in linked_apis:
            matches.append(path)
    return matches


def _human_content_conflicts(text: str, target_paths: set[str]) -> list[str]:
    start = text.find(AUTO_START)
    end = text.find(AUTO_END, start + len(AUTO_START)) if start >= 0 else -1
    body = text[:start] + text[end + len(AUTO_END):] if start >= 0 and end >= 0 else text
    found = []
    for target in target_paths:
        stem = Path(target).with_suffix("").as_posix()
        escaped = re.escape(stem)
        if re.search(rf"!?\[\[{escaped}(?:\||#|\]\])", body):
            found.append(target)
    return sorted(set(found))


def _conflict_note_hash(vault: Path, paths: list[str]) -> str:
    rows = []
    for rel in sorted(set(paths)):
        path = vault / rel
        rows.append((rel, _hash_file(path) if path.is_file() else "missing"))
    return hashlib.sha256(_json(rows).encode()).hexdigest()


def _input_paths(config: dict, vault: Path, config_path: Path) -> tuple[Path, Path, Path]:
    sync_cfg = config.get("sync") or {}
    root = config_path.resolve().parent
    brd_dir = (root / sync_cfg.get("brd_dir", "./BRDs")).resolve()
    evidence_dir = (root / sync_cfg.get("evidence_dir", str(vault / "Reports" / "native_runs"))).resolve()
    capture_db = (root / sync_cfg.get("capture_db", str(vault / ".capture_index" / "index.sqlite"))).resolve()
    return brd_dir, evidence_dir, capture_db


def _collect(config: dict, config_path: Path, vault: Path):
    sync_cfg = config.get("sync") or {}
    brd_dir, evidence_dir, capture_db = _input_paths(config, vault, config_path)
    brd_files = sorted(brd_dir.rglob("*.md")) if brd_dir.is_dir() else []
    run_path = EvidenceAdapter.newest_run(evidence_dir)
    evidence = EvidenceAdapter(capture_db, config.get("key_normalization") or {})
    run_info, api_entities = evidence.read(run_path)
    segments = BrdAdapter().read(brd_files)
    desired = []
    for item in segments:
        source = _source_dict(item)
        desired.append({"source_id": item.source_id, "kind": "segment", "name": " > ".join(item.heading_path),
                        "key": item.key, "source": source, "source_hash": _source_hash("segment", source)})
    for item in api_entities:
        source = _source_dict(item)
        desired.append({"source_id": item.source_id, "kind": item.kind, "name": item.name, "key": item.key,
                        "source": source, "source_hash": _source_hash(item.kind, source)})
    paths = [brd_dir] + brd_files + [run_path]
    if capture_db.is_file():
        paths.append(capture_db)
    paths.extend([vault])
    return desired, run_info, run_path, brd_files, capture_db, paths


def _desired_body(item: dict) -> str:
    source = dict(item["source"])
    source["name"] = item["name"]
    if item["kind"] == "segment":
        source["title"] = item["name"]
    return generated_body(item["kind"], source)


def _classify(desired: list[dict], store: SyncStore, vault: Path, run_info,
              threshold: float, stale_after_runs: int,
              key_normalization: dict | None = None) -> list[dict]:
    actual = store.entity_map()
    desired_by_id = {x["source_id"]: x for x in desired}
    known_conflicts = {}
    if store.has_schema and store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_conflicts'").fetchone():
        known_conflicts = {row["source_id"]: dict(row) for row in store.conn.execute("SELECT * FROM sync_conflicts")}
    def add_conflict(op: dict):
        source_id = op.get("source_id", "")
        item = desired_by_id.get(source_id)
        source_hash = str(op.get("source_hash") or (item or {}).get("source_hash") or "")
        paths = list(op.get("candidate_paths") or [])
        if op.get("note_path"):
            paths.append(op["note_path"])
        note_hash = _conflict_note_hash(vault, paths)
        old = known_conflicts.get(source_id)
        if old and old["source_hash"] == source_hash and old["note_hash"] == note_hash:
            return
        op["conflict_source_hash"] = source_hash
        op["conflict_note_hash"] = note_hash
        op["candidate_paths"] = sorted(set(paths))
        ops.append(op)
    missing_new = [x for x in desired if x["source_id"] not in actual]
    missing_old = [row for source_id, row in actual.items() if source_id not in desired_by_id and row["status"] != "removed"]
    assigned_old: set[str] = set()
    assigned_new: set[str] = set()
    ops: list[dict] = []

    # Rename matching is one-to-one only. Any competing candidate is reported.
    matches: list[tuple[float, str, str, str]] = []
    for new in missing_new:
        for old in missing_old:
            if old["kind"] != new["kind"]:
                continue
            if old["source_hash"] == new["source_hash"]:
                score = 1.0
            else:
                old_source = json.loads(old.get("source_json") or "{}")
                old_text = str(old_source.get("text") or _json(old_source))
                new_text = str(new["source"].get("text") or _json(new["source"]))
                score = _jaccard(old_text, new_text)
                if score < threshold:
                    continue
            matches.append((score, old["source_id"], new["source_id"], "exact_hash" if score == 1.0 else "token_jaccard"))
    by_new: dict[str, list[tuple[float, str, str, str]]] = {}
    by_old: dict[str, list[tuple[float, str, str, str]]] = {}
    for match in matches:
        by_new.setdefault(match[2], []).append(match)
        by_old.setdefault(match[1], []).append(match)
    for new_id, rows in sorted(by_new.items()):
        best_score = max(row[0] for row in rows)
        best = [row for row in rows if row[0] == best_score]
        if len(best) != 1 or len(by_old.get(best[0][1], [])) != 1:
            candidate_ids = sorted(row[1] for row in rows)
            add_conflict({"action": "CONFLICT", "source_id": new_id, "kind": desired_by_id[new_id]["kind"],
                          "reason": "ambiguous rename candidate", "candidates": candidate_ids,
                          "candidate_paths": [actual[candidate]["note_path"] for candidate in candidate_ids if candidate in actual]})
            assigned_new.add(new_id)
            continue
        score, old_id, _, basis = best[0]
        old = actual[old_id]
        new = desired_by_id[new_id]
        ops.append({"action": "RENAME", "source_id": old_id, "new_source_id": new_id,
                    "kind": new["kind"], "note_path": old["note_path"], "source_hash": new["source_hash"],
                    "generated_hash": old["generated_hash"], "match": basis, "score": score,
                    "source": new["source"], "name": new["name"], "reason": "source identity changed"})
        assigned_old.add(old_id)
        assigned_new.add(new_id)

    for item in desired:
        source_id = item["source_id"]
        if source_id in assigned_new:
            continue
        old = actual.get(source_id)
        note_path = Path(old["note_path"]) if old else _note_path(vault, item["kind"], source_id, item["source"])
        rel_path = note_path.relative_to(vault).as_posix() if note_path.is_absolute() else note_path.as_posix()
        absolute = vault / rel_path
        if not old:
            legacy_matches = _legacy_entity_notes(vault, item, key_normalization or {}) if item["kind"] in {"api", "param"} else []
            if len(legacy_matches) > 1:
                candidate_paths = [path.relative_to(vault).as_posix() for path in legacy_matches]
                add_conflict({"action": "CONFLICT", "source_id": source_id, "kind": item["kind"],
                              "candidates": candidate_paths, "candidate_paths": candidate_paths,
                              "reason": "multiple legacy notes match this normalized entity"})
                continue
            if legacy_matches:
                legacy = legacy_matches[0]
                legacy_text = legacy.read_text(encoding="utf-8", errors="replace")
                legacy_fm, _ = parse_frontmatter(legacy_text)
                expected_body = _desired_body(item)
                if current_region_hash(legacy_text) == sha256(expected_body):
                    ops.append({"action": "ADD", "source_id": source_id, "kind": item["kind"],
                                "note_path": legacy.relative_to(vault).as_posix(),
                                "source_hash": item["source_hash"], "source": item["source"],
                                "name": item["name"], "adopt_existing": True,
                                "reason": "exact generated region can be safely adopted"})
                else:
                    add_conflict({"action": "CONFLICT", "source_id": source_id, "kind": item["kind"],
                                  "note_path": legacy.relative_to(vault).as_posix(),
                                  "reason": "legacy note matches this entity but has no verifiable canonical generated region",
                                  "frontmatter_generated": bool(legacy_fm.get("generated"))})
                continue
            if absolute.exists():
                add_conflict({"action": "CONFLICT", "source_id": source_id, "kind": item["kind"],
                              "note_path": rel_path, "reason": "unmanaged file already occupies generated note path"})
            else:
                ops.append({"action": "ADD", "source_id": source_id, "kind": item["kind"],
                            "note_path": rel_path, "source_hash": item["source_hash"],
                            "source": item["source"], "name": item["name"], "reason": "not present in sync state"})
            continue
        assigned_old.add(source_id)
        if old["source_hash"] == item["source_hash"] and old["status"] == "active":
            continue
        file_path = vault / old["note_path"]
        if not file_path.is_file():
            add_conflict({"action": "CONFLICT", "source_id": source_id, "kind": item["kind"],
                          "note_path": old["note_path"], "reason": "tracked note is missing"})
            continue
        text = file_path.read_text(encoding="utf-8", errors="replace")
        if current_region_hash(text) != old["generated_hash"]:
            add_conflict({"action": "CONFLICT", "source_id": source_id, "kind": item["kind"],
                          "note_path": old["note_path"], "reason": "generated region changed by a human"})
            continue
        ops.append({"action": "UPDATE", "source_id": source_id, "kind": item["kind"],
                    "note_path": old["note_path"], "old_source_hash": old["source_hash"],
                    "source_hash": item["source_hash"], "generated_hash": old["generated_hash"],
                    "source": item["source"], "name": item["name"], "reason": "normalized source changed"})

    current_run_seen = store.conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_info.run_id,)).fetchone() if store.has_schema else None
    for old in missing_old:
        source_id = old["source_id"]
        if source_id in assigned_old:
            continue
        absence_count = int(old.get("absence_count", 0))
        if old["kind"] == "segment":
            action = "DELETE"
            reason = "BRD source segment disappeared"
        elif not run_info.complete:
            action = "STALE"
            reason = "incomplete evidence run cannot prove absence"
        else:
            count = absence_count + (0 if current_run_seen else 1)
            absence_count = count
            if count >= stale_after_runs:
                action, reason = "DELETE", f"absent from {count} consecutive complete evidence runs"
            else:
                action, reason = "STALE", f"absent from {count} complete evidence run(s)"
        ops.append({"action": action, "source_id": source_id, "kind": old["kind"],
                    "note_path": old["note_path"], "source_hash": old["source_hash"],
                    "generated_hash": old["generated_hash"], "absence_count": absence_count,
                    "reason": reason})

    return sorted(ops, key=lambda op: (op["action"], op.get("source_id", ""), op.get("new_source_id", "")))


def _plan_links(desired: list[dict], operations: list[dict], store: SyncStore,
                config: dict, vault: Path) -> list[dict]:
    segments = [x for x in desired if x["kind"] == "segment"]
    conflicts_by_id = {op.get("source_id"): op for op in operations if op["action"] == "CONFLICT"}
    unusable = {source_id for source_id, op in conflicts_by_id.items()
                if op.get("candidates") or op.get("reason", "").startswith("unmanaged file")}
    targets = [x for x in desired if x["kind"] in {"api", "param"} and x["source_id"] not in unusable]
    direct_changed_segments = {op.get("new_source_id", op.get("source_id")) for op in operations if op["action"] in {"ADD", "UPDATE", "RENAME"} and op["kind"] == "segment"}
    changed_segments = set(direct_changed_segments)
    changed_targets = set()
    for op in operations:
        if op["action"] in {"ADD", "UPDATE", "RENAME"} and op["kind"] in {"api", "param"}:
            changed_targets.add(op.get("source_id"))
            changed_targets.add(op.get("new_source_id", op.get("source_id")))
    old_links = {(x["seg_id"], x["target_id"]): x for x in store.links()}
    rename_map = {op["source_id"]: op["new_source_id"] for op in operations if op["action"] == "RENAME" and op["kind"] == "segment"}
    target_rename_map = {op["source_id"]: op["new_source_id"] for op in operations if op["action"] == "RENAME" and op["kind"] in {"api", "param"}}
    normalized_links = {}
    for (seg_id, target_id), link in old_links.items():
        normalized_links[(rename_map.get(seg_id, seg_id), target_rename_map.get(target_id, target_id))] = link
    old_links = normalized_links
    changed_segments.update(seg_id for seg_id, target_id in old_links if target_id in changed_targets)
    if not changed_segments and not changed_targets:
        return [], {"model": "", "vectors": {}}
    retrieval = config.get("retrieval") or {}
    top_k = max(1, int(retrieval.get("top_k", 10)))
    drop_below = float(retrieval.get("drop_below", 0.35))
    stop_words = {"id", "name", "type", "status", "data", "value", "result", "success", "message",
                  "post", "get", "put", "patch", "delete", "api", "opc", "checkout", "sync", "http"}
    stop_words.update(str(word).casefold() for word in config.get("stoplist_params", []))
    embed_cfg = config.get("embed") or {}
    embed_model = str(embed_cfg.get("model") or "").strip()
    pending_embeddings = {}
    vectors = {}
    if embed_model:
        for item in segments + targets:
            cached = None
            if store.has_schema:
                try:
                    row = store.conn.execute("SELECT model,vector_json FROM embedding_cache WHERE source_hash=?", (item["source_hash"],)).fetchone()
                    if row and row["model"] == embed_model:
                        cached = json.loads(row["vector_json"])
                except Exception:
                    cached = None
            if cached is not None:
                vectors[item["source_id"]] = cached
            else:
                pending_embeddings[item["source_id"]] = item
        if pending_embeddings:
            inputs = []
            for item in pending_embeddings.values():
                src = item["source"]
                inputs.append(str(src.get("text") or (item["name"] + " " + _json(src.get("attrs", {})))))
            for source_id, vector in zip(pending_embeddings, _embedding_vectors(inputs, embed_model)):
                vectors[source_id] = vector
    planned = []
    target_paths = {}
    for item in targets:
        legacy = _legacy_entity_notes(vault, item, config.get("key_normalization") or {})
        path = legacy[0] if len(legacy) == 1 else _note_path(vault, item["kind"], item["source_id"], item["source"])
        target_paths[item["source_id"]] = path.relative_to(vault).with_suffix("").as_posix()
    target_by_id = {target["source_id"]: target for target in targets}
    changed_target_ids = changed_targets.intersection(target_by_id)
    affected_by_new_target = set()
    for target_id in changed_target_ids:
        target = target_by_id[target_id]
        ranked = []
        for seg in segments:
            basis = _entity_terms(target)
            score = (_cosine(vectors[seg["source_id"]], vectors[target_id])
                     if embed_model and seg["source_id"] in vectors and target_id in vectors
            else _retrieval_score(str(seg["source"].get("text") or ""), basis, stop_words))
            ranked.append((score, seg["source_id"]))
        affected_by_new_target.update(seg_id for score, seg_id in sorted(ranked, key=lambda row: (-row[0], row[1]))[:top_k]
                                      if score >= drop_below)
    changed_segments.update(affected_by_new_target)
    for seg in segments:
        if seg["source_id"] not in changed_segments:
            continue
        text = str(seg["source"].get("text") or "")
        candidates = []
        for target in targets:
            prior = old_links.get((seg["source_id"], target["source_id"]))
            if seg["source_id"] not in direct_changed_segments:
                # Target-only refresh: judge the changed target when it is linked or
                # reaches its top segment set; leave unrelated decisions untouched.
                if target["source_id"] not in changed_target_ids:
                    continue
                if seg["source_id"] not in affected_by_new_target and not prior:
                    continue
            basis = _entity_terms(target)
            score = (_cosine(vectors[seg["source_id"]], vectors[target["source_id"]])
                     if embed_model and seg["source_id"] in vectors and target["source_id"] in vectors
                     else _retrieval_score(text, basis, stop_words))
            if score >= drop_below:
                candidates.append({"target_id": target["source_id"], "score": round(score, 6), "state": "proposed",
                                   "evidence": "embedding_cosine" if embed_model else "token_overlap",
                                   "note_path": target_paths[target["source_id"]],
                                   "target_hash": target["source_hash"]})
        candidates.sort(key=lambda row: (-row["score"], row["target_id"]))
        judge_cfg = config.get("judge") or {}
        if seg["source_id"] in direct_changed_segments:
            candidates = candidates[:min(top_k, max(1, int(judge_cfg.get("max_candidates", top_k))))]
            already = {row["target_id"] for row in candidates}
            for (linked_seg, target_id), prior in old_links.items():
                if linked_seg == seg["source_id"] and prior["state"] == "manual" and target_id in target_by_id and target_id not in already:
                    candidates.append({"target_id": target_id, "score": 0.0, "state": "manual",
                                       "evidence": "retained manual link",
                                       "note_path": target_paths.get(target_id, ""),
                                       "target_hash": target_by_id[target_id]["source_hash"]})
        else:
            candidates = candidates[:1]
        keep = []
        manual_disagreements = []
        for candidate in candidates:
            prior = old_links.get((seg["source_id"], candidate["target_id"]))
            target = target_by_id[candidate["target_id"]]
            if prior and prior["state"] == "rejected" and prior["seg_hash"] == seg["source_hash"] and prior["tgt_hash"] == target["source_hash"]:
                continue
            if prior and prior["state"] in {"accepted", "rejected"} and prior["seg_hash"] == seg["source_hash"] and prior["tgt_hash"] == target["source_hash"]:
                candidate["state"] = prior["state"]
                candidate["evidence"] = prior["evidence"]
                keep.append(candidate)
                continue
            if prior and prior["state"] == "manual":
                candidate["state"] = "manual"
                candidate["evidence"] += "; retained manual decision"
            model = str(judge_cfg.get("model") or "").strip()
            if model:
                from capture_app.integrations.openrouter import chat_json
                judgment = chat_json(
                    "Decide whether the API or parameter is materially relevant to the BRD segment. Return JSON with relevant (boolean) and reason (string).",
                    _json({"segment": seg["source"].get("text", ""), "target": target["source"], "score": candidate["score"]}),
                    model=model, temperature=float(judge_cfg.get("temperature", 0)), max_tokens=300)
                relevant = bool(judgment.get("relevant", False))
                if candidate["state"] == "manual":
                    if not relevant:
                        manual_disagreements.append(candidate["target_id"])
                    candidate["evidence"] += "; judge=" + str(judgment.get("reason", ""))
                else:
                    candidate["state"] = "accepted" if relevant else "rejected"
                    candidate["evidence"] = str(judgment.get("reason", "model judged candidate"))
            keep.append(candidate)
        seg_note = next((op.get("note_path") for op in operations
                         if op.get("new_source_id", op.get("source_id")) == seg["source_id"]
                         and op["action"] in {"ADD", "UPDATE", "RENAME"}), None)
        if not seg_note:
            existing = store.entity_map().get(seg["source_id"])
            seg_note = existing["note_path"] if existing else _note_path(vault, "segment", seg["source_id"], seg["source"]).relative_to(vault).as_posix()
        planned.append({"action": "RELINK", "source_id": seg["source_id"], "kind": "segment",
                        "note_path": seg_note,
                        "candidates": keep, "manual_disagreements": manual_disagreements,
                        "reason": "segment or target content changed"})
    # Removed IDs lose all stored links; human content is scanned and only reported.
    removed = {op["note_path"] for op in operations if op["action"] == "DELETE"}
    if removed:
        for path in vault.rglob("*.md"):
            if any(part in {".linker_backup", "_archive"} for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            refs = _human_content_conflicts(text, {str(x).replace("\\", "/").removesuffix(".md") for x in removed})
            managed_targets = {str(x).replace("\\", "/").removesuffix(".md") for x in removed}
            start = text.find(AUTO_START)
            end = text.find(AUTO_END, start + len(AUTO_START)) if start >= 0 else -1
            managed_block = text[start:end] if start >= 0 and end >= 0 else ""
            has_managed = any(re.search(rf"!?\[\[{re.escape(target)}(?:\||#|\]\])", managed_block)
                              for target in managed_targets)
            if refs or has_managed:
                planned.append({"action": "UNLINK", "note_path": path.relative_to(vault).as_posix(),
                                "human_targets": refs,
                                "managed_targets": sorted(managed_targets),
                                "reason": "human-authored links will be reported; managed links will be removed"})
    return planned, {"model": embed_model, "vectors": {
        item["source_hash"]: vector for source_id, vector in vectors.items()
        for item in [next((entry for entry in segments + targets if entry["source_id"] == source_id), None)] if item
    }}


def build_plan(config: dict, config_path: str | Path, *, force: bool = False) -> dict:
    config_path = Path(config_path).resolve()
    vault = Path(config.get("vault", "./ObsidianVault"))
    if not vault.is_absolute():
        vault = (config_path.parent / vault).resolve()
    if not vault.is_dir():
        raise SyncError(f"Vault directory does not exist: {vault}")
    sync_cfg = config.get("sync") or {}
    desired, run_info, run_path, brd_files, capture_db, input_paths = _collect(config, config_path, vault)
    state_db = vault / sync_cfg.get("state_db", ".capture_index/brd_linker_sync.sqlite")
    fingerprint_paths = input_paths[:-1] + [config_path]
    input_hash = _fingerprint(fingerprint_paths, vault=vault, state_db=state_db, include_state=False)
    vault_hash = _fingerprint([vault], vault=vault, state_db=state_db)
    desired_hash = hashlib.sha256(_json(sorted(desired, key=lambda row: row["source_id"])).encode()).hexdigest()
    with SyncStore(state_db, create=False) as store:
        operations = _classify(desired, store, vault, run_info,
                               float(sync_cfg.get("rename_threshold", 0.85)),
                               int(sync_cfg.get("stale_after_runs", 2)),
                               config.get("key_normalization") or {})
        previous_path = config_path.parent / "out" / "sync_plan.json"
        previous = None
        if previous_path.is_file():
            try:
                previous = json.loads(previous_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                previous = None
        if (previous and previous.get("planner_version") == 7
                and previous.get("input_hash") == input_hash and previous.get("vault_hash") == vault_hash
                and previous.get("desired_hash") == desired_hash
                and previous.get("link_decisions")):
            link_operations = previous["link_decisions"]
            embedding_cache = previous.get("embedding_cache") or {"model": "", "vectors": {}}
        else:
            link_operations, embedding_cache = _plan_links(desired, operations, store, config, vault)
        operations.extend(link_operations)
        counts = {}
        for op in operations:
            counts[op["action"]] = counts.get(op["action"], 0) + 1
        active_generated = sum(1 for row in store.entities() if row["status"] != "removed")
        removals = counts.get("DELETE", 0)
        fraction = removals / max(1, active_generated)
        blockers = []
        brd_dir = _input_paths(config, vault, config_path)[0]
        if not brd_files:
            blockers.append(f"BRD source directory is missing or empty: {brd_dir}")
        if not desired or not any(x["kind"] in {"api", "param"} for x in desired):
            blockers.append("evidence run has zero entities")
        if fraction > float(sync_cfg.get("max_delete_fraction", 0.20)) and not force:
            blockers.append(f"removals are {fraction:.1%}, above max_delete_fraction")
        plan = {
            "version": 1,
            "planner_version": 7,
            "vault": str(vault), "state_db": str(state_db),
            "brd_dir": str(brd_dir),
            "config_path": str(config_path),
            "capture_db": str(capture_db),
            "brd_sources": [str(p) for p in brd_files],
            "evidence_run": {"path": str(run_path), **run_info.__dict__},
            "embedding_cache": embedding_cache,
            "input_hash": input_hash, "vault_hash": vault_hash,
            "desired_hash": desired_hash,
            "operations": sorted(operations, key=_operation_order),
            "link_decisions": link_operations,
            "counts": dict(sorted(counts.items())), "removal_fraction": round(fraction, 6),
            "max_delete_fraction": float(sync_cfg.get("max_delete_fraction", 0.20)),
            "blockers": blockers,
            "notes": ["Exclude _archive/ from Obsidian graph filters."],
        }
    return plan


def _operation_order(op: dict) -> tuple:
    order = {"ADD": 0, "UPDATE": 1, "RENAME": 2, "STALE": 3, "DELETE": 4,
             "RELINK": 5, "UNLINK": 6, "CONFLICT": 7}
    return order.get(op.get("action", ""), 99), op.get("source_id", ""), op.get("note_path", "")


def write_plan(plan: dict, json_path: str | Path, markdown_path: str | Path) -> None:
    json_path, markdown_path = Path(json_path), Path(markdown_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = ["# Vault Sync Plan", "", f"- Vault: `{plan['vault']}`",
             f"- Evidence run: `{plan['evidence_run']['run_id']}` ({plan['evidence_run']['status']}, complete={plan['evidence_run']['complete']})",
             f"- Removal fraction: {plan['removal_fraction']:.1%}", "", "## Counts", ""]
    lines.extend(f"- {action}: {count}" for action, count in plan["counts"].items())
    if not plan["counts"]:
        lines.append("- No changes")
    lines.extend(["", "## Operations", "", "| Action | Source | Kind | Path | Reason |", "|---|---|---|---|---|"])
    for op in plan["operations"]:
        lines.append("| {action} | {source} | {kind} | {path} | {reason} |".format(
            action=op["action"], source=op.get("source_id", ""), kind=op.get("kind", ""),
            path=op.get("note_path", ""), reason=op.get("reason", "").replace("|", "\\|") ))
    if plan["blockers"]:
        lines.extend(["", "## Apply blockers", "", *[f"- {x}" for x in plan["blockers"]]])
    lines.extend(["", "## Notes", "", *[f"- {x}" for x in plan["notes"]], ""])
    markdown_path.write_text("\n".join(lines), encoding="utf-8")


def _atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _save_entity(store: SyncStore, source_id: str, kind: str, note_path: str, source_hash: str,
                 generated_hash: str, source: dict, status="active", absence_count=0, first_seen=None):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    previous = store.conn.execute("SELECT first_seen FROM entities WHERE source_id=?", (source_id,)).fetchone()
    first_seen = first_seen or (previous[0] if previous else now)
    store.conn.execute("""INSERT INTO entities(source_id,kind,note_path,source_hash,generated_hash,status,first_seen,last_seen,
        removed_at,absence_count,source_json) VALUES(?,?,?,?,?,?,?,?,NULL,?,?)
        ON CONFLICT(source_id) DO UPDATE SET kind=excluded.kind,note_path=excluded.note_path,
        source_hash=excluded.source_hash,generated_hash=excluded.generated_hash,status=excluded.status,
        last_seen=excluded.last_seen,removed_at=NULL,absence_count=excluded.absence_count,source_json=excluded.source_json""",
        (source_id, kind, note_path, source_hash, generated_hash, status, first_seen, now,
         absence_count, _json(source)))


def apply_plan(plan_path: str | Path, *, force: bool = False,
               resume_journal: str | Path | None = None) -> dict:
    plan_path = Path(plan_path).resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("blockers") and not force:
        raise SyncError("Plan has blockers: " + "; ".join(plan["blockers"]))
    vault = Path(plan["vault"])
    state_db = Path(plan["state_db"])
    # Check the exact planned inputs and vault tree before creating any state or backup.
    current_input_paths = [Path(x) for x in plan.get("brd_sources", [])]
    if plan.get("brd_dir"):
        current_input_paths.append(Path(plan["brd_dir"]))
    current_input_paths.extend([Path(plan["evidence_run"]["path"])])
    if plan.get("config_path"):
        current_input_paths.append(Path(plan["config_path"]))
    capture_db = Path(plan.get("capture_db", vault / ".capture_index" / "index.sqlite"))
    if capture_db.exists():
        current_input_paths.append(capture_db)
    if resume_journal:
        if _fingerprint(current_input_paths, vault=vault, state_db=state_db, include_state=False) != plan["input_hash"]:
            raise SyncError("Source inputs changed since this apply was interrupted; create a new plan.")
    else:
        if _fingerprint(current_input_paths, vault=vault, state_db=state_db, include_state=False) != plan["input_hash"]:
            raise SyncError("Source inputs changed since this plan was created; run sync plan again.")
        if _fingerprint([vault], vault=vault, state_db=state_db) != plan["vault_hash"]:
            raise SyncError("Vault changed since this plan was created; run sync plan again.")
    force_cfg_fraction = float(plan.get("removal_fraction", 0))
    if force_cfg_fraction > float(plan.get("max_delete_fraction", 0.20)) and not force:
        raise SyncError("Removal fraction exceeds max_delete_fraction; pass --force only after reviewing the plan.")

    if resume_journal:
        journal_path = Path(resume_journal).resolve()
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        if journal.get("state") == "complete":
            return {"applied": 0, "message": "journal already complete", "journal": str(journal_path)}
        backup_root = Path(journal["backup"])
    else:
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        backup_root = vault / ".linker_backup" / timestamp
        journal_path = vault / ".capture_index" / f"sync_journal_{timestamp}.json"
        journal = {"plan": str(plan_path), "state": "applying", "completed": [], "backup": str(backup_root),
                   "state_db_existed": state_db.is_file()}
        backup_root.mkdir(parents=True, exist_ok=True)
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        if state_db.is_file():
            shutil.copy2(state_db, backup_root / "sync_state.sqlite")
        _atomic_write(journal_path, json.dumps(journal, indent=2) + "\n")
    summary = {"applied": 0, "conflicts": [], "human_links": []}
    with SyncStore(state_db) as store:
        def checkpoint(sequence: int):
            if sequence not in journal["completed"]:
                journal["completed"].append(sequence)
            journal["state"] = "applying"
            store.conn.commit()
            _atomic_write(journal_path, json.dumps(journal, indent=2) + "\n")

        # Backup all files known to be touched before the first operation.
        touched = {op["note_path"] for op in plan["operations"] if op.get("note_path") and
                   (op["action"] in {"UPDATE", "RENAME", "DELETE", "STALE", "UNLINK", "RELINK"}
                    or (op["action"] == "ADD" and op.get("adopt_existing")))}
        if not resume_journal:
            for rel in sorted(touched):
                source = vault / rel
                if source.is_file():
                    target = backup_root / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
        cache = plan.get("embedding_cache") or {}
        if cache.get("model"):
            for source_hash, vector in cache.get("vectors", {}).items():
                store.conn.execute("INSERT INTO embedding_cache(source_hash,model,vector_json) VALUES(?,?,?) "
                                   "ON CONFLICT(source_hash) DO UPDATE SET model=excluded.model,vector_json=excluded.vector_json",
                                   (source_hash, cache["model"], _json(vector)))
        for sequence, op in enumerate(plan["operations"]):
            if sequence in set(journal.get("completed", [])):
                continue
            action = op["action"]
            if action == "CONFLICT":
                summary["conflicts"].append(op)
                store.conn.execute("""INSERT INTO sync_conflicts(source_id,source_hash,note_hash,note_path,reason,details_json,first_seen,last_seen)
                    VALUES(?,?,?,?,?,?,datetime('now'),datetime('now')) ON CONFLICT(source_id) DO UPDATE SET
                    source_hash=excluded.source_hash,note_hash=excluded.note_hash,note_path=excluded.note_path,
                    reason=excluded.reason,details_json=excluded.details_json,last_seen=excluded.last_seen""",
                    (op.get("source_id", ""), op.get("conflict_source_hash", ""), op.get("conflict_note_hash", ""),
                     op.get("note_path", ""), op.get("reason", ""), _json(op)))
                checkpoint(sequence)
                continue
            if action == "UNLINK":
                note = vault / op["note_path"]
                if note.is_file():
                    text = note.read_text(encoding="utf-8", errors="replace")
                    start = text.find(AUTO_START)
                    end = text.find(AUTO_END, start + len(AUTO_START)) if start >= 0 else -1
                    if start >= 0 and end >= 0:
                        block = text[start:end + len(AUTO_END)]
                        managed_targets = set(op.get("managed_targets", []))
                        kept = []
                        for line in block.splitlines()[1:-1]:
                            target = re.search(r"!?\[\[([^\]|#]+)", line)
                            if target and target.group(1).replace("\\", "/") in managed_targets:
                                continue
                            kept.append(line)
                        updated = text[:start] + AUTO_START + "\n" + "\n".join(kept) + "\n" + AUTO_END + text[end + len(AUTO_END):]
                        if updated != text:
                            gen_hash = current_region_hash(updated)
                            updated = set_managed_frontmatter(updated, {"generated_hash": gen_hash})
                            _atomic_write(note, updated)
                            fm, _ = parse_frontmatter(updated)
                            if fm.get("source_id"):
                                store.conn.execute("UPDATE entities SET generated_hash=? WHERE source_id=?", (gen_hash, fm["source_id"]))
                    if op.get("human_targets"):
                        summary["human_links"].append(op)
                checkpoint(sequence)
                continue
            if action == "RELINK":
                seg_id = op["source_id"]
                store.conn.execute("DELETE FROM links WHERE seg_id=? AND state='proposed'", (seg_id,))
                seg_row = store.conn.execute("SELECT source_hash FROM entities WHERE source_id=?", (seg_id,)).fetchone()
                for candidate in op.get("candidates", []):
                    tgt_row = store.conn.execute("SELECT source_hash FROM entities WHERE source_id=?", (candidate["target_id"],)).fetchone()
                    store.conn.execute("""INSERT INTO links(seg_id,target_id,state,evidence,seg_hash,tgt_hash,decided_by)
                        VALUES(?,?,?,?,?,?,?) ON CONFLICT(seg_id,target_id) DO UPDATE SET
                        evidence=excluded.evidence,seg_hash=excluded.seg_hash,tgt_hash=excluded.tgt_hash""",
                        (seg_id, candidate["target_id"], candidate["state"], candidate["evidence"],
                         seg_row[0] if seg_row else "", tgt_row[0] if tgt_row else candidate.get("target_hash", ""), "sync"))
                segment = store.conn.execute("SELECT note_path FROM entities WHERE source_id=?", (seg_id,)).fetchone()
                if segment:
                    note = vault / segment[0]
                    if note.is_file():
                        text = note.read_text(encoding="utf-8", errors="replace")
                        accepted = []
                        target_path_by_id = {candidate["target_id"]: candidate.get("note_path", "") for candidate in op.get("candidates", [])}
                        for linked in store.conn.execute("SELECT target_id FROM links WHERE seg_id=? AND state IN ('accepted','manual') ORDER BY target_id", (seg_id,)):
                            target = store.conn.execute("SELECT note_path,status FROM entities WHERE source_id=?", (linked["target_id"],)).fetchone()
                            if target and target["status"] != "removed":
                                target_path = Path(target["note_path"]).with_suffix("").as_posix()
                                accepted.append(f"- [[{target_path}|{linked['target_id']}]]")
                            elif target_path_by_id.get(linked["target_id"]):
                                target_path = target_path_by_id[linked["target_id"]]
                                accepted.append(f"- [[{target_path}|{linked['target_id']}]]")
                        updated = replace_auto_links(text, accepted)
                        if updated != text:
                            gen_hash = current_region_hash(updated)
                            updated = set_managed_frontmatter(updated, {"generated_hash": gen_hash})
                            _atomic_write(note, updated)
                            store.conn.execute("UPDATE entities SET generated_hash=? WHERE source_id=?", (gen_hash, seg_id))
                checkpoint(sequence)
                continue
            if action == "ADD":
                rel = op["note_path"]
                source = op["source"]
                path = vault / rel
                content, gen_hash = compose_note(kind=op["kind"], source_id=op["source_id"], source=source,
                                                 source_hash=op["source_hash"], title=op["name"],
                                                 last_seen={"brd_snapshot": plan["input_hash"]} if op["kind"] == "segment" else {"evidence_run": plan["evidence_run"]["run_id"]})
                if path.exists():
                    if not op.get("adopt_existing"):
                        existing = path.read_text(encoding="utf-8", errors="replace")
                        fm, _ = parse_frontmatter(existing)
                        stored = store.conn.execute("SELECT source_hash,generated_hash FROM entities WHERE source_id=?", (op["source_id"],)).fetchone()
                        if (fm.get("source_id") == op["source_id"] and stored
                                and stored["source_hash"] == op["source_hash"]
                                and current_region_hash(existing) == stored["generated_hash"]):
                            # Apply was interrupted after writing the file but before checkpoint.
                            gen_hash = stored["generated_hash"]
                        else:
                            summary["conflicts"].append({**op, "reason": "path occupied at apply time"})
                            checkpoint(sequence)
                            continue
                    else:
                        existing = path.read_text(encoding="utf-8", errors="replace")
                        if current_region_hash(existing) != sha256(_desired_body(op)):
                            summary["conflicts"].append({**op, "reason": "legacy region changed since planning"})
                            checkpoint(sequence)
                            continue
                        gen_hash = current_region_hash(existing)
                        existing = set_managed_frontmatter(existing, {
                            "generated": True, "kind": op["kind"], "source_id": op["source_id"],
                            "source_hash": op["source_hash"], "generated_hash": gen_hash,
                            "status": "active", "last_seen": {"evidence_run": plan["evidence_run"]["run_id"]},
                        })
                        _atomic_write(path, existing)
                else:
                    _atomic_write(path, content)
                _save_entity(store, op["source_id"], op["kind"], rel, op["source_hash"], gen_hash, source)
                store.conn.execute("DELETE FROM sync_conflicts WHERE source_id=?", (op["source_id"],))
            elif action in {"UPDATE", "RENAME"}:
                old_id = op["source_id"]
                new_id = op.get("new_source_id", old_id)
                rel = op["note_path"]
                path = vault / rel
                if not path.is_file():
                    summary["conflicts"].append({**op, "reason": "tracked file missing at apply time"})
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                current = store.conn.execute("SELECT generated_hash FROM entities WHERE source_id=?", (old_id,)).fetchone()
                expected = current[0] if current else op.get("generated_hash")
                if current_region_hash(text) != expected:
                    summary["conflicts"].append({**op, "reason": "generated region changed since planning"})
                    continue
                body, source = _desired_body(op), op["source"]
                updated, gen_hash = replace_generated(text, body)
                updated = set_managed_frontmatter(updated, {"source_id": new_id, "source_hash": op["source_hash"],
                    "generated_hash": gen_hash, "status": "active", "title": op["name"],
                    "last_seen": {"brd_snapshot": plan["input_hash"]} if op["kind"] == "segment" else {"evidence_run": plan["evidence_run"]["run_id"]}})
                _atomic_write(path, updated)
                if new_id != old_id:
                    store.conn.execute("DELETE FROM entities WHERE source_id=?", (old_id,))
                    store.conn.execute("UPDATE links SET seg_id=? WHERE seg_id=?", (new_id, old_id))
                    store.conn.execute("UPDATE links SET target_id=? WHERE target_id=?", (new_id, old_id))
                _save_entity(store, new_id, op["kind"], rel, op["source_hash"], gen_hash, source)
                store.conn.execute("DELETE FROM sync_conflicts WHERE source_id IN (?,?)", (old_id, new_id))
            elif action in {"STALE", "DELETE"}:
                source_id = op["source_id"]
                rel = op["note_path"]
                path = vault / rel
                row = store.conn.execute("SELECT * FROM entities WHERE source_id=?", (source_id,)).fetchone()
                if row is None:
                    continue
                if path.is_file():
                    text = path.read_text(encoding="utf-8", errors="replace")
                    if current_region_hash(text) != row["generated_hash"]:
                        summary["conflicts"].append({**op, "reason": "generated region changed since planning"})
                        continue
                    if action == "STALE":
                        updated = set_managed_frontmatter(text, {"status": "stale"})
                        _atomic_write(path, updated)
                    else:
                        removed_at = date.today().isoformat()
                        archive_rel = f"_archive/{removed_at}/{rel}"
                        archive = vault / archive_rel
                        if archive.exists():
                            summary["conflicts"].append({**op, "reason": "archive destination already exists"})
                            checkpoint(sequence)
                            continue
                        updated = set_managed_frontmatter(text, {"status": "removed", "removed_at": removed_at})
                        _atomic_write(path, updated)
                        archive.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(path, archive)
                        original = json.loads(row["source_json"] or "{}")
                        original["_original_note_path"] = rel
                        store.conn.execute("UPDATE entities SET note_path=?,status='removed',removed_at=?,absence_count=?,source_json=? WHERE source_id=?",
                                           (archive_rel, removed_at, op.get("absence_count", 0), _json(original), source_id))
                        store.conn.execute("DELETE FROM links WHERE seg_id=? OR target_id=?", (source_id, source_id))
                        store.conn.execute("DELETE FROM sync_conflicts WHERE source_id=?", (source_id,))
                if action == "STALE":
                    store.conn.execute("UPDATE entities SET status='stale',absence_count=? WHERE source_id=?",
                                       (op.get("absence_count", 0), source_id))
            checkpoint(sequence)
            summary["applied"] += 1
        # Record the evidence snapshot and its entity summary only after successful operations.
        run_info = plan["evidence_run"]
        store.conn.execute("INSERT OR IGNORE INTO runs(run_id,kind,ts,complete,stats_json) VALUES(?,?,?,?,?)",
                           (run_info["run_id"], "evidence", run_info.get("ts", ""), int(bool(run_info.get("complete"))),
                            _json({"status": run_info.get("status"), "entities": sum(1 for x in plan["operations"] if x.get("kind") in {"api", "param"})})))
        store.conn.commit()
    journal["state"] = "complete"
    _atomic_write(journal_path, json.dumps(journal, indent=2) + "\n")
    changelog = vault / "_sync" / f"{date.today().isoformat()}-{plan['evidence_run']['run_id']}.md"
    lines = [f"# Sync {plan['evidence_run']['run_id']}", "", f"Applied operations: {summary['applied']}",
             f"Conflicts: {len(summary['conflicts'])}", f"Human links requiring review: {len(summary['human_links'])}",
             "", "Exclude `_archive/` from Obsidian graph filters.", ""]
    _atomic_write(changelog, "\n".join(lines))
    summary["journal"] = str(journal_path)
    summary["backup"] = str(backup_root)
    summary["changelog"] = str(changelog)
    return summary


def status_report(vault: str | Path, state_db: str | Path | None = None) -> dict:
    vault = Path(vault).resolve()
    state_db = Path(state_db) if state_db else vault / ".capture_index" / "brd_linker_sync.sqlite"
    with SyncStore(state_db, create=False) as store:
        conflicts = []
        if store.has_schema and store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_conflicts'").fetchone():
            conflicts = [dict(row) for row in store.conn.execute("SELECT * FROM sync_conflicts ORDER BY source_id")]
        return {"entities": store.entities(), "links": store.links(), "conflicts": conflicts,
                "runs": [dict(row) for row in store.conn.execute("SELECT * FROM runs ORDER BY ts DESC")] if store.has_schema else []}


def resume_journal(journal_path: str | Path, *, force: bool = False) -> dict:
    journal_path = Path(journal_path).resolve()
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    return apply_plan(journal["plan"], force=force, resume_journal=journal_path)


def rollback_journal(journal_path: str | Path) -> dict:
    journal_path = Path(journal_path).resolve()
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    plan = json.loads(Path(journal["plan"]).read_text(encoding="utf-8"))
    vault = Path(plan["vault"])
    backup_root = Path(journal["backup"])
    for op in plan.get("operations", []):
        rel = op.get("note_path")
        if not rel:
            continue
        backup = backup_root / rel
        original = vault / rel
        if backup.is_file():
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, original)
        elif op.get("action") == "ADD" and original.is_file():
            original.unlink()
        if op.get("action") == "DELETE":
            for archived in (vault / "_archive").glob(f"*/{rel}"):
                if archived.is_file():
                    archived.unlink()
    changelog = vault / "_sync" / f"{date.today().isoformat()}-{plan['evidence_run']['run_id']}.md"
    if changelog.exists():
        changelog.unlink()
    state_db = Path(plan["state_db"])
    state_backup = backup_root / "sync_state.sqlite"
    if journal.get("state_db_existed") and state_backup.is_file():
        shutil.copy2(state_backup, state_db)
    elif state_db.exists():
        state_db.unlink()
    journal["state"] = "rolled_back"
    _atomic_write(journal_path, json.dumps(journal, indent=2) + "\n")
    return {"rolled_back": True, "journal": str(journal_path), "backup": str(backup_root)}


def purge(vault: str | Path, older_than_days: int, state_db: str | Path | None = None) -> int:
    if older_than_days < 0:
        raise SyncError("--older-than must be zero or greater")
    vault = Path(vault).resolve()
    state_db = Path(state_db) if state_db else vault / ".capture_index" / "brd_linker_sync.sqlite"
    cutoff = datetime.now(timezone.utc).timestamp() - older_than_days * 86400
    removed = 0
    with SyncStore(state_db) as store:
        for row in store.conn.execute("SELECT source_id,note_path,removed_at FROM entities WHERE status='removed'").fetchall():
            stamp = row["removed_at"] or ""
            try:
                age = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                age = 0
            if age <= cutoff:
                path = vault / row["note_path"]
                if path.is_file():
                    path.unlink()
                store.conn.execute("DELETE FROM entities WHERE source_id=?", (row["source_id"],))
                store.conn.execute("DELETE FROM links WHERE seg_id=? OR target_id=?", (row["source_id"], row["source_id"]))
                removed += 1
        store.conn.commit()
    return removed


def restore(vault: str | Path, source_id: str, state_db: str | Path | None = None,
            config: dict | None = None) -> str:
    vault = Path(vault).resolve()
    state_db = Path(state_db) if state_db else vault / ".capture_index" / "brd_linker_sync.sqlite"
    with SyncStore(state_db) as store:
        row = store.conn.execute("SELECT * FROM entities WHERE source_id=? AND status='removed'", (source_id,)).fetchone()
        if row is None:
            raise SyncError(f"No soft-deleted source found: {source_id}")
        archived = vault / row["note_path"]
        original = row["source_json"]
        original_path = _note_path(vault, row["kind"], source_id, json.loads(original))
        if not archived.is_file():
            raise SyncError(f"Archived note is missing: {archived}")
        original_path.parent.mkdir(parents=True, exist_ok=True)
        if original_path.exists():
            raise SyncError(f"Cannot restore; target path is occupied: {original_path}")
        text = archived.read_text(encoding="utf-8", errors="replace")
        text = set_managed_frontmatter(text, {"status": "active", "removed_at": None})
        _atomic_write(original_path, text)
        archived.unlink()
        rel = original_path.relative_to(vault).as_posix()
        store.conn.execute("UPDATE entities SET note_path=?,status='active',removed_at=NULL,absence_count=0 WHERE source_id=?",
                           (rel, source_id))
        # Rebuild proposed links for the restored source; no previous deleted link
        # decision is silently reinstated.
        cfg = config or {}
        retrieval = cfg.get("retrieval") or {}
        top_k = max(1, int(retrieval.get("top_k", 10)))
        cutoff = float(retrieval.get("drop_below", 0.35))
        stop_words = {"id", "name", "type", "status", "data", "value", "result", "success", "message",
                      "post", "get", "put", "patch", "delete", "api", "opc", "checkout", "sync", "http"}
        stop_words.update(str(word).casefold() for word in cfg.get("stoplist_params", []))
        restored_source = json.loads(original)
        if row["kind"] == "segment":
            segment = {"source_id": source_id, "source": restored_source}
            segment_text = str(restored_source.get("text") or "")
            targets = [r for r in store.entities() if r["kind"] in {"api", "param"} and r["status"] != "removed"]
            candidates = []
            for target in targets:
                target_source = json.loads(target["source_json"] or "{}")
                target_item = {"kind": target["kind"], "name": target_source.get("name") or target_source.get("key", ""),
                               "source": target_source}
                score = _retrieval_score(segment_text, _entity_terms(target_item), stop_words)
                if score >= cutoff:
                    candidates.append((score, target, target_source))
            candidates.sort(key=lambda item: (-item[0], item[1]["source_id"]))
            for score, target, _ in candidates[:top_k]:
                store.conn.execute("INSERT OR REPLACE INTO links(seg_id,target_id,state,evidence,seg_hash,tgt_hash,decided_by) VALUES(?,?,?,?,?,?,?)",
                    (source_id, target["source_id"], "proposed", f"restore token_overlap {score:.4f}",
                     row["source_hash"], target["source_hash"], "restore"))
        else:
            target_item = {"kind": row["kind"], "name": restored_source.get("name") or restored_source.get("key", ""),
                           "source": restored_source}
            segments = [r for r in store.entities() if r["kind"] == "segment" and r["status"] != "removed"]
            candidates = []
            for segment_row in segments:
                segment_source = json.loads(segment_row["source_json"] or "{}")
                score = _retrieval_score(str(segment_source.get("text") or ""), _entity_terms(target_item), stop_words)
                if score >= cutoff:
                    candidates.append((score, segment_row))
            candidates.sort(key=lambda item: (-item[0], item[1]["source_id"]))
            for score, segment_row in candidates[:top_k]:
                store.conn.execute("INSERT OR REPLACE INTO links(seg_id,target_id,state,evidence,seg_hash,tgt_hash,decided_by) VALUES(?,?,?,?,?,?,?)",
                    (segment_row["source_id"], source_id, "proposed", f"restore token_overlap {score:.4f}",
                     segment_row["source_hash"], row["source_hash"], "restore"))
        store.conn.commit()
        return rel
