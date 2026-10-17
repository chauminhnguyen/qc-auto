#!/usr/bin/env python3
"""
tools/merge_crawler_fixes.py -- one-time merge of the agreed crawler fixes into your EXISTING files.

Edits, in place:   best_first_crawler.py   ui_crawler.py   vault.py   ui_transitions.py
No new files are added to your project: this script deletes itself when it succeeds
(use --keep to keep it). Originals are saved next to each file as *.bak.

Safe: every edit is anchored on exact existing code and must match exactly once; if ANY
anchor is missing, NOTHING is written and the script tells you which one.

    python tools/merge_crawler_fixes.py [folder]   (default: current folder)
"""
import os, pathlib, sys

# ======================================================================================
# best_first_crawler.py
# ======================================================================================
LEDGER = '''# ----------------------------------------------------------------------
# "Press every control" bookkeeping. No button names anywhere: the crawler learns what
# each control DOES by pressing it, and stops pressing a control once pressing it stops
# teaching anything new.
#   control key : kind|role|name of a control, ignoring which copy of it it is
#   context     : the kind of page the press happened on (URL path shape)
#   effect      : where the press ended up + which server calls it sent + the SHAPE of
#                 each reply (its field names and success flag, not its values)
# ----------------------------------------------------------------------
def ctrl_key(action: Action) -> str:
    return f"{action.kind}|{action.role}|{norm_name(action.name)}"


def reply_shape(body) -> str:
    """Field names + success flag of a JSON reply -- not its values."""
    if not body or body == "<binary or unavailable>":
        return "unreadable"
    try:
        data = json.loads(body)
    except Exception:
        return "text"
    if isinstance(data, dict):
        flag = data.get("success")
        tag = "ok" if flag is True else "fail" if flag is False else "-"
        return tag + ":" + ",".join(sorted(data))
    return "list" if isinstance(data, list) else "value"


def _call_sig(api: dict) -> str:
    return f"{api_sig(api.get('method', ''), api.get('url', ''))}~{reply_shape(api.get('response_body'))}"


def effect_sig(dest_sid: str, same_screen: bool, apis: list, ok: bool = True) -> str:
    """What a press did. same_screen -> it did not navigate, so the screen id is left out
    and the same press on two pages of one kind compares equal."""
    if not ok:
        return "FAIL"
    calls = ";".join(sorted(_call_sig(a) for a in apis))
    return f"{'SAME' if same_screen else dest_sid}|{calls}"


def _effect_kind(effect: str) -> str:
    """fail | call (sent server requests) | nav (moved to another screen, sent nothing) | noop."""
    if effect == "FAIL":
        return "fail"
    head, _, calls = effect.partition("|")
    if calls:
        return "call"
    return "noop" if head == "SAME" else "nav"


class PressLedger:
    """Remembers what every press did, so the crawler can press every control once, keep
    pressing the ones that keep surprising it, and drop the ones that never do
    (menus, footers, buttons that do nothing)."""

    def __init__(self, patience: int = 2, furniture_contexts: int = 2):
        self.patience = patience
        self.furniture_contexts = furniture_contexts
        self._hist: dict[str, list[tuple[str, str]]] = {}   # control key -> [(context, effect)]
        self._seen_on: dict[str, set] = {}                  # control key -> kinds of page it appeared on
        self._reply_shapes: set[str] = set()                # every (endpoint, reply shape) ever seen

    def observe(self, ck: str, context: str) -> None:
        self._seen_on.setdefault(ck, set()).add(context)

    def spread(self, ck: str) -> int:
        """On how many kinds of page has this control appeared? (menus/footers: many; content: few)"""
        return len(self._seen_on.get(ck, ()))

    def times(self, ck: str, context: str | None = None) -> int:
        h = self._hist.get(ck, [])
        return len(h) if context is None else sum(1 for c, _ in h if c == context)

    def surprise(self, ck: str) -> int:
        """How many different effects this control has shown so far."""
        return len({e for _, e in self._hist.get(ck, [])})

    def bored(self, ck: str, context: str) -> bool:
        h = self._hist.get(ck, [])
        if not h:
            return False
        kinds = [_effect_kind(e) for _, e in h]
        if len(h) >= 2 and all(k == "noop" for k in kinds):      # did nothing visible, twice
            return True
        navs = {e for (_, e), k in zip(h, kinds) if k == "nav"}  # menu/footer style: always goes to one place
        if (len(navs) == 1 and all(k in ("nav", "noop") for k in kinds)
                and len(self._seen_on.get(ck, ())) >= self.furniture_contexts):
            return True
        seen: set[str] = set()                                   # its last presses HERE repeated old effects
        stale_here: list[bool] = []
        for c, e in h:
            if c == context:
                stale_here.append(e in seen)
            seen.add(e)
        return len(stale_here) >= self.patience and all(stale_here[-self.patience:])

    def record(self, ck: str, context: str, effect: str, apis: list) -> float:
        """Log one press; returns the extra reward it earned (a new kind of effect, a reply
        with field names never seen before)."""
        h = self._hist.setdefault(ck, [])
        fresh = all(e != effect for _, e in h)
        h.append((context, effect))
        bonus = 0.5 if fresh else 0.0
        for api in apis:
            sig = _call_sig(api)
            if sig not in self._reply_shapes:
                self._reply_shapes.add(sig)
                bonus += 1.0
        return bonus


'''

INIT_EXTRA = '''self._rng = random.Random(rng_seed)
        # Generic "press every control" policy (see PressLedger above). Set use_ledger = False
        # to get the old ordering back (kept as _pick_legacy / _score_legacy).
        self.use_ledger = True
        self.ledger = PressLedger()
        # A press that fails with a message naming a form field ("Length in cm") -> fill that
        # field and press again.
        self.repair_failed_presses = True
        self._repaired: set = set()
        # Start from the pages the manual capture session visited, not only from the home page.
        self.seed_pages = True
        self.max_seed_pages = 12'''

METHODS = '''def _score(self, n: Node, total_visits: int) -> float:
        base = self._score_legacy(n, total_visits)
        if not self.use_ledger:
            return base
        # prefer screens that still hold controls nobody has pressed yet
        return base + 0.5 * min(1.0, self._potential(n) / 8.0)

    def _candidates(self, node: Node) -> list:
        """Every control on this screen still worth pressing, best first (no names involved).

        Order: controls never pressed ANYWHERE first, then controls not yet pressed on THIS
        kind of page, then the ones whose effect has varied the most (a control that always
        does the same thing is furniture), then controls found on few kinds of page (content)
        before controls found on many (menus/footers); tier and page position break ties.
        Controls the ledger calls bored are dropped."""
        productive = {t for t, s in self.tier_stats.items() if s["new"] + s["apis"] > 0}
        ranked = rank_actions(node.items, self._text_samples,
                              banner_done=self._banner_done, productive=productive)
        ctx = path_shape(node.url)
        pos: dict = {}
        for i, it in enumerate(node.items):
            pos.setdefault((it.role, it.name, it.dup_index), i)
        L = self.ledger
        out = []
        for tier, action in ranked:
            ck = ctrl_key(action)
            L.observe(ck, ctx)
            if _akey(action) in node.attempted:
                continue
            if L.bored(ck, ctx):
                continue
            out.append((tier, action, ck))
        out.sort(key=lambda t: (L.times(t[2]) > 0, L.times(t[2], ctx) > 0,
                                -min(L.surprise(t[2]), 3), L.spread(t[2]), t[0],
                                pos.get((t[1].role, t[1].name, t[1].dup_index), 0)))
        return out

    def _potential(self, node: Node) -> int:
        return sum(1 for _t, _a, ck in self._candidates(node) if self.ledger.times(ck) == 0)

    def _ledger_note(self, node: Node, action: Action, tr) -> float:
        """Log what this press did; returns extra reward for a new kind of effect/reply."""
        same = bool(tr.ok) and tr.to_fp == node.sid
        effect = effect_sig(tr.to_fp, same, tr.apis, ok=bool(tr.ok))
        return self.ledger.record(ctrl_key(action), path_shape(node.url), effect,
                                  tr.apis if tr.ok else [])

    # -- start from the pages the human visited ------------------------------------------
    def _seed_nodes(self, page, screens: ScreenIndex, start_url: str, seed_path: list, nodes: dict) -> list:
        """Each page the manual capture session navigated to becomes a starting node, so the
        crawler reaches e.g. a product page directly instead of hoping to find its way there."""
        out = []
        seen_urls = {start_url}
        for a in seed_path:
            if len(out) >= self.max_seed_pages:
                break
            if a.kind != "goto" or a.name in seen_urls or not self._in_scope(a.name):
                continue
            seen_urls.add(a.name)
            try:
                page.goto(a.name)
                self._settle(page)
                items = self._snapshot(page)
            except Exception as e:
                self._vlog(f"seed page {a.name} failed: {str(e)[:120]}")
                continue
            if not items:
                continue
            saved, screens.fold = screens.fold, None      # never fold a page the human chose to visit
            try:
                sid, is_new = screens.resolve(page.url, items)
            finally:
                screens.fold = saved
            if not is_new or sid in nodes:
                continue
            node = Node(sid, page.url, [Action("goto", "link", a.name)], 1, items)
            node.credit = 1.0
            nodes[sid] = node
            out.append(node)
            self._log(f"seed page {sid[:8]}: {page.url} ({len(items)} interactables)")
        return out

    # -- retry a failed press using the server's own error message ---------------------------
    def _failure_messages(self, tr) -> list:
        """Messages from any reply that says success:false, e.g. ["Length in cm"]."""
        msgs: list = []
        for a in tr.apis:
            try:
                data = json.loads(a.get("response_body") or "")
            except Exception:
                continue
            if isinstance(data, dict) and data.get("success") is False:
                m = data.get("message")
                msgs += [str(x) for x in (m if isinstance(m, list) else [m] if m else [])]
        return msgs

    def _repair_fields(self, tr) -> list:
        """Inputs on the page whose label matches something the failure message named."""
        msgs = [norm_name(m) for m in self._failure_messages(tr)]
        msgs = [m for m in msgs if len(m) >= 3]
        if not msgs:
            return []
        out = []
        for it in tr.to_items:
            if it.disabled or not it.name or it.role not in ("textbox", "searchbox", "combobox", "listbox", "spinbutton"):
                continue
            n = norm_name(it.name)
            if len(n) >= 3 and any(m in n or n in m for m in msgs):
                out.append(it)
        return out[:3]

    def _repair(self, page, screens: ScreenIndex, node: Node, action: Action, tier: int, tr,
                pending: list, transitions: list, used: int):
        """A press failed and the server named the missing field(s): fill them, press again.
        Returns (actions_spent, moved_away, extra_reward)."""
        fields = self._repair_fields(tr)
        key = (node.sid, _akey(action))
        if not fields or key in self._repaired:
            return 0, False, 0.0
        self._repaired.add(key)
        spent, gained = 0, 0.0

        def run(act: Action, act_tier: int):
            nonlocal spent, gained
            spent += 1
            t = self._act(page, node.sid, node.path + pending, act)
            t.tier = act_tier
            here = False
            if t.ok and not self._in_scope(t.to_url):
                t.ok, t.detail = False, "left scope"
            elif t.ok:
                sid2, _new = screens.resolve(t.to_url, t.to_items, from_shape=path_shape(node.url))
                t.to_fp = sid2
                here = sid2 == node.sid
                if here:
                    pending.append(act)
            if self.use_ledger:
                gained += self._ledger_note(node, act, t)
            transitions.append(t)
            self._log(f"  [{used + spent}] T{act_tier} {act.label} -> ok={t.ok} apis={len(t.apis)} "
                      f"to={t.to_fp[:8] if t.to_fp else '-'} (retry: server named a missing field) "
                      f"{t.detail or ''}".rstrip())
            return t, here

        for candidate in ("1", "test"):
            if used + spent + len(fields) + 1 > self.max_actions:
                break
            for it in fields:
                if it.role in ("combobox", "listbox"):
                    opts = [o for o in it.options if o.strip()]
                    if not opts:
                        continue
                    fix = Action("select", it.role, it.name, opts[1] if len(opts) > 1 else opts[0],
                                 dup_index=it.dup_index)
                else:
                    fix = Action("fill", it.role, it.name, candidate, dup_index=it.dup_index)
                _t, here = run(fix, 2)
                if not here:
                    return spent, True, gained
            t, here = run(action, tier)
            if not here:
                return spent, True, gained
            if not self._failure_messages(t):
                break
        return spent, False, gained

    def _score_legacy(self, n: Node, total_visits: int) -> float:'''

PICK = '''def _pick(self, node: Node, transitions: list[Transition]) -> tuple[list[tuple[int, Action]], bool]:
        """Press-every-control selection (see PressLedger). Safety gates are the same as the
        legacy version: denied / delete-like / submit-like."""
        if not self.use_ledger:
            return self._pick_legacy(node, transitions)
        cands = self._candidates(node)
        picks: list[tuple[int, Action]] = []
        exhausted = True
        for tier, action, _ck in cands:
            if len(picks) >= self.k_per_visit:
                exhausted = False
                break
            node.attempted.add(_akey(action))
            if is_denied(_item_for(node.items, action)):
                continue
            if is_delete_like(action) and not self.allow_delete:
                transitions.append(Transition(
                    from_fp=node.sid, action=action, ok=False, tier=tier, path=node.path,
                    detail="delete-like action held (allow_delete=False)"))
                continue
            if is_submit_like(action) and not self.allow_submit:
                transitions.append(Transition(
                    from_fp=node.sid, action=action, ok=False, tier=tier, path=node.path,
                    detail="submit-like action held (allow_submit=False)"))
                continue
            picks.append((tier, action))
        return picks, exhausted

    def _pick_legacy(self, node: Node, transitions: list[Transition]) -> tuple[list[tuple[int, Action]], bool]:'''

SEED_BLOCK = '''self._log(f"root {sid[:8]}: {len(items)} interactables")
            if self.seed_pages:
                for seed_node in self._seed_nodes(page, screens, start_url, seed_path, nodes):
                    frontier.append(seed_node)
                cur = None          # the browser is now on a seed page, not on the root'''

LEDGER_HOOK = '''if self.use_ledger:
                        reward += self._ledger_note(node, action, tr)
                    st = self.tier_stats[tier]'''

REPAIR_HOOK = '''f"to={tr.to_fp[:8] if tr.to_fp else '-'} {tr.detail or ''}".rstrip())
                    if (self.repair_failed_presses and tr.ok and action.kind == "click"
                            and tr.to_fp == node.sid and used < self.max_actions):
                        spent, moved, gained = self._repair(page, screens, node, action, tier, tr,
                                                            pending, transitions, used)
                        used += spent
                        if moved:
                            cur = None
                        if gained:
                            node.credit += gained
                            stale = 0'''

# ======================================================================================
# ui_crawler.py
# ======================================================================================
CAPTURE = '''CAPTURE_JS = """(() => {
      if (window.__crawlerCapture) return;
      window.__crawlerCapture = true;
      const emit = (rec) => { try { window.__crawlerApi(rec); } catch (e) {} };
      const abs = (u) => { try { return new URL(u, location.href).href; } catch (e) { return String(u); } };
      const bodyText = (b) => {
        if (b == null) return null;
        if (typeof b === 'string') return b;
        if (typeof URLSearchParams !== 'undefined' && b instanceof URLSearchParams) return b.toString();
        return null;
      };
      // ---- XMLHttpRequest (jQuery.ajax and most older sites) ----
      const xOpen = XMLHttpRequest.prototype.open, xSend = XMLHttpRequest.prototype.send,
            xHeader = XMLHttpRequest.prototype.setRequestHeader;
      XMLHttpRequest.prototype.open = function (method, url) {
        this.__c = { method: String(method || 'GET').toUpperCase(), url: abs(url), headers: {}, payload: null, ts: 0 };
        return xOpen.apply(this, arguments);
      };
      XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
        try { if (this.__c) this.__c.headers[String(k).toLowerCase()] = String(v); } catch (e) {}
        return xHeader.apply(this, arguments);
      };
      XMLHttpRequest.prototype.send = function (body) {
        const c = this.__c;
        if (c) {
          c.ts = Date.now();
          c.payload = bodyText(body);
          this.addEventListener('loadend', () => {
            let text = '';
            try { if (this.responseType === '' || this.responseType === 'text') text = this.responseText; } catch (e) {}
            emit(Object.assign({}, c, { status: this.status, body: text }));
          });
        }
        return xSend.apply(this, arguments);
      };
      // ---- fetch ----
      const realFetch = window.fetch;
      if (realFetch) {
        window.fetch = function (input, init) {
          const c = { method: 'GET', url: '', headers: {}, payload: null, ts: Date.now() };
          try {
            const isReq = typeof Request !== 'undefined' && input instanceof Request;
            c.url = abs(isReq ? input.url : input);
            c.method = String((init && init.method) || (isReq ? input.method : 'GET')).toUpperCase();
            const h = (init && init.headers) || (isReq ? input.headers : null);
            if (h) {
              if (typeof h.forEach === 'function') h.forEach((v, k) => { c.headers[String(k).toLowerCase()] = String(v); });
              else Object.keys(h).forEach((k) => { c.headers[k.toLowerCase()] = String(h[k]); });
            }
            c.payload = bodyText(init && init.body);
            if (c.payload !== null && init && init.body instanceof URLSearchParams && !c.headers['content-type'])
              c.headers['content-type'] = 'application/x-www-form-urlencoded;charset=UTF-8';
          } catch (e) {}
          const p = realFetch.apply(this, arguments);
          p.then((r) => {
            try {
              r.clone().text().then(
                (t) => emit(Object.assign({}, c, { status: r.status, body: t })),
                () => emit(Object.assign({}, c, { status: r.status, body: '' })));
            } catch (e) {}
          }, () => {});
          return p;
        };
      }
    })()"""

    def _attach_capture(self, page):
        """Record every XHR/fetch from INSIDE the page the moment it completes. Playwright's
        response.text() fails for calls followed by a navigation (e.g. an add-to-cart reply
        that redirects), which used to leave "<binary or unavailable>" instead of the reply;
        and stamping each call with when it STARTED (not when its reply arrived) means a slow
        reply can no longer be credited to whichever action happens to run next."""
        self._inpage = False
        try:
            page.expose_function("__crawlerApi", self._on_inpage_call)
            page.context.add_init_script(script=self.CAPTURE_JS)
            self._inpage = True
        except Exception as e:
            self._vlog(f"in-page capture unavailable ({e}) -- using response events instead")
        page.on("response", self._on_response)

    def _on_inpage_call(self, rec):
        try:
            url = str(rec.get("url") or "")
            if not url.startswith("http") or is_ignored(url):
                return
            body = rec.get("body")
            self._api_buffer.append({
                "method": str(rec.get("method") or "GET"), "url": url,
                "status": rec.get("status"), "ts": float(rec.get("ts") or time.time() * 1000),
                "request_headers": rec.get("headers") or {},
                "payload": rec.get("payload"),
                "response_body": body if isinstance(body, str) else "",
            })
        except Exception:
            pass  # never break the page

    def _on_response(self, response):'''

GUARD = '''if getattr(self, "_inpage", False):
                return  # calls are captured from inside the page instead (see _attach_capture)
            req = response.request
            if req.resource_type not in ("xhr", "fetch"):'''

EDITS = {
"best_first_crawler.py": [
 ("import math\nimport random\nimport time", "import json\nimport math\nimport random\nimport time"),
 ("is_denied, is_submit_like, path_shape, rank_actions, _CHROME_LANDMARKS,",
  "is_denied, is_submit_like, norm_name, path_shape, rank_actions, _CHROME_LANDMARKS,"),
 ("class BestFirstCrawler(HeadlessCrawler):", LEDGER + "class BestFirstCrawler(HeadlessCrawler):"),
 ("self._rng = random.Random(rng_seed)", INIT_EXTRA),
 ("def _score(self, n: Node, total_visits: int) -> float:", METHODS),
 ("def _pick(self, node: Node, transitions: list[Transition]) -> tuple[list[tuple[int, Action]], bool]:", PICK),
 ("start_url, _seed_path = self.seed()", "start_url, seed_path = self.seed()"),
 ('page = browser.new_context().new_page()\n            page.on("response", self._on_response)',
  'page = browser.new_context().new_page()\n            self._attach_capture(page)'),
 ('self._log(f"root {sid[:8]}: {len(items)} interactables")', SEED_BLOCK),
 ("st = self.tier_stats[tier]", LEDGER_HOOK),
 ('''f"to={tr.to_fp[:8] if tr.to_fp else '-'} {tr.detail or ''}".rstrip())''', REPAIR_HOOK),
 ('f"novel_apis={new_apis} new_screen={is_new} "', 'f"apis={len(tr.apis)} novel_apis={new_apis} new_screen={is_new} "'),
],
"ui_crawler.py": [
 ("NETWORK_IDLE_TIMEOUT_MS = 3000", "NETWORK_IDLE_TIMEOUT_MS = 5000"),
 ("def _on_response(self, response):", CAPTURE),
 ('req = response.request\n            if req.resource_type not in ("xhr", "fetch"):', GUARD),
 ('page = context.new_page()\n            page.on("response", self._on_response)',
  'page = context.new_page()\n            self._attach_capture(page)'),
 ("self._do(page, action)\n                self._settle(page)\n            except Exception:\n                break",
  "self._do(page, action)\n                self._settle(page)\n"
  "                self._wait_for_network_quiet(page)  # let this step's own replies land BEFORE the next action starts its clock\n"
  "            except Exception:\n                break"),
 ('samples.append("test")\n        return samples[:3]',
  'if item.value.strip().replace(".", "", 1).isdigit():\n'
  '            # a number box (e.g. Quantity, pre-filled "1"): typing "test" only gets the request rejected\n'
  '            return [item.value.strip()]\n'
  '        samples.append("test")\n        return samples[:3]'),
],
"vault.py": [
 ("for name, value in self._extract_response_params(response_str).items():",
  "# read the field names from the FULL reply -- response_str is cut at MAX_BODY_CHARS for display, and a cut-off\n"
  "        # JSON reply cannot be parsed, which used to lose every field of a long reply (e.g. a cart with a mini-cart HTML)\n"
  "        for name, value in self._extract_response_params((record.get(\"response_body\") or \"\")[:5_000_000]).items():"),
],
"ui_transitions.py": [
 ('crawl_p.add_argument("--crawl-id", default=None, help="defaults to a timestamp")',
  'crawl_p.add_argument("--crawl-id", default=None, help="defaults to a timestamp")\n'
  '    crawl_p.add_argument("--max-actions", type=int, default=400, help="how many presses the crawl may spend (default 400)")\n'
  '    crawl_p.add_argument("--max-states", type=int, default=100, help="how many distinct screens to keep (default 100)")'),
 ("max_states=60,", "max_states=args.max_states,"),
 ("max_actions=150,", "max_actions=args.max_actions,"),
 ('find_p.add_argument("--role", choices=["request", "response"], default="request")',
  'find_p.add_argument("--role", choices=["request", "response"], default=None,\n'
  '                        help="which side to look on (default: try request, then response)")'),
 ('''results = find_paths_for_param(vault, args.param_name, args.resource_key, args.role)
        if not results:
            print(f"No {args.role}-side path found for '{args.param_name}' yet -- "
                  f"run a crawl or a manual capture session first.")
            return''',
  '''roles = [args.role] if args.role else ["request", "response"]
        results, used_role = [], None
        for r in roles:
            results = find_paths_for_param(vault, args.param_name, args.resource_key, r)
            if results:
                used_role = r
                break
        if not results:
            print(f"No path found for '{args.param_name}' ({' or '.join(roles)} side) yet -- "
                  f"run a crawl or a manual capture session first.")
            return
        print(f"'{args.param_name}' is on the {used_role} side "
              f"({'sent by the site' if used_role == 'request' else 'sent back by the server'}).")'''),
],
}
DONE_MARK = {"best_first_crawler.py": "class PressLedger", "ui_crawler.py": "_attach_capture",
             "vault.py": "read the field names from the FULL reply", "ui_transitions.py": "--max-actions"}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    keep = "--keep" in sys.argv
    root = pathlib.Path(args[0] if args else ".")
    staged, problems = {}, []
    for fname, edits in EDITS.items():
        p = root / fname
        if not p.exists():
            problems.append(f"{fname}: not found in {root.resolve()}")
            continue
        with open(p, encoding="utf-8", newline="") as fh:
            raw = fh.read()
        crlf = "\r\n" in raw
        text = raw.replace("\r\n", "\n")
        if DONE_MARK[fname] in text:
            print(f"{fname}: already merged, skipping")
            continue
        for i, (old, new) in enumerate(edits, 1):
            n = text.count(old)
            if n != 1:
                problems.append(f"{fname}: edit #{i} anchor matched {n}x (need exactly 1): {old.strip().splitlines()[0][:90]}")
                continue
            text = text.replace(old, new)
        staged[p] = text.replace("\n", "\r\n") if crlf else text
    if problems:
        print("NOTHING WRITTEN. Problems:\n" + "\n".join("  - " + s for s in problems))
        sys.exit(1)
    for p, text in staged.items():
        p.with_suffix(p.suffix + ".bak").write_bytes(p.read_bytes())
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        print(f"merged into {p.name}  (backup: {p.name}.bak)")
    print("done.")
    if staged and not keep:
        try:
            os.remove(os.path.abspath(__file__))
            print("(this merge script deleted itself -- nothing new is left in your project)")
        except OSError:
            pass


if __name__ == "__main__":
    main()