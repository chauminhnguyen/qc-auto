"""Token counting and LLM cost tracking.

Chat LLM usage tracking and reporting. Standard library only.

Where the numbers come from, in order of trust:
  1. The usage block of the provider's response (this is what you are billed on).
  2. A rough estimate from text length, only when the response has no usage data.
Cost = tokens x price from pricing.json (USD per 1,000,000 tokens). If the
response itself carries a numeric cost (some providers, e.g. OpenRouter with usage
accounting enabled, return one) that value wins. Local Ollama models cost 0 unless
you set a price. Prompt caching is not modelled.

One question can trigger several LLM calls (query rewrite, answer, retry). Each call
is recorded separately; question_summary() adds them up.

Usage:
    tracker = UsageTracker()                  # once per process
    tracker.start_question()                  # at the start of each question
    tracker.record_response("ollama", model, response, label="answer",
                            prompt_text=prompt, completion_text=answer_text,
                            seconds=elapsed)
    summary = tracker.question_summary()      # summary["line"] is ready to print

Report over the saved log:
    python usage_cost.py report [--days 7] [--log usage_log.jsonl]
"""
from __future__ import annotations

import argparse
import json
import math
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRICING_PATH = _PROJECT_ROOT / "pricing.json"
DEFAULT_LOG_PATH = _PROJECT_ROOT / "usage_log.jsonl"

# Local models are free by default; everything else must be set in pricing.json.
DEFAULT_PRICES = {"ollama/*": {"input_per_mtok": 0, "output_per_mtok": 0}}


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #
def estimate_tokens(text: str) -> int:
    """Rough token estimate, for pre-call budgeting or when the API gave no usage.

    Vietnamese text produces more tokens per character than English, so this errs
    on the high side (about 3 characters per token). Never use it for billing.
    """
    return math.ceil(len(text) / 3) if text else 0


def _as_dict(obj: Any) -> dict:
    if isinstance(obj, dict):
        return obj
    for attr in ("model_dump", "to_dict", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                out = fn()
                if isinstance(out, dict):
                    return out
            except Exception:
                pass
    return {}


def extract_usage(response: Any) -> Optional[dict]:
    """Read token counts from a provider response (dict or SDK object).

    Handles the OpenAI-style usage block (OpenAI, OpenRouter, Ollama's OpenAI-compatible
    endpoint), the Anthropic block, and Ollama's native fields. Returns
    {"input_tokens", "output_tokens", "reported_cost"} or None if there is no usage data.
    """
    data = _as_dict(response)
    usage = _as_dict(data.get("usage"))

    if "prompt_tokens" in usage or "completion_tokens" in usage:
        inp, out = usage.get("prompt_tokens"), usage.get("completion_tokens")
    elif "input_tokens" in usage or "output_tokens" in usage:
        inp, out = usage.get("input_tokens"), usage.get("output_tokens")
    elif "prompt_eval_count" in data or "eval_count" in data:
        inp, out = data.get("prompt_eval_count"), data.get("eval_count")
    else:
        return None

    cost = usage.get("cost")
    return {
        "input_tokens": int(inp or 0),
        "output_tokens": int(out or 0),
        "reported_cost": float(cost) if isinstance(cost, (int, float)) else None,
    }


# --------------------------------------------------------------------------- #
# Prices and cost
# --------------------------------------------------------------------------- #
def load_pricing(path: Optional[Path] = None) -> dict:
    p = Path(path) if path else DEFAULT_PRICING_PATH
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def lookup_price(pricing: dict, provider: str, model: str) -> Optional[dict]:
    for key in (f"{provider}/{model}", f"{provider}/*"):
        if key in pricing:
            return pricing[key]
    return None


def compute_cost(price: Optional[dict], input_tokens: int, output_tokens: int) -> Optional[float]:
    """USD cost, or None if the price is unknown."""
    if not price:
        return None
    p_in, p_out = price.get("input_per_mtok"), price.get("output_per_mtok")
    if p_in is None or p_out is None:
        return None
    return (input_tokens * p_in + output_tokens * p_out) / 1_000_000


def fmt_cost(cost: Optional[float]) -> str:
    if cost is None:
        return "n/a"
    if cost == 0:
        return "$0.00"
    return f"${cost:.6f}" if cost < 0.0001 else f"${cost:.4f}"


def estimate_cost(
    pricing: dict, provider: str, model: str, prompt_text: str, expected_output_tokens: int = 500
) -> dict:
    """Pre-call estimate, e.g. to show 'about to send ~6k tokens (~$0.02)'."""
    inp = estimate_tokens(prompt_text)
    cost = compute_cost(lookup_price(pricing, provider, model), inp, expected_output_tokens)
    return {"input_tokens": inp, "output_tokens": expected_output_tokens, "cost_usd": cost}


# --------------------------------------------------------------------------- #
# Tracker
# --------------------------------------------------------------------------- #
@dataclass
class CallRecord:
    ts: float
    question_id: str
    label: str  # e.g. rewrite / answer / retry
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    estimated: bool  # True when tokens were estimated from text, not read from the API
    cost_usd: Optional[float]
    cost_source: str  # "reported" | "pricing" | "unpriced"
    seconds: Optional[float]


class UsageTracker:
    def __init__(
        self,
        pricing_path: Optional[Path] = None,
        log_path: Optional[Path] = DEFAULT_LOG_PATH,
    ) -> None:
        """log_path=None turns the JSONL log off."""
        self.pricing = {**DEFAULT_PRICES, **load_pricing(pricing_path)}
        self.log_path = Path(log_path) if log_path else None
        self.question_id = ""
        self.calls: list[CallRecord] = []

    def start_question(self) -> str:
        self.question_id = uuid.uuid4().hex[:8]
        self.calls = []
        return self.question_id

    def record(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        *,
        label: str = "answer",
        reported_cost: Optional[float] = None,
        estimated: bool = False,
        seconds: Optional[float] = None,
    ) -> CallRecord:
        if not self.question_id:
            self.start_question()
        if reported_cost is not None:
            cost, source = reported_cost, "reported"
        else:
            cost = compute_cost(lookup_price(self.pricing, provider, model), input_tokens, output_tokens)
            source = "pricing" if cost is not None else "unpriced"
        rec = CallRecord(
            time.time(),
            self.question_id,
            label,
            provider,
            model,
            int(input_tokens),
            int(output_tokens),
            estimated,
            cost,
            source,
            seconds,
        )
        self.calls.append(rec)
        self._append_log(rec)
        return rec

    def record_response(
        self,
        provider: str,
        model: str,
        response: Any,
        *,
        label: str = "answer",
        prompt_text: str = "",
        completion_text: str = "",
        seconds: Optional[float] = None,
    ) -> CallRecord:
        """Record a call from its raw response; falls back to estimates if it has no usage."""
        usage = extract_usage(response)
        if usage:
            return self.record(
                provider, model, usage["input_tokens"], usage["output_tokens"],
                label=label, reported_cost=usage["reported_cost"], seconds=seconds,
            )
        return self.record(
            provider, model, estimate_tokens(prompt_text), estimate_tokens(completion_text),
            label=label, estimated=True, seconds=seconds,
        )

    def question_summary(self) -> dict:
        calls = self.calls
        inp = sum(c.input_tokens for c in calls)
        out = sum(c.output_tokens for c in calls)
        priced = [c.cost_usd for c in calls if c.cost_usd is not None]
        summary = {
            "question_id": self.question_id,
            "calls_count": len(calls),
            "input_tokens": inp,
            "output_tokens": out,
            "total_tokens": inp + out,
            "cost_usd": sum(priced) if priced else None,
            "cost_complete": len(priced) == len(calls),
            "estimated": any(c.estimated for c in calls),
            "seconds": sum(c.seconds for c in calls if c.seconds),
            "calls": [
                {
                    "label": c.label, "model": c.model,
                    "input_tokens": c.input_tokens, "output_tokens": c.output_tokens,
                    "cost_usd": c.cost_usd, "estimated": c.estimated,
                }
                for c in calls
            ],
        }
        summary["line"] = self._line(summary, {c.provider for c in calls})
        return summary

    @staticmethod
    def _line(s: dict, providers: set) -> str:
        if not s["calls_count"]:
            return ""
        mark = "~" if s["estimated"] else ""
        parts = [
            f"Tokens: {mark}{s['input_tokens']:,} in / {mark}{s['output_tokens']:,} out "
            f"({mark}{s['total_tokens']:,} total)"
        ]
        if s["cost_usd"] is None:
            parts.append("Cost: n/a (price not set in pricing.json)")
        else:
            cost = fmt_cost(s["cost_usd"])
            if not s["cost_complete"]:
                cost += "+ (some calls unpriced)"
            elif providers == {"ollama"}:
                cost += " (local model)"
            parts.append(f"Cost: {cost}")
        parts.append(f"{s['calls_count']} call{'s' if s['calls_count'] != 1 else ''}")
        if s["seconds"]:
            parts.append(f"{s['seconds']:.1f}s")
        return " | ".join(parts)

    def _append_log(self, rec: CallRecord) -> None:
        if not self.log_path:
            return
        try:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
        except OSError:
            pass  # never break an answer because the log cannot be written


# --------------------------------------------------------------------------- #
# Report over the log
# --------------------------------------------------------------------------- #
def read_log(path: Path = DEFAULT_LOG_PATH) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # skip a damaged line instead of failing the whole report
    return rows


def _aggregate(rows: list[dict], key_fn) -> dict:
    groups: dict[str, dict] = {}
    for r in rows:
        g = groups.setdefault(
            key_fn(r), {"calls": 0, "in": 0, "out": 0, "cost": 0.0, "unpriced": 0, "questions": set()}
        )
        g["calls"] += 1
        g["in"] += r.get("input_tokens", 0)
        g["out"] += r.get("output_tokens", 0)
        g["questions"].add(r.get("question_id"))
        if r.get("cost_usd") is None:
            g["unpriced"] += 1
        else:
            g["cost"] += r["cost_usd"]
    return groups


def _table(title: str, groups: dict) -> list[str]:
    lines = [title, f"  {'':<40} {'questions':>9} {'calls':>6} {'input':>10} {'output':>10} {'cost':>12}"]
    for key in sorted(groups):
        g = groups[key]
        cost = fmt_cost(g["cost"]) + ("+" if g["unpriced"] and g["cost"] else "")
        if g["unpriced"] and not g["cost"]:
            cost = "n/a"
        lines.append(
            f"  {key:<40.40} {len(g['questions']):>9} {g['calls']:>6} {g['in']:>10,} {g['out']:>10,} {cost:>12}"
        )
    return lines


def report(path: Path = DEFAULT_LOG_PATH, days: Optional[int] = None) -> str:
    rows = read_log(path)
    if days:
        cutoff = time.time() - days * 86400
        rows = [r for r in rows if r.get("ts", 0) >= cutoff]
    if not rows:
        return "No usage recorded."

    by_model = _aggregate(rows, lambda r: f"{r['provider']}/{r['model']}")
    by_day = _aggregate(rows, lambda r: time.strftime("%Y-%m-%d", time.localtime(r["ts"])))
    total = _aggregate(rows, lambda r: "TOTAL")["TOTAL"]
    n_q = len(total["questions"])

    lines = _table("By model", by_model) + [""] + _table("By day", by_day) + [""]
    lines.append(
        f"Total: {n_q} questions, {total['calls']} calls, {total['in']:,} in / {total['out']:,} out tokens, "
        f"cost {fmt_cost(total['cost'])}" + ("+ (some calls unpriced)" if total["unpriced"] else "")
    )
    if n_q and total["cost"]:
        lines.append(f"Average per question: {fmt_cost(total['cost'] / n_q)}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="LLM usage and cost report.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("report", help="summarize the usage log")
    rp.add_argument("--days", type=int, help="only the last N days")
    rp.add_argument("--log", default=str(DEFAULT_LOG_PATH), help="path to usage_log.jsonl")
    args = ap.parse_args(argv)
    if args.cmd == "report":
        print(report(Path(args.log), args.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
