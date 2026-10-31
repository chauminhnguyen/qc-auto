import json
import tempfile
import re
import time
import unittest
import urllib.request
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from capture_app.state.standard_flow import build_standard_flow
from capture_app.storage.vault import VaultWriter
from capture_app.workflows import android_capture
from capture_app.workflows.android_capture import AndroidCaptureRunner, ProcessScopedLogcat, redact_log_line
from capture_app.workflows.android_capture_support import (
    AndroidAppProfile,
    CaptureCollector,
    assign_events_to_markers,
    host_allowed,
    load_profiles,
    parse_network_file,
    sanitize_hierarchy,
    sanitize_event,
    validate_marked_flows,
)


class AndroidCaptureSupportTests(unittest.TestCase):
    def test_profile_loads_speedtest_package_and_relative_flow_directory(self):
        root = Path(__file__).resolve().parents[1]
        profiles = load_profiles(root / "android" / "apps.yaml")
        self.assertEqual(profiles["speedtest"].app_id, "org.zwanoo.android.speedtest")
        self.assertEqual(profiles["speedtest"].flows, root / "android" / "flows")
        self.assertEqual(profiles["speedtest"].network_source, "proxy")
        self.assertTrue(profiles["speedtest"].allow_prefix_replay)
        self.assertTrue(profiles["speedtest"].ui_snapshots)
        self.assertTrue(profiles["speedtest"].maestro_permitted)

    def test_capture_flow_requires_unique_markers_and_settle_wait(self):
        root = Path(__file__).resolve().parents[1]
        flow = root / "android" / "flows" / "smoke.yaml"
        self.assertEqual(validate_marked_flows(flow), ["launch-app", "home-screen", "__end__"])

    def test_hierarchy_redaction_preserves_ui_labels_and_masks_sensitive_node_values(self):
        safe = sanitize_hierarchy(
            '<hierarchy><node resource-id="app:id/password" text="typed-value" />'
            '<node text="Password" /><node text="person@example.test" /></hierarchy>'
        )
        self.assertNotIn("typed-value", safe)
        self.assertNotIn("person@example.test", safe)
        self.assertIn("Password", safe)

    def test_log_redaction_preserves_xml_structure(self):
        safe = redact_log_line('<failure password="secret">person@example.test</failure>')
        self.assertIn('<failure password="[REDACTED]">', safe)
        self.assertIn('</failure>', safe)
        self.assertNotIn("secret", safe)
        self.assertNotIn("person@example.test", safe)

    def test_host_allowlist_supports_exact_wildcard_and_discovery(self):
        self.assertTrue(host_allowed("api.example.test", ()))
        self.assertTrue(host_allowed("api.example.test", ("api.example.test",)))
        self.assertTrue(host_allowed("v2.api.example.test", ("*.example.test",)))
        self.assertFalse(host_allowed("example.test", ("*.example.test",)))

    def test_event_redaction_happens_before_report_serialization(self):
        event = sanitize_event({
            "method": "POST", "url": "https://api.test/path?token=topsecret&id=7",
            "request_headers": {"Authorization": "Bearer topsecret", "X-OTP-Code": "123456",
                                "Content-Type": "application/json"},
            "payload": '{"password":"topsecret","otp":"123456","query":"visible"}',
            "response_body": '{"access_token":"anothersecret","ok":true}',
        })
        serialized = json.dumps(event)
        self.assertNotIn("topsecret", serialized)
        self.assertNotIn("anothersecret", serialized)
        self.assertNotIn("123456", serialized)
        self.assertIn("visible", serialized)

    def test_har_12_import_extracts_request_and_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capture.har"
            path.write_text(json.dumps({"log": {"entries": [{
                "startedDateTime": "2026-10-27T10:00:00Z", "time": 12,
                "request": {"method": "POST", "url": "https://api.test/search",
                            "headers": [{"name": "Content-Type", "value": "application/json"}],
                            "postData": {"text": '{"q":"tea"}'}},
                "response": {"status": 200, "headers": [{"name": "Content-Type", "value": "application/json"}],
                             "content": {"text": '{"result":"tea"}'}}
            }]}}), encoding="utf-8")
            events = parse_network_file(path, "har")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["method"], "POST")
        self.assertEqual(events[0]["status"], 200)
        self.assertIn('"q": "tea"', events[0]["payload"])
        self.assertAlmostEqual(events[0]["response_ts"] - events[0]["ts"], 0.012, places=3)

    def test_normalized_json_import_rejects_missing_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.json"
            path.write_text(json.dumps([{"method": "GET"}]), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be an object with method and url"):
                parse_network_file(path, "json")

    def test_marker_windows_flag_calls_that_finish_after_next_marker(self):
        markers = [{"step_id": "search", "ts": 100}, {"step_id": "results", "ts": 110}]
        events = [
            {"url": "https://api.test/a", "ts": 101, "response_ts": 109},
            {"url": "https://api.test/b", "ts": 102, "response_ts": 111},
            {"url": "https://api.test/c", "ts": 99, "response_ts": 100},
        ]
        assigned = assign_events_to_markers(markers, events)
        self.assertEqual(assigned[0]["action_id"], "search")
        self.assertEqual(assigned[0]["correlation_status"], "matched")
        self.assertIsNone(assigned[1]["action_id"])
        self.assertIn("after the next marker", assigned[1]["correlation_reason"])
        self.assertEqual(assigned[2]["correlation_status"], "ambiguous")

    def test_marker_window_does_not_claim_late_request_for_last_interaction(self):
        assigned = assign_events_to_markers(
            [{"step_id": "start", "ts": 100}],
            [{"url": "https://api.test/late", "ts": 131, "response_ts": 132}],
        )
        self.assertIsNone(assigned[0]["action_id"])
        self.assertEqual(assigned[0]["correlation_status"], "ambiguous")
        self.assertIn("arrived late", assigned[0]["correlation_reason"])

    def test_collector_accepts_marker_and_sanitized_event(self):
        collector = CaptureCollector().start()
        try:
            marker_req = urllib.request.Request(
                collector.base_url + "/marker", data=b'{"step_id":"home"}',
                headers={"Content-Type": "application/json"}, method="POST",
            )
            urllib.request.urlopen(marker_req, timeout=2).read()
            event_req = urllib.request.Request(
                collector.base_url + "/event",
                data=json.dumps({"method": "GET", "url": "https://api.test/?token=secret", "ts": 1}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            urllib.request.urlopen(event_req, timeout=2).read()
            self.assertEqual(collector.markers[0]["step_id"], "home")
            self.assertNotIn("secret", json.dumps(collector.events))
            self.assertEqual(collector.hosts, {"api.test"})
        finally:
            collector.close()

    def test_collector_counts_host_connection_and_tls_failures(self):
        collector = CaptureCollector().start()
        try:
            for kind in ("connection", "tls"):
                request = urllib.request.Request(
                    collector.base_url + "/failure",
                    data=json.dumps({"host": "api.test", "kind": kind}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                urllib.request.urlopen(request, timeout=2).read()
            self.assertEqual(collector.host_failures, {
                "api.test": {"connection_failures": 1, "tls_failures": 1},
            })
        finally:
            collector.close()

    def test_collector_records_sanitized_proxy_debug_counters(self):
        collector = CaptureCollector().start()
        try:
            for kind, host in (("client_connections", ""),
                               ("proxy_connect_requests", "api.test"),
                               ("client_tls_failures", "api.test"),
                               ("http_requests", "api.test")):
                request = urllib.request.Request(
                    collector.base_url + "/diagnostic",
                    data=json.dumps({"kind": kind, "host": host}).encode(),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                urllib.request.urlopen(request, timeout=2).read()
            self.assertEqual(collector.proxy_diagnostics["client_connections"], 1)
            self.assertEqual(collector.proxy_diagnostics["proxy_connect_requests"], 1)
            self.assertEqual(collector.proxy_diagnostics["client_tls_failures"], 1)
            self.assertEqual(collector.proxy_diagnostics["http_requests"], 1)
            self.assertEqual(collector.proxy_host_diagnostics["api.test"], {
                "proxy_connect_requests": 1, "client_tls_failures": 1, "http_requests": 1,
            })
        finally:
            collector.close()

    def test_logcat_is_scoped_to_current_process(self):
        self.assertEqual(
            ProcessScopedLogcat.command_for_pid("adb", "phone", "4321"),
            ["adb", "-s", "phone", "shell", "logcat", "--pid", "4321", "-v", "epoch"],
        )


class AndroidCaptureRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.flow = self.root / "flow.yaml"
        self.flow.write_text(
            "appId: ${APP_ID}\n---\n"
            "- waitForAnimationToEnd:\n    timeout: 10\n"
            "- runScript:\n    file: marker.js\n    env:\n      STEP_ID: home\n      STEP_LABEL: Home\n"
            "- assertVisible: Home\n"
            "- waitForAnimationToEnd:\n    timeout: 10\n"
            "- runScript:\n    file: marker.js\n    env:\n      STEP_ID: __end__\n",
            encoding="utf-8",
        )
        self.profile = AndroidAppProfile("test", "com.example.app", self.flow, "Home",
                                         allow_prefix_replay=True)
        self.report_dir = self.root / "reports"
        self.vault_dir = self.root / "vault"
        self.maestro_result = 0
        self.proxy_tool = True
        self.requested_mode = "api"
        self.flow_running = False
        self.commands = []

    def tearDown(self):
        self.temp.cleanup()

    class FakeLogcat:
        def __init__(self, *_args, **_kwargs):
            self.pids = ["123"]
            self.errors = []

        def start(self):
            return self

        def close(self):
            pass

    class FakeProxy:
        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def _which(self, name):
        values = {"adb": "adb.exe", "maestro": "maestro.bat", "mitmdump": "mitmdump.exe" if self.proxy_tool else None}
        return values.get(name)

    def _command(self, command, **kwargs):
        self.commands.append(command)
        parts = command if isinstance(command, list) else [command]
        rendered = " ".join(map(str, parts))
        if "dumpsys window" in rendered:
            return CompletedProcess(command, 0, "mFocusedApp=AppWindowToken{Token{u0 com.example.app/.MainActivity}}\n", "")
        if "dumpsys package" in rendered:
            return CompletedProcess(command, 0, "Packages:\n  Package [com.example.app]\n", "")
        if "uptime" in rendered:
            return CompletedProcess(command, 0, "1234.0 2000.0\n", "")
        if "settings get global http_proxy" in rendered:
            return CompletedProcess(command, 0, "null\n", "")
        if "hierarchy" in rendered:
            if self.flow_running:
                self.fail("Hierarchy must not run while a Maestro flow is active")
            return CompletedProcess(command, 0, '<hierarchy text="Home"/>', "")
        if " test " in f" {rendered} ":
            match = re.search(r"CAPTURE_MARKER_URL=(\S+)", rendered)
            marker_arg = match.group(1) if match else None
            if marker_arg:
                self.flow_running = True
                try:
                    self._post(marker_arg + "/marker", {"step_id": "home", "label": "Home", "action_kind": "observe"})
                    self._post(marker_arg + "/event", {
                        "method": "POST", "url": "https://api.example.test/search?query=book",
                        "status": 200, "request_headers": {"Content-Type": "application/json"},
                        "payload": '{"query":"book","password":"secret"}',
                        "response_body": '{"items":["book"]}', "ts": time.time(),
                        "response_ts": time.time() + 0.000001,
                    })
                    self._post(marker_arg + "/marker", {"step_id": "__end__", "label": "End", "action_kind": "observe"})
                finally:
                    self.flow_running = False
            return CompletedProcess(command, self.maestro_result, "flow output", "")
        self.fail(f"Unexpected command: {command!r}")

    @staticmethod
    def _post(url, body):
        request = urllib.request.Request(url, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(request, timeout=2).read()

    def test_api_run_persists_sanitized_events_steps_and_hierarchy(self):
        with patch.object(android_capture, "ProcessScopedLogcat", self.FakeLogcat), \
             patch.object(AndroidCaptureRunner, "_start_proxy", return_value=self.FakeProxy()):
            runner = AndroidCaptureRunner(
                self.profile, report_dir=self.report_dir, vault_path=self.vault_dir,
                device_id="phone", which=self._which, command_runner=self._command,
            )
            result = runner.run("api")
        self.assertEqual(result["status"], "passed")
        artifact = Path(result["artifact_dir"])
        self.assertTrue((artifact / "hierarchy" / "home.xml").exists())
        events_text = (artifact / "network-events.json").read_text(encoding="utf-8")
        self.assertNotIn('"secret"', events_text)
        self.assertEqual(result["correlation_summary"]["matched"], 1)
        vault = VaultWriter(self.vault_dir)
        try:
            rows = vault.conn.execute(
                "SELECT request_body, action_id, correlation_status FROM session_api_events WHERE session_id=?",
                (result["vault_session_id"],),
            ).fetchall()
            self.assertEqual(rows[0][1], "home")
            self.assertEqual(rows[0][2], "matched")
            self.assertNotIn("secret", rows[0][0])
            graph, _ = build_standard_flow(vault, result["vault_session_id"])
            self.assertEqual(len(graph.states), 1)
            self.assertEqual(vault.conn.execute("SELECT COUNT(*) FROM steps WHERE session_id=?",
                                                (result["vault_session_id"],)).fetchone()[0], 1)
        finally:
            vault.conn.close()

    def test_api_mode_fails_clearly_when_proxy_tool_is_missing(self):
        self.proxy_tool = False
        runner = AndroidCaptureRunner(
            self.profile, report_dir=self.report_dir, vault_path=self.vault_dir,
            device_id="phone", which=self._which, command_runner=self._command,
        )
        result = runner.run("api")
        self.assertEqual(result["status"], "failed")
        self.assertIn("mitmdump was not found", result["failure"])

    def test_nonrepeatable_profile_is_rejected_before_device_run(self):
        profile = AndroidAppProfile("test", "com.example.app", self.flow, "Home")
        runner = AndroidCaptureRunner(
            profile, report_dir=self.report_dir, vault_path=self.vault_dir,
            which=self._which, command_runner=self._command,
        )
        result = runner.run("results")
        self.assertEqual(result["status"], "failed")
        self.assertIn("allow_prefix_replay: true", result["failure"])
        self.assertEqual(self.commands, [])

    def test_maestro_is_blocked_for_restricted_profile_before_adb_or_flow(self):
        profile = AndroidAppProfile(
            "restricted", "com.example.app", self.flow, "Home",
            allow_prefix_replay=True, maestro_permitted=False,
        )
        result = AndroidCaptureRunner(
            profile, report_dir=self.report_dir, vault_path=self.vault_dir,
            which=self._which, command_runner=self._command,
        ).run("results")
        self.assertEqual(result["status"], "failed")
        self.assertIn("forbidden by profile", result["failure"])
        self.assertEqual(self.commands, [])

    def test_results_mode_does_not_start_proxy(self):
        with patch.object(android_capture, "ProcessScopedLogcat", self.FakeLogcat), \
             patch.object(AndroidCaptureRunner, "_start_proxy", side_effect=AssertionError("proxy must not start")):
            result = AndroidCaptureRunner(
                self.profile, report_dir=self.report_dir, vault_path=self.vault_dir,
                device_id="phone", which=self._which, command_runner=self._command,
            ).run("results")
        self.assertEqual(result["status"], "passed")
        self.assertIn("no global proxy detected", result["proxy_mode"])

    def test_maestro_failure_is_nonzero_and_summarized(self):
        self.maestro_result = 4
        with patch.object(android_capture, "ProcessScopedLogcat", self.FakeLogcat), \
             patch.object(AndroidCaptureRunner, "_start_proxy", return_value=self.FakeProxy()):
            result = AndroidCaptureRunner(
                self.profile, report_dir=self.report_dir, vault_path=self.vault_dir,
                device_id="phone", which=self._which, command_runner=self._command,
            ).run("api")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 4)
        self.assertIn("Maestro failed", result["failure"])

    def test_hierarchy_conflict_fallback_replays_prefix_then_dumps(self):
        runner = AndroidCaptureRunner(self.profile, which=self._which, command_runner=self._command)
        logs = []
        with patch.object(runner, "_run", return_value=CompletedProcess([], 0, "", "")), \
             patch.object(runner, "_hierarchy", return_value=(True, "<hierarchy/>")):
            status = runner._hierarchy_fallback(
                "maestro", "phone", self.flow,
                [{"step_id": "home", "ts": 10}], self.root / "hierarchy", logs,
            )
        self.assertTrue(status["home"])
        self.assertTrue((self.root / "hierarchy" / "home.xml").is_file())
        self.assertTrue(any("Hierarchy prefix home" in line for line in logs))

    def test_proxy_startup_error_when_child_exits(self):
        class ExitedProxy:
            def poll(self):
                return 1
        profile = AndroidAppProfile("test", "com.example.app", self.flow, allow_prefix_replay=True)
        runner = AndroidCaptureRunner(profile, which=self._which, command_runner=self._command,
                                      process_factory=lambda *_args, **_kwargs: ExitedProxy())
        collector = CaptureCollector()
        with self.assertRaisesRegex(android_capture.PreflightError, "mitmdump exited"):
            runner._start_proxy(collector, "mitmdump", "phone", [])

    def test_import_network_populates_report_and_session_graph(self):
        export = self.root / "events.json"
        export.write_text(json.dumps({"events": [{
            "method": "GET", "url": "https://api.example.test/items?id=3",
            "status": 200, "request_headers": {}, "payload": "",
            "response_headers": {"Content-Type": "application/json"},
            "response_body": '{"items":[{"id":3}]}', "ts": 1000,
        }]}), encoding="utf-8")
        from capture_app.workflows.android_capture import import_network_export
        result = import_network_export(export, "json", self.vault_dir, self.report_dir,
                                       "test", "com.example.app")
        self.assertEqual(result["status"], "passed")
        vault = VaultWriter(self.vault_dir)
        try:
            graph, _ = build_standard_flow(vault, result["vault_session_id"])
            self.assertEqual(len(graph.states), 1)
            from capture_app.reporting.param_report import gather
            data = gather(vault, "id")
            self.assertGreaterEqual(len(data.examples.get("request", [])), 1)
        finally:
            vault.conn.close()


if __name__ == "__main__":
    unittest.main()

