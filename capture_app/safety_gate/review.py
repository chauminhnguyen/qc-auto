"""Bounded end-of-run human review for queued endpoint decisions."""
from __future__ import annotations

import json
import os
import select
import sys
import time
from pathlib import Path

from .decisions import DecisionStore


def _read_key(timeout: int):
    deadline = time.monotonic() + timeout
    if sys.platform == "win32":
        import msvcrt
        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                return msvcrt.getwch().lower()
            time.sleep(0.05)
        return ""
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    return sys.stdin.read(1).lower() if ready else ""


def review(path="decisions.json", cap=10, timeout=30):
    store = DecisionStore(path)
    before_allowlist = set(store.data["allowlist"])
    before_never = set(store.data["never"])
    pending = [q for q in store.data["queue"].values() if q.get("status") == "pending"][:cap]
    if not pending:
        print("No pending endpoint questions.")
        return
    print("\nWrite-safe endpoint review")
    for index, item in enumerate(pending, 1):
        print(f"\n{index}. {item['method']} {item['url']}\n   Pattern: {item['pattern']}\n   Trigger: {'; '.join(item.get('triggers', []))}")
        method, endpoint = item["pattern"].split(" ", 1)
        host, _, route_path = endpoint.partition("/")
        family = f"{method} {host}/{'/'.join(route_path.split('/')[:2])}"
        approvals = sum(count for pattern, count in store.data["approvals"].items()
                        if pattern.startswith(family))
        if approvals >= 3:
            print("   Three prior approvals for this endpoint family; consider Always allow.")
    print("\n[m] Mock always (default)  [a] Allow once  [l] Always allow  [b] Block always  [d] Defer")
    deadline = time.monotonic() + timeout
    for index, item in enumerate(pending, 1):
        remaining = max(0, int(deadline - time.monotonic()))
        print(f"{index}/{len(pending)} choice (remaining batch time {remaining}s): ", end="", flush=True)
        key = _read_key(remaining) if remaining else ""
        print(key if key else "(timeout; remains mocked and queued)")
        if key in {"m", "a", "l", "b"}:
            store.apply_review(item["pattern"], {"m": "mock", "a": "allow-once", "l": "allow", "b": "block"}[key])
    report_dir = Path(os.getenv("WRITE_SAFE_REPORT_DIR", "safety_reports"))
    report_dir.mkdir(parents=True, exist_ok=True)
    import datetime
    review_path = report_dir / f"review-{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
    review_path.write_text(json.dumps({
        "allowlist_added": sorted(set(store.data["allowlist"]) - before_allowlist),
        "never_added": sorted(set(store.data["never"]) - before_never),
        "pending_questions": [q for q in store.data["queue"].values() if q.get("status") == "pending"],
    }, indent=2), encoding="utf-8")
    print(f"Review complete. Choices apply to future matching requests. Review report: {review_path}")


def review_if_interactive(path="decisions.json", timeout=30):
    """Show one bounded end-of-run batch only when a tester has a terminal."""
    if sys.stdin.isatty():
        review(os.getenv("WRITE_SAFE_DECISIONS", path), cap=10, timeout=timeout)
