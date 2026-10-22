"""UTF-8 note discovery and block splitting."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NoteBlock:
    source: str
    block_key: str
    text: str
    line_start: int
    line_end: int
    sha256: str

    @property
    def locator(self) -> str:
        return f"{self.source}#L{self.line_start}-L{self.line_end}"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_block(source: str, key: str, lines: list[str], start: int, end: int) -> NoteBlock:
    text = "\n".join(lines).strip()
    return NoteBlock(source, key, text, start, end, _hash(text))


def parse_note(path: Path, notes_root: Path) -> list[NoteBlock]:
    path, notes_root = Path(path).resolve(), Path(notes_root).resolve()
    try:
        relative_path = path.relative_to(notes_root)
    except ValueError:
        raise ValueError(f"Note resolves outside Notes-raw: {path}")
    text = path.read_text(encoding="utf-8-sig")
    rel = relative_path.as_posix()
    lines = text.splitlines()
    blocks: list[NoteBlock] = []
    current: list[str] = []
    start = 1
    ordinal = 0
    explicit_key = ""
    key_counts: dict[str, int] = {}

    def flush(end_line: int):
        nonlocal current, ordinal, explicit_key
        if not "\n".join(current).strip():
            current, explicit_key = [], ""
            return
        ordinal += 1
        base_key = explicit_key or f"block-{ordinal:04d}"
        key_counts[base_key] = key_counts.get(base_key, 0) + 1
        key = base_key if key_counts[base_key] == 1 else f"{base_key}-{key_counts[base_key]:02d}"
        blocks.append(_make_block(rel, key, current, start, end_line))
        current, explicit_key = [], ""

    for index, line in enumerate(lines, start=1):
        marker = re.match(r"^\s*<!--\s*knowledge-id:\s*([^>]+?)\s*-->\s*$", line, re.I)
        if marker:
            if current:
                flush(index - 1)
            explicit_key = "id-" + marker.group(1).strip()
            start = index + 1
            continue
        if line.startswith("## ") or line.startswith("### "):
            if current:
                flush(index - 1)
            heading = line.lstrip("# ").strip()
            explicit_key = "heading-" + re.sub(r"\s+", "-", heading.casefold())
            current = [line]
            start = index
            continue
        if not line.strip():
            if current and not explicit_key:
                flush(index - 1)
            if not current:
                start = index + 1
            continue
        if not current:
            start = index
        current.append(line)
    if current:
        flush(len(lines))
    return blocks


def discover_notes(notes_root: Path) -> list[Path]:
    root = Path(notes_root)
    if root.is_file():
        return [root] if root.suffix.casefold() in {".md", ".txt"} else []
    if not root.is_dir():
        raise FileNotFoundError(f"Notes folder not found: {root}")
    return sorted(p for p in root.rglob("*") if p.is_file()
                  and p.suffix.casefold() in {".md", ".txt"})


def load_note_documents(notes_root: Path):
    """Load raw note blocks as shared source documents with explicit markers."""
    from .sources import SourceDocument

    notes_root = Path(notes_root).resolve()
    documents = []
    for path in discover_notes(notes_root):
        root = notes_root if notes_root.is_dir() else notes_root.parent
        for block in parse_note(path, root):
            heading = next((line.lstrip("# ").strip() for line in block.text.splitlines()
                            if line.startswith("#")), path.stem)
            documents.append(SourceDocument(
                source_type="notes", id=block.locator, title=heading,
                markdown=block.text,
                metadata={"file": block.source, "block_key": block.block_key,
                          "line_start": block.line_start, "line_end": block.line_end,
                          "sha256": block.sha256},
            ))
    return documents
