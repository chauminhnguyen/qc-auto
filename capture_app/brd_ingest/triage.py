from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SUPPORTED_EXTENSIONS = {".md", ".txt", ".pdf", ".docx"}


@dataclass
class FileProfile:
    source_path: str
    file_hash: str
    file_name: str
    ext: str
    tier: str
    score: int
    reason_codes: list[str]
    pages: int | None = None
    metadata: dict[str, object] | None = None

    def to_row(self) -> dict[str, object]:
        return {
            "source_path": self.source_path,
            "file_hash": self.file_hash,
            "file_name": self.file_name,
            "ext": self.ext,
            "tier": self.tier,
            "score": self.score,
            "reason_codes": json.dumps(self.reason_codes, ensure_ascii=False),
            "pages": self.pages,
            "metadata": json.dumps(self.metadata or {}, ensure_ascii=False),
        }


class InventoryDB:
    """SQLite inventory for BRD source profiling and triage."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self._initialize()

    def _initialize(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inventory (
                source_path TEXT PRIMARY KEY,
                file_hash TEXT NOT NULL,
                file_name TEXT NOT NULL,
                ext TEXT NOT NULL,
                tier TEXT NOT NULL,
                score INTEGER NOT NULL,
                reason_codes TEXT NOT NULL,
                pages INTEGER,
                metadata TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            )
            """
        )
        self.conn.commit()

    def upsert(self, profile: FileProfile) -> None:
        ts = datetime.now(tz=timezone.utc).isoformat()
        row = profile.to_row()
        self.conn.execute(
            """
            INSERT INTO inventory (
                source_path, file_hash, file_name, ext, tier, score, reason_codes,
                pages, metadata, first_seen, last_seen, status
            ) VALUES (
                :source_path, :file_hash, :file_name, :ext, :tier, :score,
                :reason_codes, :pages, :metadata, :first_seen, :last_seen, :status
            )
            ON CONFLICT(source_path) DO UPDATE SET
                file_hash = excluded.file_hash,
                file_name = excluded.file_name,
                ext = excluded.ext,
                tier = excluded.tier,
                score = excluded.score,
                reason_codes = excluded.reason_codes,
                pages = excluded.pages,
                metadata = excluded.metadata,
                last_seen = excluded.last_seen,
                status = CASE WHEN inventory.file_hash != excluded.file_hash THEN excluded.status
                              WHEN excluded.status = 'manual' AND inventory.status NOT IN ('approved','rejected')
                              THEN 'manual' ELSE inventory.status END
            """,
            {
                **row,
                "first_seen": ts,
                "last_seen": ts,
                "status": "manual" if profile.tier == "C" else "pending",
            },
        )
        self.conn.commit()

    def __del__(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def close(self) -> None:
        self.conn.close()

    def list_profiles(self) -> list[FileProfile]:
        rows = self.conn.execute(
            """
            SELECT source_path, file_hash, file_name, ext, tier, score, reason_codes, pages, metadata
            FROM inventory ORDER BY source_path
            """
        ).fetchall()
        out: list[FileProfile] = []
        for row in rows:
            out.append(
                FileProfile(
                    source_path=row[0],
                    file_hash=row[1],
                    file_name=row[2],
                    ext=row[3],
                    tier=row[4],
                    score=int(row[5]),
                    reason_codes=json.loads(row[6] or "[]"),
                    pages=row[7],
                    metadata=json.loads(row[8] or "{}"),
                )
            )
        return out


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_read_text(path: Path) -> str:
    encodings = ("utf-8-sig",)
    for encoding in encodings:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_bytes().decode("utf-8", errors="replace")


def _detect_markdown_features(text: str) -> tuple[int, int, int, int, list[str]]:
    headings = len(re.findall(r"^#{1,6}\s+", text, flags=re.MULTILINE))
    table_rows = 0
    for line in text.splitlines():
        if "|" in line and re.search(r"\|.*\|", line):
            table_rows += 1
    image_count = len(re.findall(r"!\[[^\]]*\]\([^)]*\)", text))
    lines = [line for line in text.splitlines() if line.strip()]
    non_empty = len(lines)
    reasons: list[str] = []
    if headings:
        reasons.append("markdown-headings")
    if table_rows:
        reasons.append("table-like")
    if image_count:
        reasons.append("contains-images")
    return headings, table_rows, image_count, non_empty, reasons


def classify_brd_file(path: Path) -> FileProfile:
    path = path.resolve()
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported BRD file type: {path}")

    file_hash = _sha256(path)
    reason_codes: list[str] = []
    score = 0
    pages: int | None = None
    metadata: dict[str, object] = {"size_bytes": path.stat().st_size}

    if ext in {".md", ".txt"}:
        text = _safe_read_text(path)
        metadata["char_count"] = len(text)
        metadata["word_count"] = len(re.findall(r"\b\w+\b", text))
        headings, table_rows, image_count, non_empty, markdown_reasons = _detect_markdown_features(text)
        reason_codes.extend(markdown_reasons)

        clean_text = bool(text.strip())
        garble_count = text.count("\ufffd") + sum(0xE000 <= ord(c) <= 0xF8FF for c in text)
        metadata["garble_char_count"] = garble_count
        if clean_text and garble_count == 0:
            score += 2
            reason_codes.append("text-layer-present")
        elif garble_count:
            score -= 2
            reason_codes.append("garbled-text")
        if headings and text.lstrip().startswith("#"):
            score += 2
            reason_codes.append("headings-present")
        if table_rows:
            score += 1
            expected_columns: set[int] = set()
            for line in text.splitlines():
                if "|" in line:
                    cells = len([part for part in line.strip().strip("|").split("|")])
                    if cells > 1 and not re.fullmatch(r"[\s|:-]+", line):
                        expected_columns.add(cells)
            if len(expected_columns) > 1:
                score -= 1
                reason_codes.append("irregular-table-columns")
            else:
                reason_codes.append("regular-table-columns")
        # Markdown image links do not prove that image text is searchable.
        # Treat them as assets to preserve, not as evidence for a higher tier.
        if len(text.strip()) < 250 and image_count and non_empty <= 8 and not headings and not table_rows:
            score -= 2
            reason_codes.append("image-dominant-risk")
        if len(text.strip()) < 250 and not image_count and not headings and not table_rows:
            score -= 2
            reason_codes.append("thin-text")
    elif ext == ".pdf":
        score = 0
        try:
            import fitz  # type: ignore[import-not-found]
            document = fitz.open(path)
            pages = len(document)
            texts: list[str] = []
            image_dominant = 0
            table_anomalies = 0
            for page in document:
                page_text = page.get_text("text") or ""
                texts.append(page_text)
                image_area = 0.0
                for info in page.get_images(full=True):
                    image_area += sum(r.width * r.height for r in page.get_image_rects(info[0]))
                page_area = page.rect.width * page.rect.height
                if page_area and image_area / page_area >= 0.35 and len(page_text.strip()) < 200:
                    image_dominant += 1
                finder = getattr(page, "find_tables", None)
                if callable(finder):
                    try:
                        for table in getattr(finder(), "tables", []):
                            rows = table.extract()
                            widths = {len(row) for row in rows if row}
                            if len(widths) > 1:
                                table_anomalies += 1
                    except Exception:
                        table_anomalies += 1
            document.close()
            full_text = "\n".join(texts)
            garble_count = full_text.count("\ufffd") + sum(0xE000 <= ord(c) <= 0xF8FF for c in full_text)
            metadata.update({"text_chars": len(full_text), "image_dominant_pages": image_dominant,
                             "garble_char_count": garble_count, "table_anomalies": table_anomalies})
            if full_text.strip() and not garble_count:
                score += 2
                reason_codes.append("text-layer-present")
            elif garble_count:
                score -= 2
                reason_codes.append("garbled-text")
            heading_count = len(re.findall(r"(?m)^\s*(?:\d+(?:\.\d+)*\s+)?[A-Z][^\n]{3,80}$", full_text))
            if heading_count:
                score += 2
                reason_codes.append("headings-present")
            if table_anomalies:
                score -= 1
                reason_codes.append("irregular-tables")
            else:
                reason_codes.append("tables-not-known-irregular")
            if image_dominant:
                score -= 2
                reason_codes.append("image-dominant-pages")
            if pages and len(full_text.strip()) / pages < 80:
                score -= 2
                reason_codes.append("low-text-density")
        except Exception as exc:
            pages = None
            reason_codes.extend(["binary-source", "manual-review-required", "pdf-extractor-unavailable"])
            metadata["pdf_profile_error"] = str(exc)
    else:
        try:
            from .docx_extract import extract_docx_document
            with tempfile.TemporaryDirectory(prefix="brd-profile-") as temp_dir:
                doc_profile, chunks = extract_docx_document(path, temp_dir)
            text = "\n".join(chunk.markdown_text for chunk in chunks)
            score = 2 if text.strip() else -2
            if re.search(r"(?m)^#{1,6}\s", text):
                score += 2
                reason_codes.append("headings-present")
            if any(chunk.tables for chunk in chunks):
                score += 1
                reason_codes.append("tables-extracted")
            if not text.strip():
                reason_codes.extend(["no-text-layer", "manual-review-required"])
            metadata["docx_issues"] = doc_profile.issues
            metadata["docx_extraction"] = "native-xml"
            pages = None
        except Exception as exc:
            score = 0
            pages = None
            reason_codes.extend(["binary-source", "manual-review-required", "docx-extractor-unavailable"])
            metadata["docx_profile_error"] = str(exc)

    if score >= 4:
        tier = "A"
    elif score >= 1:
        tier = "B"
    else:
        tier = "C"

    if {"manual-review-required", "garbled-text"}.intersection(reason_codes) and tier != "C":
        tier = "C"

    profile = FileProfile(
        source_path=str(path),
        file_hash=file_hash,
        file_name=path.name,
        ext=ext,
        tier=tier,
        score=score,
        reason_codes=sorted(set(reason_codes)),
        pages=pages,
        metadata=metadata,
    )
    return profile


def scan_brd_directory(directory: str | Path, db_path: str | Path | None = None) -> list[FileProfile]:
    root = Path(directory).resolve()
    if not root.exists():
        raise FileNotFoundError(f"BRD directory does not exist: {root}")

    db_target = Path(db_path).resolve() if db_path is not None else None
    inventory: list[FileProfile] = []
    db = InventoryDB(db_target) if db_target is not None else None
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if db_target is not None and path.resolve() == db_target:
            continue
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        profile = classify_brd_file(path)
        inventory.append(profile)
        if db is not None:
            db.upsert(profile)
    if db is not None:
        db.close()
    return inventory
