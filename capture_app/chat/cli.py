from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .qa_render import render_answer
from .usage_cost import UsageTracker

from .config import ChatConfig
from .db import ChatDB
from .ingest import refresh_index
from .llm import provider_from_env
from .retriever import SearchEngine


def _format_result(row: dict) -> str:
    return (
        f"source: {row.get('source_path', 'n/a')}\n"
        f"heading: {row.get('heading_path') or row.get('title') or 'n/a'}\n"
        f"body: {row.get('text', row.get('body', ''))}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and query the separate vault chat index")
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_cmd = subparsers.add_parser("build-index", help="Refresh the chat SQLite index from the vault")
    index_cmd.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    index_cmd.add_argument("--db", type=Path, default=None)
    index_cmd.add_argument("--full", action="store_true", help="rebuild the index into a temporary database")
    index_cmd.add_argument("--exclude", action="append", default=[], help="exclude a vault-relative path glob; repeatable")

    query_cmd = subparsers.add_parser("search", help="Search the chat index")
    query_cmd.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    query_cmd.add_argument("--db", type=Path, default=None)
    query_cmd.add_argument("query", help="Text to search for")
    query_cmd.add_argument("--limit", type=int, default=5)

    ask_cmd = subparsers.add_parser("ask", help="Ask a question using the indexed notes as context")
    ask_cmd.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    ask_cmd.add_argument("--db", type=Path, default=None)
    ask_cmd.add_argument("query", help="Question to answer")
    ask_cmd.add_argument("--limit", type=int, default=5)
    ask_cmd.add_argument("--no-refresh", action="store_true", help="skip the automatic index refresh")
    ask_cmd.add_argument("--provider", default=None)
    ask_cmd.add_argument("--model", default=None)
    ask_cmd.add_argument("--api-base", default=None)
    ask_cmd.add_argument("--api-key", default=None)
    ask_cmd.add_argument("--temperature", type=float, default=None)
    ask_cmd.add_argument("--max-tokens", dest="max_tokens", type=int, default=5120)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = ChatConfig.from_args(args)
    if args.command == "build-index":
        summary = refresh_index(cfg.vault_path, excludes=args.exclude, db_path=cfg.db_path, full=args.full)
        print(json.dumps({
            "db": str(cfg.db_path or (cfg.vault_path / ".chat_index" / "index.sqlite")),
            "indexed": summary.total_chunks,
            "added": summary.added,
            "updated": summary.updated,
            "removed": summary.removed,
            "unchanged": summary.unchanged,
            "total_chunks": summary.total_chunks,
            "elapsed_seconds": round(summary.elapsed_seconds, 3),
            "schema_rebuilt": summary.schema_rebuilt,
        }, ensure_ascii=False))
        return 0

    db = ChatDB(cfg.db_path or (args.vault / ".chat_index" / "index.sqlite"))
    engine = SearchEngine(db)
    if args.command == "search":
        results = engine.search(args.query, limit=args.limit)
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    if args.command == "ask":
        if not args.no_refresh:
            summary = refresh_index(cfg.vault_path, db_path=cfg.db_path)
            if summary.changed:
                print(f"Chat index refreshed: +{summary.added} added, {summary.updated} updated, "
                      f"-{summary.removed} removed.")
        results = engine.search(args.query, limit=args.limit)
        tracker = UsageTracker()
        tracker.start_question()
        if not results:
            result = {"answer": "Not in the knowledge base", "source_ids": [], "questions_for_BA": [args.query], "not_found": True, "warnings": []}
            render_answer(result, question=args.query, usage=tracker.question_summary())
            print(json.dumps({"answer": result["answer"], "source_ids": [], "questions_for_BA": [args.query]}, ensure_ascii=False))
            return 0
        context = "\n\n---\n\n".join(_format_result(row) for row in results)
        client = provider_from_env(
            provider=cfg.provider,
            api_base=cfg.api_base,
            model=cfg.model,
            api_key=cfg.api_key,
        )
        prompt = (
            "Answer using only the provided notes. For every factual claim include the source path in the answer.\n\n"
            f"Question: {args.query}\n\nContext:\n{context}\n"
        )
        started = time.perf_counter()
        answer, resp_json = client.complete_with_usage(
            prompt,
            system_prompt="You are a safe project QA assistant. Be explicit about uncertainty.",
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
        )
        elapsed = time.perf_counter() - started
        tracker.record_response(
            client.provider,
            client.model,
            resp_json,
            label="answer",
            prompt_text=prompt,
            completion_text=answer,
            seconds=elapsed,
        )
        result = {
            "answer": answer,
            "source_ids": [row["id"] for row in results],
            "questions_for_BA": [],
            "not_found": False,
            "warnings": [],
            "claims": [],
        }
        render_answer(result, question=args.query, usage=tracker.question_summary())
        print(json.dumps({"answer": answer, "source_ids": [row["id"] for row in results], "questions_for_BA": []}, ensure_ascii=False))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
