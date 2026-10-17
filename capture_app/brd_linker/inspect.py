"""Read-only inspection of an Obsidian vault for the BRD linker."""
from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # inspect remains usable before installing project extras
    yaml = None  # type: ignore[assignment]


WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
FRONTMATTER_RE = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)


@dataclass(frozen=True)
class Note:
    path: Path
    relative_path: str
    text: str
    frontmatter: dict[str, Any]
    title: str
    link_count: int
    tags: tuple[str, ...]


def _frontmatter(text: str) -> dict[str, Any]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    if yaml is None:
        # Inspection only needs top-level key names, title, and tags. Keep this
        # small fallback so the read-only first stage can run in a bare venv.
        value: dict[str, Any] = {}
        current_list_key: str | None = None
        for line in match.group(1).splitlines():
            if re.match(r"^\s+", line) and current_list_key:
                item = re.match(r"^\s+-\s*(.*?)\s*$", line)
                if item:
                    value.setdefault(current_list_key, []).append(item.group(1).strip("\"'"))
                continue
            current_list_key = None
            key_value = re.match(r"^([A-Za-z0-9_-]+):(?:\s*(.*))?$", line)
            if not key_value:
                continue
            key, raw = key_value.group(1), (key_value.group(2) or "").strip()
            if key == "tags":
                if raw.startswith("[") and raw.endswith("]"):
                    value[key] = [part.strip().strip("\"'") for part in raw[1:-1].split(",") if part.strip()]
                elif raw:
                    value[key] = [part.strip().strip("\"'") for part in raw.split(",") if part.strip()]
                else:
                    value[key] = []
                    current_list_key = key
            else:
                value[key] = raw.strip("\"'")
        return value
    try:
        value = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return value if isinstance(value, dict) else {}


def _tags(frontmatter: dict[str, Any]) -> tuple[str, ...]:
    value = frontmatter.get("tags", [])
    if isinstance(value, str):
        values = re.split(r"[,\s]+", value.strip(" []"))
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = []
    return tuple(sorted({str(tag).strip().lstrip("#") for tag in values if str(tag).strip()}))


def _title(path: Path, text: str, frontmatter: dict[str, Any]) -> str:
    title = frontmatter.get("title")
    if title:
        return str(title)
    heading = re.search(r"(?m)^#\s+(.+?)\s*$", FRONTMATTER_RE.sub("", text, count=1))
    return heading.group(1) if heading else path.stem


def _load_notes(vault: Path) -> list[Note]:
    notes: list[Note] = []
    for path in sorted(vault.rglob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        fm = _frontmatter(text)
        notes.append(Note(
            path=path,
            relative_path=path.relative_to(vault).as_posix(),
            text=text,
            frontmatter=fm,
            title=_title(path, text, fm),
            link_count=len(WIKILINK_RE.findall(text)),
            tags=_tags(fm),
        ))
    return notes


def _graph_groups(vault: Path) -> list[dict[str, Any]]:
    path = vault / ".obsidian" / "graph.json"
    if not path.is_file():
        return []
    try:
        graph = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    groups = graph.get("colorGroups", []) if isinstance(graph, dict) else []
    return [group for group in groups if isinstance(group, dict) and group.get("query")]


def _matches_group(note: Note, query: str) -> bool:
    query = query.strip()
    tag_match = re.fullmatch(r"tag:#?([\w/-]+)", query, flags=re.IGNORECASE)
    if tag_match:
        return tag_match.group(1).casefold() in {tag.casefold() for tag in note.tags}
    path_match = re.fullmatch(r"path:(.+)", query, flags=re.IGNORECASE)
    if path_match:
        wanted = path_match.group(1).replace("\\", "/").strip("/").casefold()
        note_path = note.relative_path.casefold()
        return note_path == wanted or note_path.startswith(wanted + "/")
    return False


def _folder_name(note: Note) -> str:
    parts = Path(note.relative_path).parts
    return "/".join(parts[:-1]) if len(parts) > 1 else "."


def inspect_vault(vault: Path, csv_path: Path, sample_limit: int = 2) -> dict[str, Any]:
    """Summarize vault conventions and save exact per-note link counts."""
    vault = vault.resolve()
    if not vault.is_dir():
        raise FileNotFoundError(f"Vault directory does not exist: {vault}")

    notes = _load_notes(vault)
    folders = Counter(_folder_name(note) for note in notes)
    frontmatter_keys: Counter[str] = Counter()
    tags: Counter[str] = Counter()
    for note in notes:
        frontmatter_keys.update(str(key) for key in note.frontmatter)
        tags.update(note.tags)

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["path", "title", "wikilink_count", "tags"])
        writer.writeheader()
        for note in notes:
            writer.writerow({
                "path": note.relative_path,
                "title": note.title,
                "wikilink_count": note.link_count,
                "tags": ",".join(note.tags),
            })

    group_reports = []
    for group in _graph_groups(vault):
        query = str(group["query"])
        members = [note for note in notes if _matches_group(note, query)]
        if not members:
            continue
        samples = [
            {"path": note.relative_path, "title": note.title}
            for note in members[:sample_limit]
        ]
        group_reports.append({"query": query, "count": len(members), "samples": samples})

    return {
        "note_count": len(notes),
        "folder_counts": dict(sorted(folders.items())),
        "frontmatter_key_counts": dict(sorted(frontmatter_keys.items())),
        "common_tags": tags.most_common(20),
        "color_groups": group_reports,
        "wikilinks": {
            "notes_with_none": sum(note.link_count == 0 for note in notes),
            "notes_with_one_or_more": sum(note.link_count > 0 for note in notes),
            "total": sum(note.link_count for note in notes),
        },
        "classification": {
            "segment": {"folder": "BRDs/raw/"},
            "api": {"tag": "api"},
            "param": {"tag": "param"},
        },
        "per_note_csv": str(csv_path.resolve()),
    }


def print_report(report: dict[str, Any]) -> None:
    print(f"Markdown notes: {report['note_count']}")
    print("\nNotes by folder:")
    for folder, count in report["folder_counts"].items():
        print(f"  {folder}: {count}")
    print("\nFrontmatter keys (notes containing each key):")
    for key, count in report["frontmatter_key_counts"].items():
        print(f"  {key}: {count}")
    print("\nCommon tags:")
    for tag, count in report["common_tags"]:
        print(f"  #{tag}: {count}")
    print("\nSamples from nonempty Obsidian graph color groups:")
    for group in report["color_groups"]:
        print(f"  {group['query']} ({group['count']} notes)")
        for sample in group["samples"]:
            print(f"    {sample['path']} — {sample['title']}")
    links = report["wikilinks"]
    print("\nWikilinks:")
    print(f"  total: {links['total']}")
    print(f"  notes with none: {links['notes_with_none']}")
    print(f"  notes with one or more: {links['notes_with_one_or_more']}")
    print(f"  per-note counts: {report['per_note_csv']}")
    print("\nProposed classification (confirm in config.yaml before model-backed commands):")
    for kind, rule in report["classification"].items():
        print(f"  {kind}: {next(iter(rule))}={next(iter(rule.values()))}")

