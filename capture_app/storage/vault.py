"""
vault.py
--------
Owns the on-disk "database" (SQLite) and regenerates the Obsidian-readable
Markdown files from it. This is the ONLY module that should touch the
filesystem for vault data. It's designed to be called from a single writer
task (see capture.py) so there is never a race between two writes.

Vault layout:

    <vault>/
      APIs/<domain>/<METHOD>_<path-slug>.md
      Params/<resource>__<param-name>.md
      Screenshots/<domain>/<timestamp>.png   <- UI state right after each captured call
      Flows/<session_id>.yaml       <- runnable Maestro flow for one capture.py session
      Flows/<session_id>.md         <- same flow, human-readable + linked to its APIs
      .capture_index/index.sqlite   <- source of truth, .md files are regenerated from this

Every capture.py run is a "session" (an id generated at startup). Every tap,
text entry, and page navigation the user makes gets logged in order via
record_step(); export_maestro_flow() turns that ordered log into a Maestro
YAML flow plus a companion note once the session ends.

A param's identity is (resource, name), not just name -- see _resource_key().
Two APIs can both have an "id" field that mean completely different things
(a pet's id vs a user's id); scoping by resource keeps those separate while
still linking up genuinely-repeated params within the same resource.

Run this module directly (`python vault.py`) for an interactive cleanup
pass: it lists every captured API, flags likely noise (third-party domains,
telemetry-looking paths) as suggestions, and lets you pick which to
permanently remove -- typically after capture.py and before enrich.py.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit, parse_qs, parse_qsl, urlencode

MAX_BODY_CHARS = 5000       # truncate huge payloads/responses in the .md view
MAX_EXAMPLE_VALUES = 10     # how many distinct example values to keep per param (per role)
MAX_RESPONSE_LIST_ITEMS = 20  # cap when a response body is a JSON array of objects
MAX_SCREENSHOTS_PER_API = 5   # keep only the N most recent UI screenshots per endpoint
SCENARIO_ANNOTATION_VERSION = 1
SCENARIO_CONSTRAINT_SOURCES = {"curated", "openapi", "json-schema", "observed"}
SCENARIO_REPLAY_RISKS = {"safe", "mutating", "destructive", "unknown"}

# Path substrings that read as telemetry/analytics/tracking rather than app
# functionality -- used by VaultWriter.review_interactive() to *suggest*
# removals. capture.py's IGNORE_DOMAIN_SUBSTRINGS already blocks known
# third-party trackers by domain before they ever reach the vault; this
# catches the same *kind* of traffic when it's served from the app's own
# domain (first-party analytics, internal telemetry endpoints) instead.
NOISE_PATH_HINTS = [
    "telemetry", "analytics", "beacon", "pixel", "collect", "metrics",
    "tracking", "heartbeat", "/ping", "/log", "logging", "diagnostics",
]

# Obsidian graph-view node colors. Color groups use Obsidian search queries;
# RGB values are decimal integers as required by graph.json.
GRAPH_COLOR_GROUPS = {
    "tag:#api": 3900150,                    # #3b82f6 blue
    "tag:#param": 16096779,                 # #f59e0b orange
    "path:BRDs": 9135350,                    # #8b5cf6 purple
    "tag:#flow": 1355942,                   # #14b8a6 teal
    "path:Reports/native_runs": 15485081,   # #ec4899 pink
    "path:Screenshots": 10066329,           # #999999 gray
    "path:Knowledge/Reviewed": 2278750,     # #22c55e green
    "path:Knowledge/_inbox": 16347926,      # #f97316 orange-red
    "path:Knowledge/Changes": 15680580,     # #ef4444 red
    "path:Knowledge/Reports": 440020,       # #06b6d4 cyan
    "path:Knowledge/QUESTIONS.md": 15337224, # #eab308 yellow
}


def slugify(text: str, maxlen: int = 60) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")
    return (text or "root")[:maxlen]


def template_url_path(path: str) -> str:
    """Replace concrete path identifiers with stable route placeholders."""
    segments = [segment for segment in (path or "/").split("/") if segment]
    output = []
    for index, segment in enumerate(segments):
        parent = segments[index - 1].casefold() if index else ""
        if re.fullmatch(r"\d+", segment) and parent in {
                "catalog", "details", "orderdetails", "product", "products", "item", "items"}:
            label = {"catalog": "productId", "details": "productId",
                     "orderdetails": "orderId", "product": "productId"}.get(parent, "id")
            segment = "{" + label + "}"
        elif re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}", segment):
            segment = "{id}"
        output.append(segment)
    return "/" + "/".join(output)


def canonical_param_name(name: str) -> str:
    """Collapse nopCommerce product-specific attribute field names."""
    if re.fullmatch(r"product_attribute_\d+_\d+_\d+", name, re.I):
        return "product_attribute"
    if re.fullmatch(r"addtocart_\d+\.EnteredQuantity", name, re.I):
        return "addtocart_EnteredQuantity"
    return name


def _yaml_escape(text: str) -> str:
    """Minimal escaping for values we drop into a double-quoted YAML scalar."""
    return (text or "").replace("\\", "\\\\").replace('"', '\\"')


_SENSITIVE_FIELD_RE = re.compile(
    r"authorization|cookie|token|secret|password|passwd|api[_-]?key|csrf|xsrf|"
    r"session|card|cvv|cvc|email|phone|first.?name|last.?name|address|postal|zip",
    re.IGNORECASE,
)
_EMAIL_ADDRESS_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


def _is_sensitive_field(name) -> bool:
    return bool(_SENSITIVE_FIELD_RE.search(str(name)))


def _redact_structured_value(value):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_sensitive_field(key) else _redact_structured_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_structured_value(item) for item in value]
    if isinstance(value, str):
        return _EMAIL_ADDRESS_RE.sub("[REDACTED:email]", value)
    return value


def _redact_headers(headers) -> dict:
    if not isinstance(headers, dict):
        return {}
    return {
        key: "[REDACTED]" if _is_sensitive_field(key) else value
        for key, value in headers.items()
    }


def _curl_shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _api_curl_example(api_row: dict) -> tuple[str, bool]:
    """Build an importable cURL command from already-redacted API evidence."""
    headers = json.loads(api_row.get("headers_json") or "{}")
    if not isinstance(headers, dict):
        headers = {}

    omitted_headers = {
        "accept-encoding", "connection", "content-length", "host", "priority",
    }
    command = [
        "curl --location",
        f"--request {str(api_row['method']).upper()}",
        f"--url {_curl_shell_quote(str(api_row['full_url']))}",
    ]
    has_redacted_values = False
    for name, value in headers.items():
        normalized_name = str(name).lower()
        if (normalized_name in omitted_headers or normalized_name.startswith("sec-")
                or not isinstance(value, str)):
            continue
        if _is_sensitive_field(name):
            header_value = f"{name}: [REDACTED]"
            has_redacted_values = True
        else:
            header_value = f"{name}: {value}"
        command.append(f"--header {_curl_shell_quote(header_value)}")

    payload = str(api_row.get("payload_json") or "")
    if payload:
        command.append(f"--data-raw {_curl_shell_quote(payload)}")
    has_redacted_values = has_redacted_values or any("[REDACTED" in value for value in (
        str(api_row.get("full_url") or ""),
        str(api_row.get("payload_json") or ""),
        json.dumps(headers, ensure_ascii=False),
    ))
    return " ".join(command), has_redacted_values


def _redact_url(url: str) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    has_sensitive_query = any(_is_sensitive_field(key) for key, _ in query)
    has_userinfo = parts.username is not None or parts.password is not None
    if not has_sensitive_query and not has_userinfo:
        return url
    safe_query = parts.query
    if has_sensitive_query:
        safe_query = urlencode([
            (key, "[REDACTED]" if _is_sensitive_field(key) else value)
            for key, value in query
        ])
    netloc = parts.netloc
    if has_userinfo:
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parts.port
        except ValueError:
            port = None
        netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parts.scheme, netloc, parts.path, safe_query, parts.fragment))


def _redact_body(body: str | None) -> str:
    if not body:
        return ""
    try:
        parsed = json.loads(body)
    except (TypeError, json.JSONDecodeError):
        parsed = None
    else:
        return json.dumps(_redact_structured_value(parsed), ensure_ascii=False)

    fields = parse_qsl(body, keep_blank_values=True)
    if fields and "=" in body:
        return urlencode([
            (key, "[REDACTED]" if _is_sensitive_field(key) else value)
            for key, value in fields
        ])
    if _SENSITIVE_FIELD_RE.search(body):
        return "[REDACTED: unstructured body contains sensitive field names]"
    return _EMAIL_ADDRESS_RE.sub("[REDACTED:email]", body)


def redact_http_response(headers: dict, body: str | None) -> tuple[dict, str]:
    """Return response headers and body with sensitive values removed."""
    return _redact_headers(headers), _redact_body(body)


def _fixture_variable_for_step(step: dict) -> str:
    hint = step.get("label_for") or step.get("label") or step.get("input_type") or "value"
    hint = _EMAIL_ADDRESS_RE.sub("email", str(hint))
    words = re.findall(r"[A-Za-z0-9]+", hint.upper())
    name = "_".join(words)[:40].strip("_") or "VALUE"
    return f"TEST_{name}"


def validate_scenario_annotation(annotation: dict) -> dict:
    if not isinstance(annotation, dict):
        raise ValueError("scenario annotation must be a JSON object")
    if annotation.get("schema_version") != SCENARIO_ANNOTATION_VERSION:
        raise ValueError(f"scenario annotation schema_version must be {SCENARIO_ANNOTATION_VERSION}")

    text_fields = (
        "scenario_id", "scenario_name", "intent", "actor_role", "environment",
        "fixture_alias", "starting_state",
    )
    normalized = {"schema_version": SCENARIO_ANNOTATION_VERSION}
    for field in text_fields:
        value = annotation.get(field, "")
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string")
        normalized[field] = value

    variants = annotation.get("selected_variants", {})
    if not isinstance(variants, dict):
        raise ValueError("selected_variants must be an object")
    normalized["selected_variants"] = variants

    constraints = annotation.get("field_constraints", [])
    if not isinstance(constraints, list):
        raise ValueError("field_constraints must be an array")
    normalized_constraints = []
    for index, constraint in enumerate(constraints):
        if not isinstance(constraint, dict):
            raise ValueError(f"field_constraints[{index}] must be an object")
        field = constraint.get("field")
        rule = constraint.get("rule")
        source = constraint.get("source")
        if not all(isinstance(value, str) and value.strip() for value in (field, rule, source)):
            raise ValueError(f"field_constraints[{index}] needs non-empty field, rule, and source")
        if source not in SCENARIO_CONSTRAINT_SOURCES:
            raise ValueError(
                f"field_constraints[{index}].source must be one of "
                f"{', '.join(sorted(SCENARIO_CONSTRAINT_SOURCES))}"
            )
        normalized_constraints.append(constraint)
    normalized["field_constraints"] = normalized_constraints

    for field in ("expected_assertions", "side_effects"):
        values = annotation.get(field, [])
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError(f"{field} must be an array of strings")
        normalized[field] = values

    replay_risk = annotation.get("replay_risk", "unknown")
    if replay_risk not in SCENARIO_REPLAY_RISKS:
        raise ValueError(f"replay_risk must be one of {', '.join(sorted(SCENARIO_REPLAY_RISKS))}")
    normalized["replay_risk"] = replay_risk
    return _redact_structured_value(normalized)


# Keywords used to recognize a "dismiss this banner/modal" click -- cookie
# consent prompts being the extremely common case. Deliberately narrow
# (rather than generic words like "accept"/"close"/"allow" alone) to avoid
# false-positiving on a real app action that happens to share wording, e.g.
# an e-commerce "Accept Order" button.
BANNER_DISMISS_KEYWORDS = ("cookie", "gdpr", "consent", "allow all", "accept all", "i agree", "got it")


def _is_banner_dismiss(step: tuple) -> bool:
    """step: (step_type, selector, label, value, input_type, page_url,
    tag, elem_type, role, checked, is_native_control, label_for,
    select_kind, selected_options_json, ts). Only the first three fields
    are used here. True if this looks like a one-time banner/modal
    dismissal rather than an app-specific action -- these get treated as
    universal setup (see export_maestro_flows) instead of belonging to
    whichever segment they happened to fall in during the original capture
    session."""
    step_type, _selector, label, *_ = step
    if step_type != "tap":
        return False
    text = (label or "").lower()
    return any(kw in text for kw in BANNER_DISMISS_KEYWORDS)


def _fuzzy_label_regex(label: str) -> str:
    """Turns a captured label into a case-insensitive substring regex that
    tolerates however much (or little) whitespace actually separates its
    words in the real DOM.

    Why this is needed: capture.py reads a clicked element's innerText,
    which can bundle together several visually-separate child elements
    (e.g. Swagger UI's method badge + path + description are three
    different elements, laid out with CSS flex/gap, not literal space
    characters). Depending on whether those children are inline or
    block-level, innerText can join them with a real space, a literal
    newline, or nothing at all -- and that can differ between operations
    on the SAME page. An exact-text tapOn baked in at capture time bakes in
    whatever separator happened to show up that one time, which is fragile.

    Splitting the label into words and rejoining with `\\s*` (zero or more
    whitespace characters, not `\\s+`) means the pattern matches regardless
    of which of those three cases the real page turns out to use."""
    tokens = label.split()
    if not tokens:
        return ""
    escaped = [re.escape(t) for t in tokens]
    return "(?i).*" + r"\s*".join(escaped) + ".*"


def _scroll_and_tap_lines(pattern: str, human_target: str) -> tuple:
    """Builds a conditional-scroll + tapOn pair for a text-based target.

    Swagger-style pages (long operation lists) routinely need scrolling to
    reach something off-screen -- but scrollUntilVisible isn't safe to run
    unconditionally before every tap. If the target is ALREADY on screen
    (e.g. a cookie-consent button sitting at the top of the page), telling
    it to scroll anyway can overshoot past a small, already-visible element
    (a known Maestro rough edge on long/dense lists) and then fail to find
    it again. So the scroll is wrapped in a `when: notVisible` guard --
    it only runs at all if the element genuinely isn't on screen yet."""
    body = [
        '- runFlow:\n'
        '    when:\n'
        '      notVisible:\n'
        f'        text: "{_yaml_escape(pattern)}"\n'
        '    commands:\n'
        '      - scrollUntilVisible:\n'
        '          element:\n'
        f'            text: "{_yaml_escape(pattern)}"\n'
        '          direction: DOWN',
        f'- tapOn: "{_yaml_escape(pattern)}"',
    ]
    human = [f'Scroll to (only if needed) and tap "{human_target}"']
    return body, human


class VaultWriter:
    def __init__(self, vault_path: Path):
        self.vault_path = Path(vault_path)
        self.apis_dir = self.vault_path / "APIs"
        self.params_dir = self.vault_path / "Params"
        self.screenshots_dir = self.vault_path / "Screenshots"
        self.flows_dir = self.vault_path / "Flows"
        self.index_dir = self.vault_path / ".capture_index"
        for d in (self.apis_dir, self.params_dir, self.screenshots_dir, self.flows_dir, self.index_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.db_path = self.index_dir / "index.sqlite"
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._init_schema()
        self._normalize_screenshot_paths()
        self.ensure_graph_colors()

    # ------------------------------------------------------------------
    def ensure_graph_colors(self, color_groups: dict | None = None):
        """
        Writes/merges .obsidian/graph.json so each vault content type gets a
        distinct color in Obsidian's Graph view. Safe to call repeatedly:
        preserves any other graph settings and any color groups you added
        manually for queries other than the ones we manage.
        """
        color_groups = color_groups or GRAPH_COLOR_GROUPS
        obsidian_dir = self.vault_path / ".obsidian"
        obsidian_dir.mkdir(parents=True, exist_ok=True)
        graph_path = obsidian_dir / "graph.json"

        if graph_path.exists():
            try:
                config = json.loads(graph_path.read_text(encoding="utf-8"))
            except Exception:
                config = {}
        else:
            config = {}

        existing_groups = config.get("colorGroups", [])
        managed_queries = set(color_groups.keys())
        # keep any group the user set up themselves for other queries
        kept = [g for g in existing_groups if g.get("query") not in managed_queries]
        new_groups = [
            {"query": query, "color": {"a": 1, "rgb": rgb}}
            for query, rgb in color_groups.items()
        ]
        config["colorGroups"] = kept + new_groups

        graph_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    def _init_schema(self):
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS apis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                method TEXT NOT NULL,
                url_path TEXT NOT NULL,
                path_template TEXT,
                domain TEXT NOT NULL,
                full_url TEXT,
                first_seen TEXT,
                last_seen TEXT,
                capture_count INTEGER DEFAULT 0,
                headers_json TEXT,
                payload_json TEXT,
                response_json TEXT,
                entry_points_json TEXT,
                screenshots_json TEXT DEFAULT '[]',
                UNIQUE(method, url_path, domain)
            )
            """
        )
        apis_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(apis)").fetchall()}
        if "screenshots_json" not in apis_cols:
            self.conn.execute("ALTER TABLE apis ADD COLUMN screenshots_json TEXT DEFAULT '[]'")
        if "path_template" not in apis_cols:
            self.conn.execute("ALTER TABLE apis ADD COLUMN path_template TEXT")
        self.conn.execute("UPDATE apis SET path_template=url_path WHERE path_template IS NULL")
        self._migrate_params_schema()
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS variant_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_id INTEGER NOT NULL,
                variant_payload_json TEXT,
                response_status INTEGER,
                response_headers_json TEXT,
                response_body TEXT,
                created_at TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                start_url TEXT,
                started_at TEXT,
                ended_at TEXT,
                api_refs_json TEXT DEFAULT '[]'
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_annotations (
                session_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                annotation_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                step_type TEXT NOT NULL,
                selector TEXT,
                label TEXT,
                value TEXT,
                input_type TEXT,
                page_url TEXT,
                ts REAL,
                tag TEXT,
                elem_type TEXT,
                role TEXT,
                checked INTEGER,
                is_native_control INTEGER,
                label_for TEXT,
                select_kind TEXT,
                selected_options_json TEXT,
                action_id TEXT
            )
            """
        )
        # DOM-classification columns, added after the original 'steps'
        # table -- older vaults recaptured before this change won't have
        # them yet, so backfill on open rather than forcing a fresh DB.
        steps_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(steps)").fetchall()}
        for col, decl in (
            ("tag", "TEXT"), ("elem_type", "TEXT"), ("role", "TEXT"),
            ("checked", "INTEGER"), ("is_native_control", "INTEGER"),
            ("label_for", "TEXT"), ("select_kind", "TEXT"),
            ("selected_options_json", "TEXT"), ("action_id", "TEXT"),
        ):
            if col not in steps_cols:
                self.conn.execute(f"ALTER TABLE steps ADD COLUMN {col} {decl}")
        # One row per API call actually captured during a session (as
        # opposed to `apis`, which is one row per DISTINCT endpoint,
        # overwritten on every recapture). This table is what lets
        # export_maestro_flows() figure out, in true chronological order,
        # which UI steps happened between one API call and the next -- so
        # each exported flow only contains the steps for the ONE call it's
        # testing, not the whole session's worth.
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_api_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                ref_stem TEXT NOT NULL,
                domain TEXT,
                method TEXT,
                status INTEGER,
                response_body TEXT,
                ts REAL,
                request_url TEXT,
                request_headers_json TEXT,
                request_body TEXT,
                response_headers_json TEXT,
                entry_point TEXT,
                correlation_status TEXT,
                action_id TEXT
            )
            """
        )
        event_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(session_api_events)").fetchall()}
        for col, decl in (
            ("request_url", "TEXT"), ("request_headers_json", "TEXT"),
            ("request_body", "TEXT"), ("response_headers_json", "TEXT"),
            ("entry_point", "TEXT"), ("correlation_status", "TEXT"), ("action_id", "TEXT"),
        ):
            if col not in event_cols:
                self.conn.execute(f"ALTER TABLE session_api_events ADD COLUMN {col} {decl}")
        self.conn.commit()

    def _normalize_screenshot_paths(self):
        """One-time self-heal: captures taken with an older capture.py on
        Windows stored screenshot paths with backslash separators (str() on
        a WindowsPath), which Obsidian's ![[...]] embeds can't resolve --
        it always expects forward slashes regardless of host OS. Rewrites
        any such paths in the DB and re-renders the affected notes so
        already-captured screenshots start showing up without a recapture.
        Cheap no-op once everything's already forward-slashed."""
        rows = self.conn.execute(
            "SELECT id, method, url_path, domain, full_url, first_seen, last_seen, "
            "capture_count, headers_json, payload_json, response_json, "
            "entry_points_json, screenshots_json FROM apis"
        ).fetchall()
        changed = 0
        for (api_id, method, url_path, domain, full_url, first_seen, last_seen,
             capture_count, headers_json, payload_json, response_json,
             entry_points_json, screenshots_json) in rows:
            paths = json.loads(screenshots_json or "[]")
            fixed = [p.replace("\\", "/") for p in paths]
            if fixed == paths:
                continue
            fixed_json = json.dumps(fixed)
            self.conn.execute("UPDATE apis SET screenshots_json=? WHERE id=?", (fixed_json, api_id))
            changed += 1

            ref_stem = f"{method}_{slugify(url_path)}"
            resource_key = self._resource_key(domain, url_path)
            api_row = {
                "full_url": full_url, "method": method, "domain": domain,
                "url_path": url_path, "first_seen": first_seen, "last_seen": last_seen,
                "capture_count": capture_count, "headers_json": headers_json,
                "payload_json": payload_json, "response_json": response_json,
                "entry_points_json": entry_points_json, "screenshots_json": fixed_json,
            }
            self._render_api_md(api_row, self._param_roles_for_ref(ref_stem, resource_key), resource_key)

        if changed:
            self.conn.commit()
            print(f"  [vault] fixed screenshot path separators for {changed} API note(s).")

    def _create_params_table(self):
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS params (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                resource_key TEXT NOT NULL DEFAULT '',
                example_values_json TEXT,
                api_refs_json TEXT,
                definition TEXT,
                UNIQUE(resource_key, name)
            )
            """
        )

    def _migrate_params_schema(self):
        """Params used to be uniquely keyed by name alone, which silently
        merged e.g. a pet's "id" with a user's "id" into one connected note.
        If an old-shape table is found, split each row apart by the resource
        each of its refs actually belongs to and rebuild under the new
        (resource_key, name) identity. The old table is kept around (renamed)
        rather than dropped, since request/response example values were
        never tracked per-ref and can't be perfectly re-attributed -- this
        migration is best-effort, not lossless."""
        exists = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='params'"
        ).fetchone()
        if not exists:
            self._create_params_table()
            return

        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(params)").fetchall()}
        if "resource_key" in cols:
            return  # already on the new schema

        old_rows = self.conn.execute(
            "SELECT name, example_values_json, api_refs_json, definition FROM params"
        ).fetchall()
        self.conn.execute("ALTER TABLE params RENAME TO params_legacy_pre_resource_split")
        self._create_params_table()

        for name, example_values_json, api_refs_json, definition in old_rows:
            refs = self._coerce_refs(api_refs_json)
            values = self._coerce_values(example_values_json)
            groups: dict = {}
            for ref_stem, roles in refs.items():
                groups.setdefault(self._resource_key_from_ref_stem(ref_stem), {})[ref_stem] = roles
            if not groups:
                groups = {"_unknown": {}}
            for resource_key, grouped_refs in groups.items():
                # Best-effort: carry the full historical value pool into
                # every split-out group since we can't tell which value came
                # from which ref; it'll naturally settle out as new captures
                # come in and re-attribute values per resource.
                self.conn.execute(
                    "INSERT INTO params (name, resource_key, example_values_json, api_refs_json, definition) "
                    "VALUES (?,?,?,?,?)",
                    (name, resource_key, json.dumps(values), json.dumps(grouped_refs), definition),
                )
            self.conn.commit()
            # Remove the old bare-name note (e.g. Params/id.md) -- it's been
            # superseded by one or more resource-scoped notes below.
            old_note = self.params_dir / f"{slugify(name)}.md"
            if old_note.exists():
                old_note.unlink()
            for resource_key in groups:
                self._render_param_md(name, resource_key)
        if old_rows:
            print(f"  [vault] migrated {len(old_rows)} legacy param(s) into resource-scoped groups "
                  f"(old data kept in table 'params_legacy_pre_resource_split').")

    # ------------------------------------------------------------------
    # Public entry point (called from the writer task in capture.py)
    # ------------------------------------------------------------------
    def ingest(self, record: dict, session_id: str | None = None):
        """record: {method, url, request_headers, payload, response_body, entry_point}
        session_id, if given, links this capture to a capture.py run so
        export_maestro_flow() can list which APIs that session triggered."""
        with self.lock:
            self._ingest_locked(record, session_id)

    # ------------------------------------------------------------------
    def _ingest_locked(self, record: dict, session_id: str | None = None):
        parsed = urlparse(record["url"])
        domain = parsed.hostname or ""
        try:
            port = parsed.port
        except ValueError:
            port = None
        if ":" in domain and not domain.startswith("["):
            domain = f"[{domain}]"
        if port is not None:
            domain = f"{domain}:{port}"
        url_path = parsed.path or "/"
        method = record["method"]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        request_url = _redact_url(record["url"])
        safe_record = dict(record)
        safe_record["url"] = request_url
        safe_record["request_headers"] = _redact_headers(record.get("request_headers"))
        safe_record["response_headers"] = _redact_headers(record.get("response_headers"))
        safe_record["payload"] = _redact_body(record.get("payload"))[:MAX_BODY_CHARS]
        safe_record["response_body"] = _redact_body(record.get("response_body"))[:MAX_BODY_CHARS]

        headers_json = json.dumps(safe_record["request_headers"], indent=2)
        response_headers_json = json.dumps(safe_record["response_headers"], indent=2)
        payload_str = safe_record["payload"]
        response_str = safe_record["response_body"]
        entry_point = _EMAIL_ADDRESS_RE.sub("[REDACTED:email]", record["entry_point"])

        existing = self.conn.execute(
            "SELECT id, entry_points_json, capture_count, first_seen, screenshots_json "
            "FROM apis WHERE method=? AND url_path=? AND domain=?",
            (method, url_path, domain),
        ).fetchone()

        new_screenshot = record.get("screenshot_path")

        if existing is None:
            entry_points = [entry_point]
            screenshots = [new_screenshot] if new_screenshot else []
            self.conn.execute(
                """INSERT INTO apis
                   (method, url_path, domain, full_url, first_seen, last_seen,
                    capture_count, headers_json, payload_json, response_json,
                    entry_points_json, screenshots_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (method, url_path, domain, request_url, now, now, 1,
                 headers_json, payload_str, response_str, json.dumps(entry_points),
                 json.dumps(screenshots)),
            )
            api_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            capture_count = 1
            first_seen = now
        else:
            api_id, entry_points_json, capture_count, first_seen, screenshots_json = existing
            entry_points = json.loads(entry_points_json or "[]")
            if entry_point not in entry_points:
                entry_points.append(entry_point)
                entry_points = entry_points[-20:]  # cap growth
            screenshots = json.loads(screenshots_json or "[]")
            if new_screenshot:
                screenshots.append(new_screenshot)
                screenshots = screenshots[-MAX_SCREENSHOTS_PER_API:]
            capture_count += 1
            self.conn.execute(
                """UPDATE apis SET last_seen=?, capture_count=?, headers_json=?,
                   payload_json=?, response_json=?, entry_points_json=?, full_url=?,
                   screenshots_json=?
                   WHERE id=?""",
                (now, capture_count, headers_json, payload_str, response_str,
                      json.dumps(entry_points), request_url, json.dumps(screenshots), api_id),
            )
        self.conn.commit()

        self.conn.execute("UPDATE apis SET path_template=? WHERE method=? AND url_path=? AND domain=?",
                          (template_url_path(url_path), method, url_path, domain))
        ref_stem = f"{method}_{slugify(url_path)}"
        resource_key = self._resource_key(domain, url_path)
        for name, value in self._extract_params(safe_record).items():
            self._upsert_param(name, value, ref_stem, resource_key, source="request")
        for name, value in self._extract_response_params(response_str).items():
            self._upsert_param(name, value, ref_stem, resource_key, source="response")

        api_row = {
            "full_url": request_url, "method": method, "domain": domain,
            "url_path": url_path, "first_seen": first_seen, "last_seen": now,
            "capture_count": capture_count, "headers_json": headers_json,
            "payload_json": payload_str, "response_json": response_str,
            "entry_points_json": json.dumps(entry_points),
            "screenshots_json": json.dumps(screenshots),
        }
        # Use the full accumulated role map for this API (not just what this
        # one call happened to carry) so the note's Params list doesn't
        # flicker between captures that vary in which optional fields appear.
        self._render_api_md(api_row, self._param_roles_for_ref(ref_stem, resource_key), resource_key)

        if session_id:
            self._touch_session_api_locked(session_id, domain, ref_stem)
            self._record_session_api_event_locked(
                session_id, ref_stem, domain, method, record.get("status"),
                response_str, record.get("ts"), request_url, headers_json,
                payload_str, response_headers_json, entry_point,
                record.get("correlation_status"), record.get("event_seq"), record.get("action_id"),
            )

    def _touch_session_api_locked(self, session_id: str, domain: str, ref_stem: str):
        """Records that this session triggered this API, for the 'APIs
        Triggered' section of the exported flow note. Assumes self.lock is
        already held (called from inside _ingest_locked)."""
        row = self.conn.execute(
            "SELECT api_refs_json FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            return  # session wasn't started via start_session() -- skip rather than error
        refs = json.loads(row[0] or "[]")
        entry = {"domain": domain, "ref_stem": ref_stem}
        if entry not in refs:
            refs.append(entry)
            self.conn.execute(
                "UPDATE sessions SET api_refs_json=? WHERE session_id=?",
                (json.dumps(refs), session_id),
            )

    def _record_session_api_event_locked(self, session_id: str, ref_stem: str, domain: str,
                                          method: str, status, response_body: str, ts,
                                          request_url: str, request_headers_json: str,
                                          request_body: str, response_headers_json: str,
                                          entry_point: str, correlation_status: str | None,
                                          event_seq: int | None = None, action_id: str | None = None):
        """Logs ONE capture event (not the aggregated `apis` row) so
        export_maestro_flows() can interleave it against `steps` by real
        timestamp and know exactly which API this particular click caused.
        Assumes self.lock is already held (called from inside _ingest_locked).
        No-ops quietly if this session was never started via start_session()
        -- same convention as _touch_session_api_locked."""
        exists = self.conn.execute(
            "SELECT 1 FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if not exists:
            return
        seq = event_seq
        if seq is None:
            seq = self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM session_api_events WHERE session_id=?",
                (session_id,),
            ).fetchone()[0]
        self.conn.execute(
            """INSERT INTO session_api_events
                    (session_id, seq, ref_stem, domain, method, status, response_body, ts,
                     request_url, request_headers_json, request_body, response_headers_json,
                       entry_point, correlation_status, action_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, seq, ref_stem, domain, method, status,
                 (response_body or "")[:MAX_BODY_CHARS], ts, request_url,
                 request_headers_json, request_body, response_headers_json,
                 entry_point, correlation_status, action_id),
        )

    # ------------------------------------------------------------------
    # Session / step tracking -- feeds export_maestro_flow()
    # ------------------------------------------------------------------
    def start_session(self, session_id: str, start_url: str):
        safe_start_url = _redact_url(start_url)
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, start_url, started_at, api_refs_json) "
                "VALUES (?,?,?,?)",
                (session_id, safe_start_url, datetime.now(timezone.utc).isoformat(timespec="seconds"), "[]"),
            )
            self.conn.commit()

    def end_session(self, session_id: str):
        with self.lock:
            self.conn.execute(
                "UPDATE sessions SET ended_at=? WHERE session_id=?",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), session_id),
            )
            self.conn.commit()

    def save_scenario_annotation(self, session_id: str, annotation: dict):
        safe_annotation = validate_scenario_annotation(annotation)
        with self.lock:
            self.conn.execute(
                """INSERT INTO scenario_annotations
                   (session_id, schema_version, annotation_json, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(session_id) DO UPDATE SET
                     schema_version=excluded.schema_version,
                     annotation_json=excluded.annotation_json,
                     updated_at=excluded.updated_at""",
                (session_id, SCENARIO_ANNOTATION_VERSION,
                 json.dumps(safe_annotation, ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            self.conn.commit()
        return safe_annotation

    def get_scenario_annotation(self, session_id: str | None) -> dict | None:
        if not session_id:
            return None
        row = self.conn.execute(
            "SELECT annotation_json FROM scenario_annotations WHERE session_id=?",
            (session_id,),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def record_step(self, session_id: str, step: dict):
        """step: {step_type, selector, label, value, input_type, page_url, ts,
        tag, elem_type, role, checked, is_native_control, label_for,
        select_kind, selected_options}. step_type is 'tap', 'input', or
        'navigate'. For input steps where input_type == 'password', value
        should already be None -- the password's characters should never
        reach this far; capture.py masks it client-side before the step is
        even sent to Python.

        The DOM-classification fields (tag/elem_type/role/checked/
        is_native_control/label_for/select_kind/selected_options) are
        captured client-side in capture.py's STEP_SCRIPT and let
        _render_step_lines() tell a checkbox/radio tap or a native
        single-vs-multi <select> apart from a plain button/link tap,
        instead of treating every click identically. Any of them may be
        None/absent for step types that don't apply (e.g. navigate)."""
        selected_options = _redact_structured_value(step.get("selected_options"))
        safe_step = dict(step)
        for field in ("label", "label_for", "selector"):
            if isinstance(safe_step.get(field), str):
                safe_step[field] = _EMAIL_ADDRESS_RE.sub("[REDACTED:email]", safe_step[field])
        value = step.get("value")
        sensitive_hint = any(
            _is_sensitive_field(step.get(field, ""))
            for field in ("label", "label_for", "selector", "input_type")
        )
        if value is not None and (sensitive_hint or _EMAIL_ADDRESS_RE.search(str(value))):
            safe_step["value"] = f"${{{_fixture_variable_for_step(step)}}}"
        else:
            safe_step["value"] = _redact_structured_value(value)
        if isinstance(safe_step.get("page_url"), str):
            safe_step["page_url"] = _redact_url(safe_step["page_url"])
        with self.lock:
            seq = self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM steps WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            self.conn.execute(
                """INSERT INTO steps
                   (session_id, seq, step_type, selector, label, value, input_type, page_url, ts,
                    tag, elem_type, role, checked, is_native_control, label_for, select_kind,
                    selected_options_json, action_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (session_id, seq, safe_step.get("step_type"), safe_step.get("selector"), safe_step.get("label"),
                 safe_step.get("value"), safe_step.get("input_type"), safe_step.get("page_url"), safe_step.get("ts"),
                 safe_step.get("tag"), safe_step.get("elem_type"), safe_step.get("role"),
                 safe_step.get("checked"), safe_step.get("is_native_control"), safe_step.get("label_for"),
                 safe_step.get("select_kind"), json.dumps(selected_options) if selected_options else None,
                 safe_step.get("action_id")),
            )
            self.conn.commit()

    @staticmethod
    def _render_step_lines(steps: list) -> tuple:
        """Turns a list of (step_type, selector, label, value, input_type,
        page_url, tag, elem_type, role, checked, is_native_control,
        label_for, select_kind, selected_options_json, ts) rows into
        (body_lines, human_lines, env_vars) -- the same per-step rendering
        export_maestro_flows() needs for both the shared preamble and each
        segment's own delta of steps.

        The DOM-classification fields (tag/elem_type/role/checked/...) are
        what let this tell a checkbox/radio tap, or a native single- vs.
        multi-<select>, apart from a plain button/link tap or a plain text
        input -- older captures made before this classification existed
        will simply have these as None/empty, and fall through to the same
        generic handling as before."""
        body_lines = []
        human_lines = []
        env_vars: dict = {}
        password_counter = 0

        for (step_type, selector, label, value, input_type, page_url,
             tag, elem_type, role, checked, is_native_control, label_for,
             select_kind, selected_options_json, ts) in steps:

            if step_type == "tap":
                is_checkbox_like = (tag == "input" and elem_type in ("checkbox", "radio")) or \
                                    role in ("checkbox", "radio", "switch")
                if is_checkbox_like:
                    # Checkboxes/radios are often a few tappable pixels --
                    # prefer the associated <label> text (a much bigger, more
                    # reliable hit target) over the tiny control itself, and
                    # note which kind it is and the resulting checked state
                    # (recorded at click time) for the human-readable log.
                    kind = elem_type or role
                    target = (label_for or label or "").strip()
                    state_note = "" if checked is None else (" -> checked" if checked else " -> unchecked")
                    if target and "[REDACTED" not in target.upper():
                        pattern = _fuzzy_label_regex(target)
                        body, human = _scroll_and_tap_lines(pattern, target)
                        body_lines += body
                        human_lines.append(f'Tap {kind} "{target}"{state_note}')
                    else:
                        body_lines.append(f'- tapOn: "{_yaml_escape(selector or "unknown")}"')
                        human_lines.append(
                            f'Tap {kind} (no label found, selector was `{selector}`){state_note} -- please verify'
                        )
                    continue

                target = (label_for or label or "").strip()
                if target and "[REDACTED" not in target.upper():
                    pattern = _fuzzy_label_regex(target)
                    body, human = _scroll_and_tap_lines(pattern, target)
                    body_lines += body
                    human_lines += human
                elif selector and selector.startswith("#"):
                    elem_id = selector[1:]
                    body_lines.append(
                        '- runFlow:\n'
                        '    when:\n'
                        '      notVisible:\n'
                        f'        id: "{_yaml_escape(elem_id)}"\n'
                        '    commands:\n'
                        '      - scrollUntilVisible:\n'
                        '          element:\n'
                        f'            id: "{_yaml_escape(elem_id)}"\n'
                        '          direction: DOWN'
                    )
                    body_lines.append(f'- tapOn:\n    id: "{_yaml_escape(elem_id)}"')
                    human_lines.append(f'Scroll to (only if needed) and tap element `#{elem_id}`')
                else:
                    body_lines.append(
                        f'# TODO: unclear tap target (selector was "{selector}") -- verify/edit this step'
                    )
                    body_lines.append(f'- tapOn: "{_yaml_escape(selector or "unknown")}"')
                    human_lines.append(f'Tap (uncertain target, selector was `{selector}`) -- please verify')

            elif step_type == "input":
                if select_kind == "single":
                    # Native single-<select> dropdowns render their option
                    # list as an OS-level popup outside the page's own
                    # layout/DOM tree once opened -- Maestro (like most
                    # browser-automation tools working off web locators)
                    # generally can't tap into that popup directly. Rather
                    # than emit a tapOn that will pass locally and then
                    # silently fail elsewhere, flag it plainly: best-effort
                    # tap on the select itself (opens it) plus a TODO
                    # calling out that the actual option pick needs a
                    # manual fix (e.g. driving it via keyboard input on
                    # some browsers, or an OS-specific picker command).
                    target = (label or label_for or "").strip()
                    chosen = (value or "").strip()
                    if target:
                        pattern = _fuzzy_label_regex(target)
                        body, human = _scroll_and_tap_lines(pattern, target)
                        body_lines += body
                    else:
                        body_lines.append(f'- tapOn: "{_yaml_escape(selector or "unknown")}"')
                    body_lines.append(
                        f'# TODO: native single-select dropdown -- Maestro generally cannot tap '
                        f'options inside its OS-level popup. Originally selected "{chosen}" here; '
                        f'replace this with a manual fix (e.g. keyboard-driven selection or an '
                        f'OS-specific picker command).'
                    )
                    human_lines.append(
                        f'Open dropdown "{target or selector}" and select "{chosen}" '
                        f'-- NEEDS MANUAL FIX (native picker, not directly tappable)'
                    )

                elif select_kind == "multiple":
                    # Multi-selects render as an in-page listbox -- each
                    # option is a real, tappable DOM node, so this expands
                    # into one tap per option the change event reported as
                    # selected (unlike the single-select case above).
                    try:
                        options = json.loads(selected_options_json) if selected_options_json else []
                    except (TypeError, ValueError):
                        options = []
                    if not options:
                        body_lines.append(
                            f'# TODO: multi-select "{label or selector}" changed but no selected '
                            f'options were captured -- verify/edit this step'
                        )
                        human_lines.append(f'Select option(s) in "{label or selector}" -- please verify')
                    else:
                        for opt in options:
                            pattern = _fuzzy_label_regex(opt)
                            body, human = _scroll_and_tap_lines(pattern, opt)
                            body_lines += body
                            human_lines.append(f'Select "{opt}" in "{label or selector}"')

                elif input_type == "password":
                    password_counter += 1
                    var_name = "PASSWORD" if password_counter == 1 else f"PASSWORD_{password_counter}"
                    env_vars[var_name] = "<fill in>"
                    body_lines.append(f'- inputText: "${{{var_name}}}"')
                    human_lines.append(f'Type into "{label or selector}" (password -- value not recorded)')
                elif re.fullmatch(r"\$\{(TEST_[A-Z0-9_]+)\}", str(value or "")):
                    var_name = value[2:-1]
                    env_vars[var_name] = "<fill in>"
                    body_lines.append(f'- inputText: "${{{var_name}}}"')
                    human_lines.append(
                        f'Fill "{label or selector}" using {var_name} (captured value redacted)'
                    )
                else:
                    body_lines.append(f'- inputText: "{_yaml_escape(value or "")}"')
                    human_lines.append(f'Type "{value}" into "{label or selector}"')

            elif step_type == "navigate":
                body_lines.append(f'- openLink: "{_yaml_escape(page_url or "")}"')
                human_lines.append(f'Navigate to {page_url}')

        return body_lines, human_lines, env_vars

    def _build_assertion_for_event(self, ref_stem: str, status, response_body: str) -> tuple:
        """Picks something on-screen worth checking after the API call this
        event represents. Returns (assert_body_lines, assert_human_lines).

        Two checks, when available:
          1. The HTTP status code -- Swagger-style API explorers (and most
             "try it" UIs) print this directly, so it's a cheap, stable signal.
          2. One short, distinctive value pulled from the response body
             (reusing _extract_response_params -- the same extraction used
             to populate Params/ notes) -- catches cases where the status
             looks fine but the UI is showing stale/wrong data.
        Both are written as case-insensitive substring regexes (the "(?i)"
        prefix), not exact-text matches -- the raw response string and what
        actually renders in the DOM are rarely byte-identical (whitespace,
        casing via CSS text-transform, HTML entity encoding, etc.)."""
        assert_body = []
        assert_human = []

        is_api_explorer = bool(re.search(r"(swagger|openapi|api explorer|try it)", response_body or "", re.I))
        if status is not None and is_api_explorer:
            pattern = f"(?i).*\\b{status}\\b.*"
            assert_body.append(f'- assertVisible: "{_yaml_escape(pattern)}"')
            assert_human.append(f'Verify status `{status}` is shown for {ref_stem}')

        candidate_value = None
        for name, value in self._extract_response_params(response_body or "").items():
            text = str(value).strip()
            if not text or "[REDACTED" in text.upper():
                continue
            text = re.sub(r"<[^>]+>", "", text)
            if not text:
                continue
            # Count-like values (e.g. cart totals or live counters) are noisy
            # and too likely to match unrelated pages; skip them.
            if re.fullmatch(r"\(?\d+\)?", text) or re.fullmatch(r"\d+", text):
                continue
            # Skip boring/generic values (booleans, tiny numbers, empty
            # strings) that are too likely to appear on screen already, by
            # coincidence, whether or not this call actually succeeded.
            if 2 <= len(text) <= 60 and text.lower() not in ("true", "false", "null", "none", "0", "1", ""):
                candidate_value = (name, text)
                break

        if candidate_value:
            name, text = candidate_value
            pattern = f"(?i).*{re.escape(text)}.*"
            assert_body.append(f'- assertVisible: "{_yaml_escape(pattern)}"')
            assert_human.append(f'Verify response value for `{name}` ("{text}") is shown')

        if not assert_body:
            assert_body.append(f'# TODO: no stable UI assertion was available for {ref_stem}')
            assert_human.append(f'(no assertion available for {ref_stem} -- please add one manually)')

        return assert_body, assert_human

    def export_maestro_flows(self, session_id: str):
        """Splits a capture.py session into ONE Maestro flow PER API call it
        triggered, instead of one giant flow for the whole session.

        How the split works: every UI step (tap/input/navigate) and every
        API call are both timestamped, so they're merged into one true
        chronological timeline. Walking that timeline, each API call closes
        off a "segment" -- the run of steps since the previous API call (or
        since the session started, for the first one). Every exported flow
        = the shared preamble (steps before the very first API call, e.g.
        dismissing a cookie banner -- repeated in EVERY file, since each
        `maestro test` run starts a brand new browser with no memory of
        earlier files) + that segment's own steps + an assertion built from
        that call's real response.

        Returns a list of dicts, one per exported flow:
            {'yaml': path, 'md': path, 'session_md': path, 'ref_stem': str}
        (all entries share the same 'session_md'). Returns [] if the
        session was never started, or recorded no API-triggering steps."""
        session = self.conn.execute(
            "SELECT start_url, started_at, ended_at, api_refs_json FROM sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if session is None:
            return []
        start_url, started_at, ended_at, api_refs_json = session
        annotation = self.get_scenario_annotation(session_id) or {}

        # ts stays last in this SELECT -- code below indexes it via s[-1]
        # when building the chronological timeline (see _is_banner_dismiss
        # and the timeline-merge below), so the DOM-classification columns
        # are inserted before it, not appended after.
        steps = self.conn.execute(
            "SELECT step_type, selector, label, value, input_type, page_url, "
            "tag, elem_type, role, checked, is_native_control, label_for, "
            "select_kind, selected_options_json, ts "
            "FROM steps WHERE session_id=? ORDER BY seq ASC",
            (session_id,),
        ).fetchall()
        events = self.conn.execute(
            "SELECT id, seq, ref_stem, domain, method, status, response_body, action_id, ts "
            "FROM session_api_events WHERE session_id=? ORDER BY seq ASC",
            (session_id,),
        ).fetchall()
        if not events:
            return []  # nothing to assert on, and nothing meaningful to split by

        # Preamble = steps that LOOK like one-time banner/modal dismissals
        # (see _is_banner_dismiss), pulled out by content rather than
        # position. Position doesn't work here: a cookie banner isn't
        # guaranteed to be the very first thing clicked in a session --
        # it might not get dismissed until partway through, whenever it
        # happened to block a click -- so "steps before the first API call"
        # or "steps before the first navigation" can both miss it entirely
        # in one segment and never repeat it in the others. Matching by what
        # the click looks like, instead of when it happened, means it gets
        # pulled into every flow regardless of where it fell originally.
        preamble = []
        seen_banner_labels = set()
        filtered_steps = []
        for s in steps:
            if _is_banner_dismiss(s):
                key = (s[2] or "").strip().lower()
                if key not in seen_banner_labels:
                    seen_banner_labels.add(key)
                    preamble.append(s)
                # else: banner reappeared and was dismissed again -- already
                # covered by the first occurrence, don't add it twice.
            else:
                filtered_steps.append(s)

        # Merge into one timeline. Steps sort before an event at the exact
        # same millisecond -- a step is what CAUSED the call, so it belongs
        # to the segment ending at that call, not the one after it.
        timeline = (
            [("step", s[-1] if s[-1] is not None else 0.0, s) for s in filtered_steps] +
            [("event", e[-1] if e[-1] is not None else 0.0, e) for e in events]
        )
        timeline.sort(key=lambda item: (item[1], item[0] == "event"))

        segments = []       # [{'steps': [...], 'event': (...)}]
        current_steps = []
        for kind, ts, payload in timeline:
            if kind == "step":
                current_steps.append(payload)
            else:
                segments.append({"steps": current_steps, "event": payload})
                current_steps = []
        trailing_dropped = len(current_steps)  # steps after the last API call -- nothing to assert, so dropped

        self.flows_dir.mkdir(parents=True, exist_ok=True)
        results = []
        width = max(2, len(str(len(segments))))

        for i, seg in enumerate(segments, start=1):
            event_id, event_seq, ref_stem, domain, method, status, response_body, action_id, ts = seg["event"]
            delta_steps = preamble + seg["steps"]

            if delta_steps:
                first_step = next((s for s in delta_steps if s[5]), None)
                first_page_url = first_step[5] if first_step else None
                if first_page_url and first_page_url != start_url:
                    body_lines, human_lines, env_vars = self._render_step_lines(delta_steps)
                    body_lines = [f'- openLink: "{_yaml_escape(first_page_url)}"'] + body_lines
                    human_lines = [f'Navigate to {first_page_url}'] + human_lines
                else:
                    body_lines, human_lines, env_vars = self._render_step_lines(delta_steps)
            else:
                body_lines, human_lines, env_vars = self._render_step_lines([])
            assert_body, assert_human = self._build_assertion_for_event(ref_stem, status, response_body)
            body_lines += assert_body
            human_lines += assert_human

            header_lines = [
                f"# event_id: {event_id}",
                f"# event_seq: {event_seq}",
                f"# action_id: {action_id or ''}",
                f"# scenario_id: {annotation.get('scenario_id', '')}",
                f"url: {start_url}",
            ]
            if env_vars:
                header_lines.append("env:")
                for k, v in env_vars.items():
                    header_lines.append(f'  {k}: "{v}"')
            header_lines.append("---")
            # clearState: true, not a bare "- launchApp" -- Maestro's web
            # mode reuses the SAME browser tab/context across flows run in
            # one test session and does NOT reset cookies, localStorage, or
            # scroll position on its own (confirmed in Maestro's own web
            # docs). Without this, a flow that passes in isolation can fail
            # on a second run in the same session: scrollUntilVisible only
            # scrolls DOWN, so if a prior run left the page scrolled further
            # down (or a Swagger accordion already expanded), the target
            # element can end up ABOVE the current viewport and never be
            # found. Forcing a clean origin state makes every exported flow
            # self-contained regardless of what ran before it.
            launch_lines = ["- launchApp:", "    clearState: true"]
            yaml_content = "\n".join(header_lines + launch_lines + body_lines) + "\n"

            file_stem = f"{session_id}__{str(i).zfill(width)}_{slugify(ref_stem)}"
            yaml_path = self.flows_dir / f"{file_stem}.yaml"
            yaml_path.write_text(yaml_content, encoding="utf-8")

            steps_md = "\n".join(f"{j + 1}. {line}" for j, line in enumerate(human_lines)) or "_(no steps recorded)_"
            env_note = (
                "\n_Fill in the `env:` block at the top of the YAML "
                "(redacted password field(s)) before running._\n"
                if env_vars else ""
            )
            md_content = f"""---
tags: [flow, maestro]
session_id: {session_id}
event_id: {event_id}
event_seq: {event_seq}
action_id: {action_id or ''}
scenario_id: {annotation.get('scenario_id', '')}
segment: {i} of {len(segments)}
api: {ref_stem}
domain: {domain}
status: {status}
---

## Steps
{steps_md}

## API Triggered
- [[{ref_stem}]] ({domain})

## Maestro Flow
See `Flows/{file_stem}.yaml` -- run with:
```
maestro test Flows/{file_stem}.yaml
```
{env_note}"""
            md_path = self.flows_dir / f"{file_stem}.md"
            md_path.write_text(md_content, encoding="utf-8")

            results.append({
                "yaml": str(yaml_path), "md": str(md_path), "ref_stem": ref_stem,
                "event_id": event_id, "event_seq": event_seq,
                "action_id": action_id, "scenario_id": annotation.get("scenario_id"),
            })

        # One index note per session, linking every segment together.
        index_lines = "\n".join(
            f"{i}. [[{Path(r['md']).stem}|event {r['event_id']} / {r['ref_stem']}]]"
            for i, r in enumerate(results, start=1)
        )
        trailing_note = (
            f"\n_{trailing_dropped} step(s) after the last API call were not part of any flow._\n"
            if trailing_dropped else ""
        )
        session_md_content = f"""---
tags: [flow, maestro, session-index]
session_id: {session_id}
start_url: {start_url}
started_at: {started_at}
ended_at: {ended_at or '(in progress)'}
flow_count: {len(results)}
scenario_id: {annotation.get('scenario_id', '')}
---

## Flows in this session
{index_lines}
{trailing_note}"""
        session_md_path = self.flows_dir / f"{session_id}.md"
        session_md_path.write_text(session_md_content, encoding="utf-8")

        for r in results:
            r["session_md"] = str(session_md_path)
        return results

    # ------------------------------------------------------------------
    def _extract_params(self, record: dict) -> dict:
        """Pulls params from the query string AND the payload (JSON or form-encoded)."""
        params = {}

        query = urlparse(record["url"]).query
        for k, vs in parse_qs(query).items():
            params[k] = vs[0] if vs else ""

        payload = record.get("payload")
        if payload:
            headers = {k.lower(): v for k, v in (record.get("request_headers") or {}).items()}
            ctype = headers.get("content-type", "")
            if "json" in ctype:
                try:
                    data = json.loads(payload)
                    if isinstance(data, dict):
                        for k, v in data.items():
                            params[k] = v if isinstance(v, (str, int, float, bool)) else json.dumps(v)
                except Exception:
                    pass
            elif "x-www-form-urlencoded" in ctype:
                for k, vs in parse_qs(payload).items():
                    params[k] = vs[0] if vs else ""
        return params

    def _extract_response_params(self, response_body: str) -> dict:
        """Pulls top-level fields out of a JSON response body. Handles a
        single object, or a list of objects (union of keys across items,
        capped at MAX_RESPONSE_LIST_ITEMS). Mirrors _extract_params'
        scalarization so nested values (objects/arrays) render the same way
        -- as their JSON string form -- instead of being flattened."""
        params: dict = {}
        if not response_body:
            return params
        try:
            data = json.loads(response_body)
        except Exception:
            return params  # not JSON (HTML, empty, binary placeholder, etc.) -- nothing to extract

        def add_from_dict(d: dict):
            for k, v in d.items():
                params[k] = v if isinstance(v, (str, int, float, bool)) else json.dumps(v)

        if isinstance(data, dict):
            add_from_dict(data)
        elif isinstance(data, list):
            for item in data[:MAX_RESPONSE_LIST_ITEMS]:
                if isinstance(item, dict):
                    add_from_dict(item)
        return params

    # ------------------------------------------------------------------
    # Backward-compatible coercion: params used to store example_values_json
    # as a flat list of values and api_refs_json as a flat list of ref
    # stems, both implicitly "request"-sourced. New rows store role-aware
    # dicts instead ({"request": [...], "response": [...]} and
    # {ref_stem: ["request", "response"]}). These helpers upgrade old rows
    # on read so existing vaults keep working without a hard migration.
    # ------------------------------------------------------------------
    @staticmethod
    def _coerce_values(raw_json: str | None) -> dict:
        data = json.loads(raw_json or "{}")
        if isinstance(data, list):
            return {"request": data, "response": []}
        if isinstance(data, dict):
            return {"request": data.get("request", []), "response": data.get("response", [])}
        return {"request": [], "response": []}

    @staticmethod
    def _coerce_refs(raw_json: str | None) -> dict:
        data = json.loads(raw_json or "{}")
        if isinstance(data, list):
            return {ref: ["request"] for ref in data}
        if isinstance(data, dict):
            return {ref: list(roles) for ref, roles in data.items()}
        return {}

    @staticmethod
    def _resource_key(domain: str, url_path: str) -> str:
        """Coarse 'what resource is this' grouping, e.g.
        petstore.swagger.io:pet vs petstore.swagger.io:user. Takes the first
        path segment that isn't a version marker, a numeric/uuid-ish id, or a
        {templated} placeholder. This is a heuristic, not a guarantee: two
        very different sub-resources under the same top segment (e.g.
        /store/order vs /store/inventory) still share a group. It trades
        perfect precision for a simple, cheap, no-LLM-call-per-capture rule
        that fixes the common case (top-level resources sharing field names
        like "id"/"status"/"name" with unrelated meanings)."""
        def is_placeholder(seg: str) -> bool:
            if re.fullmatch(r"v\d+", seg, flags=re.IGNORECASE):
                return True
            if re.fullmatch(r"\d+", seg):
                return True
            if seg.startswith("{") and seg.endswith("}"):
                return True
            if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}", seg):
                return True  # uuid-shaped
            return False

        segments = [s for s in (url_path or "").split("/") if s]
        resource = next((s for s in segments if not is_placeholder(s)), None)
        return f"{domain}:{resource}" if resource else f"{domain}:_root"

    @staticmethod
    def _resource_key_from_ref_stem(ref_stem: str) -> str:
        """Best-effort fallback for migrating legacy param rows, which only
        ever stored ref_stem strings (no domain/url_path). ref_stem looks
        like 'POST_v2_pet_0' -- strip the method token, then reuse the same
        placeholder-filtering heuristic on the remaining underscore-joined
        tokens. Prefixed with '_legacy' so it can never collide with a
        real domain-scoped key computed from live captures."""
        parts = ref_stem.split("_", 1)
        rest = parts[1] if len(parts) > 1 else ref_stem
        tokens = [t for t in rest.split("_") if t]

        def is_placeholder(tok: str) -> bool:
            return bool(re.fullmatch(r"v\d+", tok, flags=re.IGNORECASE) or re.fullmatch(r"\d+", tok))

        resource = next((t for t in tokens if not is_placeholder(t)), None)
        return f"_legacy:{resource}" if resource else "_legacy:_root"

    @staticmethod
    def _param_filename(name: str, resource_key: str) -> str:
        """Filename stem (no extension) for a param note -- must be unique
        per (resource_key, name) since that's now the param's real identity."""
        return f"{slugify(resource_key)}__{slugify(canonical_param_name(name))}"

    def _param_roles_for_ref(self, ref_stem: str, resource_key: str) -> dict:
        """name -> set of roles ('request'/'response') for every param the
        table has ever recorded against this API ref, scoped to its own
        resource group (a ref_stem only ever belongs to one)."""
        result = {}
        rows = self.conn.execute(
            "SELECT name, api_refs_json FROM params WHERE resource_key=?", (resource_key,)
        ).fetchall()
        for name, refs_json in rows:
            roles = self._coerce_refs(refs_json).get(ref_stem)
            if roles:
                canonical = canonical_param_name(name)
                result.setdefault(canonical, set()).update(roles)
        return result



    def _upsert_param(self, name: str, value, ref_stem: str, resource_key: str, source: str = "request"):
        """source is 'request' or 'response' -- which side of the exchange
        this value was seen on for this particular API. resource_key scopes
        identity so e.g. a pet's "id" and a user's "id" stay separate."""
        value_str = str(value)
        row = self.conn.execute(
            "SELECT example_values_json, api_refs_json FROM params WHERE name=? AND resource_key=?",
            (name, resource_key),
        ).fetchone()

        values = self._coerce_values(row[0]) if row else {"request": [], "response": []}
        refs = self._coerce_refs(row[1]) if row else {}

        bucket = values.setdefault(source, [])
        if value_str not in bucket:
            bucket.append(value_str)
            values[source] = bucket[-MAX_EXAMPLE_VALUES:]

        roles = refs.setdefault(ref_stem, [])
        if source not in roles:
            roles.append(source)

        if row is None:
            self.conn.execute(
                "INSERT INTO params (name, resource_key, example_values_json, api_refs_json) VALUES (?,?,?,?)",
                (name, resource_key, json.dumps(values), json.dumps(refs)),
            )
        else:
            self.conn.execute(
                "UPDATE params SET example_values_json=?, api_refs_json=? WHERE name=? AND resource_key=?",
                (json.dumps(values), json.dumps(refs), name, resource_key),
            )
        self.conn.commit()
        self._render_param_md(name, resource_key)

    # ------------------------------------------------------------------
    # Markdown rendering (always a full overwrite -> idempotent & safe)
    # ------------------------------------------------------------------
    def _render_api_md(self, api_row: dict, param_roles: dict, resource_key: str):
        """param_roles: {param_name: {'request'} | {'response'} | {'request','response'}}"""
        domain = api_row["domain"]
        method = api_row["method"]
        url_path = api_row["url_path"]
        path_template = template_url_path(url_path)
        filename = f"{method}_{slugify(path_template)}.md"
        folder = self.apis_dir / slugify(domain)
        folder.mkdir(parents=True, exist_ok=True)

        route_rows = self.conn.execute(
            "SELECT url_path, full_url, capture_count, first_seen, last_seen FROM apis WHERE method=? AND domain=?",
            (method, domain),
        ).fetchall()
        observed_rows = [row for row in route_rows if template_url_path(row[0]) == path_template]
        observed_paths = sorted({row[0] for row in observed_rows})
        ref_stems = [f"{method}_{slugify(path)}" for path in observed_paths]
        browser_counts: dict[str, int] = {}
        source_sessions: set[str] = set()
        if ref_stems:
            placeholders = ",".join("?" for _ in ref_stems)
            try:
                source_rows = self.conn.execute(
                    f"SELECT session_id, request_headers_json FROM session_api_events "
                    f"WHERE ref_stem IN ({placeholders})", ref_stems).fetchall()
            except sqlite3.Error:
                source_rows = []
            for session_id, headers_json in source_rows:
                source_sessions.add(str(session_id or ""))
                try:
                    headers = {str(k).casefold(): str(v) for k, v in json.loads(headers_json or "{}").items()}
                except (TypeError, ValueError, json.JSONDecodeError):
                    headers = {}
                agent = headers.get("user-agent", "")
                family = "HeadlessChrome" if "headlesschrome" in agent.casefold() else (
                    "Chrome" if "chrome" in agent.casefold() else "Other/unknown")
                browser_counts[family] = browser_counts.get(family, 0) + 1

        entry_points = json.loads(api_row["entry_points_json"] or "[]")
        entry_points_md = "\n".join(f"- {ep}" for ep in entry_points) or "- (none recorded)"
        for observed_path in observed_paths:
            ref = f"{method}_{slugify(observed_path)}"
            for name, roles in self._param_roles_for_ref(ref, self._resource_key(domain, observed_path)).items():
                param_roles.setdefault(name, set()).update(roles)
        param_links = "\n".join(
            f"- [[Params/{self._param_filename(name, resource_key)}]] _{', '.join(sorted(roles))}_"
            for name, roles in sorted(param_roles.items())
        ) or "- (none)"
        examples_md = "\n".join(
            f"- `{row[1] or row[0]}` (captures: {row[2]}, first: {row[3]}, last: {row[4]})"
            for row in observed_rows
        ) or f"- `{api_row['full_url']}`"
        source_md = "; ".join(f"{family}: {count} event(s)"
                               for family, count in sorted(browser_counts.items())) or "No per-session browser provenance recorded"
        if source_sessions:
            source_md += f"; {len(source_sessions)} capture session(s)"
        screenshots = json.loads(api_row.get("screenshots_json") or "[]")
        # Newest first; paths are vault-relative so Obsidian's embed syntax
        # resolves them regardless of which note is viewing them.
        screenshots_md = "\n".join(f"![[{p}]]" for p in reversed(screenshots)) \
            or "_(no screenshot captured yet)_"
        curl_command, has_redacted_values = _api_curl_example(api_row)
        curl_notes = [
            "_Import this cURL command into Postman using **Import → Raw text**. Captured credentials and personal data are not embedded._",
        ]
        if has_redacted_values:
            curl_notes.append(
                "In Postman, replace `[REDACTED]` header or URL values with credentials for a test account "
                "only if the endpoint requires them."
            )
        curl_notes.append("Review the method before sending: this request may change server-side data.")
        curl_notes_md = "\n".join(curl_notes)

        content = f"""---
url: {api_row['full_url']}
method: {method}
path_template: {template_url_path(url_path)}
domain: {domain}
first_seen: {api_row['first_seen']}
last_seen: {api_row['last_seen']}
capture_count: {sum(row[2] or 0 for row in observed_rows)}
tags: [api, captured]
---

## Headers
```
{api_row['headers_json']}
```

## Params
{param_links}

## Concrete captured paths
{examples_md}

## Capture provenance
{source_md}

## Sample Payload
```
{api_row['payload_json'] or '(none)'}
```

## cURL (Postman)
```bash
{curl_command}
```
{curl_notes_md}

## Sample Response
```
{api_row['response_json'] or '(none)'}
```

## Applications / Entry Points
{entry_points_md}

## UI After Call
_Most recent {len(screenshots)} of up to {MAX_SCREENSHOTS_PER_API} kept, newest first._
{screenshots_md}
"""
        from capture_app.knowledge.change_store import rendered_contract_section
        entity_id = f"APIs/{slugify(domain)}/{method}_{slugify(path_template)}"
        content += rendered_contract_section(self.vault_path, entity_id)
        canonical_path = folder / filename
        canonical_path.write_text(content, encoding="utf-8")
        for old_path in observed_paths:
            old_filename = f"{method}_{slugify(old_path)}.md"
            alias_path = folder / old_filename
            if alias_path != canonical_path and alias_path.exists():
                alias_path.write_text(
                    f"---\ntags: [api, legacy-alias]\npath_template: {path_template}\n---\n\n"
                    f"This concrete captured route is represented by [[APIs/{slugify(domain)}/{filename[:-3]}]].\n",
                    encoding="utf-8")

    def _render_param_md(self, name: str, resource_key: str):
        canonical = canonical_param_name(name)
        rows = self.conn.execute(
            "SELECT name, example_values_json, api_refs_json, definition FROM params "
            "WHERE resource_key=? ORDER BY name", (resource_key,)
        ).fetchall()
        matching = [row for row in rows if canonical_param_name(row[0]) == canonical]
        values = {"request": [], "response": []}
        refs: dict = {}
        definitions = []
        wire_names = []
        for raw_name, values_json, refs_json, raw_definition in matching:
            wire_names.append(raw_name)
            raw_values = self._coerce_values(values_json)
            for role in ("request", "response"):
                for value in raw_values.get(role, []):
                    if value not in values[role]:
                        values[role].append(value)
            for ref, roles in self._coerce_refs(refs_json).items():
                refs.setdefault(ref, set()).update(roles)
            if raw_definition and raw_definition not in definitions:
                definitions.append(raw_definition)
        definition = "\n\n".join(definitions)

        request_values = values.get("request", [])
        response_values = values.get("response", [])
        seen_as = ", ".join(
            role for role in ("request", "response")
            if any(role in roles for roles in refs.values())
        ) or "none"

        domain_label, _, resource_label = resource_key.partition(":")
        links_md = "\n".join(
            f"- [[{ref}]] _{', '.join(sorted(roles))}_" for ref, roles in sorted(refs.items())
        ) or "- (none)"
        request_values_md = "\n".join(f"- `{v}`" for v in request_values) or "- (none)"
        response_values_md = "\n".join(f"- `{v}`" for v in response_values) or "- (none)"
        definition_md = definition or "_Not yet analyzed — run `enrich.py` to generate a definition for this param._"

        content = f"""---
tags: [param]
resource: {resource_label or '(unknown)'}
domain: {domain_label}
canonical_name: {canonical}
wire_names: {json.dumps(wire_names, ensure_ascii=False)}
seen_in_apis: {len(refs)}
seen_as: {seen_as}
---

## Definition
{definition_md}

_Note: this note only covers "{name}" as used under the **{resource_label or '(unknown)'}** resource.
The same name may exist as a completely separate, independently-defined param under other resources._

## Used by
{links_md}

## Example values seen (request)
{request_values_md}

## Example values seen (response)
{response_values_md}
"""
        filename = self._param_filename(canonical, resource_key)
        from capture_app.knowledge.change_store import rendered_contract_section
        content += rendered_contract_section(self.vault_path, f"Params/{filename}")
        (self.params_dir / f"{filename}.md").write_text(content, encoding="utf-8")

        # Preserve old concrete-name paths as migration notes so existing
        # links resolve while all newly rendered API links use the canonical note.
        for wire_name in wire_names:
            old_filename = f"{slugify(resource_key)}__{slugify(wire_name)}"
            if old_filename != filename:
                alias_path = self.params_dir / f"{old_filename}.md"
                alias_path.write_text(
                    f"---\ntags: [param, legacy-alias]\ncanonical_name: {canonical}\n---\n\n"
                    f"This captured wire name is represented by [[Params/{filename}]].\n",
                    encoding="utf-8")

    def regenerate_capture_notes(self) -> dict:
        """Rebuild canonical API/parameter notes and keep legacy paths resolvable."""
        api_rows = self.conn.execute(
            "SELECT method, url_path, domain, full_url, first_seen, last_seen, capture_count, "
            "headers_json, payload_json, response_json, entry_points_json, screenshots_json FROM apis"
        ).fetchall()
        for row in api_rows:
            (method, url_path, domain, full_url, first_seen, last_seen, capture_count,
             headers_json, payload_json, response_json, entry_points_json, screenshots_json) = row
            ref_stem = f"{method}_{slugify(url_path)}"
            resource_key = self._resource_key(domain, url_path)
            api_row = {"method": method, "url_path": url_path, "domain": domain,
                       "full_url": full_url, "first_seen": first_seen, "last_seen": last_seen,
                       "capture_count": capture_count, "headers_json": headers_json,
                       "payload_json": payload_json, "response_json": response_json,
                       "entry_points_json": entry_points_json, "screenshots_json": screenshots_json}
            self._render_api_md(api_row, self._param_roles_for_ref(ref_stem, resource_key), resource_key)
        param_rows = self.conn.execute("SELECT name, resource_key FROM params ORDER BY resource_key, name").fetchall()
        for name, resource_key in param_rows:
            self._render_param_md(name, resource_key)
        self.conn.commit()
        return {"api_rows": len(api_rows), "parameter_rows": len(param_rows)}

    # ------------------------------------------------------------------
    # Read/write API used by enrich.py
    # ------------------------------------------------------------------
    def list_apis_with_params(self) -> list:
        """Every captured API that has at least one linked param, with enough
        detail (headers/payload) for enrich.py to rebuild a real request."""
        apis = self.conn.execute(
            "SELECT id, method, url_path, domain, full_url, capture_count, "
            "headers_json, payload_json FROM apis"
        ).fetchall()

        # Only count request-side params here -- this list feeds enrich.py's
        # "pick APIs to fuzz" picker, and an endpoint whose only tracked
        # params come from its response body has nothing to fuzz.
        ref_counts: dict = {}
        for (refs_json,) in self.conn.execute("SELECT api_refs_json FROM params").fetchall():
            for ref, roles in self._coerce_refs(refs_json).items():
                if "request" in roles:
                    ref_counts[ref] = ref_counts.get(ref, 0) + 1

        result = []
        for (api_id, method, url_path, domain, full_url, capture_count,
             headers_json, payload_json) in apis:
            ref_stem = f"{method}_{slugify(url_path)}"
            count = ref_counts.get(ref_stem, 0)
            if count > 0:
                result.append({
                    "id": api_id, "method": method, "url_path": url_path,
                    "domain": domain, "full_url": full_url,
                    "capture_count": capture_count, "param_count": count,
                    "ref_stem": ref_stem, "headers_json": headers_json,
                    "payload_json": payload_json,
                    "resource_key": self._resource_key(domain, url_path),
                })
        return result

    def list_all_apis(self) -> list:
        """Every captured API, regardless of whether it has linked params --
        unlike list_apis_with_params() (which only shows APIs worth fuzzing),
        this is meant for a review/cleanup UI where the user prunes noise
        out of the vault after a capture session."""
        apis = self.conn.execute(
            "SELECT id, method, url_path, domain, full_url, capture_count, "
            "first_seen, last_seen, screenshots_json FROM apis ORDER BY domain, url_path, method"
        ).fetchall()

        ref_param_counts: dict = {}
        for (refs_json,) in self.conn.execute("SELECT api_refs_json FROM params").fetchall():
            for ref in self._coerce_refs(refs_json):
                ref_param_counts[ref] = ref_param_counts.get(ref, 0) + 1

        result = []
        for (api_id, method, url_path, domain, full_url, capture_count,
             first_seen, last_seen, screenshots_json) in apis:
            ref_stem = f"{method}_{slugify(url_path)}"
            result.append({
                "id": api_id, "method": method, "url_path": url_path,
                "domain": domain, "full_url": full_url,
                "capture_count": capture_count, "first_seen": first_seen,
                "last_seen": last_seen, "ref_stem": ref_stem,
                "param_count": ref_param_counts.get(ref_stem, 0),
                "screenshot_count": len(json.loads(screenshots_json or "[]")),
                "resource_key": self._resource_key(domain, url_path),
            })
        return result

    def delete_apis(self, api_ids: list) -> dict:
        """Removes captured APIs entirely: the api row, its variant_runs,
        its Markdown note under APIs/, its saved Screenshots/ files, and its
        footprint in Params/ -- dropping just this API's ref from a param
        that's still used elsewhere, or deleting the param (row + note)
        outright if this was its only source. Returns counts for reporting
        back to the user."""
        stats = {"apis_removed": 0, "params_updated": 0, "params_removed": 0, "screenshots_removed": 0}
        if not api_ids:
            return stats
        with self.lock:
            for api_id in api_ids:
                row = self.conn.execute(
                    "SELECT method, url_path, domain, screenshots_json FROM apis WHERE id=?",
                    (api_id,),
                ).fetchone()
                if not row:
                    continue
                method, url_path, domain, screenshots_json = row
                ref_stem = f"{method}_{slugify(url_path)}"
                resource_key = self._resource_key(domain, url_path)

                param_rows = self.conn.execute(
                    "SELECT name, resource_key, api_refs_json FROM params WHERE resource_key=?",
                    (resource_key,),
                ).fetchall()
                for name, res_key, refs_json in param_rows:
                    refs = self._coerce_refs(refs_json)
                    if ref_stem not in refs:
                        continue
                    del refs[ref_stem]
                    if refs:
                        self.conn.execute(
                            "UPDATE params SET api_refs_json=? WHERE name=? AND resource_key=?",
                            (json.dumps(refs), name, res_key),
                        )
                        self._render_param_md(name, res_key)
                        stats["params_updated"] += 1
                    else:
                        self.conn.execute(
                            "DELETE FROM params WHERE name=? AND resource_key=?",
                            (name, res_key),
                        )
                        note = self.params_dir / f"{self._param_filename(name, res_key)}.md"
                        if note.exists():
                            note.unlink()
                        stats["params_removed"] += 1

                self.conn.execute("DELETE FROM variant_runs WHERE api_id=?", (api_id,))
                self.conn.execute("DELETE FROM apis WHERE id=?", (api_id,))
                self.conn.commit()

                note_path = self.apis_dir / slugify(domain) / f"{method}_{slugify(url_path)}.md"
                if note_path.exists():
                    note_path.unlink()

                for rel_path in json.loads(screenshots_json or "[]"):
                    shot_path = self.vault_path / rel_path
                    if shot_path.exists():
                        shot_path.unlink()
                        stats["screenshots_removed"] += 1

                stats["apis_removed"] += 1

        return stats

    # ------------------------------------------------------------------
    # Interactive cleanup -- run directly via `python vault.py`, typically
    # after capture.py and before enrich.py.
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_selection(raw: str, max_index: int) -> set:
        """Accepts comma-separated indices and ranges, e.g. '1,3,5-7,10'."""
        chosen = set()
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                lo, _, hi = part.partition("-")
                try:
                    lo, hi = int(lo), int(hi)
                except ValueError:
                    print(f"  skipping invalid range: '{part}'")
                    continue
                for i in range(lo, hi + 1):
                    if 1 <= i <= max_index:
                        chosen.add(i)
                    else:
                        print(f"  skipping out-of-range index: {i}")
            else:
                try:
                    i = int(part)
                except ValueError:
                    print(f"  skipping invalid choice: '{part}'")
                    continue
                if 1 <= i <= max_index:
                    chosen.add(i)
                else:
                    print(f"  skipping out-of-range index: {i}")
        return chosen

    @staticmethod
    def _suggest_reasons(api: dict, primary_domain: str) -> list:
        reasons = []
        if api["domain"] != primary_domain:
            reasons.append("third-party domain")
        path_lower = api["url_path"].lower()
        if any(hint in path_lower for hint in NOISE_PATH_HINTS):
            reasons.append("telemetry/analytics-looking path")
        return reasons

    def review_interactive(self):
        """Lists every captured API -- including ones with no linked params
        -- flags likely-noise ones (third-party domains, telemetry-looking
        paths) as suggestions, and lets the user pick which to permanently
        remove via delete_apis(). Nothing is removed without confirmation."""
        apis = self.list_all_apis()
        if not apis:
            print("No captured APIs found yet. Run capture.py first.")
            return

        # "Primary" domain = whichever domain accounts for the most
        # captures. Everything else is a candidate "third-party" suggestion.
        domain_totals: dict = {}
        for api in apis:
            domain_totals[api["domain"]] = domain_totals.get(api["domain"], 0) + api["capture_count"]
        primary_domain = max(domain_totals, key=domain_totals.get)

        print(f"\nPrimary domain (most captures): {primary_domain}\n")

        suggested_indices = set()
        current_domain = None
        for i, api in enumerate(apis, 1):
            if api["domain"] != current_domain:
                current_domain = api["domain"]
                tag = "" if current_domain == primary_domain else "  (third-party)"
                print(f"\n[{current_domain}]{tag}")

            reasons = self._suggest_reasons(api, primary_domain)
            marker = f"  \u2605 suggested ({', '.join(reasons)})" if reasons else ""
            if reasons:
                suggested_indices.add(i)

            print(
                f"  [{i:>3}] {api['method']:<6} {api['url_path']:<45} "
                f"captures={api['capture_count']:<3} params={api['param_count']:<3} "
                f"shots={api['screenshot_count']:<3}{marker}"
            )

        default_str = ",".join(str(i) for i in sorted(suggested_indices))
        prompt = "\nNumbers to REMOVE (comma-separated, ranges like 3-6 ok), 'all', 'none'"
        prompt += f", or press ENTER to accept the suggested set [{default_str}]: " if default_str \
            else " (nothing was flagged as suggested), or 'q' to quit: "

        choice = input(prompt).strip().lower()

        if choice == "q":
            print("No changes made.")
            return
        if choice == "" and default_str:
            choice = default_str
        if choice in ("", "none"):
            print("No changes made.")
            return
        if choice == "all":
            selected = set(range(1, len(apis) + 1))
        else:
            selected = self._parse_selection(choice, len(apis))

        if not selected:
            print("Nothing valid selected -- no changes made.")
            return

        to_remove = [apis[i - 1] for i in sorted(selected)]
        print(f"\nAbout to permanently remove {len(to_remove)} API(s) from the vault:")
        for api in to_remove:
            print(f"  - {api['method']} {api['full_url']}")

        confirm = input("\nType 'y' to confirm removal: ").strip().lower()
        if confirm != "y":
            print("Cancelled -- no changes made.")
            return

        stats = self.delete_apis([api["id"] for api in to_remove])
        print(
            f"\nDone. Removed {stats['apis_removed']} API(s) and "
            f"{stats['screenshots_removed']} screenshot(s); "
            f"{stats['params_updated']} param(s) updated (still used elsewhere), "
            f"{stats['params_removed']} param(s) removed entirely (no longer used anywhere)."
        )

    def get_params_for_api(self, ref_stem: str, role: str | None = None) -> list:
        """role=None returns every param seen for this API on either side.
        role='request' or 'response' restricts to params seen on that side
        for this specific API (a param can be request-only on one API and
        response-only on another)."""
        rows = self.conn.execute(
            "SELECT name, example_values_json, api_refs_json, resource_key FROM params"
        ).fetchall()
        result = []
        for name, examples_json, refs_json, resource_key in rows:
            roles = self._coerce_refs(refs_json).get(ref_stem)
            if roles is None:
                continue
            if role is not None and role not in roles:
                continue
            values = self._coerce_values(examples_json)
            result.append({
                "name": name,
                "examples": values.get("request", []),
                "response_examples": values.get("response", []),
                "roles": roles,
                "resource_key": resource_key,
            })
        return result

    def save_variant_run(self, api_id: int, variant_payload, status, response_headers, response_body):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        safe_headers, safe_body = redact_http_response(response_headers, response_body)
        with self.lock:
            self.conn.execute(
                """INSERT INTO variant_runs
                   (api_id, variant_payload_json, response_status,
                    response_headers_json, response_body, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (api_id, json.dumps(variant_payload), status,
                      json.dumps(safe_headers), safe_body[:MAX_BODY_CHARS], now),
            )
            self.conn.commit()

            # Also harvest whatever fields this LIVE response actually
            # returned (e.g. server-generated ids, computed/echoed fields)
            # into Params/, tagged as response-side for this API.
            row = self.conn.execute(
                "SELECT method, url_path, domain FROM apis WHERE id=?", (api_id,)
            ).fetchone()
            if row:
                method, url_path, domain = row
                ref_stem = f"{method}_{slugify(url_path)}"
                resource_key = self._resource_key(domain, url_path)
                for name, value in self._extract_response_params(safe_body).items():
                    self._upsert_param(name, value, ref_stem, resource_key, source="response")

    def set_param_definition(self, name: str, resource_key: str, definition: str):
        with self.lock:
            exists = self.conn.execute(
                "SELECT 1 FROM params WHERE name=? AND resource_key=?", (name, resource_key)
            ).fetchone()
            if not exists:
                return  # LLM referenced a param/resource pair we don't actually have -- ignore it
            self.conn.execute(
                "UPDATE params SET definition=? WHERE name=? AND resource_key=?",
                (definition, name, resource_key),
            )
            self.conn.commit()
            self._render_param_md(name, resource_key)


if __name__ == "__main__":
    # Cleanup entry point -- run after capture.py, typically before
    # enrich.py, to prune noise (trackers, telemetry, stuff you don't care
    # about) out of the vault before you spend LLM calls enriching it.
    vault_path = Path(input("Obsidian vault folder path [./ObsidianVault]: ").strip() or "./ObsidianVault")
    VaultWriter(vault_path).review_interactive()
