from __future__ import annotations

import json

from capture_app.chat.db import ChatDB
from capture_app.chat.ingest import refresh_index
from capture_app.chat.retriever import SearchEngine


def _note(path, title, body, **fields):
    lines = ["---", f"title: {json.dumps(title)}"]
    lines.extend(f"{key}: {json.dumps(value)}" for key, value in fields.items())
    lines.extend(["---", "", f"# {title}", "", body, ""])
    target = path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")


def test_brd_neighbors_and_api_refs_expand_with_labels_and_exclude_hub(tmp_path):
    vault = tmp_path / "vault"
    parts = [f"BRDs/raw/part{i}.md" for i in range(1, 5)]
    hub = "BRDs/index/overview.md"
    _note(vault / hub, "BRD Hub", "Hub listing", type="brd-hub")
    for i, rel in enumerate(parts, 1):
        _note(vault / rel, f"Part {i}",
              "uniqueparttwo only appears here" if i == 2 else f"context for part {i}",
              origin="brd", brd_id="sample", part_id=f"sample#part-{i}",
              order=i, parent=hub,
              prev=parts[i - 2] if i > 1 else "",
              next=parts[i] if i < 4 else "",
              refs=[{"kind": "endpoint", "value": "/api/payments", "target": "APIs/payments.md"}] if i == 2 else [])
    _note(vault / "APIs" / "payments.md", "Payments API", "Endpoint details for payments.",
          path_template="/api/payments")
    refresh_index(vault)
    db = ChatDB(vault / ".chat_index" / "index.sqlite")
    try:
        seeds = SearchEngine(db).search("uniqueparttwo", limit=5)
        assert [row["source_path"] for row in seeds] == [parts[1]]
        expanded = SearchEngine(db).expand_context(seeds)
        context = [row for row in expanded if row["origin"] == "expansion"]
        paths = {row["source_path"] for row in context}
        assert {parts[0], parts[2], "APIs/payments.md"} <= paths
        assert hub not in paths
        assert all(row["context_label"].startswith("context: ") for row in context)
        assert any(row["edge_kind"] == "cross_reference" for row in context)
        assert any(row["edge_kind"] in {"prev", "next"} for row in context)
    finally:
        db.close()


def test_v2_schema_migration_preserves_chunks_and_incremental_edges(tmp_path):
    vault = tmp_path / "vault"
    a = vault / "BRDs" / "raw" / "a.md"
    b = vault / "BRDs" / "raw" / "b.md"
    _note(a, "A", "first note", brd_id="same", next="BRDs/raw/b.md",
          refs=[{"kind": "requirement_id", "value": "BR-77"}])
    _note(b, "B", "second note", brd_id="same", prev="BRDs/raw/a.md",
          refs=[{"kind": "requirement_id", "value": "BR-77"}])
    refresh_index(vault)
    db_path = vault / ".chat_index" / "index.sqlite"
    db = ChatDB(db_path)
    before = db.conn.execute("SELECT id FROM chunks ORDER BY id").fetchall()
    db.conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    db.conn.commit()
    db.close()

    migrated = ChatDB(db_path)
    try:
        assert migrated.schema_rebuilt is False
        assert migrated.conn.execute("SELECT id FROM chunks ORDER BY id").fetchall() == before
    finally:
        migrated.close()

    # The next refresh backfills graph rows without rechunking unchanged notes.
    summary = refresh_index(vault)
    assert summary.updated == 0
    db = ChatDB(db_path)
    try:
        assert db.conn.execute("SELECT COUNT(*) FROM edges WHERE kind='next'").fetchone()[0] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM edges WHERE kind='prev'").fetchone()[0] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM edges WHERE kind='shared_entity'").fetchone()[0] >= 2
        edge_a = db.conn.execute("SELECT dst FROM edges WHERE src='BRDs/raw/a.md' AND kind='next'").fetchone()[0]
        assert edge_a == "BRDs/raw/b.md"
    finally:
        db.close()

    _note(a, "A", "first note updated", brd_id="same", next="BRDs/raw/c.md")
    _note(vault / "BRDs" / "raw" / "c.md", "C", "third note")
    summary = refresh_index(vault)
    assert summary.updated == 1 and summary.added == 1
    db = ChatDB(db_path)
    try:
        assert db.conn.execute("SELECT COUNT(*) FROM edges WHERE src='BRDs/raw/a.md' AND kind='next'").fetchone()[0] == 1
        assert db.conn.execute("SELECT dst FROM edges WHERE src='BRDs/raw/a.md' AND kind='next'").fetchone()[0] == "BRDs/raw/c.md"
        assert db.conn.execute("SELECT COUNT(*) FROM edges WHERE kind='shared_entity'").fetchone()[0] == 0
    finally:
        db.close()


def test_expansion_obeys_budget_and_seed_only_mode_is_available(tmp_path):
    vault = tmp_path / "vault"
    _note(vault / "part-1.md", "Part 1", "neighbor context", next="part-2.md")
    _note(vault / "part-2.md", "Part 2", "onlyquery direct match", prev="part-1.md")
    refresh_index(vault)
    db = ChatDB(vault / ".chat_index" / "index.sqlite")
    try:
        engine = SearchEngine(db)
        seeds = engine.search("onlyquery", limit=5)
        assert len(engine.expand_context(seeds, budget_chars=1)) == 0
        assert len(engine.expand_context(seeds, budget_chars=1000)) > len(seeds)
        assert len(seeds) == 1  # the CLI's --no-expand path uses this unchanged list
    finally:
        db.close()


def test_same_note_adjacent_chunks_are_merged_in_reading_order(tmp_path):
    vault = tmp_path / "vault"
    _note(vault / "seed.md", "Seed", "uniquequery seed context [[related.md]]")
    related = vault / "related.md"
    related.parent.mkdir(parents=True, exist_ok=True)
    related.write_text(
        "# Related\n\n## Neighbor A\n\nseed context first passage\n\n"
        "## Neighbor B\n\nseed context second passage\n\n"
        "## Far section\n\nunrelated ending\n", encoding="utf-8")
    refresh_index(vault)
    db = ChatDB(vault / ".chat_index" / "index.sqlite")
    try:
        engine = SearchEngine(db)
        seeds = engine.search("uniquequery", limit=5)
        expanded = engine.expand_context(seeds)
        merged = [row for row in expanded if row.get("origin") == "expansion"]
        merged = next(row for row in merged if row["source_path"] == "related.md")
        assert "seed context first passage" in merged["text"]
        assert "seed context second passage" in merged["text"]
        assert merged["chunk_ids"] == sorted(merged["chunk_ids"])
    finally:
        db.close()
