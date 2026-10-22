import sqlite3
import tempfile
import unittest
from pathlib import Path

from capture_app.knowledge.retrieval import extract_anchors, index_vault, parse_markdown_chunks
from capture_app.knowledge.retrieval import rank_issue_nodes
from capture_app.knowledge.jira_config import JiraIngestConfig


class KnowledgeRetrievalIndexTests(unittest.TestCase):
    def test_anchor_extraction_classifies_identifiers_by_specificity(self):
        anchors = extract_anchors(
            'GET /api/v2/orders/{orderId} returned HTTP 404; "Save changes" '
            'failed for SD-17 and userAccountId.'
        )
        by_text = {item.text: (item.kind, item.weight) for item in anchors}
        self.assertEqual(by_text["/api/v2/orders/{orderId}"], ("api_path", 3))
        self.assertEqual(by_text["404"], ("error_code", 3))
        self.assertEqual(by_text["Save changes"], ("quoted_label", 2))
        self.assertEqual(by_text["SD-17"], ("ticket_key", 2))
        self.assertEqual(by_text["userAccountId"], ("identifier", 1))

    def test_heading_chunks_keep_breadcrumbs_and_aliases(self):
        source = (
            '---\ntitle: "Patient Access"\naliases:\n  - access\n  - patient portal\n---\n'
            '# Registration\n\nVietnamese: đăng nhập\n\n## Errors\n\nHTTP 401 is shown.\n'
        )
        chunks = parse_markdown_chunks(Path("example.md"), "BRDs/example.md", source, 8000)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0].title, "Patient Access")
        self.assertEqual(chunks[0].aliases, "access patient portal")
        self.assertEqual(chunks[1].heading, "Registration / Errors")
        self.assertIn("# Registration\n## Errors", chunks[1].body)

    def test_repeated_headings_have_distinct_stable_chunk_ids_and_respect_size_limit(self):
        source = "# Details\n\n" + ("x" * 600) + "\n\n# Details\n\nSecond section.\n"
        chunks = parse_markdown_chunks(Path("example.md"), "BRDs/example.md", source, 256)
        ids = [chunk.chunk_id for chunk in chunks]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(len(chunk.body) <= 256 for chunk in chunks))

    def test_incremental_fts_indexes_accented_text_and_partial_identifiers(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp) / "vault"
            note = vault / "BRDs" / "registration.md"
            note.parent.mkdir(parents=True)
            note.write_text(
                '---\naliases: ["accountKey", "user id"]\n---\n'
                '# Registration\n\nNgười dùng đăng nhập bằng tài khoản.\n',
                encoding="utf-8",
            )
            ledger = vault / "Knowledge" / "_state" / "ledger.sqlite"
            first = index_vault(vault, ledger)
            second = index_vault(vault, ledger)
            self.assertEqual(first["files_changed"], 1)
            self.assertEqual(second["files_changed"], 0)

            conn = sqlite3.connect(ledger)
            try:
                accent = conn.execute(
                    "SELECT chunk_id FROM vault_chunks_fts WHERE vault_chunks_fts MATCH ?",
                    ('"nguoi dung"',),
                ).fetchall()
                partial = conn.execute(
                    "SELECT chunk_id FROM vault_identifiers_fts WHERE vault_identifiers_fts MATCH ?",
                    ("accountK",),
                ).fetchall()
            finally:
                conn.close()
            self.assertTrue(accent)
            self.assertTrue(partial)

    def test_changed_file_replaces_old_fts_content(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp) / "vault"
            vault.mkdir()
            note = vault / "API.md"
            note.write_text("# API\n\nOld response behavior\n", encoding="utf-8")
            ledger = vault / "Knowledge" / "_state" / "ledger.sqlite"
            index_vault(vault, ledger)
            note.write_text("# API\n\nNew response behavior\n", encoding="utf-8")
            stats = index_vault(vault, ledger)
            self.assertEqual(stats["files_changed"], 1)
            conn = sqlite3.connect(ledger)
            try:
                old = conn.execute(
                    "SELECT count(*) FROM vault_chunks_fts WHERE vault_chunks_fts MATCH 'old'"
                ).fetchone()[0]
                new = conn.execute(
                    "SELECT count(*) FROM vault_chunks_fts WHERE vault_chunks_fts MATCH 'new'"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(old, 0)
            self.assertEqual(new, 1)

    def test_rank_issue_nodes_uses_anchor_and_bm25_and_excludes_jira_notes(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp) / "vault"
            (vault / "APIs" / "demo").mkdir(parents=True)
            (vault / "BRDs").mkdir()
            (vault / "Jira").mkdir()
            (vault / "APIs" / "demo" / "GET_auth_login.md").write_text(
                '---\naliases: ["/api/auth/login"]\n---\n# POST login\n\n'
                'Cookie session persists after sign in to the demo store.\n', encoding="utf-8")
            (vault / "BRDs" / "Login.md").write_text(
                "# Login\n\nThe login button appears after a user signs out.\n", encoding="utf-8")
            (vault / "Jira" / "SD-1.md").write_text(
                "# SD-1\n\nThe login button disappears.\n", encoding="utf-8")
            config = JiraIngestConfig(relative_threshold=0.0, min_matched_terms=1)
            results = rank_issue_nodes(
                vault, "The login button disappears at /api/auth/login after sign in.",
                config, exclude_path="Jira/SD-1.md",
            )
            paths = {row["path"] for row in results}
            self.assertIn("APIs/demo/GET_auth_login.md", paths)
            self.assertIn("BRDs/Login.md", paths)
            self.assertNotIn("Jira/SD-1.md", paths)
            api = next(row for row in results if row["path"].startswith("APIs/"))
            self.assertIn("/api/auth/login", api["matched_anchors"])
            self.assertTrue(api["matched_terms"])
            self.assertGreater(api["anchor_part"], 0)
            self.assertGreater(api["bm25_part"], 0)

    def test_graph_boost_only_reorders_direct_matches_and_no_evidence_returns_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp) / "vault"
            (vault / "BRDs").mkdir(parents=True)
            (vault / "APIs").mkdir()
            (vault / "BRDs" / "Login.md").write_text(
                "# Login\n\nlogin cookie session expires after sign out.\n", encoding="utf-8")
            (vault / "APIs" / "Login.md").write_text(
                "# Login API\n\nlogin cookie session persists after sign in.\n", encoding="utf-8")
            (vault / "BRDs" / "Login.md").write_text(
                "# Login\n\nlogin cookie session expires after sign out.\n\n"
                "Related: [[APIs/Login]]\n", encoding="utf-8")
            config = JiraIngestConfig(relative_threshold=0.0, min_matched_terms=1)
            results = rank_issue_nodes(vault, "login cookie session persists", config)
            self.assertTrue(results)
            self.assertTrue(all(row["path"] in {"BRDs/Login.md", "APIs/Login.md"} for row in results))
            self.assertTrue(any(row["graph_part"] > 0 for row in results))
            self.assertEqual(rank_issue_nodes(vault, "unrelated terminology xyzqv", config), [])


if __name__ == "__main__":
    unittest.main()
