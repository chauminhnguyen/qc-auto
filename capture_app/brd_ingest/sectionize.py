"""Zero-LLM Markdown section extraction and Obsidian raw-note generation."""
from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

from .ir import PageChunk, SourceAnchor
from .ledger import BrdLedger
from .structure import brd_id as make_brd_id, extract_refs, slug, update_note


_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def markdown_chunks(path: str | Path, file_hash: str = "") -> tuple[list[PageChunk], str | None]:
    source = Path(path).resolve()
    text = source.read_text(encoding="utf-8-sig", errors="replace")
    version_match = re.search(r"\bversion\*{0,2}\s*:?\*{0,2}\s*([\w.-]+)", text, re.I)
    version = version_match.group(1) if version_match else None
    matches = list(_HEADING.finditer(text))
    pieces: list[tuple[str, str]] = []
    if not matches:
        pieces = [(source.stem, text.strip())]
    else:
        preface = text[:matches[0].start()].strip()
        if preface:
            pieces.append(("Introduction", preface))
        stack: list[str] = []
        for i, match in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            title = match.group(2).strip()
            level = len(match.group(1))
            stack = stack[:level - 1]
            stack.append(title)
            section_path = " > ".join(stack)
            # Heading sections retain nested headings as part of the parent content;
            # split at every heading for reliable source anchors and review.
            pieces.append((section_path, text[match.start():end].strip()))
    chunks: list[PageChunk] = []
    for index, (title, body) in enumerate(pieces):
        section_id = hashlib.sha256(f"{source}|{file_hash}|{index}|{title}".encode()).hexdigest()[:16]
        chunks.append(PageChunk(1, str(source), section_id, body,
                               anchors=[SourceAnchor(str(source), title, 1)]))
    return chunks, version


def build_raw_section_notes(source: str | Path, vault: str | Path,
                            ledger: BrdLedger, file_hash: str, *, chunks_override: list[PageChunk] | None = None,
                            version_override: str | None = None,
                            asset_dir: str | Path | None = None) -> list[Path]:
    source_path = Path(source).resolve()
    vault_path = Path(vault).resolve()
    chunks, version = markdown_chunks(source_path, file_hash) if chunks_override is None else (chunks_override, version_override)
    version = version_override or version
    raw_root = vault_path / "BRDs" / "raw"
    asset_root = vault_path / "BRDs" / "assets"
    raw_root.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    ledger.conn.execute("UPDATE sections SET status='superseded' WHERE source_path=?", (str(source_path),))
    ledger.conn.execute("DELETE FROM pages WHERE source_path=? AND file_hash=?", (str(source_path), file_hash))
    used_image_hashes: set[str] = set()
    hub_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", source_path.stem).strip("-.") or "BRD"
    hub_rel = f"BRDs/index/{hub_slug}.md"
    note_paths = []
    for order, chunk in enumerate(chunks):
        section = (chunk.anchors[0].section if chunk.anchors else None) or f"Page {chunk.page_number}"
        section_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", section).strip("-.")[:48] or "section"
        note_paths.append(raw_root / f"{source_path.stem}-{file_hash[:8]}-{order + 1:02d}-{section_slug}.md")
    for order, chunk in enumerate(chunks):
        title = (chunk.anchors[0].section if chunk.anchors else None) or f"Page {chunk.page_number}"
        section_id = chunk.section_id or hashlib.sha256(f"{source_path}|{file_hash}|{chunk.page_number}|{title}".encode()).hexdigest()[:16]
        section_hash = hashlib.sha256(chunk.markdown_text.encode("utf-8")).hexdigest()
        note_path = note_paths[order]
        prev_rel = note_paths[order - 1].relative_to(vault_path).as_posix() if order else ""
        next_rel = note_paths[order + 1].relative_to(vault_path).as_posix() if order + 1 < len(note_paths) else ""
        section_path = title
        stable_id = make_brd_id(source_path)
        breadcrumb = f"{stable_id} > {section_path}"
        body = chunk.markdown_text

        def localize_image(match: re.Match[str]) -> str:
            caption, target = match.group(1), match.group(2).strip()
            if re.match(r"^(?:https?:|data:)", target, re.I):
                return match.group(0)
            src = (source_path.parent / target.split("#", 1)[0]).resolve()
            if not src.is_file():
                return match.group(0)
            digest = hashlib.sha256(src.read_bytes()).hexdigest()
            used_image_hashes.add(digest)
            dest_dir = asset_root / digest[:20]
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / src.name
            if not dest.exists():
                shutil.copy2(src, dest)
            rel = Path("..").joinpath("assets", digest[:20], src.name).as_posix()
            return f"![{caption}]({rel})"

        body = _IMAGE.sub(localize_image, body)
        if asset_dir is not None:
            asset_base = Path(asset_dir).resolve()
            embeds = []
            for image in chunk.images:
                src = (asset_base / image.relative_path).resolve()
                if not src.is_file():
                    continue
                digest = hashlib.sha256(src.read_bytes()).hexdigest()
                used_image_hashes.add(digest)
                dest_dir = asset_root / digest[:20]
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / src.name
                if not dest.exists():
                    shutil.copy2(src, dest)
                rel = Path("..").joinpath("assets", digest[:20], src.name).as_posix()
                embeds.append(f"![{image.caption or f'Figure page {chunk.page_number}'}]({rel})")
            if embeds:
                body = (body + "\n\n" if body else "") + "\n".join(embeds)
        safe = lambda value: '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ') + '"'
        note = ("---\n" f"origin: brd\ntags: [brd-segment]\nsource_file: {safe(source_path.name)}\n"
                f"source_version: {safe(version or 'unknown')}\nsource_section: {safe(title)}\n"
                f"pages: {safe(chunk.page_number)}\nstatus: raw\nsource_hash: " + safe(file_hash) +
                f"\nbrd_id: {safe(stable_id)}\npart_id: {safe(stable_id + '#' + slug(section_path))}\n"
                f"section_path: {safe(section_path)}\norder: {order}\nparent: {safe(hub_rel)}\n"
                f"prev: {safe(prev_rel)}\nnext: {safe(next_rel)}\nbreadcrumb: {safe(breadcrumb)}\nrefs: []" +
                "\n---\n\n# Section: " + title + "\n\n## Source evidence\n\n" + body + "\n")
        note_path.write_text(note, encoding="utf-8")
        nav = [f"Parent: [[{hub_rel[:-3]}]] | "
               f"Prev: [[{prev_rel[:-3]}]] | "
               f"Next: [[{next_rel[:-3]}]]"]
        update_note(note_path, {"brd_id": stable_id, "part_id": f"{stable_id}#{slug(section_path)}",
                               "section_path": section_path, "order": order, "parent": hub_rel,
                               "prev": prev_rel, "next": next_rel, "breadcrumb": breadcrumb}, nav,
                    extract_refs(vault_path, note))
        outputs.append(note_path)
        ledger.conn.execute("""INSERT INTO sections(id,source_path,file_hash,section_title,order_index,
          page_start,page_end,section_hash,raw_note_path,status) VALUES(?,?,?,?,?,?,?,?,?,'raw')
          ON CONFLICT(id) DO UPDATE SET file_hash=excluded.file_hash,section_title=excluded.section_title,
          order_index=excluded.order_index,section_hash=excluded.section_hash,
          raw_note_path=excluded.raw_note_path,status='raw'""",
          (section_id, str(source_path), file_hash, title, order, chunk.page_number, chunk.page_number, section_hash, str(note_path)))
        table_count = sum(1 for _ in re.finditer(r"(?m)(?:^\s*\|.*\|\s*\n?)+", chunk.markdown_text))
        ledger.conn.execute("""INSERT INTO pages(id,source_path,file_hash,page_number,section_id,
          section_title,page_text_md,image_count,table_count,unique_hash) VALUES(?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(id) DO UPDATE SET file_hash=excluded.file_hash,section_id=excluded.section_id,
          section_title=excluded.section_title,page_text_md=excluded.page_text_md,
          image_count=excluded.image_count,table_count=excluded.table_count,unique_hash=excluded.unique_hash""",
          (f"{section_id}:{chunk.page_number}", str(source_path), file_hash, chunk.page_number, section_id,
           title, body, len(chunk.images) + len(_IMAGE.findall(body)), max(table_count, len(chunk.tables)), section_hash))
    ledger.conn.execute("DELETE FROM assets WHERE source_path=?", (str(source_path),))
    for digest in used_image_hashes:
        path = next((p for p in asset_root.glob(f"{digest[:20]}/*") if p.is_file()), None)
        if path:
            asset_id = hashlib.sha256(f"{source_path}|{digest}".encode()).hexdigest()
            ledger.conn.execute("INSERT INTO assets(id,source_path,asset_type,relative_path,original_hash,dedupe_key) VALUES(?,?,?,?,?,?)",
                                (asset_id, str(source_path), "image", str(path.relative_to(vault_path)), digest, digest))
    row = ledger.inventory_row(str(source_path))
    if row:
        ledger.conn.execute("UPDATE inventory SET raw_note_path=?,status=CASE WHEN status='pending' THEN 'raw_ready' ELSE status END WHERE source_path=?",
                            (str(raw_root), str(source_path)))
    current_paths = {str(path.resolve()) for path in outputs}
    for stale in ledger.conn.execute("SELECT raw_note_path FROM sections WHERE source_path=? AND status='superseded'",
                                     (str(source_path),)).fetchall():
        stale_path = Path(stale["raw_note_path"] or "")
        if stale_path.is_file() and str(stale_path.resolve()) not in current_paths:
            stale_text = stale_path.read_text(encoding="utf-8", errors="replace")
            stale_path.write_text(stale_text.replace("status: raw", "status: superseded", 1), encoding="utf-8")
    ledger.record(str(source_path), "sections", "build_raw_notes", "complete",
                  f"Created {len(outputs)} raw section notes",
                  {"sections": len(outputs), "pages": len(chunks), "assets": len(used_image_hashes)})
    ledger.conn.commit()
    return outputs
