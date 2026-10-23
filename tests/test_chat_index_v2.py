from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from capture_app.chat.db import ChatDB
from capture_app.chat.ingest import TARGET_CHUNK_CHARS, _split_long, refresh_index
from capture_app.chat.retriever import SearchEngine


def _paths(db: ChatDB) -> dict[str, list[int]]:
    rows = db.conn.execute("""
        SELECT f.path,c.id FROM files f JOIN chunks c ON c.file_id=f.id
        ORDER BY f.path,c.ord
    """).fetchall()
    result: dict[str, list[int]] = {}
    for row in rows:
        result.setdefault(row["path"], []).append(row["id"])
    return result


def test_incremental_noop_and_edit_preserve_unaffected_ids(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "one.md").write_text("# One\n\nold content", encoding="utf-8")
    (vault / "two.md").write_text("# Two\n\nstable content", encoding="utf-8")
    first = refresh_index(vault)
    db = ChatDB(vault / ".chat_index" / "index.sqlite")
    ids = _paths(db)
    db.close()

    second = refresh_index(vault)
    assert second.updated == 0
    assert second.unchanged == 2
    db = ChatDB(vault / ".chat_index" / "index.sqlite")
    assert _paths(db) == ids
    db.close()

    (vault / "one.md").write_text("# One\n\nnew changed content", encoding="utf-8")
    edited = refresh_index(vault)
    assert edited.updated == 1
    db = ChatDB(vault / ".chat_index" / "index.sqlite")
    new_ids = _paths(db)
    assert new_ids["two.md"] == ids["two.md"]
    assert new_ids["one.md"] != ids["one.md"]
    db.close()


def test_touch_without_content_change_updates_metadata_without_rechunk(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "touch.md"
    note.write_text("# Touch\n\nsame body", encoding="utf-8")
    refresh_index(vault)
    db = ChatDB(vault / ".chat_index" / "index.sqlite")
    chunk_id = _paths(db)["touch.md"][0]
    db.close()
    stat = note.stat()
    import os
    os.utime(note, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000))
    summary = refresh_index(vault)
    assert summary.updated == 0 and summary.unchanged == 1
    db = ChatDB(vault / ".chat_index" / "index.sqlite")
    assert _paths(db)["touch.md"] == [chunk_id]
    db.close()


def test_deleted_and_excluded_notes_are_removed_from_fts(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "gone.md").write_text("# Gone\n\nunique deleted phrase", encoding="utf-8")
    (vault / "skip-me.md").write_text("# Skip\n\nexcluded secret phrase", encoding="utf-8")
    (vault / ".chatindexignore").write_text("# comment\nskip-*.md\n", encoding="utf-8")
    for directory in (".chat_index", ".obsidian", ".trash", ".git"):
        folder = vault / directory
        folder.mkdir()
        (folder / "hidden.md").write_text("hidden forbidden text", encoding="utf-8")
    (vault / "not-markdown.txt").write_text("not markdown", encoding="utf-8")
    refresh_index(vault)
    (vault / "gone.md").unlink()
    summary = refresh_index(vault, excludes=["other-*.md"])
    assert summary.removed == 1
    db = ChatDB(vault / ".chat_index" / "index.sqlite")
    assert db.search("unique deleted phrase") == []
    assert db.search("excluded secret phrase") == []
    paths = {r[0] for r in db.conn.execute("SELECT path FROM files")}
    assert paths == set()
    db.close()


def test_chunking_strips_frontmatter_and_ignores_headings_in_fences(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "chunked.md"
    note.write_text(
        "---\r\ntitle: 'Custom title'\r\n---\r\n"
        "# Custom title\r\n\r\n## Parent\r\n\r\n~~~python\r\n## fake heading\r\n# fake too\r\n~~~\r\n\r\n"
        "### Child\r\n\r\n" + ("A long paragraph with words. " * 100), encoding="utf-8-sig")
    refresh_index(vault)
    db = ChatDB(vault / ".chat_index" / "index.sqlite")
    rows = db.conn.execute("SELECT heading_path,text FROM chunks ORDER BY ord").fetchall()
    assert rows
    assert all("Custom title" in row["heading_path"] for row in rows)
    assert all("fake heading" not in row["heading_path"] for row in rows)
    assert all(len(row["text"]) <= TARGET_CHUNK_CHARS for row in rows)
    assert all("title: 'Custom title'" not in row["text"] for row in rows)
    assert any(row["heading_path"] == "Custom title > Parent > Child" for row in rows)
    db.close()


def test_long_fenced_block_is_kept_indivisible():
    block = "```text\n" + ("code line\n" * 200) + "```"
    assert _split_long(block) == [block]


def test_vietnamese_accent_insensitive_and_query_special_characters(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "vietnam.md").write_text("# Ghi chú\n\nThanh toán đơn hàng thành công.", encoding="utf-8")
    refresh_index(vault)
    db = ChatDB(vault / ".chat_index" / "index.sqlite")
    assert db.search('"thanh-toan:*')
    db.close()


def test_old_schema_rebuild_and_full_build_temp_cleanup(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# New note\n\nrebuild contents", encoding="utf-8")
    db_path = vault / ".chat_index" / "index.sqlite"
    db_path.parent.mkdir()
    old = sqlite3.connect(db_path)
    old.execute("CREATE TABLE chunks(id TEXT PRIMARY KEY, body TEXT)")
    old.execute("CREATE VIRTUAL TABLE fts_chunks USING fts5(body)")
    old.commit()
    old.close()
    summary = refresh_index(vault)
    assert summary.schema_rebuilt
    assert not (db_path.parent / "index.sqlite.tmp").exists()
    summary = refresh_index(vault, full=True)
    assert not (db_path.parent / "index.sqlite.tmp").exists()
    assert summary.total_chunks == 1


def test_full_build_failure_removes_temporary_database(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\n\ncontents", encoding="utf-8")
    with patch("capture_app.chat.ingest._insert_file", side_effect=RuntimeError("forced failure")):
        with pytest.raises(RuntimeError, match="forced failure"):
            refresh_index(vault, full=True)
    assert not (vault / ".chat_index" / "index.sqlite.tmp").exists()


def test_ask_retrieval_caps_at_two_chunks_per_note(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "many.md").write_text(
        "# Many\n\n" + "\n\n".join(f"## Section {i}\n\nsharedneedle section {i} detail" for i in range(6)),
        encoding="utf-8")
    refresh_index(vault)
    db = ChatDB(vault / ".chat_index" / "index.sqlite")
    results = SearchEngine(db).search("sharedneedle", limit=10)
    assert len(results) <= 2
    db.close()
