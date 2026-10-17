"""Read-only access to capture entities and generated vault notes."""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path


def slugify(text: str, maxlen: int = 60) -> str:
    """Match capture_app.storage.vault.slugify without importing its writer."""
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")
    return (value or "root")[:maxlen]


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(ch for ch in value if ch.isalnum())


def _template_path(path: str) -> str:
    pieces = [piece for piece in (path or "/").split("/") if piece]
    for index, piece in enumerate(pieces):
        parent = pieces[index - 1].casefold() if index else ""
        if re.fullmatch(r"\d+", piece) and parent in {
                "catalog", "details", "orderdetails", "product", "products", "item", "items"}:
            pieces[index] = "{" + {"catalog": "productId", "details": "productId",
                                   "orderdetails": "orderId", "product": "productId"}.get(parent, "id") + "}"
    return "/" + "/".join(pieces)


def _canonical_param(name: str) -> str:
    if re.fullmatch(r"product_attribute_\d+_\d+_\d+", name, re.I):
        return "product_attribute"
    if re.fullmatch(r"addtocart_\d+\.EnteredQuantity", name, re.I):
        return "addtocart_EnteredQuantity"
    return name


@dataclass(frozen=True)
class Entity:
    id: str
    kind: str
    title: str
    path: str
    aliases: tuple[str, ...]

    @property
    def wikilink(self) -> str:
        return f"[[{self.path}]]"


@dataclass
class EntityCatalog:
    entities: list[Entity]

    def __post_init__(self):
        self.by_id = {entity.id: entity for entity in self.entities}
        self.by_exact: dict[str, list[Entity]] = {}
        self.by_norm: dict[str, list[Entity]] = {}
        for entity in self.entities:
            for alias in (entity.id, entity.path, entity.title, *entity.aliases):
                exact_matches = self.by_exact.setdefault(alias, [])
                if entity not in exact_matches:
                    exact_matches.append(entity)
                key = normalize_name(alias)
                if key:
                    matches = self.by_norm.setdefault(key, [])
                    if entity not in matches:
                        matches.append(entity)

    def prompt_rows(self) -> list[dict]:
        return [{"id": e.id, "kind": e.kind, "title": e.title,
                 "path": e.path, "aliases": list(e.aliases)} for e in self.entities]

    def resolve(self, mention: str, threshold: float = 0.92,
                margin: float = 0.08) -> tuple[Entity | None, str]:
        exact = self.by_exact.get(mention, [])
        if len(exact) == 1:
            return exact[0], "exact"
        if len(exact) > 1:
            return None, "ambiguous"
        key = normalize_name(mention)
        exact = self.by_norm.get(key, [])
        if len(exact) == 1:
            return exact[0], "normalized"
        if len(exact) > 1:
            return None, "ambiguous"

        # SequenceMatcher is standard-library only. A margin check prevents
        # silently choosing between similarly named endpoints/parameters.
        from difflib import SequenceMatcher
        ranked = []
        for entity in self.entities:
            names = (entity.id, entity.path, entity.title, *entity.aliases)
            score = max((SequenceMatcher(None, key, normalize_name(n)).ratio()
                         for n in names if normalize_name(n)), default=0.0)
            if score >= threshold:
                ranked.append((score, entity))
        ranked.sort(key=lambda row: row[0], reverse=True)
        if not ranked:
            return None, "unresolved"
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < margin:
            return None, "ambiguous"
        return ranked[0][1], "fuzzy"


def open_index_readonly(db_path: Path) -> sqlite3.Connection:
    """Open an existing capture index without invoking VaultWriter or writes."""
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"Capture index not found: {db_path}")
    uri = db_path.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if row is None:
        return set()
    # Reading table_info is a PRAGMA; schema inspection is deliberately
    # avoided so all index access remains SELECT-only. The known schema is
    # detected by checking its CREATE TABLE SQL from sqlite_master.
    sql = row["sql"] or ""
    return set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s+(?:TEXT|INTEGER|REAL)", sql, re.I))


def _primary_domains(conn: sqlite3.Connection) -> set[str]:
    """Find the captured site's host(s) from sessions, or the most frequent API host."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "sessions" in tables:
        try:
            rows = conn.execute("SELECT start_url FROM sessions WHERE start_url IS NOT NULL").fetchall()
            hosts = {re.sub(r":\d+$", "", __import__("urllib.parse", fromlist=["urlparse"]).urlparse(r[0]).hostname or "").casefold()
                     for r in rows}
            hosts.discard("")
            if hosts:
                return hosts
        except sqlite3.Error:
            pass
    if "apis" not in tables:
        return set()
    try:
        rows = conn.execute("SELECT domain, COUNT(*) FROM apis GROUP BY domain ORDER BY COUNT(*) DESC").fetchall()
        if not rows:
            return set()
        highest = rows[0][1]
        return {str(domain).split(":", 1)[0].casefold() for domain, count in rows if count == highest}
    except sqlite3.Error:
        return set()


def build_entity_catalog(vault_path: Path, conn: sqlite3.Connection,
                         include_third_party: bool = False) -> EntityCatalog:
    vault_path = Path(vault_path)
    entities: list[Entity] = []
    api_cols = _table_columns(conn, "apis")
    required_api = {"method", "url_path", "domain"}
    if required_api.issubset(api_cols):
        primary_domains = _primary_domains(conn)
        has_template = "path_template" in api_cols
        select_template = ", path_template" if has_template else ""
        rows = conn.execute(f"SELECT method, url_path, domain, full_url{select_template} FROM apis").fetchall()
        seen_routes = set()
        for row in rows:
            method, url_path, domain = row["method"], row["url_path"], row["domain"]
            host = str(domain).split(":", 1)[0].casefold()
            if not include_third_party and primary_domains and host not in primary_domains:
                continue
            path_template = row["path_template"] if has_template else _template_path(url_path)
            path_template = path_template or _template_path(url_path)
            route_key = (method, domain, path_template)
            if route_key in seen_routes:
                continue
            seen_routes.add(route_key)
            stem = f"{method}_{slugify(path_template)}"
            path = f"APIs/{slugify(domain)}/{stem}"
            if not (vault_path / f"{path}.md").is_file():
                continue
            entities.append(Entity(
                id=path, kind="api", title=f"{method} {url_path}", path=path,
                aliases=tuple(x for x in (stem, row["full_url"], url_path,
                                          f"{domain} {method} {url_path}") if x),
            ))

    param_cols = _table_columns(conn, "params")
    if {"name", "resource_key"}.issubset(param_cols):
        rows = conn.execute("SELECT name, resource_key FROM params").fetchall()
        seen_params = set()
        for row in rows:
            name, resource_key = row["name"], row["resource_key"] or ""
            canonical_name = _canonical_param(name)
            path = f"Params/{slugify(resource_key)}__{slugify(canonical_name)}"
            key = (resource_key, canonical_name)
            if key in seen_params:
                continue
            seen_params.add(key)
            if not (vault_path / f"{path}.md").is_file():
                continue
            entities.append(Entity(
                id=path, kind="param", title=name, path=path,
                aliases=(name, canonical_name, resource_key, f"{resource_key}::{name}"),
            ))

    flows_dir = vault_path / "Flows"
    if flows_dir.is_dir():
        for note in sorted(flows_dir.rglob("*.md")):
            relative = note.relative_to(vault_path).with_suffix("").as_posix()
            title = note.stem
            aliases = [title]
            try:
                text = note.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            match = re.search(r"(?m)^api:\s*(.+?)\s*$", text)
            if match:
                aliases.append(match.group(1).strip().strip("\"'"))
            entities.append(Entity(relative, "flow", title, relative, tuple(aliases)))
    return EntityCatalog(entities)


def read_only_connection(vault_path: Path) -> sqlite3.Connection:
    return open_index_readonly(Path(vault_path) / ".capture_index" / "index.sqlite")
