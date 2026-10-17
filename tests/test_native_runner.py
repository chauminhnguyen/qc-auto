import json
import tempfile
import unittest
from pathlib import Path

from capture_app.storage.vault import VaultWriter
from capture_app.workflows.native_runner import NativeReplayRunner


class FakeRequest:
    def __init__(self, url, failure=""):
        self.url = url
        self.method = "POST"
        self.resource_type = "xhr"
        self.failure = failure


class FakeResponse:
    def __init__(self, url, status):
        self.request = FakeRequest(url)
        self.url = url
        self.status = status


class FakeLocator:
    def __init__(self, page, selector=""):
        self.page = page
        self.selector = selector

    def count(self):
        return self.page.selector_counts.get(self.selector, 1)

    def click(self, **_kwargs):
        self.page.click_count += 1
        self.page.clicked_selectors.append(self.selector)
        if self.selector == "#add":
            url = "https://shop.example.test/api/cart"
            self.page.emit_api_request(url)

    def scroll_into_view_if_needed(self, **_kwargs):
        self.page.scrolled_selectors.append(self.selector)

    def is_visible(self):
        return self.page.selector_visibility.get(self.selector, True)

    def evaluate(self, _expression):
        self.page.marked_selectors.append(self.selector)


class FakePage:
    def __init__(
        self,
        response_status=200,
        emit_response=True,
        emit_request=True,
        request_failure="",
        response_delay_ms=0,
    ):
        self.url = "about:blank"
        self.response_status = response_status
        self.emit_response = emit_response
        self.emit_request = emit_request
        self.request_failure = request_failure
        self.response_delay_ms = response_delay_ms
        self.waited_ms = 0
        self.pending_response = None
        self.click_count = 0
        self.handlers = {}
        self.visited = []
        self.screenshots = []
        self.selector_counts = {}
        self.selector_visibility = {}
        self.clicked_selectors = []
        self.scrolled_selectors = []
        self.marked_selectors = []
        self.goto_error = None
        self.screenshot_style_error = False
        self.screenshot_error = None
        self.style_tags = []

    def on(self, event_name, handler):
        self.handlers[event_name] = handler

    @property
    def response_handler(self):
        return self.handlers["response"]

    @property
    def request_handler(self):
        return self.handlers["request"]

    @property
    def request_failed_handler(self):
        return self.handlers["requestfailed"]

    def goto(self, url, **_kwargs):
        if self.goto_error:
            raise self.goto_error
        self.visited.append(url)
        self.url = url

    def locator(self, selector):
        return FakeLocator(self, selector)

    def get_by_role(self, _role, **_kwargs):
        return FakeLocator(self, "accessible-role")

    def wait_for_timeout(self, _milliseconds):
        self.waited_ms += _milliseconds
        if (self.pending_response and self.waited_ms >= self.response_delay_ms
                and self.emit_response):
            url = self.pending_response
            self.pending_response = None
            self.response_handler(FakeResponse(url, self.response_status))

    def emit_api_request(self, url):
        request = FakeRequest(url, self.request_failure or "")
        if self.emit_request:
            self.request_handler(request)
        if self.request_failure:
            self.request_failed_handler(request)
        elif self.emit_response:
            if self.response_delay_ms:
                self.pending_response = url
            else:
                self.response_handler(FakeResponse(url, self.response_status))

    def wait_for_load_state(self, _state, **_kwargs):
        pass

    def add_style_tag(self, *, content):
        self.style_tags.append(content)

    def screenshot(self, path, **kwargs):
        if self.screenshot_error:
            raise self.screenshot_error
        if self.screenshot_style_error and "style" in kwargs:
            raise TypeError("unsupported screenshot style option")
        screenshot = Path(path)
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        screenshot.write_bytes(b"fake screenshot")
        self.screenshots.append(screenshot)


class NativeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.vault = VaultWriter(self.root / "vault")
        self.vault.start_session("session-1", "https://shop.example.test/")
        self.vault.record_step("session-1", {
            "step_type": "tap",
            "action_id": "action-1",
            "selector": "#add",
            "label": "Add to wishlist",
            "role": "button",
            "page_url": "https://shop.example.test/",
            "ts": 1000,
        })
        self.vault.conn.execute(
            """INSERT INTO session_api_events
               (session_id, seq, ref_stem, domain, method, status, request_url, action_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("session-1", 1, "POST_add_to_wishlist", "shop.example.test", "POST", 200,
             "https://shop.example.test/api/cart", "action-1"),
        )
        self.vault.conn.commit()
        self.report_dir = self.root / "reports"

    def tearDown(self):
        self.vault.conn.close()
        self.temp_dir.cleanup()

    def test_replays_captured_step_and_matches_api_event(self):
        page = FakePage(response_status=200)
        runner = NativeReplayRunner(self.vault, allow_submit=True, report_dir=self.report_dir)

        result = runner.run("session-1", page=page)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["passed_steps"], 1)
        self.assertEqual(result["api_observations"][0]["status"], 200)
        self.assertTrue(Path(result["report_path"]).is_file())
        self.assertTrue(Path(result["json_path"]).is_file())

    def test_status_mismatch_stops_and_saves_screenshot(self):
        page = FakePage(response_status=503)
        runner = NativeReplayRunner(self.vault, allow_submit=True, report_dir=self.report_dir)

        result = runner.run("session-1", page=page)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure"]["kind"], "status_mismatch")
        self.assertTrue(result["failure"]["screenshot"])
        self.assertEqual(len(page.screenshots), 1)
        report = Path(result["report_path"]).read_text(encoding="utf-8")
        self.assertIn("diagnostic hypothesis", report)
        self.assertIn("Captured status 200; replay returned 503", report)

    def test_screenshot_style_failure_uses_redacted_css_fallback(self):
        page = FakePage(response_status=503)
        page.screenshot_style_error = True
        runner = NativeReplayRunner(self.vault, allow_submit=True, report_dir=self.report_dir)

        result = runner.run("session-1", page=page)

        screenshot_path = self.report_dir / result["failure"]["screenshot"]
        self.assertTrue(screenshot_path.is_file())
        self.assertEqual(len(page.screenshots), 1)
        self.assertTrue(page.style_tags)
        self.assertIn("redacted-CSS fallback", result["failure"]["screenshot_note"])

    def test_screenshot_failure_reason_is_written_to_report(self):
        page = FakePage(response_status=503)
        page.screenshot_error = RuntimeError("browser context is closed")
        runner = NativeReplayRunner(self.vault, allow_submit=True, report_dir=self.report_dir)

        result = runner.run("session-1", page=page)

        self.assertFalse(result["failure"]["screenshot"])
        self.assertIn("browser context is closed", result["failure"]["screenshot_note"])
        report = Path(result["report_path"]).read_text(encoding="utf-8")
        self.assertIn("browser context is closed", report)

    def test_missing_captured_api_call_fails_and_saves_screenshot(self):
        page = FakePage(emit_response=False, emit_request=False)
        runner = NativeReplayRunner(self.vault, allow_submit=True, report_dir=self.report_dir)

        result = runner.run("session-1", page=page)

        self.assertEqual(result["failure"]["kind"], "missing_api")
        self.assertTrue(result["failure"]["screenshot"])
        self.assertIn("was observed after this action", result["failure"]["detail"])
        self.assertIn("capture recorded HTTP 200", result["failure"]["detail"])

    def test_interactive_review_continues_at_next_step_and_records_override(self):
        self.vault.record_step("session-1", {
            "step_type": "tap",
            "selector": "#after",
            "label": "Next action",
            "role": "button",
            "page_url": "https://shop.example.test/",
            "ts": 2000,
        })
        page = FakePage(emit_response=False, emit_request=False)
        runner = NativeReplayRunner(
            self.vault,
            allow_submit=True,
            report_dir=self.report_dir,
            interactive_review=True,
            review_input=lambda _prompt: "CONTINUE",
        )

        result = runner.run("session-1", page=page)

        self.assertEqual(result["status"], "completed_with_overrides")
        self.assertEqual(result["overridden_steps"], 1)
        self.assertEqual(result["failed_steps"], 0)
        self.assertEqual(page.clicked_selectors, ["#add", "#after"])
        self.assertEqual(result["steps"][0]["status"], "overridden")
        self.assertEqual(result["steps"][1]["status"], "passed")
        self.assertTrue(Path(self.report_dir / result["overrides"][0]["screenshot"]).is_file())
        report = Path(result["report_path"]).read_text(encoding="utf-8")
        self.assertIn("COMPLETED WITH HUMAN OVERRIDE", report)
        self.assertIn("not clean passes", report)
        self.assertIn("View screenshot", report)

    def test_interactive_review_rejection_stops_before_next_step(self):
        self.vault.record_step("session-1", {
            "step_type": "tap",
            "selector": "#after",
            "label": "Next action",
            "role": "button",
            "page_url": "https://shop.example.test/",
            "ts": 2000,
        })
        page = FakePage(emit_response=False, emit_request=False)
        runner = NativeReplayRunner(
            self.vault,
            allow_submit=True,
            report_dir=self.report_dir,
            interactive_review=True,
            review_input=lambda _prompt: "",
        )

        result = runner.run("session-1", page=page)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["overridden_steps"], 0)
        self.assertEqual(page.clicked_selectors, ["#add"])
        self.assertEqual(result["steps"][-1]["status"], "not_run")

    def test_interactive_review_does_not_prompt_without_screenshot(self):
        page = FakePage(emit_response=False, emit_request=False)
        page.screenshot_error = RuntimeError("screenshot backend unavailable")
        prompt_calls = []
        runner = NativeReplayRunner(
            self.vault,
            allow_submit=True,
            report_dir=self.report_dir,
            interactive_review=True,
            review_input=lambda prompt: prompt_calls.append(prompt) or "CONTINUE",
        )

        result = runner.run("session-1", page=page)

        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["failure"]["screenshot"])
        self.assertEqual(prompt_calls, [])

    def test_interactive_review_cannot_override_safety_hold(self):
        page = FakePage()
        prompt_calls = []
        runner = NativeReplayRunner(
            self.vault,
            report_dir=self.report_dir,
            interactive_review=True,
            review_input=lambda prompt: prompt_calls.append(prompt) or "CONTINUE",
        )

        result = runner.run("session-1", page=page)

        self.assertEqual(result["failure"]["kind"], "held")
        self.assertTrue(result["failure"]["screenshot"])
        self.assertEqual(prompt_calls, [])
        self.assertEqual(page.clicked_selectors, [])

    def test_expected_api_request_started_but_no_response_is_reported(self):
        page = FakePage(emit_response=False)
        runner = NativeReplayRunner(self.vault, allow_submit=True, report_dir=self.report_dir)

        result = runner.run("session-1", page=page)

        self.assertEqual(result["failure"]["kind"], "api_no_response")
        self.assertIn("was sent, but no response arrived", result["failure"]["detail"])

    def test_waits_for_delayed_expected_api_response_before_failing(self):
        page = FakePage(response_delay_ms=1200)
        runner = NativeReplayRunner(self.vault, allow_submit=True, report_dir=self.report_dir)

        result = runner.run("session-1", page=page)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["api_observations"][0]["status"], 200)
        self.assertGreaterEqual(page.waited_ms, 1200)
        self.assertLess(page.waited_ms, 5000)

    def test_expected_api_response_timeout_is_bounded_at_five_seconds(self):
        page = FakePage(emit_response=False)
        runner = NativeReplayRunner(self.vault, allow_submit=True, report_dir=self.report_dir)

        result = runner.run("session-1", page=page)

        self.assertEqual(result["failure"]["kind"], "api_no_response")
        self.assertGreaterEqual(page.waited_ms, 5000)
        self.assertLessEqual(page.waited_ms, 5600)

    def test_expected_api_request_network_failure_is_reported(self):
        page = FakePage(request_failure="net::ERR_CONNECTION_RESET")
        runner = NativeReplayRunner(self.vault, allow_submit=True, report_dir=self.report_dir)

        result = runner.run("session-1", page=page)

        self.assertEqual(result["failure"]["kind"], "api_request_failed")
        self.assertIn("was sent but failed", result["failure"]["detail"])
        self.assertIn("ERR_CONNECTION_RESET", result["failure"]["detail"])

    def test_mutating_api_is_held_without_opt_in(self):
        page = FakePage()
        runner = NativeReplayRunner(self.vault, report_dir=self.report_dir)

        result = runner.run("session-1", page=page)

        self.assertEqual(result["failure"]["kind"], "held")
        self.assertEqual(page.click_count, 0)
        self.assertIn("--allow-submit", result["failure"]["detail"])

    def test_missing_fixture_captures_page_before_held_prefix_action(self):
        self.vault.record_step("session-1", {
            "step_type": "input",
            "action_id": "action-2",
            "label": "Email address",
            "value": "${TEST_EMAIL_ADDRESS}",
            "input_type": "email",
            "role": "textbox",
            "page_url": "https://shop.example.test/",
        })
        page = FakePage()
        runner = NativeReplayRunner(self.vault, report_dir=self.report_dir)

        result = runner.run("session-1", page=page)

        self.assertEqual(result["failure"]["kind"], "fixture_missing")
        self.assertEqual(page.visited, ["https://shop.example.test/"])
        self.assertEqual(page.click_count, 0)
        self.assertTrue(result["failure"]["screenshot"])
        self.assertIn("earlier stop state", result["failure"]["screenshot_note"])
        self.assertNotIn("person@example.test", Path(result["json_path"]).read_text(encoding="utf-8"))

    def test_unlabeled_tap_replays_when_captured_selector_is_unique(self):
        self.vault.record_step("session-1", {
            "step_type": "tap",
            "action_id": "action-2",
            "selector": "img.unlabeled",
            "label": "",
            "tag": "img",
            "page_url": "https://shop.example.test/wishlist",
        })
        page = FakePage()
        runner = NativeReplayRunner(self.vault, allow_submit=True, report_dir=self.report_dir)

        result = runner.run("session-1", page=page)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(page.clicked_selectors, ["#add", "img.unlabeled"])
        self.assertEqual(result["passed_steps"], 2)

    def test_unlabeled_tap_with_ambiguous_selector_reports_match_count(self):
        self.vault.record_step("session-1", {
            "step_type": "tap",
            "action_id": "action-2",
            "selector": "img.ambiguous",
            "label": "",
            "tag": "img",
            "page_url": "https://shop.example.test/",
        })
        page = FakePage()
        page.selector_counts["img.ambiguous"] = 2
        runner = NativeReplayRunner(self.vault, allow_submit=True, report_dir=self.report_dir)

        result = runner.run("session-1", page=page)

        self.assertEqual(result["failure"]["kind"], "ambiguous_target")
        self.assertIn("matched 2 elements", result["failure"]["detail"])
        self.assertEqual(page.clicked_selectors, ["#add"])
        self.assertEqual(page.marked_selectors, [])
        self.assertTrue(result["failure"]["screenshot"])
        self.assertIn("selector matched 2 elements", result["failure"]["screenshot_note"])
        report = Path(result["report_path"]).read_text(encoding="utf-8")
        self.assertIn("matched multiple elements", report)
        self.assertIn("the runner refused to click an arbitrary one", report)

    def test_redacted_accessible_name_does_not_hide_selector_ambiguity(self):
        self.vault.record_step("session-1", {
            "step_type": "tap",
            "action_id": "action-2",
            "selector": "a.account",
            "label": "[REDACTED:email]",
            "tag": "a",
            "page_url": "https://shop.example.test/",
        })
        page = FakePage()
        page.selector_counts["a.account"] = 2
        runner = NativeReplayRunner(self.vault, allow_submit=True, report_dir=self.report_dir)

        result = runner.run("session-1", page=page)

        self.assertEqual(result["failure"]["kind"], "ambiguous_target")
        self.assertIn("selector 'a.account' matched 2 elements", result["failure"]["detail"])
        self.assertIn("accessible name is redacted", result["failure"]["detail"])
        self.assertEqual(page.clicked_selectors, ["#add"])
        self.assertIn("matched 2 elements instead of one", result["failure"]["screenshot_note"])
        report = Path(result["report_path"]).read_text(encoding="utf-8")
        self.assertIn("selector `a.account` matched multiple elements", report)
        self.assertIn("matched 2 elements", report)
        self.assertNotIn("No link matched the captured label", report)

    def test_preflight_report_includes_sanitized_diagnostic_navigation_error(self):
        self.vault.record_step("session-1", {
            "step_type": "tap",
            "action_id": "action-2",
            "selector": "",
            "label": "",
            "tag": "img",
            "page_url": "https://shop.example.test/",
        })
        page = FakePage()
        page.goto_error = RuntimeError("net::ERR_CONNECTION_REFUSED https://shop.example.test/?token=secret")
        runner = NativeReplayRunner(self.vault, allow_submit=True, report_dir=self.report_dir)

        result = runner.run("session-1", page=page)

        note = result["failure"]["screenshot_note"]
        self.assertIn("ERR_CONNECTION_REFUSED", note)
        self.assertIn("[REDACTED]", note)
        self.assertNotIn("token=secret", note)
        self.assertFalse(result["failure"]["screenshot"])
        report = Path(result["report_path"]).read_text(encoding="utf-8")
        self.assertIn("failed before it could start replaying captured actions", report)
        self.assertIn("This is a browser startup/navigation error, not captured step 0.", report)
        self.assertNotIn("**Step 0:", report)
        self.assertNotIn("Accessible label: Navigation", report)

    def test_preflight_navigation_timeout_saves_same_origin_partial_screenshot(self):
        self.vault.record_step("session-1", {
            "step_type": "tap",
            "action_id": "action-2",
            "selector": "",
            "label": "",
            "tag": "img",
            "page_url": "https://shop.example.test/wishlist",
        })
        page = FakePage()
        page.url = "https://shop.example.test/"
        page.goto_error = TimeoutError("Page.goto: Timeout 5000ms exceeded.")
        runner = NativeReplayRunner(self.vault, allow_submit=True, report_dir=self.report_dir)

        result = runner.run("session-1", page=page)

        self.assertTrue(result["failure"]["screenshot"])
        self.assertTrue(Path(self.report_dir / result["failure"]["screenshot"]).is_file())
        self.assertIn("same-site page state at timeout", result["failure"]["screenshot_note"])
        report = Path(result["report_path"]).read_text(encoding="utf-8")
        self.assertIn("![Failure screenshot](screenshots/", report)

    def test_preflight_report_shows_diagnostic_stop_and_five_prior_captured_steps(self):
        for sequence in range(2, 6):
            self.vault.record_step("session-1", {
                "step_type": "tap",
                "selector": f"button.prior-{sequence}",
                "label": f"Prior action {sequence}",
                "tag": "button",
                "page_url": "https://shop.example.test/",
            })
        self.vault.record_step("session-1", {
            "step_type": "tap",
            "action_id": "action-missing",
            "selector": "span.cart-label",
            "label": "Shopping cart",
            "tag": "span",
            "page_url": "https://shop.example.test/",
        })
        self.vault.record_step("session-1", {
            "step_type": "tap",
            "action_id": "action-unsupported",
            "selector": "",
            "label": "",
            "tag": "img",
            "page_url": "https://shop.example.test/",
        })
        page = FakePage()
        page.selector_counts["span.cart-label"] = 0
        page.selector_counts["accessible-role"] = 0
        runner = NativeReplayRunner(self.vault, allow_submit=True, report_dir=self.report_dir)

        result = runner.run("session-1", page=page)

        self.assertEqual(result["failure"]["sequence"], 7)
        self.assertEqual(result["failure"]["kind"], "unsupported")
        self.assertEqual([step["sequence"] for step in result["preceding_steps"]], [2, 3, 4, 5, 6])
        self.assertEqual(result["diagnostic_stop"]["sequence"], 6)
        self.assertEqual(result["diagnostic_stop"]["kind"], "target_not_found")
        self.assertEqual(result["diagnostic_stop"]["label"], "Shopping cart")
        self.assertEqual(result["diagnostic_stop"]["selector"], "span.cart-label")

        report = Path(result["report_path"]).read_text(encoding="utf-8")
        self.assertIn("## Previous captured steps", report)
        self.assertIn("Captured context only; these are not reported as replay-passed steps.", report)
        self.assertIn("| 6 | tap | Shopping cart | <span> | span.cart-label |", report)
        self.assertIn("## Diagnostic replay stop", report)
        self.assertIn("**Step 6: Shopping cart**", report)
        self.assertIn("Type: `target_not_found`", report)
        self.assertIn("Selector: `span.cart-label`", report)

        structured = json.loads(Path(result["json_path"]).read_text(encoding="utf-8"))
        self.assertEqual(structured["failure"]["sequence"], 7)
        self.assertEqual(structured["diagnostic_stop"]["sequence"], 6)
        self.assertEqual(len(structured["preceding_steps"]), 5)


if __name__ == "__main__":
    unittest.main()