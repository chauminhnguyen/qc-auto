"""Command line entry point for BRD inspection and safe vault sync."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # inspect and adapter contracts remain available without PyYAML
    yaml = None

from .adapters import BrdAdapter, EvidenceAdapter
from .inspect import inspect_vault, print_report
from .sync import (SyncError, apply_plan, build_plan, purge, restore, rollback_journal,
                   resume_journal, status_report, write_plan)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect and synchronize the BRD evidence vault")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Read-only vault inspection")
    inspect.add_argument("--config", type=Path, default=Path("config.yaml"))
    inspect.add_argument("--vault", type=Path)
    inspect.add_argument("--out", type=Path, default=Path("out/inspect_notes.csv"))

    sync = commands.add_parser("sync", help="Plan or manage source-to-vault synchronization")
    actions = sync.add_subparsers(dest="sync_command", required=True)
    for name in ("plan", "adapters", "status", "purge", "restore"):
        sub = actions.add_parser(name)
        sub.add_argument("--config", type=Path, default=Path("config.yaml"))
    plan = actions.choices["plan"]
    plan.add_argument("--out-json", type=Path, default=Path("out/sync_plan.json"))
    plan.add_argument("--out-md", type=Path, default=Path("out/sync_plan.md"))
    plan.add_argument("--force", action="store_true", help="Include changes behind configured safety gates")
    apply = actions.add_parser("apply", help="Apply the exact previously reviewed sync plan")
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--force", action="store_true")
    apply.add_argument("--config", type=Path, default=Path("config.yaml"))
    purge_parser = actions.choices["purge"]
    purge_parser.add_argument("--older-than", type=int, required=True, metavar="DAYS")
    restore_parser = actions.choices["restore"]
    restore_parser.add_argument("source_id")
    resume = actions.add_parser("resume", help="Resume an interrupted journaled apply")
    resume.add_argument("journal", type=Path)
    resume.add_argument("--force", action="store_true")
    rollback = actions.add_parser("rollback", help="Restore files and SQLite state from an apply backup")
    rollback.add_argument("journal", type=Path)
    return parser


def _read_config(path: Path) -> dict:
    if not path.is_file():
        raise SyncError(f"Config not found: {path}")
    raw = path.read_text(encoding="utf-8")
    if yaml:
        try:
            config = yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            raise SyncError(f"Invalid YAML in {path}: {exc}") from exc
    else:
        config = _simple_yaml(raw)
    if not isinstance(config, dict):
        raise SyncError("Config must contain a YAML mapping")
    return config


def _scalar(value: str):
    value = value.strip()
    if not value:
        return None
    if value.casefold() in {"true", "false"}:
        return value.casefold() == "true"
    if value.casefold() in {"null", "~"}:
        return None
    if value.startswith("[") and value.endswith("]"):
        return [_scalar(part) for part in value[1:-1].split(",") if part.strip()]
    if value.startswith("{") and value.endswith("}"):
        result = {}
        for part in value[1:-1].split(","):
            if ":" in part:
                key, val = part.split(":", 1)
                result[key.strip().strip("\"'")] = _scalar(val)
        return result
    if (value.startswith("\"") and value.endswith("\"")) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def _simple_yaml(raw: str) -> dict:
    """Parse the simple mapping/list subset used by the linker config."""
    root = {}
    stack = [(-1, root)]
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        text = line.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if text.startswith("- "):
            if not isinstance(parent, list):
                continue
            parent.append(_scalar(text[2:]))
            continue
        if ":" not in text or not isinstance(parent, dict):
            continue
        key, value = text.split(":", 1)
        key = key.strip().strip("\"'")
        parsed = _scalar(value)
        if parsed is None and not value.strip():
            next_line_list = False
            parent[key] = {}
            stack.append((indent, parent[key]))
        else:
            parent[key] = parsed
    return root


def _adapter_demo(config: dict, config_path: Path):
    vault = Path(config.get("vault", "./ObsidianVault"))
    if not vault.is_absolute():
        vault = (config_path.resolve().parent / vault).resolve()
    sync_config = config.get("sync") or {}
    root = config_path.resolve().parent
    brd_dir = (root / sync_config.get("brd_dir", "./BRDs")).resolve()
    evidence_dir = (root / sync_config.get("evidence_dir", str(vault / "Reports" / "native_runs"))).resolve()
    capture_db = (root / sync_config.get("capture_db", str(vault / ".capture_index" / "index.sqlite"))).resolve()
    run_path = EvidenceAdapter.newest_run(evidence_dir)
    run, entities = EvidenceAdapter(capture_db, config.get("key_normalization") or {}).read(run_path)
    segments = BrdAdapter().read(sorted(brd_dir.rglob("*.md")))
    return {
        "schemas": {
            "Entity": {"kind": "api|param", "key": "stable normalized identity", "name": "display name",
                       "attrs": {"method": "optional", "path": "optional", "location": "optional",
                                 "type": "optional", "status_codes": "optional", "action_ids": "optional"},
                       "seen_in": ["capture session id"]},
            "RunInfo": {"run_id": "string", "ts": "timestamp", "complete": "boolean", "scope_summary": "string"},
            "Segment": {"brd_id": "source filename stem", "key": "heading path slug", "heading_path": ["heading"], "text": "section markdown"},
        },
        "evidence": {"path": str(run_path), "run": run.__dict__, "entity_count": len(entities),
                      "sample_entities": [entity.to_dict() for entity in entities[:3]]},
        "brds": {"directory": str(brd_dir), "segment_count": len(segments),
                 "sample_segments": [segment.to_dict() for segment in segments[:3]]},
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            config = _read_config(args.config)
            vault = args.vault or Path(config.get("vault", "./ObsidianVault"))
            report = inspect_vault(vault, args.out)
            print_report(report)
            return 0

        if args.sync_command == "apply":
            result = apply_plan(args.plan, force=args.force)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.sync_command == "resume":
            result = resume_journal(args.journal, force=args.force)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.sync_command == "rollback":
            result = rollback_journal(args.journal)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        config = _read_config(args.config)
        vault = Path(config.get("vault", "./ObsidianVault"))
        if not vault.is_absolute():
            vault = (args.config.resolve().parent / vault).resolve()
        sync_cfg = config.get("sync") or {}
        state_db = vault / sync_cfg.get("state_db", ".capture_index/brd_linker_sync.sqlite")
        if args.sync_command == "adapters":
            print(json.dumps(_adapter_demo(config, args.config), ensure_ascii=False, indent=2))
        elif args.sync_command == "plan":
            result = build_plan(config, args.config, force=args.force)
            write_plan(result, args.out_json, args.out_md)
            print(json.dumps({"plan_json": str(args.out_json.resolve()), "plan_markdown": str(args.out_md.resolve()),
                              "counts": result["counts"], "blockers": result["blockers"]}, ensure_ascii=False, indent=2))
            return 1 if result["blockers"] and not args.force else 0
        elif args.sync_command == "status":
            print(json.dumps(status_report(vault, state_db), ensure_ascii=False, indent=2))
        elif args.sync_command == "purge":
            count = purge(vault, args.older_than, state_db)
            print(json.dumps({"purged": count}, ensure_ascii=False))
        elif args.sync_command == "restore":
            path = restore(vault, args.source_id, state_db, config)
            print(json.dumps({"restored": args.source_id, "path": path}, ensure_ascii=False))
        return 0
    except (SyncError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"sync error: {exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
