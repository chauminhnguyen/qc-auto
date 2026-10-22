import contextlib
import io
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from capture_app.knowledge.cli import main
from capture_app.knowledge.jira import (
    JiraClient,
    JiraError,
    adf_to_markdown,
    format_jira_issues,
    normalize_issue_key,
    validate_since,
)
from capture_app.knowledge.jira_config import JiraIngestConfig
from capture_app.storage.vault import VaultWriter


class KnowledgeJiraTests(unittest.TestCase):
    def test_normalizes_keys_and_validates_since_before_network(self):
        self.assertEqual(normalize_issue_key("SD-1"), "SD-1")
        self.assertEqual(normalize_issue_key("https://site.atlassian.net/browse/sd-12?x=1"), "SD-12")
        with self.assertRaisesRegex(ValueError, "Invalid Jira issue"):
            normalize_issue_key("https://site.atlassian.net/issues/SD-1")
        self.assertEqual(validate_since("2026-10-01"), "2026-10-01")
        with self.assertRaisesRegex(ValueError, "ISO date"):
            validate_since("not-a-date")

    def test_adf_converter_handles_rich_nodes_and_unknown_content(self):
        adf = {"type": "doc", "content": [
            {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Title"}]},
            {"type": "paragraph", "content": [
                {"type": "text", "text": "bold", "marks": [{"type": "strong"}]},
                {"type": "text", "text": " link", "marks": [{"type": "link", "attrs": {"href": "https://example.test"}}]},
                {"type": "mention", "attrs": {"text": "@Ada"}},
                {"type": "inlineCard", "attrs": {"url": "https://card.test"}},
            ]},
            {"type": "bulletList", "content": [{"type": "listItem", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "item"}]}
            ]}]},
            {"type": "orderedList", "attrs": {"order": 3}, "content": [{"type": "listItem", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "step"}]}
            ]}]},
            {"type": "codeBlock", "attrs": {"language": "py"}, "content": [{"type": "text", "text": "print(1)"}]},
            {"type": "table", "content": [{"type": "tableRow", "content": [
                {"type": "tableHeader", "content": [{"type": "text", "text": "Name"}]},
                {"type": "tableCell", "content": [{"type": "text", "text": "Value"}]},
            ]}]},
            {"type": "panel", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Panel text"}]}]},
            {"type": "newUnknownNode", "content": [{"type": "text", "text": "preserved unknown text"}]},
        ]}
        markdown = adf_to_markdown(adf)
        for fragment in ("## Title", "**bold**", "[ link](https://example.test)", "@Ada",
                         "<https://card.test>", "- item", "3. step", "```py", "| Name | Value |",
                         "Panel text", "preserved unknown text"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, markdown)

    def test_search_uses_next_page_token_and_comments_use_start_at(self):
        calls = []

        def response(payload, status=200, headers=None):
            return SimpleNamespace(status_code=status, headers=headers or {}, json=lambda: payload)

        search_pages = [
            response({"issues": [{"key": "SD-2"}], "nextPageToken": "next"}),
            response({"issues": [{"key": "SD-3"}]}),
        ]
        comment_pages = [
            response({"comments": [{"id": "1", "created": "2026-01-01"}], "total": 2}),
            response({"comments": [{"id": "2", "created": "2026-01-02"}], "total": 2}),
        ]

        def get(url, **kwargs):
            calls.append((url, kwargs.get("params", {})))
            return (search_pages if "/search/jql" in url else comment_pages).pop(0)

        client = JiraClient("https://site.atlassian.net", "user@example.test", "token")
        with patch("capture_app.knowledge.jira.httpx.get", side_effect=get):
            found = client._search("parent = \"SD-1\"")
            comments = client._comments("SD-1", include_noise=True)

        self.assertEqual([item["key"] for item in found], ["SD-2", "SD-3"])
        self.assertEqual([item["id"] for item in comments], ["1", "2"])
        self.assertEqual(calls[1][1]["nextPageToken"], "next")
        self.assertEqual(calls[3][1]["startAt"], 1)

    def test_rate_limit_honors_retry_after_and_auth_error_is_clear(self):
        limited = SimpleNamespace(status_code=429, headers={"Retry-After": "2"}, json=lambda: {})
        success = SimpleNamespace(status_code=200, headers={}, json=lambda: {"ok": True})
        client = JiraClient("https://site.atlassian.net", "user", "secret", max_retries=1)
        with patch("capture_app.knowledge.jira.httpx.get", side_effect=[limited, success]), \
                patch.object(client, "_sleep") as sleep:
            self.assertEqual(client._get("/rest/api/3/issue/SD-1"), {"ok": True})
        sleep.assert_called_once_with(2.0)

        unauthorized = SimpleNamespace(status_code=401, headers={}, json=lambda: {})
        with patch("capture_app.knowledge.jira.httpx.get", return_value=unauthorized):
            with self.assertRaisesRegex(JiraError, "authentication failed") as error:
                client._get("/rest/api/3/issue/SD-1")
        self.assertNotIn("secret", str(error.exception))

    def test_traversal_uses_visited_set_and_warns_at_issue_cap(self):
        def issue(key, linked):
            return {"key": key, "fields": {"issuelinks": [
                {"type": {"name": "Relates"}, "outwardIssue": {"key": linked,
                    "fields": {"issuetype": {"name": "Task"}}}}
            ]}}

        client = JiraClient("https://site.atlassian.net", "u", "t", max_issues=2)
        rows = {"SD-1": issue("SD-1", "SD-2"), "SD-2": issue("SD-2", "SD-1")}
        with patch.object(client, "_get", side_effect=lambda path, params=None: rows[path.rsplit("/", 1)[-1]]), \
                patch.object(client, "_comments", return_value=[]), \
                patch.object(client, "_search", return_value=[]):
            result = client.fetch(["SD-1"], depth=3)
        self.assertEqual([row["key"] for row in result["issues"]], ["SD-1", "SD-2"])
        self.assertFalse(result["truncated"])

        capped = JiraClient("https://site.atlassian.net", "u", "t", max_issues=1)
        with patch.object(capped, "_get", side_effect=lambda path, params=None: rows[path.rsplit("/", 1)[-1]]), \
                patch.object(capped, "_comments", return_value=[]), \
                patch.object(capped, "_search", return_value=[]):
            result = capped.fetch(["SD-1"], depth=3)
        self.assertTrue(result["truncated"])
        self.assertIn("issue cap", format_jira_issues(result))

    def test_comment_fetch_keeps_noise_for_sync_but_formatter_filters_it(self):
        comments = [
            {"id": "1", "created": "2026-01-01", "author": {"displayName": "Ada"},
             "body": "A useful detail"},
            {"id": "2", "created": "2026-01-02", "author": {"displayName": "Build Bot"},
             "body": "+1"},
        ]
        client = JiraClient("https://site.atlassian.net", "user", "token")
        with patch.object(client, "_get", return_value={"comments": comments, "total": 2}):
            fetched = client._comments("SD-1")
        self.assertEqual([row["id"] for row in fetched], ["1", "2"])
        issue = {"key": "SD-1", "depth": 0, "parent_key": None,
                 "issue": {"key": "SD-1", "fields": {"summary": "Example"}},
                 "comments": fetched}
        formatted = format_jira_issues({"issues": [issue]})
        self.assertIn("A useful detail", formatted)
        self.assertNotIn("Build Bot", formatted)
        self.assertIn("Build Bot", format_jira_issues({"issues": [issue]}, include_noise=True))

    def test_jira_only_cli_writes_note_without_provider_and_llm(self):
        fixture = {"issues": [{
            "key": "SD-1", "depth": 0, "parent_key": None,
            "issue": {"key": "SD-1", "fields": {
                "summary": "Example ticket", "issuetype": {"name": "Task"},
                "status": {"name": "Open"}, "description": "plain description",
                "labels": [],
            }},
            "comments": [{"id": "19", "created": "2026-10-21", "author": {"displayName": "Ada"},
                           "body": "A comment"}],
        }], "truncated": False, "roots": ["SD-1"], "max_issues": 100}

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = SimpleNamespace(fetch=Mock(return_value=fixture))
            stdout = io.StringIO()
            with patch("capture_app.knowledge.jira_pipeline.JiraClient.from_env", return_value=client), \
                    patch.object(JiraIngestConfig, "load", return_value=JiraIngestConfig()), \
                    patch("capture_app.knowledge.llm.provider_from_args") as provider, \
                    contextlib.redirect_stdout(stdout):
                result = main(["ingest", "--vault", str(root / "vault"), "--jira", "SD-1"])

            self.assertEqual(result, 0)
            output = root / "vault" / "Jira" / "SD-1.md"
            content = output.read_text(encoding="utf-8")
            self.assertIn("[SOURCE: jira SD-1 issue]", content)
            self.assertIn("[SOURCE: jira SD-1 comment 19]", content)
            self.assertIn('capture_generated: "jira"', content)
            self.assertIn("fetched 1 issue(s)", stdout.getvalue())
            provider.assert_not_called()

    def test_fetch_failure_writes_no_ticket_note(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault = root / "vault"
            client = SimpleNamespace(fetch=Mock(side_effect=JiraError("Jira unavailable")))
            with patch("capture_app.knowledge.jira_pipeline.JiraClient.from_env", return_value=client), \
                    patch.object(JiraIngestConfig, "load", return_value=JiraIngestConfig()), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["ingest", "--vault", str(vault), "--jira", "SD-1"]), 1)
            self.assertFalse((vault / "Jira" / "SD-1.md").exists())
            self.assertFalse(vault.exists())

    def test_writer_updates_only_notes_it_owns(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp)
            writer = object.__new__(VaultWriter)
            writer.vault_path = vault
            writer.lock = threading.Lock()
            path = writer.write_jira_note("SD-1", "# Ticket\n\nInitial body")
            writer.write_jira_note("SD-1", "# Ticket\n\nUpdated body")
            self.assertIn("Updated body", path.read_text(encoding="utf-8"))
            path.write_text("# User note", encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, "Refusing to overwrite"):
                writer.write_jira_note("SD-1", "# Replacement")

    def test_config_file_loads_scope_and_retrieval_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "config.yaml"
            config_path.write_text(
                "knowledge_jira:\n  scope:\n    depth: 3\n    link_types: [Blocks]\n"
                "  retrieval:\n    top_k: 7\n", encoding="utf-8")
            fake_yaml = SimpleNamespace(safe_load=lambda _: {
                "knowledge_jira": {
                    "scope": {"depth": 3, "link_types": ["Blocks"]},
                    "retrieval": {"top_k": 7},
                }
            })
            with patch.dict(sys.modules, {"yaml": fake_yaml}):
                config = JiraIngestConfig.load(config_path)
        self.assertEqual(config.depth, 3)
        self.assertEqual(config.link_types, ("Blocks",))
        self.assertEqual(config.top_k, 7)
        self.assertEqual(config.max_issues, 100)

    def test_project_config_stopwords_are_yaml_strings(self):
        config = JiraIngestConfig.load(Path(__file__).parents[1] / "config.yaml")
        self.assertTrue(config.stopwords)
        self.assertTrue(all(isinstance(word, str) for word in config.stopwords))
        self.assertIn("on", config.stopwords)


if __name__ == "__main__":
    unittest.main()
