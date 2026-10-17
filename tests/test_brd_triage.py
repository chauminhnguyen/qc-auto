import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from capture_app.brd_ingest.triage import classify_brd_file, scan_brd_directory


class BrdTriageTests(unittest.TestCase):
    def test_markdown_brd_qualifies_as_tier_a(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "BRD-01.md"
            path.write_text(
                "# BRD-01\n\n"
                "## 1. Business Objective\n"
                "Users shall register and log in.\n\n"
                "## 2. Business Requirements\n"
                "| ID | Requirement | Priority |\n"
                "|----|-------------|----------|\n"
                "| BR-01.1 | Visitors can register. | Must |\n"
                "| BR-01.2 | Password is required. | Must |\n",
                encoding="utf-8",
            )

            profile = classify_brd_file(path)
            self.assertEqual(profile.tier, "A")
            self.assertGreaterEqual(profile.score, 4)
            self.assertIn("markdown-headings", profile.reason_codes)
            self.assertIn("table-like", profile.reason_codes)

    def test_image_heavy_document_is_flagged_for_manual_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "visual-brd.md"
            path.write_text(
                "![flowchart](diagram.png)\n"
                "![screen](screen.png)\n"
                "![summary](summary.png)\n",
                encoding="utf-8",
            )

            profile = classify_brd_file(path)
            self.assertEqual(profile.tier, "C")
            self.assertIn("image-dominant-risk", profile.reason_codes)

    def test_scan_directory_persists_inventory_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "BRD-01.md").write_text("# BRD-01\n\nThis document has headings, tables, and business rules.\n", encoding="utf-8")
            (root / "BRD-02.md").write_text("# BRD-02\n\nThis is also clean markdown text.\n", encoding="utf-8")
            db_path = Path(tempfile.gettempdir()) / f"brd_inventory_{uuid.uuid4().hex}.sqlite"
            try:
                profiles = scan_brd_directory(root, db_path)
                self.assertEqual(len(profiles), 2)

                conn = sqlite3.connect(db_path)
                try:
                    rows = conn.execute("SELECT file_name, tier FROM inventory ORDER BY file_name").fetchall()
                    self.assertEqual(rows[0][0], "BRD-01.md")
                    self.assertEqual(rows[0][1], "A")
                    self.assertEqual(rows[1][0], "BRD-02.md")
                    self.assertEqual(rows[1][1], "A")
                finally:
                    conn.close()
            finally:
                if db_path.exists():
                    db_path.unlink()


if __name__ == "__main__":
    unittest.main()
