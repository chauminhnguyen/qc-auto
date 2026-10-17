import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from capture_app.chat.crawl_ingest import index_live_runner_run, index_ui_transitions
from capture_app.chat.db import ChatDB
from capture_app.chat.ingest import build_index


class ChatCrawlIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp_dir.name) / "vault"
        self.capture_dir = self.vault / ".capture_index"
        self.capture_dir.mkdir(parents=True)
        self.capture_db_path = self.capture_dir / "index.sqlite"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_transition_table(self):
        conn = sqlite3.connect(self.capture_db_path)
        try:
            conn.execute(
                """
                CREATE TABLE ui_transitions (
                    id INTEGER PRIMARY KEY,
                    crawl_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    from_fp TEXT NOT NULL,
                    to_fp TEXT,
                    to_url TEXT,
                    action_kind TEXT,
                    action_role TEXT,
                    action_name TEXT,
                    action_value TEXT,
                    action_label TEXT,
                    action_dup_index INTEGER DEFAULT 0,
                    path_json TEXT NOT NULL,
                    triggered_apis_json TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    detail TEXT,
                    ts REAL,
                    start_url TEXT,
                    domain TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO ui_transitions VALUES (
                    17, 'crawl-1', 3, 'state-a', 'state-b',
                    'https://shop.example.test/cart', 'click', 'button',
                    'Add to wishlist', NULL, 'Add to wishlist', 0,
                    ?, ?, 1, NULL, 10.0,
                    'https://shop.example.test', 'shop.example.test'
                )
                """,
                (
                    json.dumps([{
                        "kind": "goto",
                        "role": "page",
                        "name": "https://shop.example.test/item",
                        "value": None,
                    }]),
                    json.dumps([{
                        "method": "POST",
                        "url": "https://shop.example.test/addtowishlist/42",
                        "status": 200,
                        "ref_stem": "POST_addtowishlist_42",
                    }]),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def test_build_index_imports_persisted_transitions_read_only(self):
        self._create_transition_table()
        db = build_index(self.vault)
        try:
            row = db.conn.execute(
                "SELECT * FROM chunks WHERE note_type='crawler-transition'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIn("Add to wishlist", row["body"])
            self.assertIn("POST https://shop.example.test/addtowishlist/42", row["body"])
            self.assertIn("Navigate to https://shop.example.test/item", row["body"])
            self.assertEqual(
                db.conn.execute(
                    "SELECT value FROM index_state WHERE key='indexed_ui_transitions'"
                ).fetchone()[0],
                "1",
            )
        finally:
            db.close()

    def test_live_runner_evidence_survives_a_later_index_rebuild(self):
        self.vault.mkdir(exist_ok=True)
        action = SimpleNamespace(
            kind="click",
            role="button",
            name="Add to wishlist",
            value=None,
            label="Add to wishlist",
            dup_index=0,
        )
        prior_step = SimpleNamespace(
            kind="goto",
            role="page",
            name="https://shop.example.test/item",
            value=None,
            dup_index=0,
        )
        transition = SimpleNamespace(
            action=action,
            path=[prior_step],
            apis=[{
                "method": "POST",
                "url": "https://shop.example.test/addtowishlist/42",
                "status": 200,
            }],
            ok=True,
            from_fp="state-a",
            to_fp="state-b",
            to_url="https://shop.example.test/item",
            detail=None,
        )
        index_live_runner_run(
            self.vault,
            run_id="run-1",
            session_id="session-1",
            transitions=[transition],
            discoveries=[{
                "action_label": "Add to wishlist",
                "api_method": "POST",
                "api_url": "https://shop.example.test/addtowishlist/42",
                "api_status": 200,
            }],
            summary={"good": 1, "discoveries": 1},
            report_text="Wishlist call succeeded.",
        )
        (self.vault / "Flows").mkdir()
        (self.vault / "Flows" / "sample.md").write_text(
            "# Sample flow\ncheckout\n", encoding="utf-8"
        )

        db = build_index(self.vault)
        try:
            self.assertEqual(
                db.conn.execute(
                    "SELECT COUNT(*) FROM chunks WHERE source_path LIKE 'live_runner/%'"
                ).fetchone()[0],
                3,
            )
            self.assertEqual(
                db.conn.execute(
                    "SELECT COUNT(*) FROM chunks WHERE note_type='crawler-transition'"
                ).fetchone()[0],
                0,
            )
            results = db.search("wishlist")
            self.assertTrue(any("live_runner/run-1/" in row["source_path"] for row in results))
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
