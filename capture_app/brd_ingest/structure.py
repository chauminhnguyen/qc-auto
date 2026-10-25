"""Stable BRD identity, reference extraction, and managed note blocks."""
from __future__ import annotations

import json
import re
from pathlib import Path


NAV_START, NAV_END = "<!-- brd-nav -->", "<!-- /brd-nav -->"
REF_START, REF_END = "<!-- brd-refs -->", "<!-- /brd-refs -->"
_ID = re.compile(r"\b(?:BR[-_]\d+(?:[-_][A-Z0-9]+)*|[A-Z][A-Z0-9]+-\d+)\b")
_SECTION = re.compile(r"\bsee\s+section\s+(\d+(?:\.\d+)+)\b", re.I)
_ENDPOINT = re.compile(r"(?<![A-Za-z0-9])/(?:[A-Za-z0-9_.{}-]+/)+[A-Za-z0-9_.{}-]+", re.I)
_WORD = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_]{2,}(?![A-Za-z0-9_])")


def slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "-", str(value or "").casefold()).strip("-")
    return value or "section"


def brd_id(source_path: str | Path) -> str:
    return slug(Path(source_path).stem)


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _read_frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.match(r"\A---\n(.*?)\n---\n?", text, re.S)
    fields = {}
    body = text
    if match:
        body = text[match.end():]
        for line in match.group(1).splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            try:
                fields[key.strip()] = json.loads(value.strip())
            except (ValueError, TypeError):
                fields[key.strip()] = value.strip().strip("\"'")
    return fields, body


def _note_aliases(path: Path, fields: dict, body: str) -> set[str]:
    result = {path.stem, path.name, body.splitlines()[0].lstrip("# ").strip() if body else ""}
    for key in ("title", "canonical_name", "path_template", "api", "endpoint", "aliases", "id", "source_id"):
        value = fields.get(key)
        for item in value if isinstance(value, list) else [value]:
            if item:
                result.add(str(item).strip().strip("\"'`"))
    return {item for item in result if item}


def _catalog(vault: Path) -> tuple[dict[str, set[str]], dict[str, str], dict[str, str]]:
    aliases: dict[str, set[str]] = {}
    section_targets: dict[str, str] = {}
    id_targets: dict[str, str] = {}
    for folder in ("APIs", "Params", "BRDs/entries", "BRDs/raw"):
        root = vault / folder
        if not root.is_dir():
            continue
        for path in root.rglob("*.md"):
            try:
                fields, body = _read_frontmatter(path)
            except OSError:
                continue
            rel = path.relative_to(vault).as_posix()
            for name in _note_aliases(path, fields, body):
                key = normalize(name)
                if len(key) >= 4:
                    aliases.setdefault(key, set()).add(rel)
            if fields.get("section_path"):
                section_targets[normalize(str(fields["section_path"]))] = rel
                match = re.search(r"(?:^|>\s*)(\d+(?:\.\d+)+)", str(fields["section_path"]))
                if match:
                    section_targets.setdefault(normalize(match.group(1)), rel)
            for key in ("id", "source_id"):
                value = fields.get(key)
                if value:
                    normalized = normalize(str(value))
                    prior = id_targets.get(normalized)
                    id_targets[normalized] = rel if prior in (None, rel) else ""
    return aliases, section_targets, id_targets


def extract_refs(vault: str | Path, text: str) -> list[dict]:
    """Find exact deterministic IDs/endpoints/parameters and resolve note targets."""
    root = Path(vault)
    aliases, section_targets, id_targets = _catalog(root)
    # References are extracted from source prose, not from previously rendered
    # frontmatter, navigation links, managed blocks, or generated file names.
    text = re.sub(r"\A---\n.*?\n---\n?", "", text, count=1, flags=re.S)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"!?\[\[[^\]]*\]\]", " ", text)
    text = re.sub(r"(?m)^#{1,6}\s+[A-Z0-9:_-]+\s*$", " ", text)
    refs: set[tuple[str, str, str]] = set()
    for value in _ID.findall(text):
        target = id_targets.get(normalize(value), "")
        if not target and not value.upper().startswith(("BR-", "BRD-")):
            jira_path = root / "Jira" / f"{value}.md"
            if jira_path.is_file():
                target = jira_path.relative_to(root).as_posix()
        refs.add(("requirement_id" if value.upper().startswith(("BR-", "BRD-")) else "jira", value, target))
        if value.upper().startswith(("BR-", "BRD-")):
            # BRD-prefixed issue-like IDs may be requirements or business rules.
            refs.discard(("jira", value, target))
            refs.add(("requirement_id", value, target))
    for value in _SECTION.findall(text):
        refs.add(("section", value, section_targets.get(normalize(value), "")))
    for raw_value in _ENDPOINT.findall(text):
        value = raw_value.rstrip(".,;:)")
        matches = aliases.get(normalize(value), set())
        target = next(iter(matches)) if len(matches) == 1 else ""
        refs.add(("endpoint", value, target))
    # API/parameter note aliases are matched as complete normalized words only.
    for value in _WORD.findall(text):
        matches = aliases.get(normalize(value), set())
        entity_matches = {target for target in matches if target.startswith(("APIs/", "Params/"))}
        if len(entity_matches) == 1:
            refs.add(("parameter", value, next(iter(entity_matches))))
    output = [{"kind": kind, "value": value, **({"target": target} if target else {})}
              for kind, value, target in sorted(refs, key=lambda row: (row[0], row[1].casefold(), row[2]))]
    return output


def update_note(path: Path, fields_to_set: dict, nav_lines: list[str], refs: list[dict]) -> None:
    fields, body = _read_frontmatter(path)
    # Remove the legacy renderer's navigation block before writing the single
    # canonical managed navigation block.
    body = re.sub(r"<!-- brd-ingest-links:start -->.*?<!-- brd-ingest-links:end -->\n?", "", body, flags=re.S)
    body = re.sub(re.escape(NAV_START) + r".*?" + re.escape(NAV_END) + r"\n?", "", body, flags=re.S)
    body = re.sub(re.escape(REF_START) + r".*?" + re.escape(REF_END) + r"\n?", "", body, flags=re.S)
    body = re.sub(r"(?m)^> BRD: .*\n(?:\n)?", "", body, count=1)
    body = body.lstrip()
    raw_body = body
    nav_block = "\n".join([NAV_START, *nav_lines, NAV_END])
    ref_lines = [f"- {ref['kind']}: {ref['value']}" +
                 (f" [[{ref['target'].removesuffix('.md')}]]" if ref.get("target") else "")
                 for ref in refs]
    refs_block = "\n".join([REF_START, *ref_lines, REF_END])
    breadcrumb = str(fields_to_set.get("breadcrumb") or "")
    body = nav_block + "\n\n" + (f"> BRD: {breadcrumb}\n\n" if breadcrumb else "") + raw_body
    body = body.rstrip() + "\n\n" + refs_block + "\n"
    fields.update(fields_to_set)
    raw = path.read_text(encoding="utf-8", errors="replace")
    match = re.match(r"\A---\n(.*?)\n---\n?", raw, re.S)
    existing_lines = match.group(1).splitlines() if match else []
    keys = set(fields_to_set) | {"refs"}
    retained = [line for line in existing_lines
                if not any(line.startswith(f"{key}:") for key in keys)]
    additions = [f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields_to_set.items()]
    # Store refs in JSON-compatible YAML for the chat index's deterministic parser.
    additions.append(f"refs: {json.dumps(refs, ensure_ascii=False)}")
    frontmatter = "---\n" + "\n".join(retained + additions) + "\n---\n\n"
    path.write_text(frontmatter + body, encoding="utf-8")
