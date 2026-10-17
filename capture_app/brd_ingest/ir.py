"""Small, serializable intermediate representation for BRD sources."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SourceAnchor:
    file: str
    section: str | None = None
    page: int | None = None
    xref: int | None = None
    note_id: str | None = None


@dataclass
class ExtractedImage:
    hash: str
    relative_path: str
    caption: str = ""
    width: int | None = None
    height: int | None = None
    duplicates: int = 0
    source_page: int | None = None


@dataclass
class ExtractedTable:
    page_number: int | None
    markdown: str
    rows: list[list[str]] = field(default_factory=list)
    crop_path: str | None = None
    validation_status: str = "not_applicable"
    row_count: int = 0
    col_count: int = 0


@dataclass
class FigureBlock:
    label: str
    nearby_text: str = ""
    caption: str = ""
    anchor: SourceAnchor | None = None


@dataclass
class PageChunk:
    page_number: int
    source_file: str
    section_id: str | None
    markdown_text: str
    images: list[ExtractedImage] = field(default_factory=list)
    tables: list[ExtractedTable] = field(default_factory=list)
    figure_text: list[FigureBlock] = field(default_factory=list)
    anchors: list[SourceAnchor] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DocumentProfile:
    file_hash: str
    file_path: str
    version: str | None
    pages: int | None
    tier: str
    issues: list[str] = field(default_factory=list)


def to_jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Unsupported value: {type(value).__name__}")
