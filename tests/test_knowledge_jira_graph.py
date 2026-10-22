import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from capture_app.knowledge.jira_config import JiraIngestConfig
from capture_app.knowledge.jira_pipeline import ingest_jira_roots
from capture_app.storage.vault import VaultWriter


FIXTURE = Path(__file__).parent / "fixtures" / "jira_sd1_scope.json"


def _vault_with_references(root: Path) -> Path:
    vault = root / "ObsidianVault"
    (vault / "BRDs").mkdir(parents=True)
    (vault / "APIs" / "Account").mkdir(parents=True)
    (vault / "Params").mkdir(parents=True)
    (vault / "BRDs" / "Login.md").write_text(
        "# Login\n\nThe login button stays visible while the cookie session is active.\n",
        encoding="utf-8",
    )
    (vault / "BRDs" / "Profile.md").write_text(
        "# Profile Settings\n\nPersonal settings should let a user choose gender.\n",
        encoding="utf-8",
    )
    (vault / "APIs" / "Account" / "POST_password.md").write_text(
        '---\naliases: ["password reset", "change password"]\n---\n'
        "# POST password reset\n\nReset a user's password using an account recovery token.\n",
        encoding="utf-8",
    )
    (vault / "Params" / "user_gender.md").write_text(
        '---\naliases: ["gender", "user_gender"]\n---\n'
        "# user gender\n\nThe profile gender field is optional.\n",
        encoding="utf-8",
    )
    return vault


class KnowledgeJiraGraphTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.config = JiraIngestConfig(relative_threshold=0.0, min_matched_terms=1)

    def _run(self, vault, fixture=None):
        client = SimpleNamespace(fetch=Mock(return_value=fixture or self.fixture))
        stdout = io.StringIO()
        patcher = patch("capture_app.knowledge.jira_pipeline.JiraClient.from_env",
                        return_value=client)
        with patcher, contextlib.redirect_stdout(stdout):
            return ingest_jira_roots(vault, ["SD-1"], self.config), stdout.getvalue()

    def test_scope_writes_three_linked_issue_notes_with_independent_matches(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = _vault_with_references(Path(temp))
            result, report = self._run(vault)
            self.assertEqual(result["errors"], [])
            for key in ("SD-1", "SD-2", "SD-3"):
                self.assertTrue((vault / "Jira" / f"{key}.md").is_file())
            root = (vault / "Jira" / "SD-1.md").read_text(encoding="utf-8")
            child_gender = (vault / "Jira" / "SD-2.md").read_text(encoding="utf-8")
            child_password = (vault / "Jira" / "SD-3.md").read_text(encoding="utf-8")
            self.assertIn("[[Jira/SD-2]]", root)
            self.assertIn("[[Jira/SD-3]]", root)
            self.assertIn("[[Jira/SD-1]]", child_gender)
            self.assertIn("[[Jira/SD-1]]", child_password)
            self.assertIn("[[BRDs/Login]]", root)
            self.assertIn("[[BRDs/Profile]]", child_gender)
            self.assertTrue("[[Params/user_gender]]" in child_gender or "[[BRDs/Profile]]" in child_gender)
            self.assertIn("[[APIs/Account/POST_password]]", child_password)
            self.assertIn("[SOURCE: jira SD-1 comment 10000]", root)
            self.assertIn("anchor=", report)
            self.assertEqual(result["written"][0]["issues"], 3)

    def test_unchanged_rerun_skips_note_writes_and_keeps_sync_timestamp(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = _vault_with_references(Path(temp))
            self._run(vault)
            conn = sqlite3.connect(vault / "Knowledge" / "_state" / "ledger.sqlite")
            try:
                before = dict(conn.execute("SELECT issue_key,synced_at FROM jira_issues"))
            finally:
                conn.close()

            original = VaultWriter.write_jira_note
            calls = []

            def count_writes(writer, key, markdown):
                calls.append(key)
                return original(writer, key, markdown)

            with patch("capture_app.knowledge.jira_pipeline.JiraClient.from_env",
                       return_value=SimpleNamespace(fetch=Mock(return_value=self.fixture))), \
                    patch.object(VaultWriter, "write_jira_note", count_writes):
                with contextlib.redirect_stdout(io.StringIO()):
                    result = ingest_jira_roots(vault, ["SD-1"], self.config)
            self.assertEqual(result["errors"], [])
            self.assertEqual(calls, [])
            conn = sqlite3.connect(vault / "Knowledge" / "_state" / "ledger.sqlite")
            try:
                after = dict(conn.execute("SELECT issue_key,synced_at FROM jira_issues"))
            finally:
                conn.close()
            self.assertEqual(before, after)

    def test_missing_comment_is_marked_deleted_and_removed_from_note(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = _vault_with_references(Path(temp))
            self._run(vault)
            changed = json.loads(json.dumps(self.fixture))
            changed["issues"][0]["comments"] = []
            self._run(vault, changed)
            conn = sqlite3.connect(vault / "Knowledge" / "_state" / "ledger.sqlite")
            try:
                deleted = conn.execute(
                    "SELECT deleted FROM jira_comments WHERE issue_key='SD-1' AND comment_id='10000'"
                ).fetchone()[0]
            finally:
                conn.close()
            note = (vault / "Jira" / "SD-1.md").read_text(encoding="utf-8")
            self.assertEqual(deleted, 1)
            self.assertNotIn("ID 10000", note)

    def test_unowned_child_note_and_write_failure_do_not_commit_root_sync(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = _vault_with_references(Path(temp))
            (vault / "Jira").mkdir()
            user_note = vault / "Jira" / "SD-2.md"
            user_note.write_text("User-owned note\n", encoding="utf-8")
            result, _ = self._run(vault)
            self.assertTrue(result["errors"])
            self.assertEqual(user_note.read_text(encoding="utf-8"), "User-owned note\n")
            conn = sqlite3.connect(vault / "Knowledge" / "_state" / "ledger.sqlite")
            try:
                count = conn.execute("SELECT count(*) FROM jira_issues").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 0)

    def test_oserror_during_any_note_write_does_not_advance_sync_state(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = _vault_with_references(Path(temp))
            original = VaultWriter.write_jira_note

            def fail_sd3(writer, key, markdown):
                if key == "SD-3":
                    raise OSError("simulated disk write failure")
                return original(writer, key, markdown)

            with patch("capture_app.knowledge.jira_pipeline.JiraClient.from_env",
                       return_value=SimpleNamespace(fetch=Mock(return_value=self.fixture))), \
                    patch.object(VaultWriter, "write_jira_note", fail_sd3), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = ingest_jira_roots(vault, ["SD-1"], self.config)
            self.assertTrue(result["errors"])
            conn = sqlite3.connect(vault / "Knowledge" / "_state" / "ledger.sqlite")
            try:
                count = conn.execute("SELECT count(*) FROM jira_issues").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 0)

    def test_graph_color_group_is_added_without_removing_user_groups(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp)
            obsidian = vault / ".obsidian"
            obsidian.mkdir()
            graph = obsidian / "graph.json"
            graph.write_text(json.dumps({"custom": True, "colorGroups": [
                {"query": "path:Personal", "color": {"a": 1, "rgb": 123}}
            ]}), encoding="utf-8")
            writer = object.__new__(VaultWriter)
            writer.vault_path = vault
            writer.ensure_graph_colors()
            config = json.loads(graph.read_text(encoding="utf-8"))
            groups = {row["query"]: row["color"]["rgb"] for row in config["colorGroups"]}
            self.assertTrue(config["custom"])
            self.assertEqual(groups["path:Personal"], 123)
            self.assertEqual(groups["path:Jira"], 8700950)


if __name__ == "__main__":
    unittest.main()
