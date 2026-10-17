"""Optional PyMuPDF extractor. PDF support is lazy-loaded and never OCRs silently."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .ir import DocumentProfile, ExtractedImage, ExtractedTable, PageChunk, SourceAnchor


def extract_pdf_document(path: str | Path, output_dir: str | Path) -> tuple[DocumentProfile, list[PageChunk]]:
    try:
        import fitz  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("PDF extraction requires PyMuPDF (fitz); source remains manual until installed") from exc
    source = Path(path).resolve()
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    doc = fitz.open(source)
    if getattr(doc, "needs_pass", False):
        doc.close()
        raise RuntimeError("Password-protected PDF requires manual handling")
    page_texts = [page.get_text("text") or "" for page in doc]
    edge_lines: dict[str, int] = {}
    for text in page_texts:
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        for line in (lines[:2] + lines[-2:]):
            edge_lines[line] = edge_lines.get(line, 0) + 1
    repeated = {line for line, count in edge_lines.items() if count >= max(2, (len(doc) + 1) // 2)}
    chunks: list[PageChunk] = []
    image_hashes: dict[str, Path] = {}
    issues: list[str] = []
    table_count = 0
    for index, page in enumerate(doc):
        lines = [x for x in page_texts[index].splitlines() if x.strip() and x.strip() not in repeated]
        text = "\n".join(lines)
        images: list[ExtractedImage] = []
        page_image_dominant = False
        page_image_area = 0.0
        page_area = page.rect.width * page.rect.height
        for image_info in page.get_images(full=True):
            xref = image_info[0]
            try:
                extracted = doc.extract_image(xref)
                data = extracted["image"]
                image_hash = hashlib.sha256(data).hexdigest()
                if image_hash not in image_hashes:
                    ext = extracted.get("ext", "bin")
                    dest = out / "assets" / image_hash[:20] / f"image.{ext}"
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
                    image_hashes[image_hash] = dest
                rects = page.get_image_rects(xref)
                page_image_area += sum(rect.width * rect.height for rect in rects)
                images.append(ExtractedImage(image_hash, str(image_hashes[image_hash].relative_to(out)),
                                             width=extracted.get("width"), height=extracted.get("height"),
                                             duplicates=max(0, sum(1 for c in chunks for im in c.images if im.hash == image_hash)),
                                             source_page=index + 1))
            except Exception as exc:
                issues.append(f"page-{index + 1}:image-extraction-failed:{type(exc).__name__}")
        if page_area and page_image_area / page_area >= 0.35 and len(text) < 200:
            issues.append(f"page-{index + 1}:image-dominant")
            page_image_dominant = True
        tables: list[ExtractedTable] = []
        finder = getattr(page, "find_tables", None)
        if callable(finder):
            try:
                found = finder()
                for table in getattr(found, "tables", []):
                    rows = [["" if c is None else str(c).strip() for c in row] for row in table.extract()]
                    if not rows:
                        continue
                    ncols = max(map(len, rows))
                    md = "| " + " | ".join(rows[0]) + " |\n| " + " | ".join(["---"] * ncols) + " |\n"
                    md += "".join("| " + " | ".join(r + [""] * (ncols - len(r))) + " |\n" for r in rows[1:])
                    roundtrip = [[c.strip() for c in line.strip().strip("|").split("|")]
                                 for line in md.splitlines() if line.strip().startswith("|")]
                    valid = len(roundtrip) == len(rows) + 1 and all(len(r) == ncols for r in roundtrip)
                    crop_path = None
                    validation = "validated" if valid else "flagged"
                    if not valid:
                        try:
                            crop = out / "tables" / f"{digest[:12]}-page-{index + 1}-{len(tables) + 1}.png"
                            crop.parent.mkdir(parents=True, exist_ok=True)
                            bbox = getattr(table, "bbox", None)
                            if bbox:
                                page.get_pixmap(clip=fitz.Rect(bbox), dpi=150).save(crop)
                                crop_path = str(crop.relative_to(out))
                        except Exception:
                            issues.append(f"page-{index + 1}:table-crop-failed")
                    tables.append(ExtractedTable(index + 1, md, rows, crop_path=crop_path,
                                                 validation_status=validation,
                                                 row_count=len(rows), col_count=ncols))
                    table_count += 1
            except Exception as exc:
                issues.append(f"page-{index + 1}:table-detection-failed:{type(exc).__name__}")
        # Vector flowchart labels remain in the normal page text and are surfaced nearby.
        figures = []
        if images and text.strip():
            figures.append({"label": f"Figure on page {index + 1}", "nearby_text": text[:1200]})
        anchor = SourceAnchor(str(source), None, index + 1)
        if tables:
            text = (text + "\n\n" if text else "") + "\n\n".join(table.markdown for table in tables)
        chunks.append(PageChunk(index + 1, str(source), None, text, images=images, tables=tables,
                                figure_text=figures, anchors=[anchor],
                                metadata={"text_chars": len(text), "repeated_edges_removed": len(page_texts[index].splitlines()) - len(lines),
                                          "image_count": len(images), "table_count": len(tables),
                                          "image_dominant": page_image_dominant}))
    doc.close()
    missing = [i + 1 for i, c in enumerate(chunks) if not c.markdown_text.strip() and not c.images]
    if missing:
        issues.append("empty-pages:" + ",".join(map(str, missing)))
    has_text = sum(len(c.markdown_text.strip()) for c in chunks)
    tier = "C" if not has_text or missing or any("image-dominant" in x for x in issues) else "B" if table_count else "A"
    profile = DocumentProfile(digest, str(source), None, len(chunks), tier, issues)
    return profile, chunks
