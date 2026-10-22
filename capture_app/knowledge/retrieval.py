"""Incremental lexical index and anchor extraction for Jira retrieval."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import math
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")
_TICKET = re.compile(r"\b[A-Z][A-Z0-9_]*-\d+\b", re.I)
_HTTP_ERROR = re.compile(r"\b(?:HTTP\s*)?([45]\d{2})\b", re.I)
_ERROR_CODE = re.compile(
    r"\b(?:(?:ERR|ERROR)[_-]?[A-Z0-9]{2,}(?:[_-][A-Z0-9]+)*|E\d{2,})\b", re.I
)
_API_PATH = re.compile(r"(?<![\w.])/(?:[A-Za-z0-9_.{}:-]+/)*[A-Za-z0-9_.{}:-]+(?:\?[A-Za-z0-9_=&{}.-]+)?")
_QUOTED = re.compile(
    r"(?:\"([^\"\n]{2,80})\"|\'([^\'\n]{2,80})\'|\u201c([^\u201d\n]{2,80})\u201d|"
    r"\u2018([^\u2019\n]{2,80})\u2019)"
)
_CAMEL = re.compile(r"\b(?:[A-Z][a-z0-9]+|[a-z][a-z0-9]+)(?:[A-Z][A-Za-z0-9]*)+\b")
_SNAKE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b")


@dataclass(frozen=True)
class Anchor:
    text: str
    kind: str
    weight: int


@dataclass(frozen=True)
class VaultChunk:
    chunk_id: str
    path: str
    heading: str
    title: str
    aliases: str
    frontmatter: str
    body: str
    wikilinks: tuple[str, ...]
    content_hash: str


def extract_anchors(text: str) -> list[Anchor]:
    """Extract high-signal identifiers from ticket text, preserving source spelling."""
    found: dict[tuple[str, str], Anchor] = {}

    def add(values, kind: str, weight: int):
        for value in values:
            value = str(value).strip()
            if kind == "api_path":
                value = value.rstrip(".,;:!?)]")
            else:
                value = value.strip(".,;:!?()[]{}")
            if value:
                key = (value.casefold(), kind)
                found.setdefault(key, Anchor(value, kind, weight))

    add(_API_PATH.findall(text), "api_path", 3)
    add(_HTTP_ERROR.findall(text), "error_code", 3)
    add(_ERROR_CODE.findall(text), "error_code", 3)
    add(_TICKET.findall(text), "ticket_key", 2)
    add([next((group for group in match if group), "") for match in _QUOTED.findall(text)],
        "quoted_label", 2)
    add(_CAMEL.findall(text), "identifier", 1)
    add(_SNAKE.findall(text), "identifier", 1)
    return sorted(found.values(), key=lambda item: (-item.weight, item.text.casefold(), item.kind))


def _split_frontmatter(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", text
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        return "", text
    return "\n".join(lines[1:end]), "\n".join(lines[end + 1:])


def _frontmatter_values(frontmatter: str) -> tuple[str, str]:
    """Read title and aliases from common YAML frontmatter spellings."""
    title = ""
    aliases: list[str] = []
    alias_list = False
    for line in frontmatter.splitlines():
        match = re.match(r"^\s*([A-Za-z0-9_-]+)\s*:\s*(.*?)\s*$", line)
        if match:
            key, value = match.group(1).casefold(), match.group(2)
            alias_list = key == "aliases" and not value
            if key in {"title", "name"} and value:
                title = _unquote(value)
            elif key == "aliases" and value:
                aliases.extend(_list_values(value))
            continue
        if alias_list:
            item = re.match(r"^\s*-\s*(.*?)\s*$", line)
            if item:
                aliases.append(_unquote(item.group(1)))
            elif line.strip():
                alias_list = False
    return title, " ".join(dict.fromkeys(value for value in aliases if value))


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _list_values(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1]
        return [_unquote(item.strip()) for item in re.split(r",\s*(?=(?:[^\"']|\"[^\"]*\"|'[^']*')*$)", inner) if item.strip()]
    return [_unquote(value)]


def _document_title(path: Path, body: str, frontmatter_title: str) -> str:
    if frontmatter_title:
        return frontmatter_title
    for line in body.splitlines():
        match = _HEADING.match(line)
        if match and len(match.group(1)) == 1:
            return match.group(2).strip()
    return path.stem


def _body_chunks(body: str, breadcrumbs: list[str], chunk_chars: int) -> list[tuple[str, str, int]]:
    paragraphs = re.split(r"\n\s*\n", body.strip())
    prefix = "\n".join(f"{'#' * min(index + 1, 6)} {heading}" for index, heading in enumerate(breadcrumbs))
    heading = " / ".join(breadcrumbs)
    body_limit = max(1, chunk_chars - len(prefix) - (2 if prefix else 0))
    output: list[tuple[str, str, int]] = []
    current: list[str] = []
    size = len(prefix)

    def flush():
        nonlocal current, size
        if current:
            value = "\n\n".join(current)
            output.append((prefix + ("\n\n" if prefix else "") + value, heading, len(output)))
            current = []
            size = len(prefix)

    for paragraph in paragraphs:
        if len(paragraph) > body_limit:
            flush()
            # Prefer line boundaries. A single long line is split at the configured ceiling.
            fragments: list[str] = []
            for line in paragraph.splitlines() or [paragraph]:
                while len(line) > body_limit:
                    fragments.append(line[:body_limit])
                    line = line[body_limit:]
                if line:
                    fragments.append(line)
            part = ""
            for fragment in fragments:
                candidate = (part + "\n" + fragment).strip()
                if len(candidate) > chunk_chars and part:
                    output.append((prefix + ("\n\n" if prefix else "") + part, heading, len(output)))
                    part = fragment
                else:
                    part = candidate
            if part:
                output.append((prefix + ("\n\n" if prefix else "") + part, heading, len(output)))
            continue
        extra = len(paragraph) + (2 if current else (2 if prefix else 0))
        if current and size + extra > chunk_chars:
            flush()
        current.append(paragraph)
        size += extra
    flush()
    return output


def parse_markdown_chunks(path: Path, relative_path: str, text: str,
                          chunk_chars: int = 8000) -> list[VaultChunk]:
    frontmatter, document = _split_frontmatter(text)
    frontmatter_title, aliases = _frontmatter_values(frontmatter)
    title = _document_title(path, document, frontmatter_title)
    section_rows: list[tuple[list[str], list[str]]] = []
    breadcrumbs: list[str] = []
    section_lines: list[str] = []
    for line in document.splitlines():
        match = _HEADING.match(line)
        if match:
            if section_lines:
                section_rows.append((breadcrumbs.copy(), section_lines))
            level = len(match.group(1))
            breadcrumbs = breadcrumbs[:level - 1]
            breadcrumbs.append(match.group(2).strip())
            section_lines = []
        else:
            section_lines.append(line)
    if section_lines:
        section_rows.append((breadcrumbs.copy(), section_lines))
    if not section_rows and document.strip():
        section_rows.append(([], document.splitlines()))

    chunks: list[VaultChunk] = []
    for section_index, (crumb, lines) in enumerate(section_rows):
        for content, heading, part in _body_chunks("\n".join(lines), crumb, chunk_chars):
            if not content.strip():
                continue
            stable_id = hashlib.sha256(
                f"{relative_path}\0{heading}\0{section_index}\0{part}".encode("utf-8")
            ).hexdigest()[:32]
            link_targets = tuple(dict.fromkeys(match.group(1).strip() for match in _WIKILINK.finditer(content)))
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            chunks.append(VaultChunk(stable_id, relative_path, heading, title, aliases,
                                     frontmatter, content, link_targets, content_hash))
    return chunks


def _connect_ledger(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS vault_files (
        path TEXT PRIMARY KEY, content_hash TEXT NOT NULL, indexed_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS vault_chunks (
        chunk_id TEXT PRIMARY KEY, path TEXT NOT NULL, heading TEXT NOT NULL,
        title TEXT NOT NULL, aliases TEXT NOT NULL, frontmatter TEXT NOT NULL,
        body TEXT NOT NULL, wikilinks TEXT NOT NULL, content_hash TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vault_chunks_path ON vault_chunks(path)")
    conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS vault_chunks_fts USING fts5(
        chunk_id UNINDEXED, path, title, aliases, body,
        tokenize='unicode61 remove_diacritics 2')""")
    conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS vault_identifiers_fts USING fts5(
        chunk_id UNINDEXED, identifiers, tokenize='trigram')""")
    return conn


def _markdown_files(vault_path: Path):
    excluded = {".obsidian", ".capture_index", ".git", "node_modules"}
    for path in sorted(vault_path.rglob("*.md")):
        relative = path.relative_to(vault_path)
        if excluded.intersection(relative.parts) or "_state" in relative.parts:
            continue
        yield path, relative.as_posix()


def index_vault(vault_path: Path, ledger_path: Path | None = None,
                chunk_chars: int = 8000) -> dict:
    """Incrementally index Markdown nodes; unchanged files are read but not re-chunked."""
    vault_path = Path(vault_path).resolve()
    ledger_path = Path(ledger_path or vault_path / "Knowledge" / "_state" / "ledger.sqlite")
    conn = _connect_ledger(ledger_path)
    stats = {"files_seen": 0, "files_changed": 0, "files_removed": 0, "chunks_added": 0}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    seen: set[str] = set()
    try:
        with conn:
            for path, relative in _markdown_files(vault_path):
                seen.add(relative)
                stats["files_seen"] += 1
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                file_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                previous = conn.execute("SELECT content_hash FROM vault_files WHERE path=?", (relative,)).fetchone()
                if previous and previous[0] == file_hash:
                    continue
                chunks = parse_markdown_chunks(path, relative, text, chunk_chars)
                old_ids = [row[0] for row in conn.execute("SELECT chunk_id FROM vault_chunks WHERE path=?", (relative,))]
                for chunk_id in old_ids:
                    conn.execute("DELETE FROM vault_chunks_fts WHERE chunk_id=?", (chunk_id,))
                    conn.execute("DELETE FROM vault_identifiers_fts WHERE chunk_id=?", (chunk_id,))
                conn.execute("DELETE FROM vault_chunks WHERE path=?", (relative,))
                for chunk in chunks:
                    identifier_text = " ".join((chunk.path, chunk.title, chunk.aliases,
                                                  chunk.frontmatter, chunk.heading))
                    conn.execute("INSERT INTO vault_chunks VALUES (?,?,?,?,?,?,?,?,?)",
                                 (chunk.chunk_id, chunk.path, chunk.heading, chunk.title, chunk.aliases,
                                  chunk.frontmatter, chunk.body, json.dumps(chunk.wikilinks), chunk.content_hash))
                    conn.execute("INSERT INTO vault_chunks_fts (chunk_id,path,title,aliases,body) VALUES (?,?,?,?,?)",
                                 (chunk.chunk_id, chunk.path, chunk.title, chunk.aliases, chunk.body))
                    conn.execute("INSERT INTO vault_identifiers_fts (chunk_id,identifiers) VALUES (?,?)",
                                 (chunk.chunk_id, identifier_text))
                    stats["chunks_added"] += 1
                conn.execute("INSERT OR REPLACE INTO vault_files VALUES (?,?,?)", (relative, file_hash, now))
                stats["files_changed"] += 1
            stored = {row[0] for row in conn.execute("SELECT path FROM vault_files")}
            for removed in stored - seen:
                ids = [row[0] for row in conn.execute("SELECT chunk_id FROM vault_chunks WHERE path=?", (removed,))]
                for chunk_id in ids:
                    conn.execute("DELETE FROM vault_chunks_fts WHERE chunk_id=?", (chunk_id,))
                    conn.execute("DELETE FROM vault_identifiers_fts WHERE chunk_id=?", (chunk_id,))
                conn.execute("DELETE FROM vault_chunks WHERE path=?", (removed,))
                conn.execute("DELETE FROM vault_files WHERE path=?", (removed,))
                stats["files_removed"] += 1
    finally:
        conn.close()
    return stats


_TOKEN = re.compile(r"[^\W_]+(?:_[^\W_]+)*", re.UNICODE)


def _fold(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    return "".join(char for char in value if not unicodedata.combining(char))


def _query_terms(text: str, rows: list[sqlite3.Row], config) -> list[str]:
    query_tokens = Counter(_fold(token) for token in _TOKEN.findall(text))
    stopwords = {_fold(word) for word in getattr(config, "stopwords", ())}
    query_tokens = Counter({term: count for term, count in query_tokens.items()
                            if len(term) > 1 and term not in stopwords})
    if not query_tokens:
        return []
    document_frequency: Counter[str] = Counter()
    for row in rows:
        document_frequency.update({_fold(token) for token in _TOKEN.findall(row["body"])})
    document_count = max(1, len(rows))
    scored = []
    for term, frequency in query_tokens.items():
        inverse_frequency = math.log((document_count + 1) / (document_frequency.get(term, 0) + 1)) + 1
        scored.append((frequency * inverse_frequency, term))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [term for _, term in scored[:int(getattr(config, "query_terms", 12))]]


def _snippet(body: str, terms: list[str], limit: int = 240) -> str:
    normalized = _fold(body)
    positions = [normalized.find(term) for term in terms if normalized.find(term) >= 0]
    start = max(0, min(positions) - limit // 3) if positions else 0
    value = re.sub(r"\s+", " ", body[start:start + limit]).strip()
    if start:
        value = "…" + value
    if start + limit < len(body):
        value += "…"
    return value


def _resolve_link(target: str, aliases: dict[str, set[str]]) -> str | None:
    target = target.strip().replace("\\", "/").removesuffix(".md")
    matches = aliases.get(_fold(target), set())
    return next(iter(matches)) if len(matches) == 1 else None


def rank_issue_nodes(vault_path: Path, issue_text: str, config,
                     exclude_path: str | None = None,
                     ledger_path: Path | None = None,
                     refresh_index: bool = True) -> list[dict]:
    """Rank directly evidenced Markdown notes for one Jira issue."""
    vault_path = Path(vault_path).resolve()
    ledger_path = Path(ledger_path or vault_path / "Knowledge" / "_state" / "ledger.sqlite")
    if refresh_index:
        index_vault(vault_path, ledger_path, int(getattr(config, "chunk_chars", 8000)))
    conn = _connect_ledger(ledger_path)
    conn.row_factory = sqlite3.Row
    try:
        all_rows = conn.execute("SELECT * FROM vault_chunks ORDER BY path,chunk_id").fetchall()
        rows = [row for row in all_rows if not row["path"].startswith("Jira/")]
        if not rows:
            return []
        excluded = {exclude_path.replace("\\", "/")} if exclude_path else set()
        anchors = extract_anchors(issue_text)
        query_terms = _query_terms(issue_text, rows, config)
        term_query = " OR ".join('"' + term.replace('"', '""') + '"' for term in query_terms)

        trigram_hits: dict[str, set[str]] = {}
        trigram_available = True
        for anchor in anchors:
            if len(anchor.text) < 3:
                continue
            try:
                phrase = '"' + anchor.text.replace('"', '""') + '"'
                trigram_hits[anchor.text.casefold()] = {
                    row[0] for row in conn.execute(
                        "SELECT chunk_id FROM vault_identifiers_fts WHERE identifiers MATCH ?",
                        (phrase,),
                    )
                }
            except sqlite3.Error:
                trigram_available = False
                break

        anchor_rows: dict[str, dict] = {}
        for row in rows:
            if row["path"] in excluded:
                continue
            searchable = "\n".join((row["path"], row["title"], row["aliases"], row["frontmatter"]))
            matched = [
                anchor for anchor in anchors
                if (len(anchor.text) < 3 or not trigram_available
                    or row["chunk_id"] in trigram_hits.get(anchor.text.casefold(), set()))
                and anchor.text.casefold() in searchable.casefold()
            ]
            if not matched:
                continue
            current = anchor_rows.setdefault(
                row["path"], {"score": 0, "anchors": set(), "chunk": row}
            )
            for anchor in matched:
                folded = anchor.text.casefold()
                if folded not in current["anchors"]:
                    current["anchors"].add(folded)
                    current["score"] += anchor.weight

        bm25_rows: dict[str, dict] = {}
        if term_query:
            sql = """SELECT c.*, bm25(vault_chunks_fts) AS bm25_score
                     FROM vault_chunks_fts JOIN vault_chunks AS c USING(chunk_id)
                     WHERE vault_chunks_fts MATCH ? AND c.path NOT LIKE 'Jira/%'
                     ORDER BY bm25_score,c.path,c.chunk_id"""
            note_rank = 0
            for row in conn.execute(sql, (term_query,)).fetchall():
                if row["path"] in excluded or row["path"].startswith("Jira/"):
                    continue
                body = _fold(row["body"])
                matched_terms = [term for term in query_terms if term in body]
                if row["path"] in bm25_rows:
                    bm25_rows[row["path"]]["terms"] = sorted(
                        set(bm25_rows[row["path"]]["terms"]) | set(matched_terms)
                    )
                    continue
                note_rank += 1
                bm25_rows[row["path"]] = {
                    "rank": note_rank, "row": row,
                    "terms": matched_terms,
                    "bm25": float(row["bm25_score"]),
                }

        anchor_order = sorted(anchor_rows, key=lambda path: (-anchor_rows[path]["score"], path.casefold()))
        bm25_order = sorted(bm25_rows, key=lambda path: (bm25_rows[path]["rank"], path.casefold()))
        anchor_rank = {path: rank for rank, path in enumerate(anchor_order, 1)}
        bm25_rank = {path: rank for rank, path in enumerate(bm25_order, 1)}
        direct_paths = set(anchor_rows) | set(bm25_rows)
        if not direct_paths:
            return []
        rrf_k = int(getattr(config, "rrf_k", 60))
        anchor_rrf = {path: 1.0 / (rrf_k + anchor_rank[path]) for path in anchor_order}
        bm25_rrf = {path: 1.0 / (rrf_k + bm25_rank[path]) for path in bm25_order}
        max_anchor = max(anchor_rrf.values(), default=0.0)
        max_bm25 = max(bm25_rrf.values(), default=0.0)
        anchor_weight = float(getattr(config, "anchor_weight", 1.5)) if max_anchor else 0.0
        bm25_weight = float(getattr(config, "bm25_weight", 1.0)) if max_bm25 else 0.0
        total_weight = anchor_weight + bm25_weight or 1.0
        lexical_scores: dict[str, float] = {}
        details: dict[str, dict] = {}
        for path in direct_paths:
            anchor_part = anchor_rrf.get(path, 0.0) / max_anchor if max_anchor else 0.0
            bm25_part = bm25_rrf.get(path, 0.0) / max_bm25 if max_bm25 else 0.0
            lexical_scores[path] = (anchor_weight * anchor_part + bm25_weight * bm25_part) / total_weight
            anchor_info = anchor_rows.get(path, {})
            bm25_info = bm25_rows.get(path, {})
            best_row = bm25_info.get("row") or anchor_info.get("chunk")
            matched_anchors = [item.text for item in anchors
                               if item.text.casefold() in anchor_info.get("anchors", set())]
            matched_terms = bm25_info.get("terms", [])
            details[path] = {
                "path": path,
                "anchor_score": anchor_info.get("score", 0),
                "anchor_part": anchor_part,
                "bm25_part": bm25_part,
                "graph_part": 0.0,
                "matched_anchors": matched_anchors,
                "matched_terms": matched_terms,
                "snippet": _snippet(best_row["body"], matched_terms or matched_anchors) if best_row else "",
                "chunk_heading": best_row["heading"] if best_row else "",
                "shared_links": [],
            }

        aliases: dict[str, set[str]] = defaultdict(set)
        for row in all_rows:
            path = row["path"]
            names = (path, path.removesuffix(".md"), Path(path).stem,
                     row["title"], row["aliases"])
            for name in names:
                for alias in re.split(r"\s+", str(name)):
                    if alias:
                        aliases[_fold(alias)].add(path)
        links_by_path: dict[str, set[str]] = defaultdict(set)
        for row in all_rows:
            try:
                targets = json.loads(row["wikilinks"])
            except (TypeError, ValueError):
                continue
            links_by_path[row["path"]].update(
                resolved for target in targets if (resolved := _resolve_link(target, aliases))
            )

        seeds = sorted(direct_paths, key=lambda path: (-lexical_scores[path], path.casefold()))[
            :int(getattr(config, "graph_seeds", 5))]
        graph_raw: dict[str, float] = defaultdict(float)
        graph_links: dict[str, set[str]] = defaultdict(set)
        for rank, seed in enumerate(seeds, 1):
            neighbors = set(links_by_path.get(seed, set()))
            neighbors.update(path for path, targets in links_by_path.items() if seed in targets)
            for neighbor in neighbors & direct_paths:
                if neighbor != seed:
                    graph_raw[neighbor] += 1.0 / rank
                    graph_links[neighbor].add(seed)
        max_graph = max(graph_raw.values(), default=0.0)

        candidates = []
        minimum_terms = int(getattr(config, "min_matched_terms", 2))
        for path in direct_paths:
            has_evidence = bool(details[path]["matched_anchors"]) or (
                len(details[path]["matched_terms"]) >= minimum_terms
            )
            if not has_evidence:
                continue
            graph_part = graph_raw.get(path, 0.0) / max_graph if max_graph else 0.0
            details[path]["graph_part"] = graph_part
            details[path]["shared_links"] = sorted(graph_links.get(path, set()))
            final_score = lexical_scores[path] + float(getattr(config, "graph_weight", 0.15)) * graph_part
            candidates.append((final_score, path))
        candidates.sort(key=lambda item: (-item[0], item[1].casefold()))
        if not candidates:
            return []
        best_score = candidates[0][0]
        threshold = float(getattr(config, "relative_threshold", 0.55))
        top_k = int(getattr(config, "top_k", 10))
        return [dict(details[path], score=score, lexical_score=lexical_scores[path])
                for score, path in candidates if score >= best_score * threshold][:top_k]
    finally:
        conn.close()


def similar_jira_tickets(vault_path: Path, issue_text: str, exclude_keys: set[str] | None = None,
                          limit: int = 5, ledger_path: Path | None = None) -> list[dict]:
    """Return similar indexed Jira notes separately from ordinary vault matches."""
    vault_path = Path(vault_path).resolve()
    ledger_path = Path(ledger_path or vault_path / "Knowledge" / "_state" / "ledger.sqlite")
    conn = _connect_ledger(ledger_path)
    conn.row_factory = sqlite3.Row
    excluded = {key.upper() for key in (exclude_keys or set())}
    try:
        terms = list(dict.fromkeys(_fold(value) for value in _TOKEN.findall(issue_text)))
        terms = [term for term in terms if len(term) > 1]
        if not terms:
            return []
        query = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms[:12])
        output: list[dict] = []
        seen: set[str] = set()
        sql = """SELECT c.path,c.body,bm25(vault_chunks_fts) AS score
                 FROM vault_chunks_fts JOIN vault_chunks AS c USING(chunk_id)
                 WHERE vault_chunks_fts MATCH ? AND c.path LIKE 'Jira/%'
                 ORDER BY score,c.path"""
        for row in conn.execute(sql, (query,)).fetchall():
            key = Path(row["path"]).stem.upper()
            if key in excluded or row["path"] in seen:
                continue
            seen.add(row["path"])
            output.append({"key": key, "path": row["path"], "score": float(row["score"]),
                           "snippet": _snippet(row["body"], terms)})
            if len(output) >= limit:
                break
        return output
    finally:
        conn.close()
