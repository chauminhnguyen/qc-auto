"""
ui_crawler.py
-------------
Headless background explorer seeded by a manual capture.py session.

It replays the captured session's start URL (and, on demand, its step
path), then explores outward: snapshot the accessibility tree, list
interactables (ax_tree.py), perform one UI action, and observe what
happens. Every API the action fires is captured with the same
xhr/fetch listener logic as capture.py and attached to the resulting
transition -- so edges stay "APIs being sent", now annotated with the
UI action that caused them.

Safety mirrors enrich.py: same-domain only, a destructive-name
denylist, submit-like buttons recorded-but-not-clicked unless
`allow_submit=True`, and POST/PUT/PATCH/DELETE-triggering replays
stay behind that same flag.
"""

from __future__ import annotations

import heapq
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from capture_app.core.ax_tree import (
    Action, Interactable, is_cookie_like, is_delete_like, is_denied,
    is_submit_like, rank_actions, fingerprint,
)
from capture_app.core.url_policy import is_ignored
from capture_app.storage.vault import VaultWriter

SETTLE_MS = 400
LOAD_TIMEOUT_MS = 1500
# How long to wait, after an action, for in-flight XHR/fetch calls to
# actually finish before reading self._api_buffer. FIX: _settle() alone
# (a fixed SETTLE_MS sleep + a domcontentloaded check that no-ops for
# AJAX-only actions, since the DOM never unloads) does NOT wait for the
# response an action just triggered -- it was possible for _perform() to
# snapshot self._api_buffer before a slower endpoint's response had
# arrived, silently recording ok=1/apis=[] for an action that DID fire a
# call. networkidle blocks until there's been no in-flight network
# activity for Playwright's own ~500ms window, which is a much better
# proxy for "any response has already reached _on_response" than a fixed
# sleep. Bounded with a timeout so a page with a long-lived connection
# (websocket, polling) never stalls the crawl.
NETWORK_IDLE_TIMEOUT_MS = 3000


def _to_num(raw) -> float | None:
    try:
        return float(raw) if raw is not None and str(raw).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _item_for(items: list[Interactable], action: Action) -> Interactable:
    # dup_index matters here: several items can share (role, name) -- e.g.
    # one "Add to cart" per product tile -- and matching on name alone
    # would always return the FIRST one, silently checking the wrong
    # item's landmark/disabled/etc. against whichever specific instance
    # `action` actually refers to.
    for item in items:
        if item.role == action.role and item.name == action.name and item.dup_index == action.dup_index:
            return item
    return Interactable(role=action.role, name=action.name)

# Submit-like buttons are recorded-but-not-clicked unless allow_submit;
# delete-like ones need the stricter allow_delete. (Name lists live in
# ax_tree.py so the ranker and the gate always agree.)
@dataclass
class Transition:
    from_fp: str
    action: Action
    apis: list[dict] = field(default_factory=list)
    to_fp: str = ""
    to_url: str = ""
    to_items: list[Interactable] = field(default_factory=list)
    ok: bool = True
    detail: str = ""
    tier: int = 0
    # The full action sequence (from the crawl's start URL) used to reach
    # from_fp, i.e. the state this transition's action was performed FROM
    # -- not just from_fp's opaque fingerprint. Persisted verbatim by
    # ui_transitions.py so a later param search can hand back real
    # click-by-click directions, not just "this state led to that state".
    path: list[Action] = field(default_factory=list)
    # Actual page URL the action was performed on. A screen fingerprint can
    # be shared by several URLs, so diagrams and reports should not infer the
    # source page from a fingerprint-to-URL lookup. Kept last to preserve the
    # positional constructor shape used by older integrations.
    from_url: str = ""


class HeadlessCrawler:
    """Background (headless) explorer. One crawl = one browser session."""

    # actions_per_state default raised 8 -> 25: rank_actions() always tries
    # lower tier numbers first (see tier_of() in ax_tree.py), and acted +=1
    # fires after _perform() regardless of success/failure -- so a page
    # listing 8+ "Show details"/expand-style links (tier 1) fully consumes
    # the old budget before any tier-2/3 action (fills, Add to cart, nav
    # clicks) ever gets attempted on that state visit. Confirmed directly
    # in a real crawl log: /apparel-shoes has >=8 tier-1 links and spent
    # its entire budget on them, 0 slots left for anything else.
    def __init__(self, vault: VaultWriter, session_id: str | None = None,
                 max_states: int = 30, max_depth: int = 4,
                 actions_per_state: int = 25, allow_submit: bool = False,
                 allow_delete: bool = False, verbose: bool = False):
        self.vault = vault
        self.session_id = session_id
        self.max_states = max_states
        self.max_depth = max_depth
        self.actions_per_state = actions_per_state
        self.allow_submit = allow_submit
        self.allow_delete = allow_delete
        self.verbose = verbose
        self._api_buffer: list[dict] = []
        self._scope_host = ""
        self._banner_done = False
        # Adaptive layer: per-tier yields this run {tier: {attempts, new, apis}}.
        self.tier_stats: dict[int, dict[str, int]] = {t: {"attempts": 0, "new": 0, "apis": 0} for t in (1, 2, 3, 4, 5)}

    def _log(self, msg: str):
        print(f"[crawler] {msg}", flush=True)

    def _vlog(self, msg: str):
        if self.verbose:
            print(f"[crawler] {msg}", flush=True)

    # ------------------------------------------------------------------
    # Seed: start URL + replayable step path from the manual capture
    # ------------------------------------------------------------------
    def seed(self) -> tuple[str, list[Action]]:
        sid = self.session_id
        conn = self.vault.conn
        if sid is None:
            row = conn.execute(
                "SELECT session_id FROM sessions ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            sid = row[0] if row else None
        self.session_id = sid
        if not sid:
            return "", []
        start = conn.execute(
            "SELECT start_url FROM sessions WHERE session_id=?", (sid,)
        ).fetchone()
        start_url = start[0] if start else ""
        self._scope_host = urlparse(start_url or "").netloc
        steps = conn.execute(
            "SELECT step_type, selector, label, value, input_type, page_url, "
            "tag, elem_type, role FROM steps WHERE session_id=? ORDER BY seq ASC",
            (sid,),
        ).fetchall()
        path: list[Action] = []
        for step_type, _selector, label, value, _input_type, page_url, tag, elem_type, _role in steps:
            label = (label or "").strip()[:80]
            if step_type == "navigate" and page_url:
                path.append(Action("goto", "link", page_url))
            elif step_type == "tap" and label:
                role = "checkbox" if (tag == "input" and elem_type == "checkbox") else "button"
                path.append(Action("click", role, label))
            elif step_type == "input" and label:
                path.append(Action("fill", "textbox", label, value or "test"))
        return start_url, path

    # ------------------------------------------------------------------
    # Crawl
    # ------------------------------------------------------------------
    def crawl(self) -> list[Transition]:
        start_url, _seed_path = self.seed()
        if not start_url:
            self._log("no start URL found in vault -- nothing to crawl")
            return []
        self._log(f"start: {start_url} (scope: {self._scope_host})")
        transitions: list[Transition] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.on("response", self._on_response)

            page.goto(start_url)
            self._settle(page)
            self._log(f"loaded: {page.url} title={page.title()!r}")
            items = self._snapshot(page)
            self._log(f"initial AX snapshot: {len(items)} interactables")
            if not items:
                self._log("snapshot empty -- page may still be loading, retrying once after 3s")
                page.wait_for_timeout(3000)
                items = self._snapshot(page)
                self._log(f"retry AX snapshot: {len(items)} interactables")
            fp0 = fingerprint(page.url, items)
            visited: dict[str, list[Interactable]] = {fp0: items}
            queued: set[str] = {fp0}
            done: set[str] = set()
            # FIX (root cause of "30 states, 0 APIs" on a real multi-page
            # site): this used to be a plain FIFO queue (queue.pop(0)) --
            # strict breadth-first. A home page's own shallow breadth (every
            # top-nav category + footer link + social icon, all depth 1) is
            # routinely wide enough to exhaust max_states on its own, so the
            # crawler never got deep enough to reach a depth-2+ product tile's
            # Add to Cart button -- confirmed directly: a real run spent its
            # entire 30-state budget on home-page-level nav before finding a
            # single AJAX call. A min-heap keyed (-depth, tier, insertion
            # order) drains an entire branch -- depth first -- before
            # backtracking to try a shallower sibling, while still trying
            # the more-promising (lower-tier) discovery first among ties at
            # the same depth. Insertion order is kept purely as a
            # deterministic tiebreaker so heapq is never asked to compare
            # `path` (a list of Action objects, which define no ordering).
            frontier: list[tuple[int, int, int, str, list[Action]]] = []
            counter = 0

            def _push(fp_: str, path_: list[Action], depth_: int, tier_: int = 0):
                nonlocal counter
                heapq.heappush(frontier, (-depth_, tier_, counter, fp_, path_))
                counter += 1

            _push(fp0, [], 0)

            while frontier and len(visited) < self.max_states:
                neg_depth, _tier, _ctr, fp, path = heapq.heappop(frontier)
                depth = -neg_depth
                if fp in done:
                    continue
                done.add(fp)
                if depth >= self.max_depth:
                    continue
                self._replay(page, start_url, path)
                items = self._snapshot(page)
                fp = fingerprint(page.url, items)
                visited.setdefault(fp, items)
                self._log(f"state {fp[:8]} depth={depth} url={page.url} "
                          f"interactables={len(items)} visited={len(visited)}")

                acted = 0
                attempted: set[str] = set()
                productive = {t for t, s in self.tier_stats.items() if s["new"] + s["apis"] > 0}
                ranked = rank_actions(items, self._text_samples,
                                      banner_done=self._banner_done, productive=productive)
                self._vlog(f"ranked {len(ranked)} actions "
                           f"(tiers: {sorted({t for t, _ in ranked})})")
                for tier, action in ranked:
                    if acted >= self.actions_per_state:
                        break
                    if not self._in_scope(page.url):
                        self._log(f"out of scope: {page.url} -- stopping this branch")
                        break
                    akey = f"{action.kind}::{action.role}::{action.name}::{action.value}"
                    if akey in attempted:
                        continue
                    attempted.add(akey)
                    if is_denied(_item_for(items, action)):
                        self._vlog(f"skip denied: {action.role} {action.name!r}")
                        continue
                    if is_delete_like(action) and not self.allow_delete:
                        self._vlog(f"held delete-like: {action.label}")
                        transitions.append(Transition(
                            from_fp=fp, action=action, ok=False, tier=tier,
                            from_url=page.url,
                            detail="delete-like action held (allow_delete=False)",
                        ))
                        continue
                    if is_submit_like(action) and not self.allow_submit:
                        self._vlog(f"held submit-like: {action.label}")
                        transitions.append(Transition(
                            from_fp=fp, action=action, ok=False, tier=tier,
                            from_url=page.url,
                            detail="submit-like action held (allow_submit=False)",
                        ))
                        continue
                    tr = self._perform(page, start_url, path, fp, action)
                    tr.tier = tier
                    transitions.append(tr)
                    acted += 1
                    self.tier_stats[tier]["attempts"] += 1
                    self.tier_stats[tier]["apis"] += len(tr.apis)
                    if tr.ok and "cookie" in action.name.lower():
                        self._banner_done = True
                    self._log(f"  [{acted}] T{tier} {action.label} -> "
                              f"ok={tr.ok} apis={len(tr.apis)} "
                              f"to={tr.to_fp[:8] if tr.to_fp else '-'} "
                              f"{tr.detail or ''}".rstrip())
                    if tr.ok and tr.to_fp and tr.to_fp not in visited:
                        self.tier_stats[tier]["new"] += 1
                        if tr.to_fp in queued:
                            self._vlog(f"already queued: {tr.to_fp[:8]}")
                        elif len(visited) < self.max_states:
                            visited[tr.to_fp] = tr.to_items
                            queued.add(tr.to_fp)
                            _push(tr.to_fp, path + [action], depth + 1, tier)
                        else:
                            self._vlog(f"state cap reached ({self.max_states}) -- not enqueuing {tr.to_fp[:8]}")
            browser.close()
        stats = " ".join(
            f"T{t}=a{s['attempts']}/n{s['new']}/api{s['apis']}"
            for t, s in sorted(self.tier_stats.items())
        )
        self._log(f"done: {len(visited)} states, {len(transitions)} transitions | tiers: {stats}")
        return transitions

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _on_response(self, response):
        try:
            req = response.request
            if req.resource_type not in ("xhr", "fetch"):
                return
            if is_ignored(response.url):
                return
            # Same shape capture.py's own listener captures (payload +
            # response body), sync-API equivalents -- needed so a newly
            # -discovered API (one no manual session ever hit) can still
            # be fed through vault.ingest() and have its params extracted.
            # Previously this only recorded method/url/status/ts, which was
            # enough to color a transition ok/not-ok but not enough to know
            # what params live inside the call.
            try:
                body_text = response.text()
            except Exception:
                body_text = "<binary or unavailable>"
            try:
                req_headers = req.all_headers()
            except Exception:
                req_headers = dict(req.headers)
            self._api_buffer.append({
                "method": req.method, "url": response.url,
                "status": response.status, "ts": time.time() * 1000,
                "request_headers": req_headers,
                "payload": req.post_data,
                "response_body": body_text,
            })
        except Exception:
            pass  # never break the page

    SNAPSHOT_JS = """() => {
      const out = [];
      // Counts occurrences of each (role, name) pair as elements are found,
      // in DOM order -- several controls sharing a name is the norm (one
      // "Add to cart" per product tile on a listing page), and without a
      // way to tell them apart, dedupe_key() in ax_tree.py collapsed them
      // into a single candidate and Playwright's own name-based locator
      // always resolved to whichever was first, every visit, forever.
      const dupSeen = {};
      const ROLE_SET = new Set(['button','link','textbox','searchbox','checkbox','radio','switch','combobox','listbox','slider','spinbutton']);
      function assocLabel(el) {
        try {
          if (el.id) {
            const lf = document.querySelector('label[for="' + el.id + '"]');
            if (lf && lf.innerText.trim()) return lf.innerText.trim().slice(0, 80);
          }
          const w = el.closest && el.closest('label');
          if (w && w.innerText.trim()) return w.innerText.trim().slice(0, 80);
        } catch (e) {}
        return '';
      }
      function roleOf(el) {
        const explicit = (el.getAttribute && el.getAttribute('role')) || '';
        if (ROLE_SET.has(explicit)) return explicit;
        const tag = (el.tagName || '').toLowerCase();
        if (tag === 'button') return 'button';
        if (tag === 'a' && el.hasAttribute('href')) return 'link';
        if (tag === 'textarea') return 'textbox';
        if (tag === 'select') return el.multiple ? 'listbox' : 'combobox';
        if (tag === 'input') {
          const t = (el.type || 'text').toLowerCase();
          if (t === 'hidden') return '';
          if (t === 'checkbox') return 'checkbox';
          if (t === 'radio') return 'radio';
          if (t === 'range') return 'slider';
          if (t === 'number') return 'spinbutton';
          if (t === 'search') return 'searchbox';
          if (t === 'submit' || t === 'button' || t === 'image') return 'button';
          return 'textbox';
        }
        return '';
      }
      const els = document.querySelectorAll(
        'button, a[href], input, select, textarea, [role="button"], [role="link"], ' +
        '[role="textbox"], [role="searchbox"], [role="checkbox"], [role="radio"], ' +
        '[role="switch"], [role="combobox"], [role="listbox"], [role="slider"], [role="spinbutton"]'
      );
      for (const el of els) {
        try {
          const r = el.getBoundingClientRect();
          if (r.width === 0 && r.height === 0) continue;
          const role = roleOf(el);
          if (!role) continue;
          let landmark = '';
          try {
            const lm = el.closest && el.closest('header,footer,nav,main,[role="banner"],[role="contentinfo"],[role="navigation"],[role="main"],[role="complementary"]');
            if (lm) {
              const t = (lm.tagName || '').toLowerCase();
              const rl = (lm.getAttribute && lm.getAttribute('role')) || '';
              landmark = rl || (t === 'header' ? 'banner' : t === 'footer' ? 'contentinfo' : t);
            }
          } catch (e) {}
          const aria = (el.getAttribute && el.getAttribute('aria-label')) || '';
          let name = aria.trim();
          if (!name && (role === 'button' || role === 'link')) {
            name = (el.innerText || el.value || '').trim();
            if (!name) {
              // FIX: a browser's real accessible-name computation checks
              // "name from content" -- which includes a descendant image's
              // alt text -- BEFORE falling to this element's own title
              // attribute (checked further below). Skipping this step is
              // why our captured names didn't match what Playwright's
              // get_by_role() actually searches for: an image-only link
              // like <a title="Show details for X"><img alt="Picture of
              // X"></a> has a REAL accessible name of "Picture of X", not
              // the title text -- confirmed directly against the live
              // site (get_by_role on the title-based name matched zero
              // elements). Every "Show details for X" click was timing
              // out because the locator was searching for a name that
              // never existed in the accessibility tree.
              const img = el.querySelector && el.querySelector('img[alt]');
              if (img) name = (img.getAttribute('alt') || '').trim();
            }
          }
          if (!name) name = assocLabel(el);
          if (!name) name = ((el.getAttribute && el.getAttribute('placeholder')) || '').trim();
          if (!name) name = ((el.getAttribute && el.getAttribute('title')) || '').trim();
          if (!name && role === 'button') name = (el.value || '').trim();
          name = name.slice(0, 80);
          const tag = (el.tagName || '').toLowerCase();
          const disabled = !!(el.disabled || (el.getAttribute && el.getAttribute('aria-disabled') === 'true'));
          let value = '', checked = null, options = [];
          if (tag === 'select') {
            const sel = Array.from(el.selectedOptions || []).map(o => (o.innerText || o.value || '').trim()).filter(Boolean);
            value = sel.join(', ');
            options = Array.from(el.options || []).map(o => (o.innerText || o.value || '').trim()).filter(Boolean).slice(0, 20);
          } else if (role === 'checkbox' || role === 'radio' || role === 'switch') {
            checked = !!(el.checked || (el.getAttribute && el.getAttribute('aria-checked') === 'true'));
          } else if (tag === 'input' || tag === 'textarea') {
            const t = (el.type || '').toLowerCase();
            value = (t === 'password' || t === 'checkbox' || t === 'radio') ? '' : (el.value || '');
          }
          // Counted on the FINAL role+name (after every fallback above has
          // already run), so it matches exactly what dedupe_key() etc. see.
          const dupKey = role + '|' + name;
          const dupIndex = dupSeen[dupKey] || 0;
          dupSeen[dupKey] = dupIndex + 1;
          out.push({
            role, name, value: String(value).slice(0, 200), checked, disabled,
            landmark, dupIndex,
            expanded: el.getAttribute && el.getAttribute('aria-expanded') === 'true' ? true
              : el.getAttribute && el.getAttribute('aria-expanded') === 'false' ? false : null,
            valuemin: el.min !== undefined && el.min !== '' ? el.min : null,
            valuemax: el.max !== undefined && el.max !== '' ? el.max : null,
            valuenow: el.value !== undefined && el.value !== '' ? el.value : null,
            options,
          });
          // FIX (root cause of "Add to cart" -- and anything else deep in
          // DOM order on a content-heavy page -- never being ranked at
          // all): this walks the page in DOM order and used to hard-stop
          // at 300, silently dropping everything after that point with no
          // warning. A single category/home page easily exceeds 300
          // interactables once nav + every product tile's several controls
          // + footer are counted, so real action targets (like an Add to
          // cart button sitting after several "Show details"-style links
          // in DOM order) could be cut before ever reaching rank_actions().
          // Raised well above what a normal page needs; the real cost of
          // exploration was always bounded separately by actions_per_state,
          // not by how much of the page gets scanned.
          if (out.length >= 1500) break;
        } catch (e) { /* never break the page */ }
      }
      return out;
    }"""

    def _snapshot(self, page) -> list[Interactable]:
        try:
            rows = page.evaluate(self.SNAPSHOT_JS)
        except Exception as e:
            self._vlog(f"DOM snapshot raised: {e}")
            return []
        if not rows:
            self._vlog("DOM snapshot returned empty/None")
            return []
        items = []
        for d in rows:
            try:
                items.append(Interactable(
                    role=str(d.get("role", "")), name=str(d.get("name", "") or ""),
                    value=str(d.get("value", "") or ""), checked=d.get("checked"),
                    disabled=bool(d.get("disabled", False)),
                    landmark=str(d.get("landmark", "") or ""),
                    expanded=d.get("expanded"),
                    valuemin=_to_num(d.get("valuemin")), valuemax=_to_num(d.get("valuemax")),
                    valuenow=_to_num(d.get("valuenow")), options=list(d.get("options", []) or [])[:20],
                    dup_index=int(d.get("dupIndex", 0) or 0),
                ))
            except Exception:
                continue
        self._vlog(f"DOM snapshot: {len(rows)} raw -> {len(items)} interactables")
        return items

    def _settle(self, page):
        page.wait_for_timeout(SETTLE_MS)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=LOAD_TIMEOUT_MS)
        except Exception:
            pass

    def _wait_for_network_quiet(self, page):
        """FIX (root cause of ok=1/apis=[] on slower AJAX actions): _settle()
        alone does not wait for a response the action just triggered --
        its domcontentloaded check is a near no-op for actions that don't
        navigate. Call this AFTER _settle() and before reading
        self._api_buffer so a still-in-flight request has a real chance to
        land in _on_response first. Bounded by NETWORK_IDLE_TIMEOUT_MS so a
        page with a long-lived connection (websocket, polling) can't stall
        the crawl -- on timeout we just proceed with whatever's arrived."""
        try:
            page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
        except Exception:
            pass

    def _in_scope(self, url: str) -> bool:
        return urlparse(url or "").netloc == self._scope_host

    def _text_samples(self, item: Interactable) -> list[str]:
        try:
            rows = self.vault.conn.execute(
                "SELECT example_values_json FROM params WHERE name=? LIMIT 5",
                (item.name,),
            ).fetchall()
        except Exception:
            rows = []
        samples: list[str] = []
        if rows:
            import json
            for (raw,) in rows:
                try:
                    data = json.loads(raw or "{}")
                    vals = data.get("request", []) if isinstance(data, dict) else []
                    samples.extend(str(v) for v in vals[:2])
                except Exception:
                    continue
        if item.value.strip().replace(".", "", 1).isdigit():
            # a number box (e.g. Quantity, pre-filled "1"): typing "test" only gets the request rejected
            return [item.value.strip()]
        samples.append("test")
        return samples[:3]

    def _replay(self, page, start_url: str, path: list[Action]):
        self._goto_resilient(page, start_url)
        self._settle(page)
        for action in path:
            try:
                self._do(page, action)
                self._settle(page)
                self._wait_for_network_quiet(page)  # let this step's own replies land BEFORE the next action starts its clock
            except Exception:
                break

    def _goto_resilient(self, page, url: str):
        """FIX for a crawl-killing crash: page.goto() can be interrupted by
        a navigation still resolving in the background from an EARLIER
        action's click (_settle() only waits a fixed short window, not a
        guarantee the previous navigation fully finished) -- an observed
        real race on this site (clicking a real product link, then a
        later action's _replay() resetting to start_url mid-flight), not
        a hypothetical. One retry after a short pause covers the
        transient case. If it still fails, the exception propagates to
        _perform()'s own try/except (below), which now also wraps this
        call -- so worst case this becomes ONE failed transition, not a
        crashed crawl."""
        try:
            page.goto(url)
        except Exception:
            page.wait_for_timeout(500)
            page.goto(url)

    def _perform(self, page, start_url: str, path: list[Action],
                 fp: str, action: Action) -> Transition:
        try:
            self._replay(page, start_url, path)
        except Exception as e:
            # FIX: this used to be unprotected -- a navigation race here
            # (see _goto_resilient's docstring) crashed the entire crawl
            # instead of failing just this one action attempt.
            self._vlog(f"replay failed before action: {action.label} ({e})".rstrip()[:200])
            return Transition(from_fp=fp, action=action, ok=False,
                               from_url=page.url,
                               detail=f"replay/navigation failed: {str(e)[:180]}")
        from_url = page.url
        self._api_buffer = []
        t0 = time.time() * 1000
        try:
            self._do(page, action)
            self._settle(page)
            self._wait_for_network_quiet(page)  # give in-flight XHR/fetch a chance to land
        except Exception as e:
            self._vlog(f"action failed: {action.label} ({e})".rstrip()[:200])
            return Transition(from_fp=fp, action=action, ok=False,
                              from_url=from_url, detail=str(e)[:200])
        apis = [a for a in self._api_buffer if a["ts"] >= t0]
        items = self._snapshot(page)
        return Transition(
            from_fp=fp, action=action, from_url=from_url, apis=apis,
            to_fp=fingerprint(page.url, items), to_url=page.url,
            to_items=items, ok=True, path=path,
        )

    def _do(self, page, action: Action):
        if action.kind == "goto":
            page.goto(action.name)
            return
        try:
            self._do_strict(page, action, first_only=False)
        except Exception as e:
            # Was keyed on Playwright's "strict mode violation" text --
            # that only ever fired because the OLD code used the bare,
            # unnarrowed locator (ambiguous whenever >1 element shared a
            # name). Now that dup_index narrows to a specific occurrence
            # via .nth(), that specific failure mode is gone; broadened to
            # catch any resolution failure (a stale snapshot, a dup_index
            # that's since shifted) and fall back to the first match rather
            # than only recognizing the one error string that used to be
            # the sole cause.
            self._vlog(f"nth({action.dup_index}) lookup for {action.name!r} failed "
                       f"({e}) -- falling back to first match")
            self._do_strict(page, action, first_only=True)

    def _do_strict(self, page, action: Action, first_only: bool):
        locator = page.get_by_role(action.role, name=action.name)
        locator = locator.first if first_only else locator.nth(action.dup_index)
        if action.kind == "click":
            locator.click(timeout=5000)
        elif action.kind == "fill":
            locator.fill(action.value or "test", timeout=5000)
        elif action.kind == "toggle":
            locator.click(timeout=5000)
        elif action.kind == "check":
            locator.check(timeout=5000)
        elif action.kind == "select":
            locator.select_option(label=action.value, timeout=5000)
        elif action.kind == "set":
            try:
                locator.fill(action.value, timeout=5000)
            except Exception:
                locator.press("End", timeout=5000)
        else:
            raise ValueError(f"unsupported action kind: {action.kind}")
