from __future__ import annotations

import argparse
import os
import sys

from .llm import provider_from_env
from .pipeline import find_brd_paths, run


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m capture_app.check.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "digest"):
        p = sub.add_parser(name)
        p.add_argument("--vault", required=True)
        p.add_argument("--brd", action="append", default=[])
        p.add_argument("--provider", choices=["claude", "openrouter"])
        p.add_argument("--match-model")
        p.add_argument("--no-cache", action="store_true")
        if name == "run":
            p.add_argument("--judge-model")
            p.add_argument("--scope", action="append", default=[])
            p.add_argument("--min-confidence", choices=["low", "medium", "high"], default="medium")
            p.add_argument("--workers", type=int, default=2)
            p.add_argument("--max-calls", type=int, default=0)
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        provider = provider_from_env(args.provider)
        if provider == "claude":
            match_model = args.match_model or os.environ.get("CHECK_MATCH_MODEL", "claude-haiku-5-5")
            judge_model = getattr(args, "judge_model", None) or os.environ.get("CHECK_JUDGE_MODEL", "claude-sonnet-5-5")
        else:
            base = os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")
            match_model = args.match_model or os.environ.get("OPENROUTER_MATCH_MODEL", base)
            judge_model = getattr(args, "judge_model", None) or os.environ.get("OPENROUTER_JUDGE_MODEL", base)
        return run(args.vault, args.brd, provider=provider, match_model=match_model, judge_model=judge_model,
                   min_confidence=getattr(args, "min_confidence", "medium"), scopes=getattr(args, "scope", ()), workers=getattr(args, "workers", 2),
                   max_calls=getattr(args, "max_calls", 0), dry_run=getattr(args, "dry_run", False),
                   no_cache=getattr(args, "no_cache", False), out=getattr(args, "out", None),
                   digest_only=args.command == "digest") and 0
    except (ValueError, OSError) as error:
        print(f"check: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
