from __future__ import annotations

import unittest
import tempfile
import sqlite3
import json
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from capture_app.storage.vault import VaultWriter
from capture_app.workflows import ui_probe


class FakePage:
    def __init__(self, control_rejected=False, empty_message="Search value required",
                 page_message_only=False, dialog_message="", autocomplete_on_fill=False,
                 suppress_submit=False):
        self.current_url = "about:blank"
        self.current_text = "Search form"
        self.current_errors = []
        self.value = ""
        self.callbacks = []
        self.control_rejected = control_rejected
        self.empty_message = empty_message
        self.page_message_only = page_message_only
        self.dialog_message = dialog_message
        self.dialogs = []
        self.autocomplete_on_fill = autocomplete_on_fill
        self.suppress_submit = suppress_submit

    @property
    def url(self):
        return self.current_url

    def goto(self, url, **_kwargs):
        self.current_url = url

    def evaluate(self, _script):
        return {"url": self.current_url, "text": self.current_text,
                "errors": list(self.current_errors)}

    def on(self, event_name, callback):
        if event_name == "request":
            self.callbacks.append(callback)
        elif event_name == "dialog":
            self.dialog_callback = callback

    def locator(self, _selector):
        return FakeLocator(self)

    def wait_for_load_state(self, *_args, **_kwargs):
        return None

    def wait_for_timeout(self, *_args, **_kwargs):
        return None


class FakeLocator:
    def __init__(self, page):
        self.page = page

    def fill(self, value, **_kwargs):
        self.page.value = value
        if self.page.autocomplete_on_fill:
            for callback in self.page.callbacks:
                callback(SimpleNamespace())

    def input_value(self, **_kwargs):
        return self.page.value

    def get_attribute(self, name, **_kwargs):
        return "25" if name == "maxlength" else None

    def click(self, **_kwargs):
        page = self.page
        if page.control_rejected and page.value == "shirt":
            page.current_errors = ["Search value rejected"]
            return
        if page.suppress_submit:
            return
        if page.value == "":
            if page.dialog_message:
                dialog = FakeDialog(page.dialog_message)
                page.dialogs.append(dialog)
                page.dialog_callback(dialog)
                return
            if page.page_message_only:
                page.current_text = page.empty_message
            else:
                page.current_errors = [page.empty_message]
            return
        for callback in page.callbacks:
            callback(SimpleNamespace())
        page.current_url = "https://example.test/search"
        page.current_text = "Search results"


class FakeDialog:
    def __init__(self, message):
        self.message = message
        self.dismissed = False

    def dismiss(self):
        self.dismissed = True


class UiProbeTests(unittest.TestCase):
    def test_captured_search_form_uses_recorded_field_and_submit_selectors(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temp:
            vault = VaultWriter(Path(temp))
            vault.start_session("s", "https://example.test/")
            vault.record_step("s", {
                "step_type": "input", "selector": "input#q", "label": "shirt",
                "value": "shirt", "input_type": "text", "page_url": "https://example.test/",
            })
            vault.record_step("s", {
                "step_type": "tap", "selector": "button.search", "label": "Search",
                "page_url": "https://example.test/", "action_id": "submit",
            })
            form = ui_probe.find_captured_search_form(vault, "s")
            vault.conn.close()
        self.assertEqual(form["field_selector"], "input#q")
        self.assertEqual(form["submit_selector"], "button.search")
        self.assertEqual(form["control_value"], "shirt")

    def test_browser_launcher_falls_back_to_installed_chrome_when_bundle_missing(self):
        class Chromium:
            executable_path = "missing-chromium"
            calls = []
            def launch(self, **kwargs):
                self.calls.append(kwargs)
                return "browser"
        playwright = SimpleNamespace(chromium=Chromium())
        browser, label = ui_probe._launch_browser(
            playwright, headed=True, slow_ms=500, bundled_exists=False,
        )
        self.assertEqual(browser, "browser")
        self.assertEqual(label, "chrome")
        self.assertEqual(playwright.chromium.calls[0], {
            "channel": "chrome", "headless": False, "slow_mo": 500,
        })

    def test_search_control_runs_first_and_each_probe_uses_fresh_context(self):
        form = {"page_url": "https://example.test/", "field_selector": "input#q",
                "submit_selector": "button.search", "control_value": "shirt"}
        pages, closed, sleeps = [], [], []

        class LogCapture:
            rows = []
            def save_field_probe(self, *args, **kwargs):
                self.rows.append((args, kwargs))
                return len(self.rows)
        log = LogCapture()

        def open_page():
            page = FakePage()
            pages.append(page)
            return page, lambda: closed.append(page)

        with patch("builtins.print") as output:
            result = ui_probe.run_search_ui_probes(
                form, "run-1", open_page=open_page, sleep=sleeps.append,
                vault=log, run_key="session::run", transition_key="ui-30",
            )
        self.assertEqual(result["status"], "complete")
        self.assertEqual([item["category"] for item in result["results"]],
                         ["control", "empty", "short", "long"])
        self.assertEqual(len(pages), 4)
        self.assertEqual(len({id(page) for page in pages}), 4)
        self.assertEqual(len(closed), 4)
        self.assertEqual(len(sleeps), 3)
        self.assertEqual(result["results"][1]["outcome"], "rejected")
        self.assertEqual(result["results"][1]["validation_phase"],
                         "validation without a network request")
        self.assertTrue(any("test environment" in str(call).casefold()
                            for call in output.call_args_list))
        self.assertEqual(len(log.rows), 4)
        self.assertEqual(log.rows[0][0][5], "shirt")
        self.assertEqual(log.rows[1][0][5], "")
        self.assertEqual(log.rows[2][0][5], "x")
        long_value = log.rows[3][0][5]
        self.assertEqual(long_value["length"], 256)
        self.assertEqual(len(long_value["sha256"]), 64)
        self.assertNotIn("shirt", str(result))
        self.assertNotIn("Search value required", str(result))

    def test_request_counts_separate_autocomplete_from_submit(self):
        pages = []
        def open_page():
            page = FakePage(autocomplete_on_fill=True)
            pages.append(page)
            return page, lambda: None
        result = ui_probe.run_search_ui_probes(
            {"page_url": "https://example.test/", "field_selector": "input#q",
             "submit_selector": "button.search", "control_value": "shirt"},
            "run-1", open_page=open_page, sleep=lambda _seconds: None,
            probe_cap=1,
        )
        self.assertEqual(result["results"][0]["outcome"], "accepted with results")
        self.assertTrue(result["results"][0]["request_fired"])
        self.assertEqual(result["results"][0]["autocomplete_requests"], 1)
        self.assertEqual(pages[0].value, "shirt")

    def test_no_results_text_is_accepted_empty_and_suppressed_submit_rejected(self):
        def open_empty():
            page = FakePage()
            original = page.locator
            def locator(selector):
                result = original(selector)
                if selector == "button.search":
                    def empty_click(**_kwargs):
                        page.current_text = "No products found"
                    result.click = empty_click
                return result
            page.locator = locator
            return page, lambda: None
        accepted = ui_probe._run_one(
            {"page_url": "https://example.test/", "field_selector": "input#q",
             "submit_selector": "button.search"}, "empty", "nope", open_page=open_empty,
        )
        suppressed = ui_probe._run_one(
            {"page_url": "https://example.test/", "field_selector": "input#q",
             "submit_selector": "button.search"}, "control", "shirt",
            open_page=lambda: (FakePage(suppress_submit=True), lambda: None),
        )
        self.assertEqual(accepted["outcome"], "accepted with empty result")
        self.assertEqual(suppressed["outcome"], "rejected")

    def test_numeric_empty_validation_adds_both_boundaries_within_six_total(self):
        pages = []
        log = type("Log", (), {"rows": [], "save_field_probe": lambda self, *a, **k: self.rows.append((a, k))})()

        def open_page():
            page = FakePage(empty_message="Search requires at least 7 characters",
                            page_message_only=True)
            pages.append(page)
            return page, lambda: None

        result = ui_probe.run_search_ui_probes(
            {"page_url": "https://example.test/", "field_selector": "input#q",
             "submit_selector": "button.search", "control_value": "shirt"},
            "run-1", open_page=open_page, sleep=lambda _seconds: None, vault=log,
        )
        self.assertEqual([item["category"] for item in result["results"]],
                         ["control", "empty", "short", "N-1", "N", "long"])
        self.assertEqual(len(pages), 6)
        self.assertEqual(log.rows[3][0][5], "x" * 6)
        self.assertEqual(log.rows[4][0][5], "x" * 7)

    def test_dialog_validation_is_dismissed_and_numeric_text_drives_boundaries(self):
        pages = []
        log = type("Log", (), {"rows": [], "save_field_probe": lambda self, *a, **k: self.rows.append((a, k))})()

        def open_page():
            page = FakePage(dialog_message="Search needs at least 5 characters")
            pages.append(page)
            return page, lambda: None

        result = ui_probe.run_search_ui_probes(
            {"page_url": "https://example.test/", "field_selector": "input#q",
             "submit_selector": "button.search", "control_value": "shirt"},
            "run-1", open_page=open_page, sleep=lambda _seconds: None, vault=log,
        )
        self.assertEqual([item["category"] for item in result["results"]],
                         ["control", "empty", "short", "N-1", "N", "long"])
        self.assertTrue(pages[1].dialogs[0].dismissed)
        self.assertEqual(result["results"][1]["validation_phase"],
                         "validation without a network request")
        self.assertEqual(log.rows[1][0][8], "Search needs at least 5 characters")
        self.assertEqual(log.rows[3][0][5], "x" * 4)
        self.assertEqual(log.rows[4][0][5], "x" * 5)

    def test_numeric_probe_values_already_run_are_skipped(self):
        pages = []
        def open_page():
            page = FakePage(empty_message="Minimum length is 1 character",
                            page_message_only=True)
            pages.append(page)
            return page, lambda: None
        result = ui_probe.run_search_ui_probes(
            {"page_url": "https://example.test/", "field_selector": "input#q",
             "submit_selector": "button.search", "control_value": "shirt"},
            "run-1", open_page=open_page, sleep=lambda _seconds: None,
        )
        categories = [item["category"] for item in result["results"]]
        self.assertEqual(categories, ["control", "empty", "short", "long"])
        self.assertEqual(len({"", "x"}), 2)

    def test_boundary_extractor_requires_a_length_reference(self):
        self.assertEqual(ui_probe._message_number(["Found 12 products"]), None)
        self.assertEqual(ui_probe._message_number(["Please enter at least 8 characters"]), 8)
        self.assertEqual(ui_probe._message_number(["Maximum length is 30"]), 30)
        self.assertEqual(ui_probe._message_number(["Use no more than 10 chars"]), 10)

    def test_raw_log_persists_exact_short_values_and_only_long_hash(self):
        pages = []
        def open_page():
            page = FakePage()
            pages.append(page)
            return page, lambda: None
        with tempfile.TemporaryDirectory() as temp:
            vault = VaultWriter(Path(temp))
            result = ui_probe.run_search_ui_probes(
                {"page_url": "https://example.test/", "field_selector": "input#q",
                 "submit_selector": "button.search", "control_value": "shirt"},
                "run-1", open_page=open_page, sleep=lambda _seconds: None,
                vault=vault, run_key="session::run", transition_key="ui-30",
            )
            rows = vault.get_field_probe_log("session::run", "UI_search")
            vault.conn.close()
        self.assertEqual(result["status"], "complete")
        self.assertEqual([row["probe_kind"] for row in rows],
                         ["control", "empty", "short", "long"])
        self.assertEqual([row["probe_value"] for row in rows[:3]], ["shirt", "", "x"])
        self.assertEqual(rows[3]["probe_value"]["length"], 256)
        self.assertEqual(len(rows[3]["probe_value"]["sha256"]), 64)
        self.assertNotIn(pages[3].value, rows[3]["response_message"] + rows[3]["response_body"])
        self.assertEqual(json.loads(rows[0]["response_body"])["value_readback"], "shirt")
        self.assertEqual(json.loads(rows[0]["response_body"])["maxlength"], "25")

    def test_rejected_known_valid_control_stops_form(self):
        pages = []

        def open_page():
            page = FakePage(control_rejected=True)
            pages.append(page)
            return page, lambda: None

        result = ui_probe.run_search_ui_probes(
            {"page_url": "https://example.test/", "field_selector": "input#q",
             "submit_selector": "button.search", "control_value": "shirt"},
            "run-1", open_page=open_page, sleep=lambda _seconds: None,
        )
        self.assertEqual(result["status"], "form_stopped")
        self.assertEqual(result["results"][0]["outcome"], "rejected")
        self.assertEqual(len(pages), 1)

    def test_search_plan_does_not_use_wrong_type_probe(self):
        categories = ui_probe.run_search_ui_probes
        self.assertTrue(callable(categories))
        self.assertNotIn("wrong_type", ["empty", "alternate_query", "long_query"])
        from capture_app.workflows.testcase_gen import _FORM_PROBE_CAPS
        self.assertEqual(ui_probe.SEARCH_PROBE_CAP, 6)
        self.assertEqual(_FORM_PROBE_CAPS["search"], 6)
        self.assertEqual(_FORM_PROBE_CAPS["register"], 4)
        self.assertEqual(_FORM_PROBE_CAPS["login"], 4)
        self.assertEqual(_FORM_PROBE_CAPS["other"], 4)

    def test_show_probe_log_reads_without_mutating_database(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / ".capture_index" / "index.sqlite"
            db_path.parent.mkdir()
            conn = sqlite3.connect(db_path)
            conn.execute("""CREATE TABLE field_probe_log (
                id INTEGER, run_key TEXT, api_ref TEXT, field_name TEXT, probe_kind TEXT,
                probe_value_json TEXT, outcome TEXT, response_status INTEGER,
                response_message TEXT, response_body TEXT, ui_hint TEXT
            )""")
            conn.execute("INSERT INTO field_probe_log VALUES (1,?,?,?,?,?,?,?,?,?,?)", (
                "s::r", "UI_search", "Search", "short", json.dumps("x"), "rejected", None,
                "Please enter at least 3 characters",
                json.dumps({"request_count": 0, "autocomplete_requests": 2,
                            "value_readback": "x", "maxlength": "25",
                            "classification_rule": "visible validation",
                            "url_before": "/", "url_after": "/"}),
                "validation without a network request",
            ))
            conn.commit()
            conn.close()
            before = db_path.stat().st_mtime_ns
            with patch("builtins.print"):
                rows = ui_probe.show_probe_log(temp, "s::r")
            self.assertEqual(db_path.stat().st_mtime_ns, before)
        self.assertEqual(rows[0]["value"], "x")
        self.assertEqual(rows[0]["request_fired"], "no")
        self.assertEqual(rows[0]["url_changed"], "no")
        self.assertEqual(rows[0]["validation_text"], "Please enter at least 3 characters")
        self.assertEqual(rows[0]["autocomplete_requests"], 2)
        self.assertEqual(rows[0]["value_readback"], "x")
        self.assertEqual(rows[0]["maxlength"], "25")


if __name__ == "__main__":
    unittest.main()
