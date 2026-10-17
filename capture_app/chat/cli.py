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
from .ingest import build_index
from .llm import provider_from_env
from .retriever import SearchEngine


def _format_result(row: dict) -> str:
    return (
        f"[{row.get('note_type', 'note')}] {row.get('title', row.get('source_path'))}\n"
        f"feature: {row.get('feature') or 'n/a'}\n"
        f"status: {row.get('status') or 'unknown'}\n"
        f"source: {row.get('source_path', 'n/a')}\n"
        f"body: {row.get('body','')}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and query the separate vault chat index")
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_cmd = subparsers.add_parser("build-index", help="Rebuild the chat SQLite index from the vault")
    index_cmd.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    index_cmd.add_argument("--db", type=Path, default=None)

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
        db = build_index(cfg.vault_path, cfg.db_path)
        print(json.dumps({"db": str(db.db_path), "indexed": db.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]}, ensure_ascii=False))
        return 0

    db = ChatDB(cfg.db_path or (args.vault / ".chat_index" / "index.sqlite"))
    engine = SearchEngine(db)
    if args.command == "search":
        results = engine.search(args.query, limit=args.limit)
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    if args.command == "ask":
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
