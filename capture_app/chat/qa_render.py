"""Human-friendly terminal rendering of a chat answer.

Chat answer rendering. Uses `rich` for the layout
(pip install rich); without it, a plain-text layout is used instead.

As a library:
    from capture_app.chat.qa_render import render_answer
    render_answer(result, question=q, usage=tracker.question_summary())

To try it on a saved answer:
    python -m capture_app.chat.qa_render answer.json [-v]
    (or: ... | python -m capture_app.chat.qa_render -)

Expected keys in the answer (all optional except `answer`):
    answer, source_ids, questions_for_BA, not_found, warnings, claims
`warnings` may be a list of strings or of dicts with title/text/type/severity/source.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

try:
    from rich import box
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    HAVE_RICH = True
except ImportError:  # the plain-text fallback keeps the CLI usable without rich
    HAVE_RICH = False

# Matches [Flows/x.md] and also [APIs/a/b.md, Params/c.md]
_CITE_RE = re.compile(r"\[([^\[\]\n]*?\.md(?:\s*,\s*[^\[\]\n]*?\.md)*)\]")
_KIND_STYLE = {
    "flows": "magenta",
    "apis": "cyan",
    "params": "green",
    "knowledge": "yellow",
    "rules": "yellow",
}


# --------------------------------------------------------------------------- #
# Preparation (shared by the rich and plain renderers)
# --------------------------------------------------------------------------- #
def parse_result(raw: Any) -> dict:
    """Accept a dict, or a JSON string (optionally wrapped in ``` fences)."""
    if isinstance(raw, dict):
        return raw
    text = str(raw).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text)


def number_citations(answer: str) -> tuple[str, list[str]]:
    """Replace inline [path.md] citations with `[1]`, `[2]` markers.

    Returns the rewritten text and the list of cited paths in order of first
    appearance (marker N refers to sources[N-1]).
    """
    order: dict[str, int] = {}

    def repl(m: re.Match) -> str:
        nums = []
        for path in (p.strip() for p in m.group(1).split(",")):
            nums.append(str(order.setdefault(path, len(order) + 1)))
        return "`[" + ", ".join(nums) + "]`"

    return _CITE_RE.sub(repl, answer), list(order)


_BOLD_LINE_RE = re.compile(r"^\*\*([^*\n]+?)\*\*:??\s*$", re.MULTILINE)


def promote_headings(text: str) -> str:
    """Turn lines that are only **Bold** into headings.

    In Markdown a single newline is a soft break, so a bold title line would
    otherwise run into the paragraph below it.
    """
    return _BOLD_LINE_RE.sub(r"### \1", text)


def _split_source(path: str) -> tuple[str, str]:
    """('Flows', 'name') from 'Flows/name.md'."""
    parts = path.split("/")
    kind = parts[0] if len(parts) > 1 else "note"
    name = parts[-1]
    if name.endswith(".md"):
        name = name[:-3]
    return kind, name


def _warning_parts(item: Any) -> tuple[str, str, str]:
    """(tag, text, source) from a string or dict warning item."""
    if not isinstance(item, dict):
        return "", str(item), ""
    tag = str(item.get("severity") or item.get("type") or "")
    title = str(item.get("title") or "")
    body = str(item.get("text") or item.get("summary") or "")
    text = f"{title}: {body}" if title and body else (title or body)
    return tag, text, str(item.get("source") or item.get("source_id") or "")


def _prepare(result: Any) -> dict:
    result = parse_result(result)
    text, sources = number_citations(str(result.get("answer") or "").strip())
    text = promote_headings(text)

    not_found = result.get("not_found")
    if not_found is True:
        not_found = "The notes do not contain an answer to this question."
    elif not isinstance(not_found, str):
        not_found = ""

    questions = result.get("questions_for_BA") or result.get("questions_for_ba") or []
    return {
        "text": text,
        "sources": sources,
        "not_found": not_found.strip(),
        "warnings": [_warning_parts(w) for w in (result.get("warnings") or [])],
        "questions": [str(q) for q in questions if q],
        "source_ids": [str(s) for s in (result.get("source_ids") or [])],
        "claims": result.get("claims") or [],
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_answer(
    result: Any,
    *,
    question: Optional[str] = None,
    usage: Optional[dict] = None,
    verbose: bool = False,
    console: Any = None,
    source_meta: Optional[dict] = None,
) -> None:
    """Print one answer.

    usage        dict from UsageTracker.question_summary() (uses its "line" and "calls").
    verbose      also show full note paths, retrieved IDs, claims and per-call usage.
    source_meta  optional {path: {"status": ..., "last_verified": ...}} to add a Status column.
    """
    data = _prepare(result)
    if HAVE_RICH:
        _render_rich(data, question, usage, verbose, console, source_meta or {})
    else:
        _render_plain(data, question, usage, verbose, source_meta or {})


def _render_rich(d: dict, question, usage, verbose, console, source_meta) -> None:
    console = console or Console()

    if question:
        console.print(Text("Question: ", style="bold") + Text(question))

    if d["text"]:
        console.print(
            Panel(
                Markdown(d["text"]),
                title="Answer",
                title_align="left",
                border_style="cyan",
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )

    if d["not_found"]:
        console.print(
            Panel(
                Text(d["not_found"]),
                title="Not in the knowledge base",
                title_align="left",
                border_style="yellow",
                box=box.ROUNDED,
            )
        )

    if d["warnings"]:
        body = Text()
        for n, (tag, text, src) in enumerate(d["warnings"]):
            if n:
                body.append("\n")
            body.append("• ")
            if tag:
                hot = tag.lower() in ("high", "critical")
                body.append(f"[{tag}] ", style="bold red" if hot else "bold yellow")
            body.append(text)
            if src:
                body.append(f"  ({src})", style="dim")
        console.print(
            Panel(
                body,
                title="Warnings, known issues & experience",
                title_align="left",
                border_style="red",
                box=box.ROUNDED,
            )
        )

    if d["questions"]:
        body = Text("\n").join(Text(f"{n}. {q}") for n, q in enumerate(d["questions"], 1))
        console.print(
            Panel(
                body,
                title="Questions to ask the BA",
                title_align="left",
                border_style="blue",
                box=box.ROUNDED,
            )
        )

    if d["sources"]:
        table = Table(
            title="Sources",
            title_justify="left",
            title_style="bold",
            box=box.SIMPLE_HEAD,
            show_edge=False,
            pad_edge=False,
        )
        table.add_column("#", justify="right", style="bold", no_wrap=True)
        table.add_column("Type", no_wrap=True)
        table.add_column("Note", overflow="fold")
        if source_meta:
            table.add_column("Status", no_wrap=True)
        for n, path in enumerate(d["sources"], 1):
            kind, name = _split_source(path)
            row = [
                str(n),
                Text(kind, style=_KIND_STYLE.get(kind.lower(), "white")),
                Text(path if verbose else name),
            ]
            if source_meta:
                meta = source_meta.get(path, {})
                status = str(meta.get("status") or "")
                verified = str(meta.get("last_verified") or "")
                row.append(Text(f"{status} {verified}".strip(), style="dim"))
            table.add_row(*row)
        console.print(table)

    if verbose:
        if d["source_ids"]:
            console.print(Text("Retrieved IDs: " + ", ".join(d["source_ids"]), style="dim"))
        for claim in d["claims"]:
            if isinstance(claim, dict):
                ids = ", ".join(str(i) for i in claim.get("source_ids", []))
                console.print(Text(f"claim: {claim.get('text', '')}  [{ids}]", style="dim"))
        for call in (usage or {}).get("calls", []):
            console.print(Text(_call_line(call), style="dim"))

    if usage and usage.get("line"):
        console.print(Text(usage["line"], style="dim"))


def _call_line(call: dict) -> str:
    cost = call.get("cost_usd")
    cost_txt = "n/a" if cost is None else f"${cost:.6f}"
    mark = "~" if call.get("estimated") else ""
    return (
        f"call {call.get('label', '?')}: {call.get('model', '?')}  "
        f"in {mark}{call.get('input_tokens', 0):,} / out {mark}{call.get('output_tokens', 0):,}  {cost_txt}"
    )


def _render_plain(d: dict, question, usage, verbose, source_meta) -> None:
    out: list[str] = []
    if question:
        out.append(f"Question: {question}\n")
    if d["text"]:
        out += ["ANSWER", "------", d["text"].replace("`", ""), ""]
    if d["not_found"]:
        out += ["NOT IN THE KNOWLEDGE BASE", d["not_found"], ""]
    if d["warnings"]:
        out.append("WARNINGS, KNOWN ISSUES & EXPERIENCE")
        for tag, text, src in d["warnings"]:
            out.append(f"  - {'[' + tag + '] ' if tag else ''}{text}{' (' + src + ')' if src else ''}")
        out.append("")
    if d["questions"]:
        out.append("QUESTIONS TO ASK THE BA")
        out += [f"  {n}. {q}" for n, q in enumerate(d["questions"], 1)]
        out.append("")
    if d["sources"]:
        out.append("SOURCES")
        for n, path in enumerate(d["sources"], 1):
            meta = source_meta.get(path, {})
            extra = f"  ({meta.get('status', '')} {meta.get('last_verified', '')})".rstrip() if meta else ""
            out.append(f"  [{n}] {path}{extra}")
        out.append("")
    if verbose:
        if d["source_ids"]:
            out.append("Retrieved IDs: " + ", ".join(d["source_ids"]))
        out += [_call_line(c) for c in (usage or {}).get("calls", [])]
    if usage and usage.get("line"):
        out.append(usage["line"])
    print("\n".join(out))


# --------------------------------------------------------------------------- #
# Command line (for trying the renderer on a saved answer)
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Render a saved chat answer (JSON) for the terminal.")
    ap.add_argument("file", help="JSON file with the answer, or - to read stdin")
    ap.add_argument("-q", "--question", help="question text to show above the answer")
    ap.add_argument("-v", "--verbose", action="store_true", help="full paths, IDs, claims, per-call usage")
    args = ap.parse_args(argv)

    raw = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
    render_answer(raw, question=args.question, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
