"""
ui_transitions.py
------------------
Step 2 of the param -> frontend-path feature: persist what
HeadlessCrawler (ui_crawler.py) discovers, instead of letting it live only
in memory for the duration of one live_runner.py run.

Two things happen here, both against the SAME sqlite file vault.py
already manages (through vault.conn/vault.lock, never by editing
vault.py itself -- same convention as state_graph.py/state_runner.py
earlier in this project):

1. record_transitions() feeds every API the crawl discovered through
   vault.ingest(), so a param that ONLY ever showed up during autonomous
   crawling (never in a manual capture.py session) still gets extracted
   into vault.py's own `params`/`apis` tables and becomes searchable in
   Obsidian exactly like any other param. This closes a real gap: the
   crawler's own response listener used to record only
   method/url/status -- not payload/response body -- so there was
   nothing to extract params FROM. See the ui_crawler.py patch alongside
   this file (Transition.path, and body-capture in _on_response).

2. It writes one `ui_transitions` row per Transition, carrying the full
   action path used to reach it (Transition.path) and the ref_stem of
   every API it triggered -- the actual "how do I get there" data the
   plan called for.

find_paths_for_param() is the payoff: given a param name (optionally
scoped by resource_key, since vault.py deliberately keeps e.g. a "pet"
id and an "order" id as different identities), it returns every
distinct path discovered that either SETS that param (role="request":
some triggered API carries it in its request) or SHOWS it
(role="response": some triggered API carries it in its response),
newest/most-complete (crawler) paths first.

dup_index (which of several same-named controls): a listing page has one
"Add to cart" per product, all sharing one label. The crawler tells them
apart with Action.dup_index (0 = first on the page, 1 = second, ...).
Directions that say only 'Click "Add to cart"' send a person to the FIRST
one -- often a different product that never fires the API in question --
so the ordinal is saved in path_json (every step) and in the
action_dup_index column (the final action), and is part of the path
dedupe key. Databases created before this change are migrated in place
(ensure_schema adds the column); old rows read as dup_index 0.
"""

from __future__ import annotations

import json
import time
from urllib.parse import urlparse

from capture_app.storage.vault import (
    _is_sensitive_field, _redact_structured_value, _redact_url, slugify,
)

MAX_PATHS_PER_ROLE = 8  # cap results returned; a param can plausibly be
                         # touched by dozens of ref_stems on a big vault


def _api_ref_stem(method: str, url: str) -> str:
    """Same identity live_runner.py's api_to_state_id() computes --
    reimplemented here rather than imported, matching how
    state_executor.py/standard_flow.py each independently redo this same
    small computation elsewhere in the project rather than one script
    importing another as a library."""
    path = urlparse(url or "").path or "/"
    return f"{method}_{slugify(path)}"


def _dup(action) -> int:
    return int(getattr(action, "dup_index", 0) or 0)


def _safe_action_value(action) -> str:
    value = getattr(action, "value", "") or ""
    field_name = f"{getattr(action, 'role', '')} {getattr(action, 'name', '')}"
    if value and _is_sensitive_field(field_name):
        return "[TEST VALUE REQUIRED]"
    return str(_redact_structured_value(value))


def _action_to_dict(action) -> dict:
    return {
        "kind": action.kind,
        "role": action.role,
        "name": _redact_structured_value(action.name),
        "value": _safe_action_value(action),
        "dup_index": _dup(action),
    }


def _action_label(action) -> str:
    # Action.label already exists on ax_tree.Action -- reuse it so a
    # persisted row reads the same way the crawler's own log lines do.
    return getattr(action, "label", f"{action.kind} {action.name}".strip())


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# ----------------------------------------------------------------------
# Schema
# ----------------------------------------------------------------------
def ensure_schema(vault) -> None:
    with vault.lock:
        vault.conn.execute("""
            CREATE TABLE IF NOT EXISTS ui_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                crawl_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                from_fp TEXT NOT NULL,
                to_fp TEXT,
                to_url TEXT,
                action_kind TEXT, action_role TEXT, action_name TEXT,
                action_value TEXT, action_label TEXT,
                action_dup_index INTEGER DEFAULT 0,
                session_id TEXT,
                from_url TEXT,
                path_json TEXT NOT NULL,
                triggered_apis_json TEXT NOT NULL,
                ok INTEGER NOT NULL,
                detail TEXT,
                ts REAL,
                start_url TEXT,
                domain TEXT
            )
        """)
        # In-place migration for databases created before dup_index existed.
        cols = {row[1] for row in vault.conn.execute("PRAGMA table_info(ui_transitions)")}
        if "action_dup_index" not in cols:
            vault.conn.execute("ALTER TABLE ui_transitions ADD COLUMN action_dup_index INTEGER DEFAULT 0")
        if "session_id" not in cols:
            vault.conn.execute("ALTER TABLE ui_transitions ADD COLUMN session_id TEXT")
        if "from_url" not in cols:
            vault.conn.execute("ALTER TABLE ui_transitions ADD COLUMN from_url TEXT")
        vault.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ui_transitions_crawl ON ui_transitions(crawl_id)"
        )
        vault.conn.commit()


# ----------------------------------------------------------------------
# Persist
# ----------------------------------------------------------------------
def record_transitions(
    vault, crawl_id: str, start_url: str, transitions: list,
    *, session_id: str | None = None, ingest_apis: bool = True,
) -> dict:
    """Persists one HeadlessCrawler.crawl() run. Returns
    {"rows_written": N, "apis_ingested": M, "ingest_errors": K} so a CLI
    caller has something concrete to print."""
    ensure_schema(vault)
    domain = urlparse(start_url or "").netloc

    apis_ingested = 0
    ingest_errors = 0
    rows = []  # built first, written in one locked batch at the end

    for seq, tr in enumerate(transitions, start=1):
        api_entries = []
        for api in tr.apis:
            safe_url = _redact_url(api.get("url", ""))
            ref_stem = _api_ref_stem(api.get("method", ""), safe_url)
            api_entries.append({
                "method": api.get("method", ""), "url": safe_url,
                "status": api.get("status"), "ref_stem": ref_stem,
            })

            # Phase 1: teach vault.py about this API, same as capture.py
            # would -- session_id=None deliberately, so crawl discoveries
            # never land in session_api_events (that table means "a real
            # person's manual clickthrough", which this isn't).
            record = {
                "method": api.get("method", ""), "url": api.get("url", ""),
                "status": api.get("status"),
                "request_headers": api.get("request_headers") or {},
                "payload": api.get("payload"),
                "response_body": api.get("response_body"),
                "entry_point": f"[crawl {crawl_id}] {_action_label(tr.action)}",
            }
            if ingest_apis:
                try:
                    vault.ingest(record, session_id=None)
                    apis_ingested += 1
                except Exception:
                    # One malformed capture shouldn't sink the whole batch --
                    # the crawler already tolerates individual action/response
                    # failures the same way (never break the page/run).
                    ingest_errors += 1

        safe_value = _safe_action_value(tr.action)
        safe_name = _redact_structured_value(tr.action.name)
        safe_label = f"{tr.action.kind.title()} {tr.action.role} {safe_name}".strip()
        if safe_value and safe_value != "[TEST VALUE REQUIRED]":
            safe_label += f" with value {safe_value!r}"

        rows.append((
            crawl_id, seq, tr.from_fp, tr.to_fp, _redact_url(tr.to_url or ""),
            tr.action.kind, tr.action.role, safe_name, safe_value,
            safe_label, _dup(tr.action), session_id,
            _redact_url(getattr(tr, "from_url", "") or ""),
            json.dumps([_action_to_dict(a) for a in tr.path]),
            json.dumps(api_entries),
            int(bool(tr.ok)), _redact_structured_value(tr.detail), time.time(),
            _redact_url(start_url or ""), domain,
        ))

    # Phase 2: the path index itself, one locked batch.
    with vault.lock:
        vault.conn.executemany(
            """INSERT INTO ui_transitions
               (crawl_id, seq, from_fp, to_fp, to_url,
                action_kind, action_role, action_name, action_value, action_label, action_dup_index,
                session_id, from_url, path_json, triggered_apis_json, ok, detail, ts, start_url, domain)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        vault.conn.commit()

    return {"rows_written": len(rows), "apis_ingested": apis_ingested, "ingest_errors": ingest_errors}


# ----------------------------------------------------------------------
# Query -- the actual "how do I reach this param" lookup
# ----------------------------------------------------------------------
def find_paths_for_param(vault, param_name: str, resource_key: str | None = None,
                          role: str = "request", limit: int = MAX_PATHS_PER_ROLE) -> list[dict]:
    """role="request" -> "how do I SET this param" (find a ui_transitions
    row whose triggered API has this param on its request side).
    role="response" -> "how do I SEE this param" (response side instead).

    Matches vault.py's own params table by (resource_key, name) when
    resource_key is given, same scoping vault.py uses everywhere else to
    keep two unrelated same-named fields apart; without it, matches by
    name alone (broader, same tradeoff standard_flow.py already accepts
    for its own data-dependency signal).

    Returns newest-first, each entry:
      {"ref_stem", "api_method", "api_url", "path": [Action-dicts],
       "action": {kind, role, name, value, dup_index},
       "crawl_id", "ts", "start_url"}
    """
    ensure_schema(vault)

    # Step 1: which ref_stems actually carry this param in that role,
    # per vault.py's own params table (built by vault.ingest() -- either
    # from a manual capture.py session, or from record_transitions()
    # above feeding a crawl discovery through the same pipeline).
    with vault.lock:
        param_rows = vault.conn.execute(
            "SELECT api_refs_json, resource_key FROM params WHERE name=?",
            (param_name,),
        ).fetchall()

    matching_ref_stems: set[str] = set()
    for refs_json, row_resource_key in param_rows:
        if resource_key is not None and row_resource_key != resource_key:
            continue
        try:
            refs = json.loads(refs_json or "{}")
        except (TypeError, ValueError):
            continue
        for ref_stem, roles in refs.items():
            if role in (roles or []):
                matching_ref_stems.add(ref_stem)

    if not matching_ref_stems:
        return []

    # Step 2: every persisted transition whose triggered_apis_json
    # mentions one of those ref_stems.
    with vault.lock:
        ui_rows = vault.conn.execute(
            """SELECT crawl_id, ts, start_url, path_json, triggered_apis_json,
                      action_kind, action_role, action_name, action_value,
                      COALESCE(action_dup_index, 0)
               FROM ui_transitions WHERE ok=1 ORDER BY ts DESC"""
        ).fetchall()

    seen_paths: set[str] = set()  # dedupe by the action sequence itself
    results: list[dict] = []
    for (crawl_id, ts, start_url, path_json, apis_json,
         a_kind, a_role, a_name, a_value, a_dup) in ui_rows:
        try:
            apis = json.loads(apis_json or "[]")
        except (TypeError, ValueError):
            continue
        for api in apis:
            if api.get("ref_stem") not in matching_ref_stems:
                continue
            # Dedupe on the FULL path including the triggering action --
            # two different final actions from the same prior path are
            # two genuinely different directions, not duplicates. The
            # ordinal is part of "different": the 1st and 2nd "Add to
            # cart" on a page are different buttons.
            dedupe_key = f"{path_json}|{a_kind}{a_name}{a_value}|{a_dup}"
            if dedupe_key in seen_paths:
                continue
            seen_paths.add(dedupe_key)
            results.append({
                "ref_stem": api["ref_stem"],
                "api_method": api.get("method", ""), "api_url": api.get("url", ""),
                # path TO the state this action was performed from, plus
                # the action itself as the final step -- describe_path()
                # renders the two together as one continuous set of
                # directions ending at the actual param-setting click/fill.
                "path": json.loads(path_json),
                "action": {"kind": a_kind, "role": a_role, "name": a_name,
                           "value": a_value, "dup_index": int(a_dup or 0)},
                "crawl_id": crawl_id, "ts": ts, "start_url": start_url,
            })
            if len(results) >= limit:
                return results
    return results


def describe_path(entry: dict) -> str:
    """Render one find_paths_for_param() result as a plain numbered list
    of steps a person can follow -- what an Obsidian note's "how to
    reach this" section would actually show. The LAST step is always the
    action that actually triggered the API (fill/click/etc that carries
    or reveals the param) -- everything before it is just getting there."""
    lines = [f"Starting from {entry['start_url']}:"]
    steps = list(entry["path"]) + [entry["action"]]
    for i, step in enumerate(steps, start=1):
        kind, role, name, value = step["kind"], step["role"], step["name"], step["value"]
        dup = int(step.get("dup_index") or 0)
        nth = f" (the {_ordinal(dup + 1)} one on the page)" if dup > 0 else ""
        if kind == "goto":
            lines.append(f"{i}. Go to {name}")
        elif kind == "click":
            lines.append(f"{i}. Click \"{name}\"{nth}")
        elif kind == "fill":
            lines.append(f"{i}. Fill \"{name}\" with \"{value}\"{nth}")
        elif kind == "toggle":
            lines.append(f"{i}. Toggle \"{name}\"{nth}")
        elif kind == "check":
            lines.append(f"{i}. Select \"{name}\"{nth}")
        elif kind == "select":
            lines.append(f"{i}. In \"{name}\"{nth}, choose \"{value}\"")
        elif kind == "set":
            lines.append(f"{i}. Set \"{name}\"{nth} to {value}")
        else:
            lines.append(f"{i}. {kind} {name} {value}".strip())
    lines.append(f"-> triggers {entry['api_method']} {entry['api_url']} ({entry['ref_stem']})")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def main():
    import argparse
    from pathlib import Path
    from capture_app.storage.vault import VaultWriter

    parser = argparse.ArgumentParser(description="Persist and query crawler-discovered UI paths")
    parser.add_argument("--vault", default="./ObsidianVault")
    sub = parser.add_subparsers(dest="cmd", required=True)

    crawl_p = sub.add_parser("crawl", help="run the crawler and persist its discoveries")
    # Also accept --vault after the subcommand, as in the documented crawl
    # command shape, without overriding the root parser's value when omitted.
    crawl_p.add_argument("--vault", default=argparse.SUPPRESS)
    crawl_p.add_argument("--session", default=None)
    crawl_p.add_argument("--allow-submit", action="store_true")
    crawl_p.add_argument("--allow-delete", action="store_true")
    crawl_p.add_argument(
        "--auth-state", default=None,
        help="Playwright storage-state JSON; skips the manual login pause",
    )
    crawl_p.add_argument(
        "--interactive", action="store_true",
        help="ask for approval before each crawler action",
    )
    crawl_p.add_argument("--crawl-id", default=None, help="defaults to a timestamp")

    find_p = sub.add_parser("find", help="find how to reach a param from the frontend")
    find_p.add_argument("param_name")
    find_p.add_argument("--resource-key", default=None)
    find_p.add_argument("--role", choices=["request", "response"], default="request")

    args = parser.parse_args()
    vault = VaultWriter(Path(args.vault))

    if args.cmd == "crawl":
        from capture_app.crawling.best_first_crawler import BestFirstCrawler
        crawl_id = args.crawl_id or time.strftime("%Y%m%dT%H%M%S")
        # --session / --allow-submit / --allow-delete are forwarded to the
        # crawler (an earlier version parsed them and silently dropped them).
        crawler = BestFirstCrawler(
            vault, args.session,
            allow_submit=args.allow_submit,
            allow_delete=args.allow_delete,
            max_states=60,
            max_depth=10,
            max_actions=150,
            verbose=True,
            interactive=args.interactive,
            auth_state=args.auth_state,
        )
        start_url, _ = crawler.seed()
        transitions = crawler.crawl()
        summary = record_transitions(
            vault, crawl_id, start_url, transitions, session_id=args.session,
        )
        print(f"crawl_id={crawl_id}: {summary['rows_written']} transitions persisted, "
              f"{summary['apis_ingested']} APIs ingested "
              f"({summary['ingest_errors']} ingest errors)")

    elif args.cmd == "find":
        results = find_paths_for_param(vault, args.param_name, args.resource_key, args.role)
        if not results:
            print(f"No {args.role}-side path found for '{args.param_name}' yet -- "
                  f"run a crawl or a manual capture session first.")
            return
        for i, entry in enumerate(results, start=1):
            print(f"\n--- Path {i} of {len(results)} ---")
            print(describe_path(entry))


if __name__ == "__main__":
    main()
