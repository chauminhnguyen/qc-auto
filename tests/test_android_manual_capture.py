import tempfile
import time
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from capture_app.storage.vault import VaultWriter
from capture_app.workflows.android_adb import AdbCommandRejected, AdbCommandWrapper
from capture_app.workflows.android_capture_support import AndroidAppProfile, sanitize_event
from capture_app.workflows.android_manual_capture import (
    ManualAndroidCapture,
    TouchEventParser,
    classify_gesture,
    parse_display_size,
    parse_foreground_window,
    parse_ime_window_bounds,
    parse_touch_ranges,
    scale_touch,
    UptimeCalibrator,
)


class AndroidAdbAllowlistTests(unittest.TestCase):
    def test_allowlisted_operations_build_only_fixed_shell_commands(self):
        wrapper = AdbCommandWrapper("adb.exe", "192.168.1.4:1234")
        self.assertEqual(wrapper.argv("getevent_caps"), [
            "adb.exe", "-s", "192.168.1.4:1234", "shell", "getevent", "-p",
        ])
        self.assertEqual(wrapper.argv("hierarchy"), [
            "adb.exe", "-s", "192.168.1.4:1234", "shell", "uiautomator", "dump", "/dev/stdout",
        ])
        self.assertEqual(wrapper.argv("uptime")[-2:], ["cat", "/proc/uptime"])
        self.assertEqual(wrapper.argv("getevent_stream", "/dev/input/event2"), [
            "adb.exe", "-s", "192.168.1.4:1234", "shell", "-t", "getevent", "-lt",
            "/dev/input/event2",
        ])

    def test_rejects_unapproved_adb_operations_and_shell_paths(self):
        wrapper = AdbCommandWrapper("adb", "phone")
        for operation, args in (("screencap", ()), ("screenrecord", ()), ("pull", ("x",)),
                                ("getevent_stream", ("/sdcard/foo",)), ("devices", ())):
            with self.subTest(operation=operation), self.assertRaises(AdbCommandRejected):
                wrapper.argv(operation, *args)

    def test_no_image_or_recording_commands_exist_in_the_adb_allowlist(self):
        wrapper = AdbCommandWrapper("adb", "phone")
        for operation in ("screencap", "screenrecord", "exec-out", "uiautomator-screenshot"):
            with self.subTest(operation=operation), self.assertRaises(AdbCommandRejected):
                wrapper.argv(operation)


class AndroidTouchParsingTests(unittest.TestCase):
    CAPS = '''add device 1: /dev/input/event5
  name: "Samsung Touchscreen"
  events:
    ABS (0003): ABS_MT_POSITION_X : value 0, min 0, max 4095
                    ABS_MT_POSITION_Y : value 0, min 0, max 4095
'''

    def test_touch_ranges_size_and_scaling(self):
        path, xmin, xmax, ymin, ymax = parse_touch_ranges(self.CAPS)
        self.assertEqual((path, xmin, xmax, ymin, ymax), ("/dev/input/event5", 0, 4095, 0, 4095))
        size = parse_display_size("Physical size: 1080x2340\n")
        self.assertEqual(size, (1080, 2340))
        self.assertEqual(scale_touch(4095, 2048, (0, 4095, 0, 4095), size), (1079, 1170))

    def test_touch_ranges_accept_multiline_zero_padded_hex_output(self):
        text = '''add device 2: /dev/input/event6
  name: "sec_touchscreen"
    ABS (0003): ABS_MT_POSITION_X : value 00000100
                min 00000000, max 00000fff, fuzz 0, flat 0, resolution 0
                ABS_MT_POSITION_Y : value 00000200
                min 00000000, max 0000198f, fuzz 0, flat 0, resolution 0
'''
        self.assertEqual(parse_touch_ranges(text), ("/dev/input/event6", 0, 4095, 0, 6543))

    def test_samsung_numeric_abs_codes_choose_direct_touchscreen_over_touchpad(self):
        text = '''add device 4: /dev/input/event2
  name: "sec_touchscreen"
  events:
    ABS (0003): 0000  : value 0, min 0, max 1079, fuzz 0
                0001  : value 0, min 0, max 2339, fuzz 0
                0035  : value 0, min 0, max 1079, fuzz 0
                0036  : value 0, min 0, max 2339, fuzz 0
  input props:
    INPUT_PROP_DIRECT
add device 6: /dev/input/event3
  name: "sec_touchpad"
  events:
    ABS (0003): 0000  : value 0, min 0, max 1079, fuzz 0
                0001  : value 0, min 0, max 2339, fuzz 0
  input props:
    INPUT_PROP_POINTER
'''
        self.assertEqual(parse_touch_ranges(text), ("/dev/input/event2", 0, 1079, 0, 2339))

    def test_touch_access_denied_is_reported_as_missing_device_capabilities(self):
        with self.assertRaisesRegex(ValueError, "denied getevent access"):
            parse_touch_ranges("Permission denied: /dev/input/event5")

    def test_uptime_offset_uses_several_samples(self):
        class ADB:
            def __init__(self):
                self.values = iter(("10.0 0.0", "10.1 0.0", "10.2 0.0"))
            def run(self, *_args, **_kwargs):
                return CompletedProcess([], 0, next(self.values), "")
        wall_values = iter((100.0, 100.1, 100.2, 100.3, 100.4, 100.5))
        clock = UptimeCalibrator(ADB(), wall_clock=lambda: next(wall_values))
        with patch("capture_app.workflows.android_manual_capture.time.sleep", return_value=None):
            offset = clock.calibrate()
        self.assertEqual(len(clock.samples), 3)
        self.assertAlmostEqual(offset, 90.05)
        self.assertAlmostEqual(clock.epoch(11), 101.05)

    def test_getevent_boot_time_is_calibrated_and_gesture_classified(self):
        class Clock:
            def epoch(self, boot):
                return 1_800_000_000 + boot

        parser = TouchEventParser(Clock())
        lines = [
            "[ 10.000000] /dev/input/event5: EV_ABS ABS_MT_TRACKING_ID 00000001",
            "[ 10.001000] /dev/input/event5: EV_ABS ABS_MT_POSITION_X 00000100",
            "[ 10.002000] /dev/input/event5: EV_ABS ABS_MT_POSITION_Y 00000200",
            "[ 10.030000] /dev/input/event5: EV_ABS ABS_MT_POSITION_X 00000102",
            "[ 10.031000] /dev/input/event5: EV_ABS ABS_MT_TRACKING_ID ffffffff",
        ]
        gesture = None
        for line in lines:
            gesture = parser.feed(line) or gesture
        self.assertEqual(gesture["duration_ms"], 31)
        self.assertEqual(gesture["start_ts"], 1_800_000_010)
        self.assertEqual(classify_gesture({**gesture, "x0": 10, "y0": 10, "x1": 12, "y1": 11},
                                          width=1080, height=2340)[0], "tap")
        self.assertEqual(classify_gesture({**gesture, "x0": 1, "y0": 10, "x1": 800, "y1": 11},
                                          width=1080, height=2340), ("system_gesture", True))

    def test_getevent_without_device_prefix_records_a_tap(self):
        class Clock:
            def epoch(self, boot):
                return 1_800_000_000 + boot
        parser = TouchEventParser(Clock())
        lines = [
            "[ 1367423.182421] EV_ABS ABS_MT_TRACKING_ID 00002c19",
            "[ 1367423.182421] EV_KEY BTN_TOUCH DOWN",
            "[ 1367423.182421] EV_ABS ABS_MT_POSITION_X 00000221",
            "[ 1367423.182421] EV_ABS ABS_MT_POSITION_Y 000005fa",
            "[ 1367423.261223] EV_ABS ABS_MT_TRACKING_ID ffffffff",
        ]
        gesture = None
        for line in lines:
            gesture = parser.feed(line) or gesture
        self.assertIsNotNone(gesture)
        self.assertEqual(gesture["x0"], 0x221)
        self.assertEqual(gesture["y0"], 0x5fa)
        self.assertEqual(gesture["duration_ms"], 79)

    def test_getevent_clock_domain_is_corrected_when_live_event_time_disagrees(self):
        clock = UptimeCalibrator(object())
        clock.offset_seconds = 1_790_134_163.0
        parser = TouchEventParser(clock)
        lines = [
            "[ 1367423.100000] EV_KEY BTN_TOUCH DOWN",
            "[ 1367423.100000] EV_ABS ABS_MT_POSITION_X 00000100",
            "[ 1367423.100000] EV_ABS ABS_MT_POSITION_Y 00000200",
            "[ 1367423.180000] EV_KEY BTN_TOUCH UP",
        ]
        gesture = None
        for line in lines:
            gesture = parser.feed(line, received_epoch=1_793_195_996.0) or gesture
        self.assertIsNotNone(gesture)
        self.assertTrue(clock.event_offset_seconds)
        self.assertAlmostEqual(gesture["end_ts"], 1_793_195_996.0)
        self.assertAlmostEqual(gesture["start_ts"], 1_793_195_995.92, places=5)

    def test_foreground_activity_and_ime_bounds_are_parsed(self):
        window = '''mCurrentFocus=Window{abc u0 com.android.inputmethod/.Ime}
mFocusedApp=AppWindowToken{Token{u0 com.target/.LoginActivity}}
Window{ime u0 com.android.inputmethod/.Ime} frame=[0,1700][1080,2340]
'''
        self.assertEqual(parse_foreground_window(window), ("com.target", ".LoginActivity"))
        self.assertEqual(parse_ime_window_bounds(window, "com.android.inputmethod/.Ime"), (0, 1700, 1080, 2340))


class AndroidManualStepTests(unittest.TestCase):
    def test_manual_run_writes_no_images_and_invokes_no_capture_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            commands = []
            class FakeProcess:
                stdout = iter(())
                stderr = iter(())
                def __init__(self):
                    self.stopped = False
                def poll(self):
                    return 0 if self.stopped else None
                def terminate(self):
                    self.stopped = True
                def wait(self, timeout=None):
                    self.stopped = True
                    return 0
                def kill(self):
                    self.stopped = True
            def command(argv, **_kwargs):
                commands.append(argv)
                shell_command = argv[4:]
                if shell_command[:2] == ["dumpsys", "window"]:
                    output = "mFocusedApp=AppWindowToken{Token{u0 com.target.app/.MainActivity}}"
                elif shell_command[:2] == ["dumpsys", "package"]:
                    output = "Package [com.target.app]"
                elif shell_command[:2] == ["wm", "size"]:
                    output = "Physical size: 1080x2340"
                elif shell_command[:2] == ["getevent", "-p"]:
                    output = AndroidTouchParsingTests.CAPS
                elif shell_command == ["cat", "/proc/uptime"]:
                    output = "100.0 0.0"
                elif shell_command[:3] == ["settings", "get", "secure"]:
                    output = "com.android.inputmethod/.Ime"
                else:
                    raise AssertionError(f"Unexpected ADB command: {argv}")
                return CompletedProcess(argv, 0, output, "")
            profile = AndroidAppProfile(
                "restricted", "com.target.app", root / "flows", network_sources=("proxy",),
                ui_snapshots=False, maestro_permitted=False,
            )
            runner = ManualAndroidCapture(
                profile, device_id="phone", vault_path=root / "vault", report_dir=root / "reports",
                which=lambda name: "adb.exe" if name == "adb" else None,
                command_runner=command, process_factory=lambda *_args, **_kwargs: FakeProcess(),
                input_fn=lambda: "q",
            )
            with patch("builtins.print"):
                result = runner.run()
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["interaction_steps"], 0)
            self.assertIn("No in-app touch interactions", result["failure"])
            command_names = [part.casefold() for argv in commands for part in argv]
            self.assertNotIn("screencap", command_names)
            self.assertNotIn("screenrecord", command_names)
            artifact_dir = Path(result["artifact_dir"])
            image_suffixes = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".mp4", ".mov", ".mkv"}
            self.assertFalse(any(path.suffix.casefold() in image_suffixes
                                 for path in artifact_dir.rglob("*")))

    def test_snapshots_off_records_secure_placeholder_and_no_coordinates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "run").mkdir()
            profile = AndroidAppProfile("restricted", "com.target.app", root / "flows",
                                        network_sources=("proxy",), ui_snapshots=False,
                                        maestro_permitted=False)
            capture = ManualAndroidCapture(profile, device_id="phone", report_dir=root / "reports",
                                           vault_path=root / "vault")
            capture.foreground = ("com.target.app", ".Login")
            vault = VaultWriter(root / "vault")
            vault.start_session("session", "android://com.target.app")
            capture._add_step(
                vault, "session",
                {"x0": 200, "y0": 300, "x1": 200, "y1": 300, "start_ts": 100,
                 "duration_ms": 50, "snapshot": {"foreground": ("com.target.app", ".Login"),
                                               "hierarchy": None, "ime_bounds": None}},
                (1080, 2340), (0, 4095, 0, 4095), None, root / "run", None, "",
            )
            row = vault.conn.execute("SELECT step_type, selector, label FROM steps WHERE session_id=?",
                                     ("session",)).fetchone()
            self.assertEqual(tuple(row), ("secure_input", None, "secure_input"))
            self.assertFalse(list((root / "run").rglob("*.png")) if (root / "run").exists() else [])
            vault.conn.close()

    def test_hierarchy_timeout_is_marked_missing_and_only_text_snapshot_is_written(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "run" / "hierarchy").mkdir(parents=True)
            profile = AndroidAppProfile("speedtest", "com.target.app", root / "flows",
                                        ui_snapshots=True, ui_snapshot_delay_ms=0,
                                        ui_snapshot_min_interval_ms=0)
            capture = ManualAndroidCapture(profile, device_id="phone")
            class ADB:
                def run(self, *_args, **_kwargs):
                    return CompletedProcess([], 1, "", "timed out")
            with patch("capture_app.workflows.android_manual_capture.time.sleep", return_value=None):
                capture._snapshot_after_touch(ADB(), root / "run", "touch-1")
            self.assertEqual(capture.missing_ui_evidence, ["touch-1"])
            self.assertEqual(list((root / "run").rglob("*.png")), [])
            self.assertFalse(any(p.suffix.lower() in {".png", ".jpg", ".mp4"}
                                 for p in (root / "run").rglob("*")))

    def test_non_xml_uiautomator_status_text_is_not_saved_as_a_hierarchy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "run" / "hierarchy").mkdir(parents=True)
            profile = AndroidAppProfile("speedtest", "com.target.app", root / "flows",
                                        ui_snapshots=True, ui_snapshot_delay_ms=0,
                                        ui_snapshot_min_interval_ms=0)
            capture = ManualAndroidCapture(profile, device_id="phone")
            class ADB:
                def run(self, *_args, **_kwargs):
                    return CompletedProcess([], 0, "UI hierchary dumped to: /dev/stdout", "")
            with patch("capture_app.workflows.android_manual_capture.time.sleep", return_value=None):
                capture._snapshot_after_touch(ADB(), root / "run", "touch-1")
            self.assertEqual(capture.missing_ui_evidence, ["touch-1"])
            self.assertFalse(list((root / "run" / "hierarchy").glob("*.xml")))

    def test_keyboard_password_and_pin_keypad_taps_are_secure_placeholders(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "run" / "hierarchy").mkdir(parents=True)
            profile = AndroidAppProfile("speedtest", "com.target.app", root / "flows",
                                        ui_snapshots=True, ui_snapshot_delay_ms=0,
                                        ui_snapshot_min_interval_ms=0)
            capture = ManualAndroidCapture(profile, device_id="phone")
            capture.foreground = ("com.target.app", ".Login")
            vault = VaultWriter(root / "vault")
            vault.start_session("session", "android://com.target.app")
            class ADB:
                def run(self, *_args, **_kwargs):
                    return CompletedProcess([], 1, "", "hierarchy unavailable")
            snapshots = [
                {"secure": False, "nodes": [], "raw": "", "file": None},  # keyboard window
                {"secure": True, "nodes": [], "raw": "", "file": None},   # password field
                {"secure": True, "nodes": [], "raw": "", "file": None},   # PIN keypad
            ]
            ime_bounds = (0, 1500, 1080, 2340)
            for index, snapshot in enumerate(snapshots, 1):
                raw_x, raw_y = (760, 3150) if index == 1 else (200, 300)
                capture._add_step(
                    vault, "session",
                    {"x0": raw_x, "y0": raw_y, "x1": raw_x, "y1": raw_y, "start_ts": 100 + index,
                     "duration_ms": 50,
                     "snapshot": {"foreground": ("com.target.app", ".Login"),
                                  "hierarchy": snapshot,
                                  "ime_bounds": ime_bounds if index == 1 else None}},
                    (1080, 2340), (0, 4095, 0, 4095), snapshot, root / "run", ADB(), "",
                )
            for thread in capture.snapshot_threads:
                thread.join(timeout=2)
            rows = vault.conn.execute(
                "SELECT step_type, selector, label FROM steps WHERE session_id=? ORDER BY ts",
                ("session",),
            ).fetchall()
            self.assertEqual([tuple(row) for row in rows], [
                ("secure_input", None, "secure_input"),
                ("secure_input", None, "secure_input"),
                ("secure_input", None, "secure_input"),
            ])
            vault.conn.close()

    def test_snapshot_rate_limit_marks_missing_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "hierarchy").mkdir()
            profile = AndroidAppProfile("speedtest", "com.target.app", root / "flows",
                                        ui_snapshots=True, ui_snapshot_delay_ms=0,
                                        ui_snapshot_min_interval_ms=800)
            capture = ManualAndroidCapture(profile, device_id="phone")
            capture.last_snapshot_started = time.monotonic()
            with patch("capture_app.workflows.android_manual_capture.time.sleep", return_value=None):
                capture._snapshot_after_touch(object(), root, "touch-2")
            self.assertEqual(capture.missing_ui_evidence, ["touch-2"])

    def test_image_and_pdf_response_bodies_are_replaced_by_type_size_hash(self):
        for mime, data in (("image/png", b"PNG_SECRET"), ("application/pdf", b"%PDF-SECRET")):
            event = sanitize_event({
                "method": "GET", "url": "https://files.test/blob", "status": 200,
                "response_headers": {"Content-Type": mime}, "response_body": data,
            })
            self.assertNotIn("SECRET", event["response_body"])
            self.assertEqual(event["response_body_meta"]["content_type"], mime)
            self.assertEqual(event["response_body_meta"]["size_bytes"], len(data))
            self.assertEqual(len(event["response_body_meta"]["sha256"]), 64)

    def test_pending_events_flush_into_vault_once_for_reporting_and_session_graph(self):
        from capture_app.state.standard_flow import build_standard_flow
        from capture_app.workflows.android_capture_support import CaptureCollector
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = AndroidAppProfile("speedtest", "com.target.app", root / "flows")
            capture = ManualAndroidCapture(profile, device_id="phone")
            vault = VaultWriter(root / "vault")
            vault.start_session("manual-session", "android://com.target.app")
            vault.record_step("manual-session", {
                "step_type": "tap", "selector": "123,456", "label": "Results",
                "page_url": "android://com.target.app/.Main", "ts": 100,
            })
            collector = CaptureCollector()
            collector.events.append(sanitize_event({
                "method": "GET", "url": "https://api.example.test/results?id=7",
                "status": 200, "response_body": '{"items":[{"id":7}]}', "ts": 101,
            }))
            capture._flush_pending_events(vault, "manual-session", collector)
            capture._flush_pending_events(vault, "manual-session", collector)
            graph, _ = build_standard_flow(vault, "manual-session")
            from capture_app.reporting.param_report import gather
            report = gather(vault, "id")
            self.assertEqual(len(graph.states), 1)
            self.assertGreaterEqual(len(report.examples.get("request", [])), 1)
            self.assertEqual(vault.conn.execute(
                "SELECT COUNT(*) FROM session_api_events WHERE session_id=?", ("manual-session",)
            ).fetchone()[0], 1)
            vault.conn.close()


if __name__ == "__main__":
    unittest.main()
