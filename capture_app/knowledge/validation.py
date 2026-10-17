"""Read-only checks for the complete Obsidian vault graph and metadata."""
from __future__ import annotations

import json
import re
import zipfile
from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote

from .service import _parse_frontmatter


WIKILINK = re.compile(r"!?\[\[([^\]]+)\]\]")
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def _markdown_without_code(text: str) -> str:
    text = re.sub(r"(?ms)^\s*(```|~~~).*?^\s*\1\s*$", "", text)
    return re.sub(r"`[^`]*`", "", text)


def _target_path(vault: Path, source: Path, raw: str) -> Path | None:
    target = unquote(raw.strip().split("|", 1)[0].split("#", 1)[0]).strip()
    if not target or target.startswith(("http://", "https://", "mailto:", "obsidian://")):
        return None
    candidate = Path(target.replace("\\", "/").lstrip("/"))
    if candidate.suffix.casefold() not in {
            ".md", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf",
            ".yaml", ".yml", ".json", ".html", ".csv", ".webp"}:
        candidate = Path(str(candidate) + ".md")
    direct = (vault / candidate if target.startswith("/") else source.parent / candidate).resolve()
    if direct.exists():
        return direct
    vault_relative = (vault / candidate).resolve()
    if vault_relative.exists():
        return vault_relative
    # Obsidian resolves an unqualified basename anywhere in the vault.
    matches = list(vault.rglob(candidate.name))
    return matches[0].resolve() if len(matches) == 1 else direct


def _validate_directory(vault_path: Path) -> dict:
    """Return actionable findings without changing vault files or databases."""
    vault = Path(vault_path).resolve()
    findings: list[dict] = []
    requirement_ids: dict[str, list[str]] = defaultdict(list)
    markdown_files = sorted(vault.rglob("*.md"))

    for path in markdown_files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            findings.append({"kind": "unreadable_note", "path": path.relative_to(vault).as_posix(), "detail": str(exc)})
            continue
        rel = path.relative_to(vault).as_posix()
        body = _markdown_without_code(text)
        fields = _parse_frontmatter(text)
        req_id = fields.get("id") or fields.get("requirement_id")
        if (fields.get("type") == "requirement" or fields.get("origin") == "brd") and req_id:
            requirement_ids[str(req_id)].append(rel)

        if rel.startswith("Knowledge/_inbox/") or rel.startswith("Knowledge/Reviewed/"):
            expected = "draft" if "/_inbox/" in rel else "reviewed"
            review_state = fields.get("review_state")
            if review_state and str(review_state).casefold() != expected:
                findings.append({"kind": "review_state_folder_mismatch", "path": rel,
                                 "detail": f"review_state={review_state!r}, folder expects {expected!r}"})
            if expected == "reviewed" and not review_state:
                findings.append({"kind": "missing_review_state", "path": rel,
                                 "detail": "Reviewed notes need review_state frontmatter; epistemic status is tracked separately."})
            if expected == "reviewed" and fields.get("status") == "confirmed":
                if not fields.get("confirmed_by") or not fields.get("last_verified"):
                    findings.append({"kind": "incomplete_confirmation", "path": rel,
                                     "detail": "confirmed status requires confirmed_by and last_verified."})

        targets = [(m.group(1), "wikilink") for m in WIKILINK.finditer(body)]
        targets += [(m.group(1).strip().strip("<>").split()[0], "markdown")
                    for m in MARKDOWN_LINK.finditer(body) if m.group(1).strip()]
        for raw, syntax in targets:
            target = _target_path(vault, path, raw)
            if target is not None and not target.exists():
                findings.append({"kind": "dangling_link", "path": rel,
                                 "target": raw, "syntax": syntax})

        if rel.startswith("Params/") and "Not yet analyzed" in text:
            findings.append({"kind": "unenriched_param", "path": rel,
                             "detail": "Parameter definition is still the generated placeholder."})

    for req_id, paths in sorted(requirement_ids.items()):
        if len(paths) > 1:
            findings.append({"kind": "duplicate_requirement_id", "requirement_id": req_id,
                             "paths": paths})

    counts: dict[str, int] = defaultdict(int)
    for item in findings:
        counts[item["kind"]] += 1
    return {"vault": str(vault), "notes_scanned": len(markdown_files),
            "finding_count": len(findings), "counts": dict(sorted(counts.items())),
            "findings": findings}


def validate_vault(vault_path: Path) -> dict:
    """Validate a vault directory or a ZIP export without modifying either."""
    path = Path(vault_path).resolve()
    if not path.is_file() or not zipfile.is_zipfile(path):
        return _validate_directory(path)

    with TemporaryDirectory(prefix="obsidian-vault-check-", dir=path.parent) as temporary:
        extraction_root = Path(temporary).resolve()
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                name = member.filename.replace("\\", "/")
                relative = Path(name)
                if relative.is_absolute() or any(part == ".." for part in relative.parts):
                    raise ValueError(f"Unsafe path in vault archive: {member.filename!r}")
                destination = (extraction_root / relative).resolve()
                if extraction_root not in destination.parents and destination != extraction_root:
                    raise ValueError(f"Unsafe path in vault archive: {member.filename!r}")
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, destination.open("wb") as target:
                        target.write(source.read())

        # Exports may either store vault contents at the archive root or wrap
        # them in one top-level folder. Resolve the latter for useful paths.
        children = list(extraction_root.iterdir())
        root = children[0] if len(children) == 1 and children[0].is_dir() else extraction_root
        report = _validate_directory(root)
        report["archive"] = str(path)
        report["vault"] = str(path)
        return report


def write_validation_report(vault_path: Path, report: dict | None = None) -> Path:
    vault = Path(vault_path).resolve()
    report = report or validate_vault(vault)
    lines = ["# Vault validation", "", f"Scanned {report['notes_scanned']} Markdown notes.", ""]
    if report["findings"]:
        for finding in report["findings"]:
            kind = finding["kind"]
            path = finding.get("path", "")
            target = f" -> `{finding['target']}`" if finding.get("target") else ""
            detail = finding.get("detail", "")
            if finding.get("paths"):
                detail = ", ".join(finding["paths"])
            if finding.get("requirement_id"):
                detail = f"{finding['requirement_id']}: {detail}"
            lines.append(f"- **{kind}** `{path}`{target} {detail}".rstrip())
    else:
        lines.append("- No findings.")
    target = (vault.parent / f"{vault.name}.vault-validation.md"
              if vault.is_file() and zipfile.is_zipfile(vault)
              else vault / "Knowledge" / "Reports" / "vault-validation.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def migrate_requirement_ids(vault_path: Path) -> int:
    """Namespace distilled BRD requirement IDs by their stable source document."""
    vault = Path(vault_path).resolve()
    changed = 0
    for path in (vault / "BRDs" / "entries").rglob("*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        fields = _parse_frontmatter(text)
        if fields.get("type") != "requirement" or fields.get("origin") != "brd":
            continue
        source = str(fields.get("source", "")).split("#", 1)[0]
        source_id = str(fields.get("source_id") or fields.get("id") or "").strip()
        if not source or not source_id:
            continue
        source_stem = Path(source).stem
        canonical_id = f"{source_stem}::{source_id}"
        if fields.get("id") == canonical_id and fields.get("source_id") == source_id:
            continue
        fields["source_id"] = source_id
        fields["id"] = canonical_id
        body = text.split("---", 2)[-1].lstrip()
        path.write_text("---\n" + "".join(f"{key}: {json.dumps(value, ensure_ascii=False)}\n"
                                           for key, value in fields.items()) + "---\n\n" + body,
                        encoding="utf-8")
        changed += 1
    return changed
