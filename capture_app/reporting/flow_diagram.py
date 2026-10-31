"""Render a captured happy path and its live-exploration branches as HTML."""

from __future__ import annotations

import argparse
import html
import re
from pathlib import Path
from urllib.parse import urlsplit

from capture_app.storage.vault import VaultWriter, _redact_structured_value, _redact_url


_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


class FlowDiagramGenerator:
    """Writes per-session, self-contained flow diagrams into the vault."""

    def __init__(self, vault_path: Path | str):
        self.vault_path = Path(vault_path)

    @property
    def output_root(self) -> Path:
        return self.vault_path / "Reports" / "flow_visualizations"

    def generate_happy_path(self, vault: VaultWriter, session_id: str) -> Path:
        session = self._load_session(vault, session_id)
        output = self._session_dir(session_id) / "happy_path.html"
        output.write_text(self._render(session, title="Captured happy path", branches={}), encoding="utf-8")
        return output

    def generate_full_flow(
        self,
        vault: VaultWriter,
        session_id: str,
        transitions: list,
        source_url_by_fp: dict[str, str] | None = None,
    ) -> Path:
        session = self._load_session(vault, session_id)
        branches, unmatched = self._group_branches(
            session["steps"], transitions, source_url_by_fp or {}, session["start_url"]
        )
        output = self._session_dir(session_id) / "full_flow.html"
        output.write_text(
            self._render(session, title="Happy path and explored branches", branches=branches, unmatched=unmatched),
            encoding="utf-8",
        )
        return output

    def _session_dir(self, session_id: str) -> Path:
        # Session ids are generated internally today, but constrain the path
        # component so a malformed id cannot escape the reports directory.
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(session_id)).strip("._") or "session"
        path = self.output_root / safe_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _load_session(vault: VaultWriter, session_id: str) -> dict:
        conn = vault.conn
        session_row = conn.execute(
            "SELECT start_url FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if session_row is None:
            raise ValueError(f"Capture session not found: {session_id}")
        step_rows = conn.execute(
            """SELECT seq, step_type, label, input_type, page_url, tag, role,
                      elem_type, action_id
               FROM steps WHERE session_id=? ORDER BY seq ASC""",
            (session_id,),
        ).fetchall()
        event_rows = conn.execute(
            """SELECT seq, method, status, request_url, action_id,
                      correlation_status, entry_point
               FROM session_api_events WHERE session_id=? ORDER BY seq ASC""",
            (session_id,),
        ).fetchall()

        events_by_action: dict[str, list[dict]] = {}
        unlinked_events: list[dict] = []
        for seq, method, status, request_url, action_id, correlation_status, entry_point in event_rows:
            event = {
                "seq": seq,
                "method": method or "API",
                "status": status,
                "url": _url_path(request_url),
                "correlation_status": correlation_status or "unmatched",
                "entry_point": _safe_text(entry_point or ""),
            }
            if action_id:
                events_by_action.setdefault(str(action_id), []).append(event)
            else:
                unlinked_events.append(event)

        steps = []
        for seq, step_type, label, input_type, page_url, tag, role, elem_type, action_id in step_rows:
            label = _safe_text(str(_redact_structured_value(label or "")))
            linked_events = events_by_action.pop(str(action_id), []) if action_id else []
            steps.append({
                "seq": seq,
                "kind": step_type or "step",
                "label": label or _step_fallback(step_type),
                "input_type": input_type or "",
                "page": _url_path(page_url),
                "tag": tag or "",
                "role": role or "",
                "elem_type": elem_type or "",
                "has_value": step_type == "input",
                "events": linked_events,
            })

        # API records with an action id but no corresponding UI step are
        # intentionally kept separate. They may be page-load or background
        # calls, so attaching them by timestamp alone would imply a false cause.
        for events in events_by_action.values():
            unlinked_events.extend(events)

        return {
            "session_id": session_id,
            "start_url": _url_path(session_row[0]),
            "steps": steps,
            "unlinked_events": sorted(unlinked_events, key=lambda event: event["seq"]),
        }

    @classmethod
    def _group_branches(
        cls,
        steps: list[dict],
        transitions: list,
        source_url_by_fp: dict[str, str],
        start_url: str,
    ) -> tuple[dict[int, list[dict]], list[dict]]:
        step_pages: dict[str, list[int]] = {}
        for index, step in enumerate(steps):
            key = _page_key(step.get("page"))
            if key:
                step_pages.setdefault(key, []).append(index)

        branches: dict[int, list[dict]] = {}
        unmatched: list[dict] = []
        for transition_index, transition in enumerate(transitions, start=1):
            source_url = (
                getattr(transition, "from_url", "")
                or source_url_by_fp.get(getattr(transition, "from_fp", ""), "")
            )
            if not source_url and not getattr(transition, "path", None):
                source_url = start_url
            source_key = _page_key(source_url)
            candidates = step_pages.get(source_key, []) if source_key else []

            # The explored control itself is the strongest link when the user
            # exercised that same control in the captured happy path.
            action_name = _normalize_label(getattr(getattr(transition, "action", None), "name", ""))
            action_matches = [
                index for index in candidates
                if action_name and _normalize_label(steps[index].get("label")) == action_name
            ]
            anchor = action_matches[0] if len(action_matches) == 1 else None
            match_quality = "Same control on same page" if anchor is not None else ""
            if anchor is None:
                branch_apis = {
                    _api_key(api.get("method"), api.get("url"))
                    for api in getattr(transition, "apis", []) or []
                }
                branch_apis.discard("")
                api_matches = [
                    index for index in candidates
                    if branch_apis.intersection(
                        _api_key(event.get("method"), event.get("url"))
                        for event in steps[index].get("events", [])
                    )
                ]
                if len(api_matches) == 1:
                    anchor, match_quality = api_matches[0], "Matching API call"
            # Otherwise match the crawler's route to the captured action path
            # on this source page.
            if anchor is None:
                anchor = cls._path_anchor(steps, candidates, getattr(transition, "path", None) or [])
                if anchor is not None:
                    match_quality = "Matching action path"
            if anchor is None and len(candidates) == 1:
                anchor, match_quality = candidates[0], "Only step on this page"
            branch = cls._branch_data(transition, source_url)
            branch["transition_seq"] = transition_index
            if anchor is None:
                if steps:
                    anchor = cls._best_effort_anchor(steps, candidates, transition)
                    match_quality = "Inferred best-fit step"
                else:
                    unmatched.append(branch)
            if anchor is not None:
                branch["match_quality"] = match_quality
                branch["anchor_step"] = anchor + 1
                branches.setdefault(anchor, []).append(branch)
        return branches, unmatched

    @classmethod
    def _best_effort_anchor(cls, steps: list[dict], candidates: list[int], transition) -> int:
        """Always attach a branch to a happy-path step when one exists.

        Prefer evidence on the same source page, then action/API/path similarity.
        The final sequence index tie-breaker keeps the choice deterministic.
        """
        pool = candidates or list(range(len(steps)))
        action_name = _normalize_label(getattr(getattr(transition, "action", None), "name", ""))
        branch_apis = {
            _api_key(api.get("method"), api.get("url"))
            for api in getattr(transition, "apis", []) or []
        }
        branch_apis.discard("")
        route = [
            _normalize_label(getattr(action, "name", ""))
            for action in (getattr(transition, "path", None) or [])
        ]
        route = [label for label in route if label]
        source_key = _page_key(getattr(transition, "from_url", ""))

        def score(index: int) -> tuple[int, int, int, int, int]:
            step = steps[index]
            page_match = int(bool(source_key) and _page_key(step.get("page")) == source_key)
            action_match = int(bool(action_name) and _normalize_label(step.get("label")) == action_name)
            step_apis = {
                _api_key(event.get("method"), event.get("url"))
                for event in step.get("events", [])
            }
            api_match = len(branch_apis.intersection(step_apis))
            captured = [
                _normalize_label(candidate.get("label"))
                for candidate in steps[: index + 1]
                if candidate.get("kind") in {"tap", "input"} and candidate.get("label")
            ]
            path_match = cls._lcs_length(route, captured) if route else 0
            return page_match, action_match, api_match, path_match, index

        return max(pool, key=score)

    @staticmethod
    def _path_anchor(steps: list[dict], candidates: list[int], path: list) -> int | None:
        route = [_normalize_label(getattr(action, "name", "")) for action in path]
        route = [label for label in route if label]
        if not route or len(candidates) < 2:
            return None
        scored = []
        for candidate in candidates:
            captured = [
                _normalize_label(step["label"])
                for step in steps[: candidate + 1]
                if step.get("kind") in {"tap", "input"} and step.get("label")
            ]
            scored.append((FlowDiagramGenerator._lcs_length(route, captured), candidate))
        if not scored:
            return None
        best_score = max(score for score, _ in scored)
        winners = [candidate for score, candidate in scored if score == best_score]
        return winners[0] if best_score > 0 and len(winners) == 1 else None

    @staticmethod
    def _lcs_length(left: list[str], right: list[str]) -> int:
        row = [0] * (len(right) + 1)
        for item in left:
            next_row = [0]
            for index, other in enumerate(right, start=1):
                next_row.append(row[index - 1] + 1 if item == other else max(row[index], next_row[-1]))
            row = next_row
        return row[-1]

    @staticmethod
    def _branch_data(transition, source_url: str) -> dict:
        action = getattr(transition, "action", None)
        kind = getattr(action, "kind", "action") or "action"
        name = _safe_text(str(_redact_structured_value(getattr(action, "name", "") or "")))
        role = _safe_text(str(getattr(action, "role", "") or ""))
        if kind in {"fill", "set", "select"}:
            action_text = f'{kind.title()} {role} “{name}” (value not shown)'.strip()
        else:
            action_text = f'{kind.title()} {role} “{name}”'.strip()

        ok = bool(getattr(transition, "ok", False))
        detail = _safe_text(str(_redact_structured_value(getattr(transition, "detail", "") or "")))
        if not ok and "allow_submit" in detail:
            outcome = "Held · submit action requires --allow-submit"
        elif not ok and "allow_delete" in detail:
            outcome = "Held · delete action requires --allow-delete"
        elif not ok:
            outcome = "Did not complete"
        elif getattr(transition, "apis", None):
            statuses = [api.get("status") for api in transition.apis]
            if any(_is_non_2xx(status) for status in statuses):
                outcome = "API returned a non-2xx status"
            elif statuses and all(status is not None for status in statuses):
                outcome = "API call succeeded (2xx)"
            else:
                outcome = "API call observed · status unavailable"
        else:
            outcome = "Action completed · no API call observed"

        apis = []
        for api in getattr(transition, "apis", []) or []:
            status = api.get("status")
            safe_full = _safe_evidence_url(api.get("url") or "")
            apis.append({
                "method": _safe_text(str(api.get("method") or "API")),
                "url": _url_path(api.get("url") or ""),
                "full_url": safe_full,
                "status": status,
            })
        destination = _url_path(getattr(transition, "to_url", "") or "") or "Not recorded"
        destination_recorded = bool(_url_path(getattr(transition, "to_url", "") or ""))
        if detail and not apis and not ok:
            outcome += " · See the live-run report for details"
        return {
            "action": action_text or "Explored action",
            "action_kind": kind,
            "action_name": name,
            "action_role": role,
            "action_value": _safe_text(str(_redact_structured_value(getattr(action, "value", "") or ""))),
            "action_dup_index": int(getattr(action, "dup_index", 0) or 0),
            "outcome": outcome,
            "source": _url_path(source_url),
            "destination": destination,
            "destination_recorded": destination_recorded,
            "apis": apis,
        }

    @classmethod
    def _render(
        cls,
        session: dict,
        title: str,
        branches: dict[int, list[dict]],
        unmatched: list[dict] | None = None,
    ) -> str:
        unmatched = unmatched or []
        rows = []
        if session["steps"]:
            rows.append(
                '<section class="flow-row start-row"><article class="step-card start-card">'
                '<span class="eyebrow">START</span><h2>Open the application</h2>'
                f'<p>{_e(session["start_url"] or "Starting URL not recorded")}</p></article>'
                '<div class="branch-slot"></div></section>'
            )
        else:
            rows.append(
                '<section class="empty-state">No UI steps were recorded for this capture session.</section>'
            )

        for index, step in enumerate(session["steps"]):
            number = index + 1
            meta = " · ".join(x for x in (step["kind"].title(), step["role"], step["tag"]) if x)
            if step["kind"] == "input":
                meta += " · value not shown"
            details = []
            if step["page"]:
                details.append(f'<div><b>Page</b><span>{_e(step["page"])}</span></div>')
            if step["input_type"]:
                details.append(f'<div><b>Input type</b><span>{_e(step["input_type"])}</span></div>')
            api_html = "".join(cls._api_card(event) for event in step["events"])
            row_branches = branches.get(index, [])
            branch_html = "".join(cls._branch_card(branch) for branch in row_branches)
            if not branch_html:
                branch_html = '<p class="branch-empty">No explored branch attached to this step.</p>'
            branch_class = "branch-slot has-branches" if row_branches else "branch-slot"
            row_class = "flow-row has-branches" if row_branches else "flow-row"
            rows.append(
                f'<section class="{row_class}" data-search="{_attr(step["label"] + " " + step["page"])}">'
                '<article class="step-card">'
                f'<span class="eyebrow">HAPPY PATH · STEP {number}</span>'
                f'<h2>{_e(step["label"])}</h2><p class="meta">{_e(meta or "UI action")}</p>'
                f'{"".join(details)}{api_html}</article>'
                f'<div class="{branch_class}">{branch_html}</div></section>'
            )

        if session["steps"]:
            rows.append(
                '<section class="flow-row end-row"><article class="step-card end-card">'
                '<span class="eyebrow">END</span><h2>Captured flow complete</h2>'
                '<p>The manually recorded happy path ends here.</p></article>'
                '<div class="branch-slot"></div></section>'
            )

        if session["unlinked_events"]:
            unlinked_cards = "".join(cls._api_card(event, unlinked=True) for event in session["unlinked_events"])
            rows.append(
                '<section class="unmatched-section"><h2>API calls without a linked UI step</h2>'
                '<p>These calls were not attached to a step because the capture did not establish a direct action link.</p>'
                f'<div class="unmatched-grid">{unlinked_cards}</div></section>'
            )
        if unmatched:
            cards = "".join(cls._branch_card(branch, unmatched=True) for branch in unmatched)
            rows.append(
                '<section class="unmatched-section"><h2>Exploration branches without a reliable match</h2>'
                '<p>The source page did not map unambiguously to one captured happy-path step.</p>'
                f'<div class="unmatched-grid">{cards}</div></section>'
            )

        body = "\n".join(rows)
        title_text = _e(title)
        session_text = _e(session["session_id"])
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title_text} · {session_text}</title>
<style>
:root{{--bg:#151515;--panel:#202020;--happy:#0a5143;--happy-border:#28a989;--branch:#713018;--branch-border:#e8784b;--text:#f3eee9;--muted:#c3bcb5;--scale:1}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:16px/1.45 "Segoe UI",Arial,sans-serif}}
header{{position:sticky;top:0;z-index:3;background:#191919;border-bottom:1px solid #353535;padding:16px 24px;display:flex;gap:16px;align-items:center;justify-content:space-between;flex-wrap:wrap}}
h1{{font-size:20px;margin:0}} .session{{color:var(--muted);font-size:12px;margin-top:3px}} .controls{{display:flex;gap:8px;align-items:center}}
button,input{{font:inherit;color:var(--text);background:#242424;border:1px solid #555;border-radius:6px;padding:7px 10px}} button{{cursor:pointer}} button:hover{{border-color:#aaa}}
main{{padding:20px 24px 52px;max-width:1500px;margin:auto}} .legend{{display:flex;gap:22px;flex-wrap:wrap;color:var(--muted);font-size:13px;margin:0 0 20px}}
.legend span{{display:flex;align-items:center;gap:8px}} .swatch{{width:16px;height:16px;border-radius:4px;display:inline-block}} .swatch.h{{background:var(--happy);border:1px solid var(--happy-border)}} .swatch.b{{background:var(--branch);border:1px solid var(--branch-border)}}
.diagram{{transform:scale(var(--scale));transform-origin:top center;transition:transform .15s}} .flow-row{{display:grid;grid-template-columns:minmax(300px, 1fr) minmax(300px, 1fr);gap:56px;align-items:center;position:relative;padding:12px 0}}
.step-card,.branch-card{{border-radius:14px;padding:18px 22px;box-shadow:0 8px 24px #0003;position:relative;min-width:0}}
.step-card{{background:var(--happy);border:1px solid var(--happy-border);max-width:none;justify-self:stretch}} .start-card,.end-card{{background:#263532;border-color:#6da696}}
.step-card h2,.branch-card h3{{margin:4px 0 5px;font-size:20px;overflow-wrap:anywhere}} .step-card p,.branch-card p{{margin:3px 0;color:#c6e8de;overflow-wrap:anywhere}}
.eyebrow{{font-size:11px;letter-spacing:.09em;font-weight:700;color:#82d7bd}} .meta{{font-size:13px;color:#a8d8c9!important}}
.flow-row:not(.end-row) .step-card:after{{content:"";position:absolute;left:50%;top:100%;height:25px;border-left:3px solid var(--happy-border)}}
.flow-row:not(.end-row) .step-card:before{{content:"";position:absolute;left:calc(50% - 5px);top:calc(100% + 18px);width:8px;height:8px;border-right:2px solid var(--happy-border);border-bottom:2px solid var(--happy-border);transform:rotate(45deg);z-index:1}}
.branch-slot{{display:flex;flex-direction:column;gap:10px;position:relative}} .branch-slot:empty{{min-height:20px}}
.branch-card{{background:var(--branch);border:1px solid var(--branch-border)}} .branch-card h3{{font-size:17px}} .branch-card .outcome{{color:#ffb596;font-weight:600;font-size:14px}}
.branch-card .anchor-note{{color:#f4c2a9;font-size:11px;margin-top:8px}}
.flow-row.has-branches .branch-slot{{padding-left:42px}}
.flow-row.has-branches .branch-slot:before{{content:"";position:absolute;left:0;top:0;bottom:0;border-left:2px dashed var(--branch-border)}}
.flow-row.has-branches .branch-slot:after{{content:"";position:absolute;left:-56px;top:50%;width:56px;border-top:2px dashed var(--branch-border)}}
.flow-row.has-branches .branch-card:before{{content:"";position:absolute;left:-42px;top:50%;width:42px;border-top:2px dashed var(--branch-border)}}
.flow-row.has-branches .branch-card:after{{content:"";position:absolute;left:-7px;top:calc(50% - 4px);width:7px;height:7px;border-right:2px solid var(--branch-border);border-top:2px solid var(--branch-border);transform:rotate(45deg)}}
.api-card{{margin-top:12px;padding:10px 12px;background:#083d35;border-left:3px solid #64cdb2;border-radius:5px;font-size:13px;overflow-wrap:anywhere}}
.api-card strong{{color:#a1ead6}} .api-status{{color:#d5fff2}} .api-status.fail{{color:#ffb596}}
.details{{margin-top:10px}} .details summary{{font-size:12px;color:#b9ddd2;cursor:pointer}} .details div{{display:flex;gap:12px;font-size:12px;margin-top:6px;color:var(--muted);overflow-wrap:anywhere}} .details b{{min-width:78px;color:#e1ddd8}}
.branch-empty{{font-size:12px;color:#777!important;padding:0 5px}} .empty-state{{padding:24px;border:1px dashed #666;border-radius:10px;color:var(--muted);grid-column:1/-1}}
.unmatched-section{{margin:32px 0 0;border-top:1px solid #444;padding-top:22px}} .unmatched-section h2{{font-size:18px;margin:0}} .unmatched-section>p{{color:var(--muted);font-size:13px}}
.unmatched-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}} .unmatched-grid .api-card{{margin:0}}
@media(max-width:760px){{main{{padding:14px}}.flow-row{{grid-template-columns:1fr;gap:14px;padding:10px 0 22px}}.branch-slot{{margin-left:26px}}.flow-row.has-branches .branch-slot:after{{left:-26px;width:26px;top:-14px}}.flow-row.has-branches .branch-slot:before{{left:0}}.flow-row.has-branches .branch-card:before{{left:-26px;width:26px}}.flow-row.has-branches .branch-card:after{{left:-7px}}.step-card h2{{font-size:18px}}}}
</style></head><body>
<header><div><h1>{title_text}</h1><div class="session">Capture session {session_text}</div></div>
<div class="controls"><input id="search" type="search" placeholder="Filter steps and branches" aria-label="Filter diagram">
<button type="button" onclick="zoom(-0.1)" aria-label="Zoom out">−</button><button type="button" onclick="zoom(0.1)" aria-label="Zoom in">+</button>
<button type="button" onclick="resetZoom()">Reset view</button></div></header>
<main><div class="legend"><span><i class="swatch h"></i>Captured happy path</span><span><i class="swatch b"></i>Observed exploration branch</span></div>
<div class="diagram" id="diagram">{body}</div></main>
<script>
const diagram=document.getElementById('diagram');
function zoom(delta){{const root=document.documentElement;const current=parseFloat(getComputedStyle(root).getPropertyValue('--scale'))||1;root.style.setProperty('--scale',Math.max(.65,Math.min(1.4,current+delta)));}}
function resetZoom(){{document.documentElement.style.setProperty('--scale','1');document.getElementById('search').value='';filter('');}}
function filter(query){{const q=query.toLowerCase().trim();document.querySelectorAll('.flow-row').forEach(row=>{{row.hidden=!!q&&!row.innerText.toLowerCase().includes(q);}});}}
document.getElementById('search').addEventListener('input',event=>filter(event.target.value));
</script></body></html>"""

    @staticmethod
    def _api_card(event: dict, unlinked: bool = False) -> str:
        status = event.get("status")
        status_text = f"HTTP {status}" if status is not None else "status unavailable"
        failed = _is_non_2xx(status)
        status_class = "fail" if failed else ""
        label = "Unlinked API event" if unlinked else "Triggered API"
        return (
            '<div class="api-card"><strong>' + _e(label) + " · " + _e(event.get("method", "API"))
            + "</strong><br><span>" + _e(event.get("url") or "Endpoint unavailable") + "</span> · "
            + f'<span class="api-status {status_class}">{_e(status_text)}</span></div>'
        )

    @staticmethod
    def _branch_card(branch: dict, unmatched: bool = False) -> str:
        apis = "".join(
            '<div class="api-card"><strong>' + _e(api["method"]) + "</strong><br>"
            + _e(api["url"] or "Endpoint unavailable") + " · "
            + _e(f'HTTP {api["status"]}' if api["status"] is not None else "status unavailable")
            + "</div>"
            for api in branch["apis"]
        )
        location = f'<p class="meta">Source: {_e(branch["source"] or "Page unavailable")}</p>' if unmatched else ""
        destination = (
            f'<p class="meta">Destination: {_e(branch["destination"])}</p>'
            if branch["destination"] else ""
        )
        anchor_note = (
            f'<p class="anchor-note">Connected by: {_e(branch["match_quality"])}</p>'
            if branch.get("match_quality") else ""
        )
        return (
            '<article class="branch-card"><span class="eyebrow">EXPLORATION BRANCH</span>'
            f'<h3>{_e(branch["action"])}</h3><p class="outcome">{_e(branch["outcome"])}</p>'
            f'{anchor_note}{location}{destination}{apis}</article>'
        )


def init_visualization_cli(argv: list[str] | None = None) -> int:
    """Regenerate a happy-path diagram from an already captured session."""
    parser = argparse.ArgumentParser(
        prog="capture.py init",
        description="Render the happy-path visualization for a saved capture session.",
    )
    parser.add_argument("--vault", default="./ObsidianVault", help="Capture vault directory")
    parser.add_argument("--session", default=None, help="Session id (defaults to the latest capture)")
    args = parser.parse_args(argv)

    vault_path = Path(args.vault)
    db_path = vault_path / ".capture_index" / "index.sqlite"
    if not db_path.is_file():
        parser.error(f"No capture index found at {db_path}; capture a session first or pass --vault.")

    vault = VaultWriter(vault_path)
    session_id = args.session
    if session_id is None:
        row = vault.conn.execute(
            "SELECT session_id FROM sessions ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        session_id = row[0] if row else None
    if not session_id:
        parser.error(f"No saved capture sessions found in {vault_path}.")

    try:
        output = FlowDiagramGenerator(vault_path).generate_happy_path(vault, session_id)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"Happy-path diagram: {output.resolve()}")
    return 0


def _url_path(value: str | None) -> str:
    if not value:
        return ""
    safe = _redact_url(str(value))
    parts = urlsplit(safe)
    # Exclude query strings and fragments from the diagram to avoid leaking
    # identifiers or tokens that do not have sensitive-looking parameter names.
    return f"{parts.netloc}{parts.path}" if parts.netloc else parts.path or safe


def _safe_evidence_url(value: str | None) -> str:
    if not value:
        return ""
    parts = urlsplit(_redact_url(str(value)))
    if parts.netloc:
        return f"{parts.scheme or 'https'}://{parts.netloc}{parts.path}"
    return parts.path


def _page_key(value: str | None) -> str:
    if not value:
        return ""
    text = str(value)
    parts = urlsplit(text if "://" in text else "//" + text)
    return f"{parts.netloc.lower()}{parts.path.rstrip('/') or '/'}".lower()


def _safe_text(value: str) -> str:
    safe = str(_redact_structured_value(value))

    def strip_query(match: re.Match) -> str:
        return _url_path(match.group(0))

    return _URL_RE.sub(strip_query, safe)


def _step_fallback(step_type: str | None) -> str:
    return {
        "tap": "Tap an unlabeled control",
        "input": "Enter a value",
        "navigate": "Navigate to a page",
    }.get(step_type or "", "Recorded UI step")


def _normalize_label(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _api_key(method: object, url: object) -> str:
    if not method or not url:
        return ""
    text = str(url)
    parts = urlsplit(text if "://" in text else "//" + text)
    return f"{str(method).upper()} {parts.path.rstrip('/') or '/'}".casefold()


def _is_non_2xx(status: object) -> bool:
    if status is None:
        return False
    try:
        return not 200 <= int(status) < 300
    except (TypeError, ValueError):
        return False


def _e(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _attr(value: object) -> str:
    return html.escape(str(value or ""), quote=True)
