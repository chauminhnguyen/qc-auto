"""Adapters from BRD Markdown and native replay artifacts into sync entities."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


@dataclass(frozen=True)
class Entity:
    kind: str
    key: str
    name: str
    attrs: dict = field(default_factory=dict)
    seen_in: tuple[str, ...] = ()

    @property
    def source_id(self) -> str:
        if self.kind == "api":
            return f"api:{self.key}"
        if self.kind == "param":
            return f"param:{self.key}"
        return self.key

    def to_dict(self) -> dict:
        data = asdict(self)
        data["seen_in"] = list(self.seen_in)
        data["source_id"] = self.source_id
        return data


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    ts: str
    complete: bool
    scope_summary: str
    status: str = "unknown"


@dataclass(frozen=True)
class Segment:
    brd_id: str
    key: str
    heading_path: tuple[str, ...]
    text: str

    @property
    def source_id(self) -> str:
        return f"brd:{self.brd_id}#{self.key}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["heading_path"] = list(self.heading_path)
        data["source_id"] = self.source_id
        return data


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def normalize_path(path: str, rules: dict | None = None) -> str:
    """Normalize dynamic route segments, while allowing configured overrides."""
    rules = rules or {}
    configured = rules.get("patterns", [])
    path = urlsplit(path).path or "/"
    parts = path.split("/")
    result = []
    for part in parts:
        if not part:
            result.append(part)
            continue
        replacement = None
        for rule in configured:
            if isinstance(rule, dict) and re.fullmatch(str(rule.get("pattern", "")), part):
                replacement = str(rule.get("replacement", "{id}"))
                break
        if replacement is None and rules.get("numeric_segments", True) and re.fullmatch(r"\d+", part):
            replacement = "{id}"
        if replacement is None and rules.get("uuid_segments", True) and re.fullmatch(
                r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", part):
            replacement = "{id}"
        result.append(replacement or part)
    normalized = "/".join(result).rstrip("/") or "/"
    return normalized or "/"


def _json_type(value) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, (dict, list)):
        return "object" if isinstance(value, dict) else "array"
    return "string"


def _walk_params(value, prefix=""):
    if isinstance(value, dict):
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            yield name, item
            if isinstance(item, (dict, list)):
                yield from _walk_params(item, name)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                yield from _walk_params(item, prefix)


def _parse_body(raw: str):
    if not raw:
        return []
    try:
        obj = json.loads(raw)
        return [(name, _json_type(value)) for name, value in _walk_params(obj)]
    except (TypeError, json.JSONDecodeError):
        if "<" in raw[:200] or len(raw) > 20000:
            return []
        return [(str(key), "string") for key, _ in parse_qsl(raw, keep_blank_values=True)
                if re.fullmatch(r"[A-Za-z0-9_.\[\]-]{1,128}", str(key))]


class EvidenceAdapter:
    """Read the newest replay report and enrich it from that capture session."""

    def __init__(self, capture_db: str | Path | None = None, key_normalization: dict | None = None):
        self.capture_db = Path(capture_db) if capture_db else None
        self.key_normalization = key_normalization or {}

    @staticmethod
    def newest_run(reports_dir: str | Path) -> Path:
        paths = list(Path(reports_dir).glob("*.json"))
        if not paths:
            raise FileNotFoundError(f"No native replay JSON found in {reports_dir}")
        return max(paths, key=lambda p: (p.stat().st_mtime_ns, p.name))

    def read(self, run: str | Path | dict) -> tuple[RunInfo, list[Entity]]:
        if isinstance(run, dict):
            data = run
        else:
            data = json.loads(Path(run).read_text(encoding="utf-8"))
        session_id = str(data.get("session_id") or "")
        steps = data.get("steps") or []
        unrun = sum(1 for step in steps if str(step.get("status", "")).casefold() in {"not_run", "skipped"})
        status = str(data.get("status") or "unknown").casefold()
        complete = status in {"passed", "completed_with_overrides"} and unrun == 0
        ts = str(data.get("started_at") or data.get("timestamp") or "")
        scope = (f"session {session_id}; {len(steps)} recorded steps, "
                 f"{len(data.get('api_observations') or [])} replay API observations; result={status}")

        calls = self._session_calls(session_id)
        observations = data.get("api_observations") or []
        api_groups: dict[str, dict] = {}
        param_groups: dict[tuple[str, str], dict] = {}
        for observation in observations:
            url = str(observation.get("url") or observation.get("request_url") or "")
            method = str(observation.get("method") or "GET").upper()
            path = normalize_path(url, self.key_normalization)
            key = f"{method} {path}"
            row = api_groups.setdefault(key, {"method": method, "path": path, "status_codes": set(),
                                               "action_ids": set(), "sequences": set(), "domain": ""})
            if observation.get("status") is not None:
                row["status_codes"].add(int(observation["status"]))
            parsed = urlsplit(url)
            row["domain"] = parsed.netloc or row["domain"]

        for call in calls:
            url = str(call.get("request_url") or "")
            method = str(call.get("method") or "GET").upper()
            path = normalize_path(url, self.key_normalization)
            key = f"{method} {path}"
            if key not in api_groups:
                continue
            row = api_groups[key]
            if call.get("status") is not None:
                row["status_codes"].add(int(call["status"]))
            if call.get("action_id"):
                row["action_ids"].add(str(call["action_id"]))
            if call.get("seq") is not None:
                row["sequences"].add(int(call["seq"]))
            sources = [("query", [(k, "string") for k, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)])]
            sources.append(("request", _parse_body(str(call.get("request_body") or ""))))
            for location, fields in sources:
                for name, value_type in fields:
                    pkey = (key, name)
                    p = param_groups.setdefault(pkey, {"api_key": key, "location": location,
                                                       "types": set(), "action_ids": set()})
                    p["types"].add(value_type)
                    if call.get("action_id"):
                        p["action_ids"].add(str(call["action_id"]))

        # Replay observations are authoritative for positive endpoint evidence.
        entities = []
        for key, row in sorted(api_groups.items()):
            entities.append(Entity("api", key, key, {
                "method": row["method"], "path": row["path"], "domain": row["domain"],
                "status_codes": sorted(row["status_codes"]),
                "action_ids": sorted(row["action_ids"]),
                "sequences": sorted(row["sequences"]),
            }, (session_id,) if session_id else ()))
        for (api_key, name), row in sorted(param_groups.items()):
            entities.append(Entity("param", f"{api_key}.{name}", name, {
                "api_key": api_key, "location": row["location"],
                "type": "/".join(sorted(row["types"])),
                "action_ids": sorted(row["action_ids"]),
            }, (session_id,) if session_id else ()))
        fallback_id = Path(run).stem if not isinstance(run, dict) else "unknown"
        return RunInfo(str(data.get("run_id") or fallback_id),
                       ts, complete, scope, status), entities

    def _session_calls(self, session_id: str) -> list[dict]:
        if not session_id or not self.capture_db or not self.capture_db.is_file():
            return []
        try:
            conn = sqlite3.connect(f"file:{self.capture_db.resolve().as_posix()}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                return [dict(row) for row in conn.execute(
                    "SELECT seq,method,status,request_url,request_body,response_body,action_id "
                    "FROM session_api_events WHERE session_id=? ORDER BY seq", (session_id,))]
            finally:
                conn.close()
        except sqlite3.Error:
            return []


class BrdAdapter:
    """Split Markdown BRDs into heading-scoped text segments."""

    def read(self, snapshot: str | Path | list[str | Path]) -> list[Segment]:
        paths = [Path(snapshot)] if isinstance(snapshot, (str, Path)) else [Path(p) for p in snapshot]
        segments: list[Segment] = []
        for path in sorted(paths, key=lambda p: p.as_posix().casefold()):
            if not path.is_file():
                continue
            text = normalize_text(path.read_text(encoding="utf-8-sig", errors="replace"))
            lines = text.splitlines()
            headings = []
            for index, line in enumerate(lines):
                match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
                if match:
                    headings.append((index, len(match.group(1)), match.group(2).strip()))
            if not headings:
                if text:
                    segments.append(Segment(path.stem, "document", (path.stem,), text))
                continue
            parents: list[str] = []
            seen: dict[str, int] = {}
            for pos, (line_index, level, heading) in enumerate(headings):
                parents = parents[:level - 1]
                parents.append(heading)
                end = len(lines)
                if pos + 1 < len(headings):
                    end = headings[pos + 1][0]
                body = "\n".join(lines[line_index:end]).strip()
                heading_path = tuple(parents)
                raw_key = "/".join(_slug(item) for item in heading_path)
                seen[raw_key] = seen.get(raw_key, 0) + 1
                key = raw_key if seen[raw_key] == 1 else f"{raw_key}~{seen[raw_key]}"
                if body:
                    segments.append(Segment(path.stem, key, heading_path, body))
        return segments


def _slug(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "section"
