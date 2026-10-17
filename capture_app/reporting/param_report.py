"""
param_report.py
---------------
Auto-generated report for ONE parameter: how to SEE it, how to CHANGE it through
the real site, and a ready-to-paste curl that calls the same API directly.

    python param_report.py --vault ./testVault success
    python param_report.py --vault ./testVault success --resource-key "demowebshop.tricentis.com:addproducttocart"
    python param_report.py --vault ./testVault success --role response -o success.md
    python param_report.py --vault ./testVault --dump-schema      # what this script reads (paste it back if curl blocks look empty)

Reuses ui_transitions.find_paths_for_param() for the click paths and reads
vault.conn for everything else (params + apis tables). Never edits vault.py.

Safety: Cookie / Authorization headers and secret-looking fields (password,
token, csrf, session, ...) are NEVER written into the report -- they become
placeholders. Reports get shared; captured sessions must not travel with them.

The `apis` table lookup is schema-ADAPTIVE (see ApiCatalog): vault.py's column
names weren't visible when this was written, so it detects columns at runtime.
If it can't find headers/body it degrades to skeleton curl commands and says so
in the report instead of failing.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote_plus, urlencode, urlparse

from capture_app.crawling.ui_transitions import find_paths_for_param
from capture_app.storage.vault import VaultWriter, slugify

COOKIE_PLACEHOLDER = '<paste your own session cookie here - see "Getting a fresh cookie" below>'
TOKEN_PLACEHOLDER = '<paste your own token here - see "Getting a fresh cookie" below>'
ROLE_ORDER = ("response", "request")          # report order: seeing it, then changing it
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")

# Headers a browser adds that curl doesn't need (or that curl sets itself).
_NOISE_HEADERS = {
    "host", "content-length", "connection", "accept-encoding", "user-agent", "priority",
    "upgrade-insecure-requests", "dnt", "te", "pragma", "cache-control", "keep-alive",
    "transfer-encoding", "expect", "if-none-match", "if-modified-since",
}
_AUTH_HEADERS = {"authorization", "proxy-authorization"}
_OTHER_SECRET_HEADERS = {"x-csrf-token", "x-xsrf-token", "x-api-key", "x-auth-token", "x-access-token"}
_SECRET_FIELD = re.compile(
    r"password|passwd|passphrase|secret|token|api[-_]?key|authorization|csrf|xsrf|session|card[-_]?number"
    r"|(?<![a-z])(?:pwd|otp|cvv|ssn)(?![a-z])",
    re.IGNORECASE,
)
# FIX (redaction safety net): same field list as _SECRET_FIELD but shaped to
# match "key: value" / "key=value" pairs inside raw, non-JSON, non-form text,
# so a body that doesn't fit either structured case still gets scrubbed
# instead of shipped verbatim. See _prepare_body().
_SECRET_KV = re.compile(
    r'(?i)("?\b(?:password|passwd|passphrase|secret|token|api[-_]?key|authorization|csrf|xsrf|session|'
    r'card[-_]?number|pwd|otp|cvv|ssn)\b"?\s*[:=]\s*"?)[^",&}\n]*'
)
_MISSING = object()


# ----------------------------------------------------------------------
# Errors + data
# ----------------------------------------------------------------------
class ReportError(Exception):
    """Something the user can fix; the CLI prints it without a traceback."""


class ParamNotFound(ReportError):
    pass


class AmbiguousParam(ReportError):
    pass


@dataclass
class ApiSample:
    method: str = ""
    url: str = ""
    headers: dict = field(default_factory=dict)
    payload: str = ""


@dataclass
class Spot:
    """One place the parameter shows up: (role, endpoint)."""
    role: str                                 # "request" | "response"
    ref_stem: str
    method: str
    url: str                                  # "" when the vault never stored a full URL
    paths: list = field(default_factory=list) # find_paths_for_param entries, shortest first
    sample: ApiSample | None = None


@dataclass
class ParamData:
    name: str
    resource_key: str | None
    all_resource_keys: list
    roles: tuple
    examples: dict
    spots: list
    vault_path: str = ""
    catalog_note: str = ""


@dataclass
class Endpoint:
    ref_stem: str
    method: str
    url: str
    sample: ApiSample | None
    roles: set = field(default_factory=set)
    members: list = field(default_factory=list)   # every ref_stem folded into this endpoint
    template: str = ""


@dataclass
class CurlResult:
    text: str | None
    needs_credentials: bool = False
    notes: list = field(default_factory=list)


def _method_from_stem(stem: str) -> str:
    head = stem.split("_", 1)[0].upper()
    return head if head in _METHODS else ""


def _label(method: str, url: str, stem: str) -> str:
    path = urlparse(url).path if url else ""
    if not path:
        return stem            # no URL stored: show the vault's own identifier, don't invent a path
    return f"{method} {path}".strip()


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _chain(entry: dict, limit: int = 6) -> str:
    """Compact route for 'also reachable via': BOOKS -> Fiction -> Add to cart (#2)."""
    names = []
    for s in list(entry["path"]) + [entry["action"]]:
        nm = (s.get("name") or "").strip() or f"({s.get('kind', '?')})"
        n = int(s.get("dup_index") or 0)
        names.append(f"{nm} (#{n + 1})" if n > 0 else nm)
    if len(names) > limit:
        names = names[:limit - 1] + ["..."] + names[-1:]
    return " -> ".join(names)


# ----------------------------------------------------------------------
# Id templates: 'catalog/13/1/1' and 'catalog/31/1/1' are the same call.
# Rule: the first number after a word is an id; later numbers (cart type,
# quantity, ...) stay literal, so add-to-cart (.../1) and add-to-wishlist
# (.../2) are NOT merged. It is a heuristic: ids and enums can't be told
# apart in general.
# ----------------------------------------------------------------------
def _split_stem(stem: str) -> list:
    return re.split(r"([-_])", stem)


def norm_stem(stem: str) -> str:
    out, prev_word = [], False
    for tok in _split_stem(stem):
        if tok in ("-", "_", ""):
            out.append(tok)
        elif tok.isdigit():
            out.append("#" if prev_word else tok)
            prev_word = False
        else:
            out.append(tok)
            prev_word = True
    return "".join(out)


def _stem_ids(stem: str) -> str:
    """The id token(s) norm_stem() replaced, e.g. '13' (or '12/3')."""
    ids, prev_word = [], False
    for tok in _split_stem(stem):
        if tok in ("-", "_", ""):
            continue
        if tok.isdigit():
            if prev_word:
                ids.append(tok)
            prev_word = False
        else:
            prev_word = True
    return "/".join(ids)


def _template_url(url: str) -> str:
    """Same rule on URL path segments: /addproducttocart/catalog/13/1/1 -> /addproducttocart/catalog/{id}/1/1."""
    out, prev_word = [], False
    for seg in [s for s in urlparse(url or "").path.split("/") if s]:
        if seg.isdigit():
            out.append("{id}" if prev_word else seg)
            prev_word = False
        else:
            out.append(seg)
            prev_word = True
    return "/" + "/".join(out)


def _ids_of(stems) -> list:
    return sorted({_stem_ids(s) for s in stems if _stem_ids(s)}, key=lambda x: (len(x), x))


# ----------------------------------------------------------------------
# apis table: schema-adaptive lookup of captured headers / body
# ----------------------------------------------------------------------
def _map_columns(cols: list) -> dict:
    low = {c.lower(): c for c in cols}

    def pick(exact=(), contains=(), exclude=()):
        for name in exact:
            if name in low:
                return low[name]
        for c in cols:
            lc = c.lower()
            if any(x in lc for x in exclude):
                continue
            if any(s in lc for s in contains):
                return c
        return None

    return {
        "method": pick(("method", "http_method"), ("method",)),
        "path": pick(("url_path", "path", "endpoint_path", "route"), ("path",)),
        "domain": pick(("domain", "host", "netloc", "hostname"), ("domain", "host")),
        "url": pick(("url", "full_url", "example_url", "sample_url", "request_url"), ("url",), exclude=("path",)),
        "stem": pick(("ref_stem", "stem", "ref", "state_id"), ("stem",)),
        "headers": pick(("request_headers_json", "request_headers", "headers_json", "headers", "sample_headers"),
                        ("header",), exclude=("response", "resp")),
        "payload": pick(("request_payload", "payload", "sample_payload", "request_body", "payload_json",
                         "request_body_json", "body", "sample_body"),
                        ("payload", "request_body"), exclude=("response", "resp")),
    }


def _parse_headers(raw) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    text = str(raw).strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    out = {}
    if isinstance(data, list):
        for item in data:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                out[str(item[0])] = str(item[1])
            elif isinstance(item, dict) and "name" in item:
                out[str(item["name"])] = str(item.get("value", ""))
        return out
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


class ApiCatalog:
    """Reads the vault's `apis` table once and answers lookup(ref_stem).

    Matching, in order: a ref_stem-like column if the table has one; otherwise a
    stem computed from method + path exactly like ui_transitions._api_ref_stem.
    """

    def __init__(self, vault):
        self.note = ""
        self.mapping: dict = {}
        self._by_stem: dict = {}
        try:
            with vault.lock:
                cur = vault.conn.execute("SELECT * FROM apis")
                names = [d[0] for d in cur.description]
                rows = [dict(zip(names, r)) for r in cur.fetchall()]
        except Exception as e:                       # no such table, locked, ...
            self.note = f"the `apis` table could not be read ({e})"
            return
        self.mapping = m = _map_columns(names)
        # FIX: these two checks used to write to the same self.note in
        # sequence, so if both fired, only the second ("cannot match rows")
        # ever reached --dump-schema output and the headers/payload problem
        # went unreported. Collect both instead of overwriting.
        notes = []
        if not m["headers"] and not m["payload"]:
            notes.append("no headers/body columns were recognised in the `apis` table")
        if not m["stem"] and not (m["method"] and (m["path"] or m["url"])):
            notes.append("cannot match `apis` rows to endpoints (no stem, and no method+path/url columns)")
            self.note = "; ".join(notes)
            return
        self.note = "; ".join(notes)
        for row in rows:
            method = str(row.get(m["method"]) or "").upper() if m["method"] else ""
            url, path = "", ""
            if m["url"] and row.get(m["url"]):
                url = str(row[m["url"]])
                path = urlparse(url).path or "/"
            elif m["path"] and row.get(m["path"]) is not None:
                raw_path = str(row[m["path"]])
                path = urlparse(raw_path).path or "/"
                domain = str(row.get(m["domain"]) or "") if m["domain"] else ""
                url = f"https://{domain}{raw_path}" if domain else ""   # scheme isn't stored: https assumed
            stems = set()
            if m["stem"] and row.get(m["stem"]):
                stems.add(str(row[m["stem"]]))
            if method and path:
                stems.add(f"{method}_{slugify(path)}")
            if not method and stems:
                method = _method_from_stem(next(iter(stems)))
            payload = row.get(m["payload"]) if m["payload"] else ""
            if isinstance(payload, (bytes, bytearray)):
                payload = payload.decode("utf-8", "replace")
            sample = ApiSample(
                method=method, url=url,
                headers=_parse_headers(row.get(m["headers"])) if m["headers"] else {},
                payload="" if payload is None else str(payload),
            )
            for s in stems:
                self._by_stem.setdefault(s, []).append(sample)

    def lookup(self, ref_stem: str) -> ApiSample | None:
        rows = self._by_stem.get(ref_stem)
        if not rows:
            return None
        return next((r for r in rows if r.payload), rows[0])   # prefer one that carries a body


# ----------------------------------------------------------------------
# gather
# ----------------------------------------------------------------------
def gather(vault, param_name: str, resource_key: str | None = None,
           roles: tuple = ROLE_ORDER) -> ParamData:
    """Everything the report needs. Raises ParamNotFound / AmbiguousParam.

    NB: never call find_paths_for_param() while holding vault.lock -- it takes
    the same lock itself."""
    with vault.lock:
        rows = vault.conn.execute(
            "SELECT resource_key, api_refs_json, example_values_json FROM params WHERE name=?",
            (param_name,),
        ).fetchall()
    if not rows:
        with vault.lock:
            names = [r[0] for r in vault.conn.execute("SELECT DISTINCT name FROM params") if r[0]]
        close = difflib.get_close_matches(param_name, names, n=5, cutoff=0.6)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        if "=" in param_name:                       # e.g. typed `param_name=success`
            bare = param_name.split("=", 1)[1]
            hint = f" Pass just the name on its own (e.g. '{bare}'), not '{param_name}'." + hint
        raise ParamNotFound(f"No parameter named '{param_name}' in the vault.{hint}")

    keys = sorted({r[0] or "" for r in rows})
    if resource_key is None and len(keys) > 1:
        listing = "\n".join(f"  - {k or '(no resource key)'}" for k in keys)
        raise AmbiguousParam(
            f"'{param_name}' exists under {len(keys)} different resource keys - "
            f"pick one with --resource-key:\n{listing}")
    if resource_key is not None:
        rows = [r for r in rows if (r[0] or "") == resource_key]
        if not rows:
            listing = "\n".join(f"  - {k or '(no resource key)'}" for k in keys)
            raise ParamNotFound(f"'{param_name}' has no resource key '{resource_key}'. Available:\n{listing}")
    chosen = resource_key if resource_key is not None else (keys[0] or None)

    stem_roles: dict = {}
    seen_roles: set = set()
    examples = {"request": [], "response": []}
    for _rk, refs_json, ex_json in rows:
        try:
            refs = json.loads(refs_json or "{}")
        except ValueError:
            refs = {}
        for stem, rlist in (refs or {}).items():
            for r in (rlist or []):
                seen_roles.add(r)
                if r in roles:
                    stem_roles.setdefault(stem, set()).add(r)
        try:
            ex = json.loads(ex_json or "{}")
        except ValueError:
            ex = {}
        if isinstance(ex, dict):
            for r in ("request", "response"):
                for v in (ex.get(r) or [])[:10]:
                    s = str(v)
                    if s not in examples[r]:
                        examples[r].append(s)
    if not stem_roles:
        only = ", ".join(sorted(seen_roles)) or "no API at all"
        raise ParamNotFound(f"'{param_name}' exists in the vault but not on the side you asked for "
                            f"(--role {', '.join(roles)}); it only appears as: {only}.")

    paths_by: dict = {}
    for role in roles:
        for entry in find_paths_for_param(vault, param_name, chosen, role=role, limit=500):
            paths_by.setdefault((role, entry["ref_stem"]), []).append(entry)
    for lst in paths_by.values():
        lst.sort(key=lambda e: len(e["path"]))                 # shortest directions first

    catalog = ApiCatalog(vault)
    spots = []
    for role in ROLE_ORDER:
        if role not in roles:
            continue
        for stem in sorted(s for s, rs in stem_roles.items() if role in rs):
            entries = paths_by.get((role, stem), [])
            sample = catalog.lookup(stem)
            method = ((entries[0]["api_method"] if entries else "")
                      or (sample.method if sample else "") or _method_from_stem(stem))
            url = (entries[0]["api_url"] if entries else "") or (sample.url if sample else "")
            spots.append(Spot(role=role, ref_stem=stem, method=method.upper(), url=url,
                              paths=entries, sample=sample))
    return ParamData(name=param_name, resource_key=chosen, all_resource_keys=keys, roles=tuple(roles),
                     examples=examples, spots=spots, catalog_note=catalog.note)


@dataclass
class Group:
    """Spots of ONE role whose calls differ only by an id (catalog/13, /31, /52).

    Only stems that themselves carry the parameter ever land here (gather() builds spots from the
    params table), so borrowing a click path from a sibling never points at a call whose response
    lacks the parameter -- e.g. an add-to-cart that returned a redirect instead of {success: ...}."""
    role: str
    template: str
    spots: list
    rep: Spot                       # the member whose path / URL represents the group

    @property
    def paths(self) -> list:
        return self.rep.paths


def _groups(d: ParamData) -> list:
    order, by = [], {}
    for s in d.spots:
        k = (s.role, norm_stem(s.ref_stem))
        if k not in by:
            by[k] = []
            order.append(k)
        by[k].append(s)
    out = []
    for k in order:
        spots = by[k]
        rep = max(spots, key=lambda s: (bool(s.paths), bool(s.url), bool(s.sample)))
        out.append(Group(k[0], k[1], spots, rep))
    return out


def _group_label(g: Group) -> str:
    rep = g.rep
    if len(g.spots) == 1:
        return _label(rep.method, rep.url, rep.ref_stem)
    if rep.url:
        return f"{rep.method} {_template_url(rep.url)}".strip()
    return norm_stem(rep.ref_stem).replace("#", "{id}")


def _endpoints(d: ParamData) -> list:
    """Distinct API calls: one per id-template, across both roles."""
    by: dict = {}
    for s in d.spots:
        by.setdefault(norm_stem(s.ref_stem), []).append(s)
    out = []
    for tpl, spots in by.items():
        rep = max(spots, key=lambda s: (bool(s.sample and s.url), bool(s.paths), bool(s.url), bool(s.sample)))
        out.append(Endpoint(
            ref_stem=rep.ref_stem,
            method=rep.method or next((s.method for s in spots if s.method), ""),
            url=rep.url or next((s.url for s in spots if s.url), ""),
            sample=rep.sample or next((s.sample for s in spots if s.sample), None),
            roles={s.role for s in spots}, members=sorted({s.ref_stem for s in spots}), template=tpl))
    return out


def _endpoint_label(ep: Endpoint) -> str:
    if len(ep.members) > 1:
        if ep.url:
            return f"{ep.method} {_template_url(ep.url)}".strip()
        return ep.template.replace("#", "{id}")
    return _label(ep.method, ep.url, ep.ref_stem)


# ----------------------------------------------------------------------
# Plain-language directions
# ----------------------------------------------------------------------
_THING = {"link": "link", "button": "button", "checkbox": "checkbox", "radio": "option",
          "switch": "switch", "combobox": "drop-down", "listbox": "list", "textbox": "box",
          "searchbox": "search box", "slider": "slider", "spinbutton": "number box"}


def _sentence(step: dict) -> str:
    kind, role = step.get("kind", ""), step.get("role", "")
    name, value = (step.get("name") or "").strip(), step.get("value") or ""
    n = int(step.get("dup_index") or 0)          # 0 = first match on the page; N>0 = the (N+1)th
    thing = _THING.get(role, "control")
    if name:
        target = f'the {_ordinal(n + 1) + " " if n else ""}"{name}" {thing}' + (" on the page (counting from the top)" if n else "")
    else:
        target = f"the {_ordinal(n + 1) if n else 'first'} unlabelled {thing} on the page" + (" (counting from the top)" if n else "")
    if kind == "goto":
        return f"Go to {name}."
    if kind == "click":
        return f"Click {target}."
    if kind == "fill":
        return f'Type "{value}" into {target}.'
    if kind == "toggle":
        return f"Switch {target} on or off."
    if kind == "check":
        return f"Select {target}."
    if kind == "select":
        return f'In {target}, choose "{value}".'
    if kind == "set":
        return f"Set {target} to {value}."
    return f"{kind} {name} {value}".strip() + "."


def describe_path_plain(entry: dict) -> list:
    """One find_paths_for_param() entry -> friendly sentences (unnumbered).
    The last one is the step that actually sends the request."""
    start = entry.get("start_url") or ""
    lines = [f"Open {start} in your browser." if start else "Open the site in your browser."]
    steps = list(entry["path"]) + [entry["action"]]
    lines.extend(_sentence(s) for s in steps)
    lines[-1] += " *(this step sends the request)*"
    return lines


# ----------------------------------------------------------------------
# curl
# ----------------------------------------------------------------------
def _sh(s: str) -> str:
    """POSIX single-quote a string."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _title_header(k: str) -> str:
    return "-".join(p.capitalize() for p in k.split("-"))


def _prepare_headers(headers: dict) -> tuple:
    """-> (headers to print, had_cookie, had_auth). Secrets become placeholders."""
    out, had_cookie, had_auth = [], False, False
    for k, v in headers.items():
        lk = k.lower()
        if lk.startswith(":") or lk.startswith("sec-") or lk in _NOISE_HEADERS:
            continue
        if lk == "cookie":
            had_cookie = True
        elif lk in _AUTH_HEADERS:
            had_auth = True
            out.append((_title_header(k), TOKEN_PLACEHOLDER))
        elif lk in _OTHER_SECRET_HEADERS:
            had_auth = True
            out.append((_title_header(k), TOKEN_PLACEHOLDER))
        else:
            out.append((_title_header(k), v))
    return out, had_cookie, had_auth


def _redact_json(x, hit: list):
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            if _SECRET_FIELD.search(str(k)) and not isinstance(v, (dict, list)):
                out[k] = "<redacted>"
                hit.append(1)
            else:
                out[k] = _redact_json(v, hit)
        return out
    if isinstance(x, list):
        return [_redact_json(v, hit) for v in x]
    return x


def _redact_raw_text(text: str) -> tuple:
    """Last-resort scrub for a body that's neither valid JSON nor recognisably
    form-urlencoded (including JSON that failed to parse despite a JSON
    content-type). Never hand back raw, unexamined text -> (text, redacted)."""
    hit: list = []

    def _sub(m):
        hit.append(1)
        return m.group(1) + "<redacted>"

    return _SECRET_KV.sub(_sub, text), bool(hit)


def _prepare_body(payload: str, content_type: str) -> tuple:
    """-> (body, redacted_anything). Secret-looking fields are replaced."""
    if not payload:
        return "", False
    ct, text = (content_type or "").lower(), payload.strip()
    if "json" in ct or text[:1] in ("{", "["):
        try:
            data = json.loads(payload)
        except ValueError:
            data = _MISSING
        if data is not _MISSING:
            hit: list = []
            clean = _redact_json(data, hit)
            if "x-www-form-urlencoded" in ct and isinstance(clean, dict):     # vault stored a parsed form
                return urlencode({k: v for k, v in clean.items()}), bool(hit)
            return (json.dumps(clean, ensure_ascii=False), True) if hit else (payload, False)
        # FIX: content-type said JSON (or the body looked JSON-shaped) but it
        # didn't actually parse. This used to fall straight through to the
        # x-www-form-urlencoded branch below -- which could mangle it -- or,
        # if it had no "=" in it, get returned completely unredacted. Scrub
        # it generically instead of trusting either path.
        return _redact_raw_text(payload)
    if "x-www-form-urlencoded" in ct or "=" in text:
        parts, flag = [], False
        for seg in payload.split("&"):
            key = seg.split("=", 1)[0]
            if _SECRET_FIELD.search(unquote_plus(key)):
                parts.append(f"{key}=<redacted>")
                flag = True
            else:
                parts.append(seg)
        return "&".join(parts), flag
    # FIX: previously `return payload, False` here -- an unrecognised body
    # shape (not JSON-shaped, no "=", content-type not form-urlencoded) went
    # into the report completely as-is. Run it through the same safety net.
    return _redact_raw_text(payload)


def build_curl(ep: Endpoint) -> CurlResult:
    notes: list = []
    if not ep.url:
        return CurlResult(None, False, ["The full URL for this call isn't stored in the vault, so no curl command "
                                        "could be built. Capture a manual session that hits it."])
    method = (ep.method or "GET").upper()
    sample = ep.sample
    # FIX: ApiCatalog can hand back a non-None Sample whose headers AND
    # payload are both empty -- e.g. this vault's `apis` table columns
    # weren't recognised (ApiCatalog.note says so). That's really the same
    # situation as "no sample", but only `sample is None` got the
    # conservative had_cookie=True default below; an empty-but-present
    # sample silently fell into had_cookie=False, producing a curl command
    # with no Cookie placeholder for a call (e.g. add-to-cart) that very
    # likely needs one. Normalise both to the same "unknown" case.
    if sample is not None and not sample.headers and not sample.payload:
        sample = None
    if sample is None:
        hdrs, had_cookie, had_auth = [], True, False           # unknown: assume a session is needed
        payload = ""
        if method in ("GET", "HEAD"):
            notes.append("No captured headers were found for this call, so this is a skeleton.")
        else:
            notes.append("No captured headers or body were found for this call, so this is a skeleton - "
                         "add the body yourself.")
    else:
        hdrs, had_cookie, had_auth = _prepare_headers(sample.headers)
        payload = sample.payload
        if not sample.headers:
            notes.append("No request headers were captured for this call.")
    ct = next((v for k, v in hdrs if k.lower() == "content-type"), "")

    body, redacted = ("", False)
    if method in ("GET", "HEAD"):
        if payload:
            notes.append("A captured body was ignored because this is a GET/HEAD request.")
    else:
        body, redacted = _prepare_body(payload, ct)
        if body and not ct and body.lstrip()[:1] in ("{", "["):
            hdrs.append(("Content-Type", "application/json"))
    if redacted:
        notes.append("Secret-looking fields in the body were replaced with `<redacted>`. Some sites also need a "
                     "fresh anti-forgery token - copy it the same way as the cookie.")

    parts = [f"curl -X {method} {_sh(ep.url)}" if method != "GET" else f"curl {_sh(ep.url)}"]
    for k, v in hdrs:
        parts.append(f"-H {_sh(f'{k}: {v}')}")
    if had_cookie:
        parts.append(f"-H {_sh('Cookie: ' + COOKIE_PLACEHOLDER)}")
    if body:
        parts.append(f"--data-raw {_sh(body)}")
    return CurlResult(" \\\n  ".join(parts), had_cookie or had_auth, notes)


# ----------------------------------------------------------------------
# Markdown
# ----------------------------------------------------------------------
def _values(values: list, name: str) -> str:
    if _SECRET_FIELD.search(name):
        return ""
    vals = [v.replace("`", "'")[:40] for v in values if v != ""][:5]
    return ", ".join(f"`{v}`" for v in vals)


def render_markdown(d: ParamData, generated: str | None = None) -> str:
    p = d.name
    generated = generated or time.strftime("%Y-%m-%d %H:%M")
    endpoints = _endpoints(d)
    groups = _groups(d)
    n_places, n_calls = len(groups), len(endpoints)
    mapped = [g for g in groups if g.paths]
    unmapped = [g for g in groups if not g.paths]
    merged = any(len(g.spots) > 1 for g in groups)
    roles_present = {g.role for g in groups}
    out: list = []
    counter = [0]

    def heading(title: str) -> str:
        counter[0] += 1
        return f"## {counter[0]}. {title}"

    def ids_note(stems, shown: str) -> str:
        others = [i for i in _ids_of(stems) if i != shown]
        return f"{'id' if len(others) == 1 else 'ids'} {', '.join(others)}" if others else ""

    meta = f"*Generated by `param_report.py` on {generated}"
    if d.resource_key:
        meta += f" - resource key `{d.resource_key}`"
    out += [f"# Parameter report: `{p}`", "", meta + "*", ""]

    # 1. Short version
    out.append(heading("Short version"))
    if roles_present == {"request", "response"}:
        what = f"`{p}` appears on both sides of the API: the site sends it to the server, and the server sends it back."
    elif roles_present == {"request"}:
        what = f"`{p}` is something the site **sends** to the server (it appears in API requests)."
    else:
        what = f"`{p}` is something the server **sends back** to the site (it appears in API responses)."
    counts = (f"It shows up in **{n_places} place{'s' if n_places != 1 else ''}** across **{n_calls} distinct API "
              f"call{'s' if n_calls != 1 else ''}**; **{len(mapped)}** of the places {'has' if len(mapped) == 1 else 'have'} a "
              f"known click path from the site and **{len(unmapped)}** {'does' if len(unmapped) == 1 else 'do'} not yet.")
    if merged:
        counts += " Calls that differ only by an id (for example item 13 vs item 31) are counted once."
    out += ["", what, "", counts, ""]
    ex_bits = []
    for role in ROLE_ORDER:
        v = _values(d.examples.get(role, []), p) if role in d.roles else ""
        if v:
            ex_bits.append(f"{role}: {v}")
    if ex_bits:
        out.append("Example values seen - " + "; ".join(ex_bits) + ".")
        out.append("")

    # 2. Where it shows up
    out.append(heading("Where it shows up"))
    out.append("")
    for g in groups:
        side = "Server response of" if g.role == "response" else "Request to"
        lab = _group_label(g)
        ids = _ids_of(s.ref_stem for s in g.spots)
        seen = f" (seen with ids {', '.join(ids)})" if len(g.spots) > 1 and ids else ""
        if g.paths:
            tail = f"reachable from the site in {len(g.paths[0]['path']) + 2} steps"
            if len(g.spots) > 1:
                tail += f" (route recorded on id {_stem_ids(g.rep.ref_stem)})"
        else:
            tail = "we know this field exists in this call, but haven't yet mapped which button or page triggers it"
        out.append(f"- **{side}** `{lab}`{seen} - {tail}")
    out.append("")

    # 3 / 4. Step by step
    def steps_section(role: str, title: str, intro: str, outro) -> None:
        gs = [g for g in mapped if g.role == role]
        if not gs:
            return
        out.append(heading(title))
        out.extend(["", intro, ""])
        for i, g in enumerate(gs, start=1):
            lab = _group_label(g)
            concrete = _label(g.rep.method, g.paths[0].get("api_url") or g.rep.url, g.rep.ref_stem)
            out.extend([f"### {counter[0]}.{i} `{lab}`", ""])
            for n, line in enumerate(describe_path_plain(g.paths[0]), start=1):
                out.append(f"{n}. {line}")
            out.extend(["", outro(concrete), ""])
            if len(g.spots) > 1:
                via = _stem_ids(g.rep.ref_stem)
                rest = ids_note((s.ref_stem for s in g.spots), via)
                if rest:
                    out.extend([f"*This route was recorded on the call with id {via}. The same call was also seen "
                                f"with {rest}.*", ""])
            for alt in g.paths[1:3]:
                out.extend([f"*Also reachable via:* {_chain(alt)}", ""])

    steps_section(
        "response", "Step by step: seeing it",
        f"Each set of steps ends with the action that makes the site call an API whose **response** contains `{p}`.",
        lambda lab: (f"**Reading the value:** open dev tools (F12) and the **Network** tab *before* the last step, run it, "
                     f"click `{lab}` in the request list and open its **Response** tab. Look for `{p}`."))
    steps_section(
        "request", "Step by step: changing it",
        f"Each set of steps ends with the action that makes the site send `{p}` to the server.",
        lambda lab: ("**Changing the value:** what you type or choose in these steps is what ends up in the request. "
                     f"Repeat them with a different entry, then look at the **Payload** (or **Request**) tab of `{lab}` "
                     "in dev tools to see the new value."))

    # 5. Calling it directly
    out.append(heading("Calling it directly"))
    curl_no = counter[0]
    out += ["", "Each block repeats the request the site sends, without the browser. Paste it into a terminal "
                "(bash, Git Bash, WSL, macOS, Linux) or into Postman via **Import -> Raw text**. On Windows, "
                "PowerShell's `curl` is a different program - use Postman, Git Bash or WSL.", ""]
    needs_credentials = False
    no_url = skeleton = 0
    for ep in endpoints:
        lab = _endpoint_label(ep)
        res = build_curl(ep)
        if not ep.url:
            no_url += 1
        elif ep.sample is None:
            skeleton += 1
        needs_credentials = needs_credentials or res.needs_credentials
        used = " and ".join({"response": "seeing it (response)", "request": "changing it (request)"}[r]
                            for r in ROLE_ORDER if r in ep.roles)
        out += [f"### `{lab}`", "", f"*Carries `{p}` - used for: {used}.*", ""]
        if len(ep.members) > 1:
            shown = _stem_ids(ep.ref_stem)
            rest = ids_note(ep.members, shown)
            if shown and rest:
                out += [f"*The command below uses id {shown}; the same call was also seen with {rest}. Swap the id in "
                        f"the URL to use another.*", ""]
        if (ep.method or "GET").upper() in ("GET", "HEAD"):
            out.append("Asks the server for data; nothing is changed.")
        else:
            out.append("Sends data to the server, which usually creates or changes something - only run it against a "
                       "system you are allowed to test.")
        out.append("")
        if res.text:
            out += ["```bash", res.text, "```", ""]
        for note in res.notes:
            out += [f"> {note}", ""]
        vals = []
        for role in sorted(ep.roles):
            v = _values(d.examples.get(role, []), p)
            if v:
                vals.append(f"{role} - {v}")
        if vals:
            out += [f"> Example values seen for `{p}`: " + "; ".join(vals) + ".", ""]
    if needs_credentials:
        out += ["### Getting a fresh cookie (or token)", "",
                "Sessions expire, and a real cookie should never be saved in a report, so the commands above use a "
                "placeholder. To fill it in:", "",
                "1. In your browser, open the site (and sign in if it needs an account).",
                "2. Open dev tools (F12) and go to the **Network** tab.",
                "3. Repeat the action from the step-by-step section so the request appears in the list.",
                "4. Click that request, open **Headers**, and find **Request Headers -> Cookie** (or **Authorization**).",
                "5. Copy the value and paste it over the placeholder. Treat it like a password: don't commit it or send "
                "it around.", ""]

    # 6. Why this matters (generic)
    out.append(heading("Why this matters"))
    out += ["", "> *Generic text - the script cannot know the real business impact of this field. Tailor it before "
                "sharing.*", ""]
    out.append("A value the site **sends** is a value the caller controls: the server should validate it (type, range, "
               "ownership) instead of trusting the page that produced it. A value the server **sends back** can reveal "
               "data or internal state, so it is worth checking that it only contains what the caller is meant to see. "
               f"Calling the API directly (section {curl_no}) is the quickest way to test both, because it skips any "
               "checks the web page does in the browser.")
    out.append("")

    # 7. What we found
    out.append(heading("What we found / recommendation"))
    out += ["", f"- **{n_places} place{'s' if n_places != 1 else ''}, {n_calls} distinct API call{'s' if n_calls != 1 else ''}** "
                f"({len(mapped)} with a click path, {len(unmapped)} without)."]
    if unmapped:
        labs = ", ".join(f"`{_group_label(g)}`" for g in unmapped[:5])
        out.append(f"- Not yet mapped to a button or page: {labs}. Run a crawl (or a manual capture session) that "
                   f"exercises {'it' if len(unmapped) == 1 else 'them'} to fill this in. To see why, run "
                   f"`python param_report.py --vault {d.vault_path or './ObsidianVault'} {p} --diagnose`.")
    why = f" ({d.catalog_note})" if d.catalog_note else ""
    if no_url:
        out.append(f"- {no_url} call{'s' if no_url != 1 else ''} ha{'ve' if no_url != 1 else 's'} no stored URL, so no "
                   f"curl command could be built{why}.")
    if skeleton:
        out.append(f"- {skeleton} call{'s' if skeleton != 1 else ''} ha{'ve' if skeleton != 1 else 's'} no captured "
                   f"headers/body{why}; the curl block{'s are skeletons' if skeleton != 1 else ' is a skeleton'}.")
    out.append("- Cookie / Authorization headers and secret-looking fields were replaced with placeholders. "
               "This report contains no session data.")
    out.append("")
    return "\n".join(out)


# ----------------------------------------------------------------------
# --dump-schema (so the apis lookup can be pinned to the real vault.py layout)
# ----------------------------------------------------------------------
_SCRUB = re.compile(
    r'(?i)("?(?:cookie|set-cookie|authorization|x-csrf-token|x-api-key|password|passwd|token|secret)"?\s*[:=]\s*"?)'
    r'[^",&}\n]*')


def _scrub(v) -> str:
    s = _SCRUB.sub(r"\1<redacted>", str(v))
    return s if len(s) <= 160 else s[:160] + "..."


def dump_schema(vault) -> None:
    for table in ("apis", "params", "ui_transitions", "sessions", "steps", "session_api_events"):
        print(f"=== {table} ===")
        with vault.lock:
            row = vault.conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not row:
                print("(no such table)\n")
                continue
            print(row[0])
            print("rows:", vault.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            cur = vault.conn.execute(f"SELECT * FROM {table} LIMIT 2")
            names = [d[0] for d in cur.description]
            for r in cur.fetchall():
                print("  sample:", {n: _scrub(v) for n, v in zip(names, r)})
        print()
    cat = ApiCatalog(vault)
    print("=== how param_report maps the apis table ===")
    print(json.dumps(cat.mapping, indent=2) if cat.mapping else "(not mapped)")
    if cat.note:
        print("note:", cat.note)
    print("\nValues that look like cookies / tokens / passwords are scrubbed above, but skim before pasting anywhere.")


# ----------------------------------------------------------------------
# --diagnose: why does this parameter have no click path?
# ----------------------------------------------------------------------
def diagnose(vault, param_name: str, resource_key: str | None = None) -> None:
    def refs_of(raw) -> dict:
        try:
            d = json.loads(raw or "{}")
        except ValueError:
            return {}
        return d if isinstance(d, dict) else {}

    with vault.lock:
        rows = vault.conn.execute("SELECT resource_key, api_refs_json FROM params WHERE name=?", (param_name,)).fetchall()
        all_rows = vault.conn.execute("SELECT api_refs_json FROM params").fetchall()
        has_ut = vault.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ui_transitions'").fetchone()
    if resource_key is not None:
        rows = [r for r in rows if (r[0] or "") == resource_key]
    param_stems = {s for _rk, raw in rows for s in refs_of(raw)}
    all_stems = {s for (raw,) in all_rows for s in refs_of(raw)}
    print(f"Diagnosis for '{param_name}'" + (f" ({resource_key})" if resource_key else ""))
    print(f"- vault (params table): {len(param_stems)} API call(s) carry this parameter")
    for s in sorted(param_stems)[:8]:
        print(f"    {s}")
    if not has_ut:
        print("- ui_transitions: table not found -> no crawl has been saved yet. Run step 5 "
              "(python ui_transitions.py crawl ...) first.")
        return
    with vault.lock:
        total, ok = vault.conn.execute("SELECT COUNT(*), COALESCE(SUM(ok),0) FROM ui_transitions").fetchone()
        api_rows = vault.conn.execute(
            "SELECT triggered_apis_json FROM ui_transitions WHERE ok=1 AND triggered_apis_json != '[]'").fetchall()
    crawl_stems = set()
    for (raw,) in api_rows:
        try:
            crawl_stems |= {a.get("ref_stem") for a in json.loads(raw or "[]") if a.get("ref_stem")}
        except ValueError:
            pass
    print(f"- ui_transitions: {total} rows ({ok} ok); {len(api_rows)} of them triggered API calls, "
          f"{len(crawl_stems)} distinct call(s)")
    for s in sorted(crawl_stems)[:30]:
        print(f"    {s}")
    hit = crawl_stems & param_stems
    print(f"- overlap: {len(hit)} of this parameter's {len(param_stems)} call(s) were triggered by a crawl")
    covered = {s: next((h for h in sorted(hit) if norm_stem(h) == norm_stem(s)), None) for s in param_stems - hit}
    covered = {s: h for s, h in covered.items() if h}
    if covered:
        print("- covered by id-template (same call, another id; the sibling carries this parameter and has a click path):")
        for s, h in sorted(covered.items()):
            print(f"    {s}  <-  {h}")
    tpls = {norm_stem(s) for s in param_stems}
    silent = sorted(s for s in crawl_stems if s not in param_stems and norm_stem(s) in tpls)
    if silent:
        print("- the crawler also hit the same call with ids that do NOT carry this parameter (usually a different "
              "response, e.g. a redirect because the item needs options): " + ", ".join(silent))
    print("VERDICT:", end=" ")
    if not crawl_stems:
        print("no crawled action has triggered any API call yet, so there is nothing to build click paths from.")
    elif hit:
        print("click paths exist for some calls. If the report still shows none, the crawl rows may be "
              "marked not-ok - send me this output.")
    elif not (crawl_stems & all_stems):
        print("MISMATCH - the crawler triggered API calls, but none of their ids appear anywhere in the params "
              "table, so the two id formats probably differ.")
        print(f"    crawler id example: {sorted(crawl_stems)[0]}")
        print(f"    vault id example:   {sorted(all_stems)[0] if all_stems else '(none)'}")
    else:
        print("the crawler has never triggered this parameter's calls (it did trigger other calls, so ids match). "
              "These calls were only seen in a manual capture session.")
        with vault.lock:
            def count(table):
                try:
                    return vault.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except Exception:
                    return "n/a"
            print(f"    manual capture data in the vault: sessions={count('sessions')}, steps={count('steps')}, "
                  f"session_api_events={count('session_api_events')}")
        print("    To get click paths: crawl until it reaches these calls, or build the steps from the manual "
              "session's recorded clicks.")


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")       # Windows consoles choke on some characters
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Generate a report for one parameter from the vault")
    ap.add_argument("param_name", nargs="?", help="parameter name, e.g. success")
    ap.add_argument("--vault", default="./ObsidianVault")
    ap.add_argument("--resource-key", default=None, help="needed when the name exists under several resource keys")
    ap.add_argument("--role", choices=["request", "response", "both"], default="both")
    ap.add_argument("-o", "--output", default=None, help="output .md path (default: derived from the parameter name)")
    ap.add_argument("--dump-schema", action="store_true", help="print the tables this script reads, then exit")
    ap.add_argument("--diagnose", action="store_true",
                    help="explain why the parameter has no click path (needs param_name), then exit")
    args = ap.parse_args(argv)

    if not Path(args.vault).exists():
        print(f"error: vault folder not found: {args.vault}", file=sys.stderr)
        return 2
    vault = VaultWriter(Path(args.vault))
    if args.dump_schema:
        dump_schema(vault)
        return 0
    if not args.param_name:
        ap.error("param_name is required (or use --dump-schema)")
    if args.diagnose:
        diagnose(vault, args.param_name, args.resource_key)
        return 0

    roles = ROLE_ORDER if args.role == "both" else (args.role,)
    try:
        data = gather(vault, args.param_name, args.resource_key, roles)
    except ReportError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    data.vault_path = args.vault
    md = render_markdown(data)
    out_path = Path(args.output) if args.output else Path(
        f"param_report_{slugify(args.param_name)}"
        + (f"__{slugify(args.resource_key)}" if args.resource_key else "") + ".md")
    out_path.write_text(md, encoding="utf-8")
    eps, groups = _endpoints(data), _groups(data)
    mapped = sum(1 for g in groups if g.paths)
    print(f"wrote {out_path}: {len(groups)} place(s), {len(eps)} distinct API call(s), "
          f"{mapped} with a click path")
    if data.catalog_note:
        print(f"note: {data.catalog_note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())