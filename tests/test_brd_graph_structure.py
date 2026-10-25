from __future__ import annotations

from unittest.mock import patch

from capture_app.brd_ingest.cli import _run_pipeline
from capture_app.chat.ingest import refresh_index
from capture_app.chat.graph_config import EDGE_NEXT_PREV
from capture_app.chat.db import ChatDB
from capture_app.chat.ingest import _parse_frontmatter


def test_brd_structure_is_stable_idempotent_and_indexed_as_edges(tmp_path):
    source_root = tmp_path / "BRD-source"
    vault = tmp_path / "vault"
    source_root.mkdir()
    vault.mkdir()
    source = source_root / "Account Flow.md"
    source.write_text(
        "# Account flow\n\n"
        "## 1. Overview\n\nOverview context.\n\n"
        "## 2. Requirements\n\nA profile uses /api/accounts.\n\n"
        "| ID | Requirement | Priority |\n|---|---|---|\n"
        "| BR-17 | The profile must update safely. | High |\n\n"
        "## 3. Payment\n\nPayment follows section 2.1.\n\n"
        "## 4. Audit\n\nAudit records changes.\n", encoding="utf-8")
    api = vault / "APIs" / "accounts.md"
    api.parent.mkdir(parents=True)
    api.write_text('---\ntitle: "Accounts API"\npath_template: "/api/accounts"\n---\n# Accounts API\n\nAccount endpoint.\n', encoding="utf-8")
    links = {"requirement_entity_links": [], "unresolved_references": [],
             "semantic_linking": {"accepted_links": 0}}
    with patch("capture_app.brd_ingest.cli.resolve_links", return_value=links):
        _run_pipeline(source_root, vault, tmp_path / "brd.sqlite")
        first = {path.relative_to(vault).as_posix(): path.read_bytes()
                 for path in (vault / "BRDs").rglob("*.md")}
        _run_pipeline(source_root, vault, tmp_path / "brd.sqlite")
        second = {path.relative_to(vault).as_posix(): path.read_bytes()
                  for path in (vault / "BRDs").rglob("*.md")}
    assert first == second
    section_notes = sorted((vault / "BRDs" / "raw").glob("*.md"))
    assert len(section_notes) == 5  # title and four numbered sections
    first_text = section_notes[0].read_text(encoding="utf-8")
    assert 'brd_id: "account-flow"' in first_text
    assert "<!-- brd-nav -->" in first_text and "<!-- /brd-nav -->" in first_text
    assert "<!-- brd-refs -->" in first_text and "<!-- /brd-refs -->" in first_text
    assert first_text.count("<!-- brd-nav -->") == 1
    assert "> BRD: account-flow > Account flow" in first_text

    refresh_index(vault)
    db = ChatDB(vault / ".chat_index" / "index.sqlite")
    try:
        rows = db.conn.execute("SELECT src,dst,kind,weight FROM edges WHERE kind IN ('prev','next')").fetchall()
        assert rows
        assert all(abs(float(row["weight"]) - EDGE_NEXT_PREV) < 1e-9 for row in rows)
        assert len({(row["src"], row["dst"], row["kind"]) for row in rows}) == len(rows)
        requirement_section = next(path for path in section_notes if "2.-Requirements" in path.name)
        requirement_rel = requirement_section.relative_to(vault).as_posix()
        refs, _ = _parse_frontmatter(requirement_section.read_text(encoding="utf-8"))
        assert any(ref.get("target") == "APIs/accounts.md" for ref in refs["refs"])
        assert db.conn.execute(
            "SELECT 1 FROM edges WHERE src=? AND dst='APIs/accounts.md' AND kind='cross_reference'",
            (requirement_rel,),
        ).fetchone()
    finally:
        db.close()
