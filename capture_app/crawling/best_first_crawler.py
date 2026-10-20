"""
best_first_crawler.py -- reward-driven replacement for HeadlessCrawler.crawl().

A subclass, so seed / _snapshot / _replay / _do / _settle / _on_response and the
safety gates are reused untouched. Requires ax_tree_additions.py to be appended
to ax_tree.py first.

Differences from the FIFO-BFS crawl():
  * Two budgets, not one. max_states caps *nodes kept*; max_actions (and an
    optional wall-clock budget) caps the real cost. The BFS version stopped as
    soon as len(visited) hit max_states, which counted queued-but-unexpanded
    states.
  * Screen identity = which controls exist (ScreenIndex), not exact field values.
  * Best-first node selection: UCB-style score over novel-API credit.
  * Reward = endpoints never seen before (seeded with the manual capture),
    not raw call counts, so background polling can't make every tier look good.
  * A few actions per visit, then re-select. Similar controls ("Show details
    for X" x N) are clustered and capped unless the cluster pays off.
  * No replay-from-start_url between actions that leave the page on the same
    screen. Replay only after a navigation, a reveal, or a failure.
  * Every Transition.path is the *actually executed* sequence, including
    same-screen actions applied since the last replay, so persisted
    directions stay truthful.

NOT run against a live site. Treat the weights/thresholds as starting points and
read the log: 'folded' screens and per-visit picks show whether they're sane.
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from capture_app.core.ax_tree import (
    Action, Interactable, ScreenIndex, api_sig, cluster_key, is_delete_like,
    is_denied, is_submit_like, path_shape, rank_actions, _CHROME_LANDMARKS,
)
from capture_app.core.press_ledger import PressLedger, ctrl_key, effect_sig
from capture_app.crawling.ui_crawler import HeadlessCrawler, Transition, _item_for


@dataclass(eq=False)  # identity semantics: nodes are compared/removed by reference
class Node:
    sid: str
    url: str
    path: list[Action]                 # replay path from start_url to this screen
    depth: int
    items: list[Interactable]
    parent: "Node | None" = None
    visits: int = 0
    credit: float = 0.0                # novel-API reward earned here + discounted from descendants
    attempted: set[str] = field(default_factory=set)
    cluster_tries: dict[str, int] = field(default_factory=dict)
    hot: set[str] = field(default_factory=set)   # clusters that produced reward
    replay_failures: int = 0


def _akey(a: Action) -> str:
    # dup_index included: without it, trying product #1's "Add to cart"
    # would mark node.attempted in a way that also looked like product
    # #2's identical-looking button had already been tried.
    return f"{a.kind}::{a.role}::{a.name}::{a.value}::{a.dup_index}"


class BestFirstCrawler(HeadlessCrawler):
    def __init__(self, *args, max_actions: int = 150, k_per_visit: int = 4,
                 plateau: int = 60, explore_c: float = 0.7, gamma: float = 0.5,
                 cluster_cap: int = 2, fold_threshold: float | None = 0.9,
                 max_pending: int = 6, time_budget_s: float | None = None,
                 commit_reserve: int = 3, rng_seed: int = 0,
                 interactive: bool = False, auth_state: str | Path | None = None,
                 **kwargs):
        # NOTE: actions_per_state (inherited) is unused here; k_per_visit and
        # cluster_cap replace it. max_states now caps nodes, so raise it (~60).
        super().__init__(*args, auth_state=auth_state, interactive=interactive, **kwargs)
        self.max_actions = max_actions
        self.k_per_visit = k_per_visit
        self.plateau = plateau              # stop after this many actions with zero reward
        self.explore_c = explore_c
        self.gamma = gamma                  # credit discount to ancestors
        self.cluster_cap = cluster_cap
        self.fold_threshold = fold_threshold
        self.max_pending = max_pending      # force a clean replay after this many same-screen actions
        self.time_budget_s = time_budget_s
        self.interactive = interactive
        # FIX (sibling commit-button starvation, see _pick() below): was a
        # single held_commit slot; raised to a small reserved pool so a
        # product tile's several distinct commit buttons (Add to cart /
        # Add to wishlist / Add to compare list) don't all compete for one.
        self.commit_reserve = commit_reserve
        self._rng = random.Random(rng_seed)
        # Generic press-every-control policy (press_ledger.py). Set to False to get the old ordering back.
        self.use_ledger = True
        self.ledger = PressLedger()

    # ------------------------------------------------------------------
    def _known_api_sigs(self) -> set[str]:
        """Endpoints the manual capture (and any earlier crawl) already hit,
        as api_sig() strings, so 'novel' means 'not in the vault yet' --
        across every session ever captured, not just relative to this one
        crawl. Wired to vault.py's real `apis` table (method, url_path,
        domain, full_url, ...; UNIQUE(method, url_path, domain)) -- one row
        per distinct endpoint, already deduped by vault.ingest() itself.
        full_url is preferred when present since api_sig() needs a real,
        parseable URL; domain+url_path is the fallback for older rows."""
        try:
            rows = self.vault.conn.execute(
                "SELECT method, full_url, domain, url_path FROM apis"
            ).fetchall()
        except Exception as e:
            self._vlog(f"_known_api_sigs: apis table unavailable ({e}) -- "
                       f"falling back to per-crawl novelty only")
            return set()
        sigs = set()
        for method, full_url, domain, url_path in rows:
            url = full_url or f"https://{domain}{url_path or ''}"
            sigs.add(api_sig(method, url))
        return sigs

    def _score(self, n: Node, total_visits: int) -> float:
        base = self._score_legacy(n, total_visits)
        if not self.use_ledger:
            return base
        # prefer screens that still hold controls nobody has pressed yet
        return base + 0.5 * min(1.0, self._potential(n) / 8.0)

    def _candidates(self, node: Node) -> list:
        """Every control on this screen still worth pressing, best first.

        Order (no names involved): controls never pressed ANYWHERE first, then
        controls not yet pressed on THIS kind of page, then the ones whose effect
        has varied the most (a control that always does the same thing is
        furniture), then controls found on few kinds of page (content) before
        controls found on many (menus/footers); tier and page position only break ties. Controls the ledger
        calls bored are dropped."""
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

    def _score_legacy(self, n: Node, total_visits: int) -> float:
        exploit = n.credit / (1 + n.visits)
        explore = self.explore_c * math.sqrt(math.log(total_visits + 2) / (1 + n.visits))
        return exploit + explore - 0.05 * n.depth + self._rng.random() * 0.02

    def _pick(self, node: Node, transitions: list[Transition]) -> tuple[list[tuple[int, Action]], bool]:
        """Press-every-control selection (see press_ledger.py). Safety gates are
        the same as the legacy version: denied / delete-like / submit-like."""
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
                transitions.append(Transition(
                    from_fp=node.sid, action=action, ok=False, tier=tier, path=node.path,
                    from_url=node.url, detail="denied control"))
                continue
            if is_delete_like(action) and not self.allow_delete:
                transitions.append(Transition(
                    from_fp=node.sid, action=action, ok=False, tier=tier, path=node.path,
                    from_url=node.url,
                    detail="delete-like action held (allow_delete=False)"))
                continue
            if is_submit_like(action) and not self.allow_submit:
                transitions.append(Transition(
                    from_fp=node.sid, action=action, ok=False, tier=tier, path=node.path,
                    from_url=node.url,
                    detail="submit-like action held (allow_submit=False)"))
                continue
            picks.append((tier, action))
        return picks, exhausted

    def _pick_legacy(self, node: Node, transitions: list[Transition]) -> tuple[list[tuple[int, Action]], bool]:
        """Next <= k_per_visit eligible actions for this node, in ranked order.
        Returns (picks, exhausted). Gate handling matches the BFS version."""
        productive = {t for t, s in self.tier_stats.items() if s["new"] + s["apis"] > 0}
        ranked = rank_actions(node.items, self._text_samples,
                              banner_done=self._banner_done, productive=productive)
        picks: list[tuple[int, Action]] = []

        def _try_admit(tier: int, action: Action) -> None:
            """Marks `action` attempted and appends it to `picks` unless a
            safety gate holds it back -- a held-back action still gets a
            recorded Transition, matching the BFS crawler's own behavior."""
            node.attempted.add(_akey(action))
            if is_denied(_item_for(node.items, action)):
                transitions.append(Transition(
                    from_fp=node.sid, action=action, ok=False, tier=tier, path=node.path,
                    from_url=node.url, detail="denied control"))
                return
            if is_delete_like(action) and not self.allow_delete:
                transitions.append(Transition(
                    from_fp=node.sid, action=action, ok=False, tier=tier, path=node.path,
                    from_url=node.url,
                    detail="delete-like action held (allow_delete=False)"))
                return
            if is_submit_like(action) and not self.allow_submit:
                transitions.append(Transition(
                    from_fp=node.sid, action=action, ok=False, tier=tier, path=node.path,
                    from_url=node.url,
                    detail="submit-like action held (allow_submit=False)"))
                return
            ck = cluster_key(action)
            node.cluster_tries[ck] = node.cluster_tries.get(ck, 0) + 1
            picks.append((tier, action))

        held: dict[str, tuple[int, Action]] = {}
        exhausted = True
        for tier, action in ranked:
            key = _akey(action)
            if key in node.attempted:
                continue
            ck = cluster_key(action)
            if node.cluster_tries.get(ck, 0) >= self.cluster_cap and ck not in node.hot:
                continue
            if len(picks) >= self.k_per_visit:
                if (tier == 3 and ck not in held and len(held) < self.commit_reserve
                        and _item_for(node.items, action).landmark not in _CHROME_LANDMARKS):
                    held[ck] = (tier, action)
                exhausted = False
                continue
            _try_admit(tier, action)

        for tier, action in held.values():
            if not any(cluster_key(a) == cluster_key(action) for _, a in picks):
                _try_admit(tier, action)

        return picks, exhausted

    def _act(self, page, from_sid: str, base: list[Action], action: Action) -> Transition:
        """One action, NO replay. `base` is the executed sequence that put the
        browser in the state this action runs from."""
        from_url = page.url
        self._api_buffer = []
        t0 = time.time() * 1000
        try:
            self._do(page, action)
            self._settle(page)
            self._wait_for_network_quiet(page)
        except Exception as e:
            self._vlog(f"action failed: {action.label} ({e})".rstrip()[:200])
            return Transition(from_fp=from_sid, action=action, ok=False,
                              from_url=from_url, detail=str(e)[:200], path=base)
        apis = [a for a in self._api_buffer if a["ts"] >= t0]
        items = self._snapshot(page)
        if not items:
            # Same race crawl()'s own initial-load retry exists for: a
            # navigation this action just triggered can still be resolving
            # when we snapshot, so page.evaluate() throws mid-flight and
            # _snapshot() silently returns []. Confirmed directly in a real
            # run -- every node whose log line was immediately preceded by
            # "Execution context was destroyed, most likely because of a
            # navigation" came back picks=0 and stayed a permanently dead
            # branch, right at real "Add to cart" clicks that DID navigate.
            # One retry after a short wait covers the transient case.
            self._vlog(f"empty snapshot after {action.label} -- retrying once")
            page.wait_for_timeout(1000)
            items = self._snapshot(page)
        return Transition(from_fp=from_sid, action=action, from_url=from_url,
                          apis=apis, to_url=page.url,
                          to_items=items, ok=True, path=base)

    # ------------------------------------------------------------------
    def crawl(self) -> list[Transition]:
        start_url, _seed_path = self.seed()
        if not start_url:
            self._log("no start URL found in vault -- nothing to crawl")
            return []
        self._log(f"start: {start_url} (scope: {self._scope_host})")

        transitions: list[Transition] = []
        screens = ScreenIndex(self.fold_threshold)
        seen_apis = self._known_api_sigs()
        used = stale = 0
        deadline = time.time() + self.time_budget_s if self.time_budget_s else None

        self._prepare_safety_discovery(start_url)
        with sync_playwright() as p:
            try:
                headless = self._browser_headless()
                browser = p.chromium.launch(headless=headless)
            except PlaywrightError as bundled_error:
                # Prefer the installed Edge/Chrome channel when the matching
                # Playwright-managed browser binary has not been downloaded.
                browser = None
                for channel in ("msedge", "chrome"):
                    try:
                        browser = p.chromium.launch(
                            channel=channel, headless=headless)
                        self._log(
                            f"Playwright Chromium unavailable; using installed {channel} browser"
                        )
                        break
                    except PlaywrightError:
                        continue
                if browser is None:
                    raise bundled_error
            context = self._new_safe_context(browser, start_url)
            page = context.new_page()
            page.set_default_timeout(5000)
            page.set_default_navigation_timeout(10000)
            page.on("response", self._on_response)

            page.goto(start_url)
            self._settle(page)
            if not self._wait_for_manual_login(page):
                browser.close()
                self._finish_safety_run(start_url, completed=False)
                return []
            resume_path = self._resume_actions(page, start_url)
            items = self._snapshot(page)
            if not items:
                self._log("snapshot empty -- retrying once after 3s")
                page.wait_for_timeout(3000)
                items = self._snapshot(page)
            sid, _ = screens.resolve(page.url, items)
            root = Node(sid, page.url, resume_path, len(resume_path), items)
            nodes: dict[str, Node] = {sid: root}
            frontier: list[Node] = [root]
            cur: Node | None = root          # which node the browser is sitting on right now
            pending: list[Action] = []       # same-screen actions applied since the last replay
            self._log(f"root {sid[:8]}: {len(items)} interactables")

            while frontier and used < self.max_actions and stale < self.plateau:
                if deadline and time.time() > deadline:
                    self._log("time budget reached")
                    break
                total = sum(n.visits for n in nodes.values())
                node = max(frontier, key=lambda n: self._score(n, total))
                node.visits += 1
                transition_start = len(transitions)
                picks, exhausted = self._pick(node, transitions)
                if self.safety_gate:
                    for held in transitions[transition_start:]:
                        self.safety_gate.checkpoint(
                            held.action.label, held.from_url or node.url, "held",
                            start_url=start_url, resume_path=node.path,
                        )
                if exhausted:
                    frontier.remove(node)
                self._log(f"visit {node.sid[:8]} depth={node.depth} picks={len(picks)} "
                          f"credit={node.credit:.1f} nodes={len(nodes)} used={used}/{self.max_actions}")

                for tier, action in picks:
                    if used >= self.max_actions:
                        break
                    if self.safety_gate and self.safety_gate.should_skip(node.url, action.label):
                        self._vlog(f"skip repeated failure: {action.label}")
                        self.safety_gate.checkpoint(
                            action.label, node.url, "repeated failures",
                            start_url=start_url, resume_path=node.path,
                        )
                        transitions.append(Transition(
                            from_fp=node.sid, action=action, ok=False, tier=tier,
                            from_url=node.url, path=node.path,
                            detail="skipped after repeated failures",
                        ))
                        continue
                    if not self._in_scope(page.url) and cur is node:
                        cur = None
                    if cur is not node or len(pending) >= self.max_pending:
                        try:
                            if not self._replay(page, start_url, node.path):
                                raise RuntimeError("replay/navigation failed")
                            cur, pending = node, []
                        except Exception as e:
                            cur = None
                            used += 1
                            node.replay_failures += 1
                            transitions.append(Transition(
                                from_fp=node.sid, action=action, ok=False, tier=tier,
                                from_url=node.url,
                                path=node.path, detail=f"replay failed: {str(e)[:150]}"))
                            if self.safety_gate:
                                self.safety_gate.checkpoint(
                                    action.label, node.url, str(e), start_url=start_url,
                                    resume_path=node.path,
                                )
                            if node.replay_failures >= 2 and node in frontier:
                                frontier.remove(node)
                            break

                    if not self._approve_action(action):
                        self._log(f"  skipped by user: {action.label}")
                        used += 1
                        transitions.append(Transition(
                            from_fp=node.sid, action=action, ok=False, tier=tier,
                            from_url=node.url, path=node.path,
                            detail="action declined by user",
                        ))
                        if self.safety_gate:
                            self.safety_gate.checkpoint(
                                action.label, node.url, "declined", start_url=start_url,
                                resume_path=node.path,
                            )
                        continue

                    used += 1
                    base = node.path + pending
                    tr = self._act(page, node.sid, base, action)
                    tr.tier = tier
                    reward, new_apis, is_new = 0.0, 0, False

                    if tr.ok and not self._in_scope(tr.to_url):
                        tr.ok, tr.detail, cur = False, "left scope", None
                    elif tr.ok:
                        sid2, is_new = screens.resolve(
                            tr.to_url, tr.to_items, from_shape=path_shape(node.url))
                        tr.to_fp = sid2
                        fresh = {api_sig(a["method"], a["url"]) for a in tr.apis} - seen_apis
                        seen_apis |= fresh
                        new_apis = len(fresh)
                        reward = new_apis + (0.3 if is_new else 0.0)
                        if sid2 == node.sid:
                            pending.append(action)      # still on this screen: keep going, no replay
                        else:
                            cur = None                  # moved (or revealed something): replay before next pick
                            if is_new and len(nodes) < self.max_states:
                                child = Node(sid2, tr.to_url, base + [action],
                                             node.depth + 1, tr.to_items, parent=node)
                                nodes[sid2] = child
                                if child.depth < self.max_depth:
                                    frontier.append(child)
                            elif not is_new:
                                self._vlog(f"folded into existing screen {sid2[:8]}: {tr.to_url}")
                    else:
                        cur = None                      # a failed action may leave a modal / half-nav behind

                    if self.safety_gate:
                        self.safety_gate.checkpoint(
                            action.label, tr.from_url or node.url,
                            "ok" if tr.ok else tr.detail or "failed",
                            start_url=start_url,
                            resume_path=base + [action] if tr.ok else base,
                        )

                    if self.use_ledger:
                        reward += self._ledger_note(node, action, tr)
                    st = self.tier_stats[tier]
                    st["attempts"] += 1
                    st["apis"] += new_apis
                    st["new"] += int(is_new)
                    if reward:
                        node.hot.add(cluster_key(action))
                        node.credit += reward
                        g, anc = self.gamma, node.parent
                        while anc is not None:
                            anc.credit += reward * g
                            g *= self.gamma
                            anc = anc.parent
                        stale = 0
                    else:
                        stale += 1
                    if tr.ok and "cookie" in action.name.lower():
                        self._banner_done = True
                    transitions.append(tr)
                    self._log(f"  [{used}] T{tier} {action.label} -> ok={tr.ok} "
                              f"apis={len(tr.apis)} novel_apis={new_apis} new_screen={is_new} "
                              f"to={tr.to_fp[:8] if tr.to_fp else '-'} {tr.detail or ''}".rstrip())

            browser.close()

        why = ("frontier empty" if not frontier else
               "action budget" if used >= self.max_actions else
               "plateau (no reward)" if stale >= self.plateau else "time budget")
        stats = " ".join(f"T{t}=a{s['attempts']}/n{s['new']}/api{s['apis']}"
                         for t, s in sorted(self.tier_stats.items()))
        self._log(f"done ({why}): {len(nodes)} screens, {used} actions, "
                  f"{len(seen_apis)} known endpoints | {stats}")
        self._finish_safety_run(start_url, completed=not frontier)
        return transitions
