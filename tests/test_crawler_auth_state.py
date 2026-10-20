import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import Mock
from unittest.mock import patch

from capture_app.core.ax_tree import Action
from capture_app.crawling.best_first_crawler import BestFirstCrawler
from capture_app.crawling.best_first_crawler import Node
from capture_app.crawling import ui_transitions
from capture_app.workflows import live_runner


class CrawlerAuthStateTests(unittest.TestCase):
    def test_auth_state_is_passed_to_playwright_context(self):
        with tempfile.TemporaryDirectory() as directory:
            auth_state = Path(directory) / "state.json"
            auth_state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
            expected_page = object()
            context = Mock()
            context.new_page.return_value = expected_page
            browser = Mock()
            browser.new_context.return_value = context

            crawler = BestFirstCrawler(None, auth_state=auth_state)
            with patch.dict("os.environ", {
                "WRITE_SAFE_DECISIONS": str(Path(directory) / "decisions.json"),
                "WRITE_SAFE_REPORT_DIR": str(Path(directory) / "reports"),
            }):
                context_result = crawler._new_safe_context(browser, "https://example.test")

            self.assertIs(context_result, context)
            browser.new_context.assert_called_once_with(
                service_workers="block", storage_state=str(auth_state),
            )

    def test_no_auth_state_keeps_fresh_context_behavior(self):
        expected_page = object()
        context = Mock()
        context.new_page.return_value = expected_page
        browser = Mock()
        browser.new_context.return_value = context

        crawler = BestFirstCrawler(None)
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
            "WRITE_SAFE_DECISIONS": str(Path(directory) / "decisions.json"),
            "WRITE_SAFE_REPORT_DIR": str(Path(directory) / "reports"),
        }):
            context_result = crawler._new_safe_context(browser, "https://example.test")

        self.assertIs(context_result, context)
        browser.new_context.assert_called_once_with(service_workers="block")

    def test_no_auth_state_waits_for_manual_login_and_uses_visible_browser(self):
        crawler = BestFirstCrawler(None)
        crawler._settle = Mock()
        page = Mock()
        with patch("builtins.input", return_value="") as prompt:
            self.assertTrue(crawler._wait_for_manual_login(page))
        prompt.assert_called_once()
        crawler._settle.assert_called_once_with(page)
        self.assertFalse(crawler._browser_headless())

    def test_auth_state_skips_manual_login_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            auth_state = Path(directory) / "state.json"
            auth_state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
            crawler = BestFirstCrawler(None, auth_state=auth_state)
            crawler._settle = Mock()
            with patch("builtins.input") as prompt:
                self.assertTrue(crawler._wait_for_manual_login(Mock()))
            prompt.assert_not_called()
            crawler._settle.assert_not_called()
            self.assertTrue(crawler._browser_headless())

    def test_interactive_action_prompt_approves_or_declines_without_showing_value(self):
        crawler = BestFirstCrawler(None, interactive=True)
        action = Action("fill", "textbox", "Search", "private input")
        with patch("builtins.input", return_value="y") as prompt:
            self.assertTrue(crawler._approve_action(action))
        self.assertNotIn("private input", prompt.call_args.args[0])
        with patch("builtins.input", return_value="n"):
            self.assertFalse(crawler._approve_action(action))

    def test_interactive_with_auth_state_opens_visible_browser_without_login_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            auth_state = Path(directory) / "state.json"
            auth_state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
            crawler = BestFirstCrawler(None, auth_state=auth_state, interactive=True)
            crawler._settle = Mock()
            with patch("builtins.input") as prompt:
                self.assertTrue(crawler._wait_for_manual_login(Mock()))
            prompt.assert_not_called()
            self.assertFalse(crawler._browser_headless())

    def test_interactive_approval_does_not_override_submit_or_delete_safeguards(self):
        crawler = BestFirstCrawler(None, interactive=True)
        crawler.use_ledger = False
        node = Node("screen", "https://example.test", [], 0, [])
        for action, flag_name in (
            (Action("click", "button", "Save"), "allow_submit"),
            (Action("click", "button", "Delete item"), "allow_delete"),
        ):
            with self.subTest(action=action.name), patch(
                "capture_app.crawling.best_first_crawler.rank_actions",
                return_value=[(1, action)],
            ), patch("builtins.input", return_value="y") as prompt:
                picks, _ = crawler._pick(node, [])
            self.assertEqual(picks, [])
            prompt.assert_not_called()
            setattr(crawler, flag_name, True)
            node.attempted.clear()
            with patch(
                "capture_app.crawling.best_first_crawler.rank_actions",
                return_value=[(1, action)],
            ):
                picks, _ = crawler._pick(node, [])
            self.assertEqual(picks, [(1, action)])
            setattr(crawler, flag_name, False)

    def test_missing_auth_state_fails_before_context_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            browser = Mock()
            crawler = BestFirstCrawler(None, auth_state=missing)

            with self.assertRaisesRegex(FileNotFoundError, "Auth state file not found"):
                crawler._new_safe_context(browser, "https://example.test")

            browser.new_context.assert_not_called()

    def test_malformed_auth_state_error_does_not_expose_contents(self):
        secret_fragment = "private-cookie-value"
        with tempfile.TemporaryDirectory() as directory:
            auth_state = Path(directory) / "state.json"
            auth_state.write_text("{}", encoding="utf-8")
            browser = Mock()
            browser.new_context.side_effect = ValueError(secret_fragment)
            crawler = BestFirstCrawler(None, auth_state=auth_state)

            with patch.dict("os.environ", {
                "WRITE_SAFE_DECISIONS": str(Path(directory) / "decisions.json"),
                "WRITE_SAFE_REPORT_DIR": str(Path(directory) / "reports"),
            }), self.assertRaises(ValueError) as raised:
                crawler._new_safe_context(browser, "https://example.test")

            self.assertIn("valid Playwright storage-state file", str(raised.exception))
            self.assertNotIn(secret_fragment, str(raised.exception))

    def test_ui_transitions_cli_accepts_and_forwards_auth_state(self):
        crawler = Mock()
        crawler.seed.return_value = ("https://example.test", [])
        crawler.crawl.return_value = []
        with patch.object(sys, "argv", [
            "ui_transitions.py", "crawl", "--vault", "vault-dir",
            "--auth-state", "state.json", "--interactive",
        ]), patch("capture_app.storage.vault.VaultWriter"), \
             patch("capture_app.crawling.best_first_crawler.BestFirstCrawler", return_value=crawler) as crawler_type, \
             patch.object(ui_transitions, "record_transitions", return_value={
                 "rows_written": 0, "apis_ingested": 0, "ingest_errors": 0,
             }):
            ui_transitions.main()

        self.assertEqual(crawler_type.call_args.kwargs["auth_state"], "state.json")
        self.assertTrue(crawler_type.call_args.kwargs["interactive"])

    def test_live_runner_cli_accepts_and_forwards_auth_state(self):
        runner = Mock()
        runner.run.return_value = {
            "session_id": "s", "standard_states": 0, "transitions": 0,
            "good": 0, "bad": 0, "skipped_submit": 0, "held_deletes": 0,
            "fail_cases": 0, "discoveries": [], "graph_html": "graph.html",
            "flow_diagram_html": "", "chat_evidence": 0, "run_id": "r",
        }
        with patch.object(sys, "argv", [
            "live_runner.py", "--auth-state", "state.json", "--interactive",
        ]), patch.object(live_runner, "VaultWriter"), \
             patch.object(live_runner, "LiveRunner", return_value=runner) as runner_type:
            live_runner.main()

        self.assertEqual(runner_type.call_args.kwargs["auth_state"], "state.json")
        self.assertTrue(runner_type.call_args.kwargs["interactive"])


if __name__ == "__main__":
    unittest.main()
