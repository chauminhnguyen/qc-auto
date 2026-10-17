"""Native DOCX XML reader that preserves tables, notes, comments, and revisions."""
from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .ir import DocumentProfile, ExtractedImage, ExtractedTable, PageChunk, SourceAnchor

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _paragraph(node: ET.Element) -> str:
    chunks = []

    def collect(element: ET.Element) -> None:
        for child in element:
            if child.tag == W + "ins":
                chunks.append("[INSERTED: ")
                collect(child)
                chunks.append("]")
            elif child.tag == W + "del":
                chunks.append("[DELETED: ")
                collect(child)
                chunks.append("]")
            elif child.tag in {W + "t", W + "delText"} and child.text:
                chunks.append(child.text)
            else:
                collect(child)

    collect(node)
    text = "".join(chunks)
    if not text.strip():
        return ""
    style = node.find("./" + W + "pPr/" + W + "pStyle")
    style_id = style.attrib.get(W + "val", "") if style is not None else ""
    match = re.fullmatch(r"Heading([1-6])", style_id, re.I)
    return ("#" * int(match.group(1)) + " " if match else "") + text.strip()


def extract_docx_document(path: str | Path, output_dir: str | Path) -> tuple[DocumentProfile, list[PageChunk]]:
    source = Path(path).resolve()
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    issues: list[str] = []
    images: list[ExtractedImage] = []
    try:
        with zipfile.ZipFile(source) as archive:
            names = set(archive.namelist())
            root = ET.fromstring(archive.read("word/document.xml"))
            if any(next(root.iter(W + tag), None) is not None for tag in ("ins", "del")):
                issues.append("tracked-changes-retained-in-text-review-required")
            body = root.find("./" + W + "body")
            blocks: list[str] = []
            tables: list[ExtractedTable] = []
            if body is not None:
                for element in list(body):
                    if element.tag == W + "p":
                        line = _paragraph(element)
                        if line:
                            blocks.append(line)
                    elif element.tag == W + "tbl":
                        rows: list[list[str]] = []
                        for tr in element.findall("./" + W + "tr"):
                            rows.append([" ".join(t for t in (p.text for p in tc.iter(W + "t")) if t)
                                         for tc in tr.findall("./" + W + "tc")])
                        if rows:
                            cols = max(map(len, rows))
                            md = "| " + " | ".join(rows[0]) + " |\n| " + " | ".join(["---"] * cols) + " |\n"
                            md += "".join("| " + " | ".join(row + [""] * (cols - len(row))) + " |\n" for row in rows[1:])
                            blocks.append(md)
                            tables.append(ExtractedTable(None, md, rows, validation_status="extracted",
                                                         row_count=len(rows), col_count=cols))
            for part in sorted(n for n in names if n.startswith("word/header") or n.startswith("word/footer") or
                               n in {"word/footnotes.xml", "word/endnotes.xml", "word/comments.xml"}):
                try:
                    extra_root = ET.fromstring(archive.read(part))
                    extra = [_paragraph(p) for p in extra_root.iter(W + "p")]
                    extra = [x for x in extra if x]
                    if extra:
                        blocks.extend([f"## Source notes: {Path(part).name}", *extra])
                except ET.ParseError:
                    issues.append(f"unreadable-xml:{part}")
            for name in sorted(n for n in names if n.startswith("word/media/")):
                data = archive.read(name)
                image_hash = hashlib.sha256(data).hexdigest()
                ext = Path(name).suffix or ".bin"
                dest = out / "assets" / image_hash[:20] / f"image{ext}"
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    dest.write_bytes(data)
                images.append(ExtractedImage(image_hash, str(dest.relative_to(out)), duplicates=0))
            if "word/comments.xml" not in names:
                issues.append("comments-not-present")
            if "word/footnotes.xml" not in names:
                issues.append("footnotes-not-present")
            if "word/document.xml" not in names:
                issues.append("missing-document-xml")
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise RuntimeError(f"DOCX extraction failed: {exc}") from exc
    text = "\n\n".join(blocks)
    if not text.strip():
        issues.append("no-extractable-text")
    anchor = SourceAnchor(str(source), None, 1)
    chunk = PageChunk(1, str(source), None, text, images=images, tables=tables,
                      anchors=[anchor], metadata={"logical_page": True, "tracked_changes_preserved_as_text": True})
    return DocumentProfile(digest, str(source), None, None, "A" if text.strip() else "C", issues), [chunk]
