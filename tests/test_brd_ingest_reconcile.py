from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from capture_app.brd_ingest.cli import _reconcile_missing_sources, _run_pipeline


BRD_TEXT = """# {title}

## Business Requirements

| ID | Requirement | Priority |
|---|---|---|
| {req_id} | Customers shall manage their account securely. | Must |
"""


class BrdIngestReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "BRDs"
        self.vault = self.base / "ObsidianVault"
        self.db = self.base / "brd_inventory.sqlite"
        self.root.mkdir()
        self.vault.mkdir()
        self.one = self.root / "BRD-01.md"
        self.two = self.root / "BRD-02.md"
        self.one.write_text(BRD_TEXT.format(title="BRD 01", req_id="BR-01"), encoding="utf-8")
        self.two.write_text(BRD_TEXT.format(title="BRD 02", req_id="BR-02"), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_removed_source_removes_its_rows_and_archives_its_notes(self):
        first = _run_pipeline(self.root, self.vault, self.db)
        self.assertEqual(len(first["processed"]), 2)
        with closing(sqlite3.connect(self.db)) as conn:
            old_sections = conn.execute("SELECT raw_note_path FROM sections WHERE source_path=?",
                                        (str(self.two.resolve()),)).fetchall()
            old_entries = conn.execute("SELECT content_json FROM entries WHERE source_path=?",
                                       (str(self.two.resolve()),)).fetchall()
        old_notes = [Path(row[0]) for row in old_sections]
        import json
        old_notes.extend(Path(json.loads(row[0])["markdown_path"]) for row in old_entries)
        self.assertTrue(old_notes)

        self.two.unlink()
        second = _run_pipeline(self.root, self.vault, self.db)
        self.assertEqual(len(second["removed_sources"]), 1)
        self.assertEqual(Path(second["removed_sources"][0]["source"]).name, "BRD-02.md")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0], 1)
            for table in ("sections", "entries", "pages", "assets"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE source_path=?",
                                              (str(self.two.resolve()),)).fetchone()[0], 0)
        for note in old_notes:
            self.assertFalse(note.exists())
            self.assertTrue(any((self.base / "_archive").rglob(note.name)))
        master = (self.vault / "BRDs" / "BRD Master Index.md").read_text(encoding="utf-8")
        self.assertNotIn("BRD-02", master)
        self.assertEqual(second["coverage"]["sources"], 1)

    def test_empty_source_folder_does_not_purge_inventory(self):
        _run_pipeline(self.root, self.vault, self.db)
        self.one.unlink()
        self.two.unlink()
        self.assertEqual(_reconcile_missing_sources(self.root, self.vault, self.db, set()), [])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
