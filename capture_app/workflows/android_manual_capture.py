"""User-driven Android capture using only allowlisted ADB operations."""

from __future__ import annotations

import argparse
import json
import queue
import re
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from capture_app.storage.vault import VaultWriter
from capture_app.workflows.android_adb import AdbCommandWrapper
from capture_app.workflows.android_capture import (
    DEFAULT_PROFILE_PATH,
    DEFAULT_REPORT_DIR,
    AndroidCaptureRunner,
    ProcessScopedLogcat,
    _now_run_dir,
    _resolve_pid,
    _write_json,
    redact_log_line,
)
from capture_app.workflows.android_capture_support import (
    AndroidAppProfile,
    CaptureCollector,
    assign_events_to_markers,
    host_allowed,
    parse_network_file,
    redact_marker_text,
    sanitize_event,
    sanitize_hierarchy,
)


_EVENT_LINE = re.compile(
    r"\[\s*(?P<ts>\d+(?:\.\d+)?)\s*\]\s+(?:(?P<dev>/dev/input/event\d+):\s*)?"
    r"EV_(?P<kind>ABS|KEY)\s+(?P<code>\S+)\s+(?P<value>\S+)"
)
_SIZE = re.compile(r"(?:Physical|Override) size:\s*(\d+)x(\d+)")
_AXIS_NAME = re.compile(r"\b(?P<axis>ABS_MT_POSITION_[XY]|ABS_[XY])\b")
_AXIS_CODE = re.compile(r"(?:^\s*|ABS\s*\(0003\):\s*)(?P<code>0000|0001|0035|0036)\s*:")
_AXIS_RANGE = re.compile(r"\bmin\s+(?P<min>-?(?:0x)?[0-9a-fA-F]+),\s*max\s+(?P<max>-?(?:0x)?[0-9a-fA-F]+)")
_EVENT_DEVICE = re.compile(r"add device \d+: (?P<path>/dev/input/event\d+)(?P<body>.*?)(?=add device \d+:|\Z)", re.S)
_FOCUS = re.compile(r"(?:mFocusedApp|mCurrentFocus).*?\bu\d+\s+(?P<package>[A-Za-z0-9_.]+)/(?P<activity>[A-Za-z0-9_.$]+)")
_APP_PID = re.compile(r"\b(?P<pid>\d+):(?P<package>[A-Za-z0-9_.]+)(?:/|\s|$)")
_WINDOW_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def parse_display_size(text: str) -> tuple[int, int]:
    values = _SIZE.findall(text)
    if not values:
        raise ValueError("wm size did not report a physical or override size")
    width, height = map(int, values[-1])
    return width, height


def parse_touch_ranges(text: str) -> tuple[str, int, int, int, int]:
    if re.search(r"permission denied|operation not permitted", text, re.IGNORECASE):
        raise ValueError("Android denied getevent access to the touchscreen input device")
    candidates = []
    for match in _EVENT_DEVICE.finditer(text):
        body = match.group("body")
        axes = {}
        lines = body.splitlines()
        for index, line in enumerate(lines):
            axis_match = _AXIS_NAME.search(line)
            code_match = _AXIS_CODE.search(line)
            if axis_match:
                name = axis_match.group("axis")
            elif code_match:
                code = code_match.group("code")
                name = {"0000": "ABS_X", "0001": "ABS_Y",
                        "0035": "ABS_MT_POSITION_X", "0036": "ABS_MT_POSITION_Y"}[code]
            else:
                continue
            axis_range = None
            for offset, range_line in enumerate(lines[index:index + 4]):
                if offset and (_AXIS_NAME.search(range_line) or _AXIS_CODE.search(range_line)):
                    break
                axis_range = _AXIS_RANGE.search(range_line)
                if axis_range:
                    break
            if axis_range:
                low, high = axis_range.group("min"), axis_range.group("max")
                axes[name.endswith("_X") and "x" or "y"] = (
                    _parse_getevent_integer(low), _parse_getevent_integer(high)
                )
        if "x" in axes and "y" in axes:
            candidates.append((match.group("path"), axes["x"], axes["y"], body))
    direct_candidates = [candidate for candidate in candidates
                         if "input_prop_direct" in candidate[3].casefold()]
    touch_candidates = [candidate for candidate in candidates if "touch" in candidate[3].casefold()]
    selected = direct_candidates or touch_candidates or candidates
    if not selected:
        raise ValueError("getevent -p did not report a touchscreen with X/Y ranges")
    path, x_range, y_range, _ = selected[0]
    return path, x_range[0], x_range[1], y_range[0], y_range[1]


def _parse_getevent_integer(value: str) -> int:
    sign = -1 if value.startswith("-") else 1
    token = value.lstrip("-")
    if token.casefold().startswith("0x"):
        return sign * int(token[2:], 16)
    # getevent -p uses zero-padded hexadecimal values, often without a 0x prefix.
    if any(char.casefold() in "abcdef" for char in token) or (len(token) > 1 and token.startswith("0")):
        return sign * int(token, 16)
    return sign * int(token, 10)


def scale_touch(x: int, y: int, ranges: tuple[int, int, int, int], size: tuple[int, int]) -> tuple[int, int]:
    xmin, xmax, ymin, ymax = ranges
    width, height = size
    if xmax <= xmin or ymax <= ymin:
        raise ValueError("Invalid touchscreen coordinate range")
    sx = round((x - xmin) * (width - 1) / (xmax - xmin))
    sy = round((y - ymin) * (height - 1) / (ymax - ymin))
    return max(0, min(width - 1, sx)), max(0, min(height - 1, sy))


class UptimeCalibrator:
    def __init__(self, adb: AdbCommandWrapper, wall_clock=time.time):
        self.adb = adb
        self.wall_clock = wall_clock
        self.offset_seconds: float | None = None
        self.samples: list[dict] = []
        self.last_calibrated = 0.0
        self.last_uptime_seconds: float | None = None
        self.last_monotonic = 0.0
        self.event_offset_seconds: float | None = None

    def calibrate(self) -> float:
        samples = []
        for _ in range(3):
            before = self.wall_clock()
            result = self.adb.run("uptime", timeout=10)
            after = self.wall_clock()
            if result.returncode != 0:
                raise RuntimeError("Could not read /proc/uptime")
            try:
                uptime_seconds = float((result.stdout or "").split()[0])
            except (ValueError, IndexError) as exc:
                raise RuntimeError("Invalid /proc/uptime output") from exc
            samples.append({"uptime_seconds": uptime_seconds,
                            "laptop_epoch_seconds": (before + after) / 2,
                            "uncertainty_seconds": (after - before) / 2})
            time.sleep(0.02)
        best = min(samples, key=lambda row: row["uncertainty_seconds"])
        self.samples = samples
        self.offset_seconds = best["laptop_epoch_seconds"] - best["uptime_seconds"]
        self.last_calibrated = time.monotonic()
        self.last_monotonic = self.last_calibrated
        self.last_uptime_seconds = best["uptime_seconds"]
        return self.offset_seconds

    def epoch(self, boot_seconds: float) -> float:
        if self.event_offset_seconds is not None:
            return float(boot_seconds) + self.event_offset_seconds
        current_mono = time.monotonic()
        slept_or_clock_shifted = False
        if self.last_uptime_seconds is not None:
            expected_delta = current_mono - self.last_monotonic
            actual_delta = float(boot_seconds) - self.last_uptime_seconds
            slept_or_clock_shifted = abs(actual_delta - expected_delta) > 1.5
        if (self.offset_seconds is None or current_mono - self.last_calibrated >= 30
                or slept_or_clock_shifted):
            self.calibrate()
        return float(boot_seconds) + float(self.offset_seconds)

    def align_event_clock(self, event_seconds: float, received_epoch: float):
        """Align getevent's timestamp clock with laptop time when a touch arrives."""
        base_offset = self.event_offset_seconds
        if base_offset is None:
            base_offset = self.offset_seconds
        if base_offset is None or abs((float(event_seconds) + base_offset) - float(received_epoch)) > 1.5:
            self.event_offset_seconds = float(received_epoch) - float(event_seconds)


class TouchEventParser:
    """Convert getevent text into completed single-pointer gestures."""

    def __init__(self, clock: UptimeCalibrator):
        self.clock = clock
        self.active: dict | None = None
        self.slot = 0

    def feed(self, line: str, snapshot_before_touch=None, received_epoch: float | None = None) -> dict | None:
        match = _EVENT_LINE.search(line)
        if not match:
            return None
        boot_ts = float(match.group("ts"))
        kind, code, value = match.group("kind"), match.group("code"), match.group("value")
        down = False
        up = False
        if kind == "ABS" and code == "ABS_MT_SLOT":
            self.slot = int(value, 16)
        elif kind == "ABS" and code == "ABS_MT_TRACKING_ID":
            tracking = int(value, 16)
            if tracking >= 0x80000000:
                tracking = -1
            down, up = tracking != -1, tracking == -1
        elif kind == "KEY" and code == "BTN_TOUCH":
            down = value.upper() in {"DOWN", "00000001", "1"}
            up = value.upper() in {"UP", "00000000", "0"}
        if down and self.active is None:
            self.active = {"start_boot": boot_ts, "end_boot": boot_ts, "x0": None, "y0": None,
                           "x1": None, "y1": None, "snapshot": snapshot_before_touch}
        elif self.active is not None:
            self.active["end_boot"] = boot_ts
        if kind == "ABS" and self.active is not None:
            if code in {"ABS_MT_POSITION_X", "ABS_X"}:
                self.active["x1"] = int(value, 16)
                if self.active["x0"] is None:
                    self.active["x0"] = self.active["x1"]
            elif code in {"ABS_MT_POSITION_Y", "ABS_Y"}:
                self.active["y1"] = int(value, 16)
                if self.active["y0"] is None:
                    self.active["y0"] = self.active["y1"]
        if up and self.active is not None:
            active, self.active = self.active, None
            if None in (active["x0"], active["y0"], active["x1"], active["y1"]):
                return None
            if received_epoch is not None and hasattr(self.clock, "align_event_clock"):
                self.clock.align_event_clock(active["end_boot"], received_epoch)
            active["start_ts"] = self.clock.epoch(active["start_boot"])
            active["end_ts"] = self.clock.epoch(active["end_boot"])
            active["duration_ms"] = max(0, round((active["end_boot"] - active["start_boot"]) * 1000))
            return active
        return None


def classify_gesture(gesture: dict, *, width: int, height: int, dpi: float = 160.0) -> tuple[str, bool]:
    distance = ((gesture["x1"] - gesture["x0"]) ** 2 + (gesture["y1"] - gesture["y0"]) ** 2) ** 0.5
    threshold_px = max(12.0, 24.0 * dpi / 160.0)
    if distance <= threshold_px and gesture["duration_ms"] <= 500:
        return "tap", False
    edge_px = max(24, round(width * 0.05))
    edge = gesture["x0"] <= edge_px or gesture["x0"] >= width - edge_px
    return ("system_gesture" if edge else "swipe"), edge


def parse_foreground_window(text: str) -> tuple[str | None, str | None]:
    preferred = re.search(r"mFocusedApp.*?\bu\d+\s+([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)", text)
    match = preferred or _FOCUS.search(text)
    if not match:
        return None, None
    if preferred:
        return match.group(1), match.group(2)
    return match.group("package"), match.group("activity")


def parse_ime_window_bounds(text: str, ime_component: str) -> tuple[int, int, int, int] | None:
    ime_package = ime_component.split("/", 1)[0].strip()
    if not ime_package:
        return None
    blocks = re.split(r"(?=Window\{)", text)
    for block in blocks:
        if ime_package not in block[:400]:
            continue
        frames = _WINDOW_BOUNDS.findall(block[:3000])
        if frames:
            return tuple(map(int, frames[-1]))
    return None


def extract_pid(activity_dump: str, app_id: str) -> str | None:
    for match in _APP_PID.finditer(activity_dump):
        if match.group("package") == app_id:
            return match.group("pid")
    return None


def _hierarchy_details(xml_text: str) -> tuple[bool, list[dict]]:
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return False, []
    nodes = []
    digits = set()
    for node in root.iter():
        attrs = {str(k).casefold().replace("_", "-"): str(v) for k, v in node.attrib.items()}
        password = attrs.get("password", "false").casefold() == "true"
        label = attrs.get("text", "") or attrs.get("content-desc", "") or attrs.get("resource-id", "")
        bounds = _WINDOW_BOUNDS.search(attrs.get("bounds", ""))
        if bounds:
            rect = tuple(map(int, bounds.groups()))
            nodes.append({"label": label, "rect": rect, "password": password,
                          "clickable": attrs.get("clickable", "false").casefold() == "true"})
        if attrs.get("clickable", "false").casefold() == "true" and re.fullmatch(r"\d", label.strip()):
            digits.add(label.strip())
        if password:
            nodes.append({"label": "", "rect": tuple(map(int, bounds.groups())) if bounds else None,
                          "password": True, "clickable": False})
    return any(node.get("password") for node in nodes) or len(digits) >= 6, nodes


def _node_label(nodes: list[dict], x: int, y: int) -> str | None:
    containing = []
    for node in nodes:
        rect = node.get("rect")
        if not rect:
            continue
        left, top, right, bottom = rect
        if left <= x <= right and top <= y <= bottom and node.get("label"):
            containing.append((max(1, right - left) * max(1, bottom - top), node["label"]))
    return redact_marker_text(min(containing)[1]) if containing else None


def vault_safety_warnings(path: Path) -> list[str]:
    resolved = path.expanduser().resolve()
    warnings = []
    if any((parent / ".git").exists() for parent in (resolved, *resolved.parents)):
        warnings.append("Vault path is inside a Git repository.")
    normalized = str(resolved).casefold().replace("\\", "/")
    sync_markers = ("/onedrive/", "/dropbox/", "/google drive/", "/googledrive/", "/icloud drive/")
    if any(marker in f"{normalized}/" for marker in sync_markers):
        warnings.append("Vault path appears to be inside a synced folder.")
    return warnings


class ManualAndroidCapture:
    def __init__(self, profile: AndroidAppProfile, *, device_id: str,
                 vault_path="./ObsidianVault", report_dir=DEFAULT_REPORT_DIR,
                 which=None, command_runner=subprocess.run, process_factory=subprocess.Popen,
                 input_fn=input):
        self.profile = profile
        self.device_id = device_id
        self.vault_path = Path(vault_path)
        self.report_dir = Path(report_dir)
        self.which = which or __import__("shutil").which
        self.command_runner = command_runner
        self.process_factory = process_factory
        self.input_fn = input_fn
        self.stop_event = threading.Event()
        self.state_lock = threading.RLock()
        self.foreground: tuple[str | None, str | None] = (None, None)
        self.ime_bounds: tuple[int, int, int, int] | None = None
        self.ime_component = ""
        self.latest_snapshot: dict | None = None
        self.step_seq = 0
        self.steps: list[dict] = []
        self.snapshot_seq = 0
        self.last_snapshot_started = 0.0
        self.errors: list[str] = []
        self.source_errors: list[str] = []
        self.missing_ui_evidence: list[str] = []
        self.persisted_event_count = 0
        self.snapshot_threads: list[threading.Thread] = []
        self.touch_event_lines = 0
        self.completed_gestures = 0
        self.foreground_filtered_gestures = 0
        self.foreground_checks = 0

    def _snapshot_after_touch(self, adb: AdbCommandWrapper, run_dir: Path, event_id: str):
        if not self.profile.ui_snapshots:
            return
        delay = max(0, self.profile.ui_snapshot_delay_ms) / 1000
        if self.stop_event.wait(delay):
            self.missing_ui_evidence.append(event_id)
            return
        now = time.monotonic()
        if now - self.last_snapshot_started < self.profile.ui_snapshot_min_interval_ms / 1000:
            self.missing_ui_evidence.append(event_id)
            return
        self.last_snapshot_started = now
        try:
            result = adb.run("hierarchy", timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            self.errors.append(f"Hierarchy snapshot failed for {event_id}: {redact_log_line(str(exc))}")
            self.missing_ui_evidence.append(event_id)
            return
        if result.returncode != 0:
            self.errors.append(f"Hierarchy snapshot timed out or failed for {event_id}")
            self.missing_ui_evidence.append(event_id)
            return
        if self.stop_event.is_set():
            self.missing_ui_evidence.append(event_id)
            return
        raw = (result.stdout or "").strip()
        xml_start = raw.find("<?xml")
        if xml_start >= 0:
            raw = raw[xml_start:]
        else:
            hierarchy_start = raw.find("<hierarchy")
            if hierarchy_start >= 0:
                raw = raw[hierarchy_start:]
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            root = None
        if root is None or root.tag != "hierarchy":
            self.errors.append(f"Hierarchy dump returned no UI tree for {event_id}")
            self.missing_ui_evidence.append(event_id)
            return
        security_sensitive, nodes = _hierarchy_details(raw)
        safe_tree = sanitize_hierarchy(raw)
        self.snapshot_seq += 1
        file_name = f"{event_id}.xml"
        (run_dir / "hierarchy" / file_name).write_text(safe_tree, encoding="utf-8")
        with self.state_lock:
            self.latest_snapshot = {"ts": time.time(), "raw": raw, "nodes": nodes,
                                    "secure": security_sensitive, "file": f"hierarchy/{file_name}"}

    def _refresh_foreground(self, adb: AdbCommandWrapper):
        result = adb.run("window", timeout=10)
        if result.returncode != 0:
            self.errors.append("dumpsys window failed; touch evidence paused")
            with self.state_lock:
                self.foreground = (None, None)
            return
        window_text = result.stdout or ""
        with self.state_lock:
            self.foreground = parse_foreground_window(window_text)
            self.ime_bounds = parse_ime_window_bounds(window_text, self.ime_component)
            self.foreground_checks += 1

    def _foreground_loop(self, adb: AdbCommandWrapper):
        while not self.stop_event.is_set():
            try:
                self._refresh_foreground(adb)
            except Exception as exc:
                self.errors.append(f"Foreground check failed: {redact_log_line(str(exc))}")
                with self.state_lock:
                    self.foreground = (None, None)
            self.stop_event.wait(0.25)

    def _join_snapshot_threads(self, timeout: float = 2.0):
        """Give in-flight text hierarchy dumps a short chance to finish on shutdown."""
        deadline = time.monotonic() + timeout
        for thread in self.snapshot_threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

    def _add_step(self, vault: VaultWriter, session_id: str, gesture: dict, size, ranges,
                  snapshot, run_dir, adb, ime_component: str):
        context = gesture.get("snapshot") or {}
        package, activity = context.get("foreground", (None, None))
        prior_snapshot = context.get("hierarchy")
        ime_bounds = context.get("ime_bounds")
        if package != self.profile.app_id:
            return
        width, height = size
        raw_x, raw_y = gesture["x1"], gesture["y1"]
        x, y = scale_touch(raw_x, raw_y, ranges, size)
        x0, y0 = scale_touch(gesture["x0"], gesture["y0"], ranges, size)
        scaled_gesture = {**gesture, "x0": x0, "y0": y0, "x1": x, "y1": y}
        kind, edge = classify_gesture(scaled_gesture, width=width, height=height)
        in_ime = bool(ime_bounds and ime_bounds[0] <= x <= ime_bounds[2]
                      and ime_bounds[1] <= y <= ime_bounds[3])
        secure = (not self.profile.ui_snapshots or prior_snapshot is None
                  or prior_snapshot.get("secure", False) or in_ime)
        if kind == "tap" and secure:
            step_type, label, selector = "secure_input", "secure_input", None
        elif kind == "tap":
            step_type = "tap"
            label = _node_label(prior_snapshot.get("nodes", []), x, y) if prior_snapshot else None
            selector = f"{x},{y}" if not secure else None
        else:
            step_type = "system_gesture" if edge else kind
            label = "system gesture" if edge else step_type
            selector = None if secure else f"{x0},{y0}->{x},{y}"
        event_id = f"touch-{self.step_seq + 1}"
        step = {
            "step_type": step_type,
            "selector": selector,
            "label": redact_marker_text(label or step_type),
            "page_url": f"android://{self.profile.app_id}/{activity or 'unknown'}",
            "ts": gesture["start_ts"],
            "action_id": event_id,
            "tag": None,
            "elem_type": None,
            "role": None,
            "checked": None,
            "is_native_control": True,
        }
        if self.profile.ui_snapshots and prior_snapshot is None:
            step["ui_evidence"] = "missing"
            self.missing_ui_evidence.append(event_id)
        with self.state_lock:
            self.step_seq += 1
            self.steps.append({**step, "foreground_package": package, "activity": activity})
        vault.record_step(session_id, step)
        _write_json(run_dir / "steps.json", self.steps)
        print(f"Captured app interaction {self.step_seq}: {step_type}")
        snapshot_thread = threading.Thread(target=self._snapshot_after_touch,
                                           args=(adb, run_dir, event_id), daemon=True)
        self.snapshot_threads.append(snapshot_thread)
        snapshot_thread.start()

    def _read_network_log_line(self, line: str, collector: CaptureCollector):
        if not self.profile.logcat_json:
            return
        start, end = line.find("{"), line.rfind("}")
        if start < 0 or end <= start:
            return
        try:
            raw = json.loads(line[start:end + 1])
            if not isinstance(raw, dict) or not raw.get("method") or not raw.get("url"):
                return
            item = sanitize_event(raw)
        except (ValueError, TypeError):
            return
        if not host_allowed(item.get("host", ""), self.profile.allowed_hosts):
            return
        with collector.lock:
            if item.get("host"):
                collector.hosts.add(item["host"])
            collector.events.append(item)

    def run(self, *, network_export: Path | None = None) -> dict:
        started_monotonic = time.monotonic()
        run_dir = _now_run_dir(self.report_dir)
        (run_dir / "hierarchy").mkdir()
        logs = []
        result = {
            "mode": "manual", "app": self.profile.name, "app_id": self.profile.app_id,
            "device_id": self.device_id, "status": "failed", "failure": None,
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "artifact_dir": str(run_dir), "vault_session_id": None,
            "network_events": 0, "interaction_steps": 0, "foreground_samples": 0,
            "hierarchy_snapshots": 0, "network_source": None, "source_errors": [],
            "host_inventory": [], "host_failures": {}, "proxy_diagnostics": {},
            "proxy_host_diagnostics": {}, "vault_warnings": vault_safety_warnings(self.vault_path),
            "image_artifacts": [],
        }
        adb_path = self.which("adb")
        if not adb_path:
            result["failure"] = "ADB was not found on PATH"
            _write_json(run_dir / "summary.json", result)
            return result
        try:
            adb = AdbCommandWrapper(adb_path, self.device_id, command_runner=self.command_runner,
                                    process_factory=self.process_factory)
        except ValueError as exc:
            result["failure"] = str(exc)
            _write_json(run_dir / "summary.json", result)
            return result
        vault = None
        proxy = None
        logcat = None
        touch_process = None
        clock = None
        worker_threads: list[threading.Thread] = []
        collector = CaptureCollector(self.profile.allowed_hosts).start()
        session_id = "android-manual-" + uuid.uuid4().hex
        result["vault_session_id"] = session_id
        try:
            window = adb.run("window", timeout=15)
            if window.returncode != 0:
                raise RuntimeError("Phone is not reachable through the approved ADB commands")
            package = adb.run("package", self.profile.app_id, timeout=15)
            if package.returncode != 0 or self.profile.app_id not in (package.stdout or ""):
                raise RuntimeError(f"App {self.profile.app_id} is not installed or package query failed")
            size_out = adb.run("wm_size")
            caps_out = adb.run("getevent_caps")
            if size_out.returncode != 0 or caps_out.returncode != 0:
                raise RuntimeError("Could not inspect display or touch-device ranges")
            size = parse_display_size(size_out.stdout or "")
            event_device, xmin, xmax, ymin, ymax = parse_touch_ranges(caps_out.stdout or "")
            ranges = (xmin, xmax, ymin, ymax)
            clock = UptimeCalibrator(adb)
            clock.calibrate()
            ime_out = adb.run("default_ime")
            ime_component = (ime_out.stdout or "").strip()
            self.ime_component = ime_component
            result["display_size"] = {"width": size[0], "height": size[1]}
            result["touch_device"] = event_device
            result["clock_calibration"] = {
                "samples": clock.samples,
                "uptime_offset_seconds": clock.offset_seconds,
                "getevent_offset_seconds": clock.event_offset_seconds,
                "getevent_clock_corrected": clock.event_offset_seconds is not None,
            }
            result["input_method_component"] = redact_marker_text(ime_component)
            vault = VaultWriter(self.vault_path)
            vault.start_session(session_id, f"android://{self.profile.app_id}")

            live_sources = list(self.profile.network_sources)
            active_source_index = None
            for source_index, source in enumerate(live_sources):
                if source == "proxy":
                    proxy_path = self.which("mitmdump")
                    if not proxy_path:
                        self.source_errors.append("proxy: mitmdump was not found")
                        continue
                    try:
                        helper = AndroidCaptureRunner(
                            self.profile, report_dir=self.report_dir, vault_path=self.vault_path,
                            device_id=self.device_id, which=self.which,
                            command_runner=self.command_runner, process_factory=self.process_factory,
                        )
                        proxy = helper._start_proxy(collector, proxy_path, self.device_id, logs)
                        result["network_source"] = "proxy"
                        active_source_index = source_index
                        break
                    except Exception as exc:
                        self.source_errors.append("proxy: " + redact_log_line(str(exc)))
                elif source == "logcat":
                    pid = _resolve_pid(adb_path, self.device_id, self.profile.app_id, self.command_runner)
                    if not pid:
                        self.source_errors.append("logcat: unable to resolve app PID from dumpsys activity")
                        continue
                    try:
                        logcat = ProcessScopedLogcat(
                            adb_path, self.device_id, self.profile.app_id,
                            run_dir / "device-logcat-redacted.txt",
                            on_line=lambda line: self._read_network_log_line(line, collector),
                        ).start()
                        result["network_source"] = "logcat"
                        active_source_index = source_index
                        break
                    except Exception as exc:
                        self.source_errors.append("logcat: " + redact_log_line(str(exc)))
                elif source == "app_log":
                    if network_export and network_export.is_file():
                        try:
                            imported_events = parse_network_file(network_export, "json")
                            result["network_source"] = "app_log"
                            active_source_index = source_index
                            for event in imported_events:
                                if host_allowed(event.get("host", ""), self.profile.allowed_hosts):
                                    with collector.lock:
                                        collector.events.append(event)
                                        if event.get("host"):
                                            collector.hosts.add(event["host"])
                            break
                        except (OSError, ValueError, TypeError) as exc:
                            self.source_errors.append("app_log: " + redact_log_line(str(exc)))
                    else:
                        self.source_errors.append("app_log: provide a copied normalized JSON file with --network-export")

            foreground_thread = threading.Thread(target=self._foreground_loop, args=(adb,), daemon=True)
            foreground_thread.start()
            worker_threads.append(foreground_thread)
            touch_process = adb.popen("getevent_stream", event_device, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
            if touch_process.stdout is None:
                raise RuntimeError("getevent did not provide a readable event stream")
            touch_stderr: list[str] = []
            def drain_touch_stderr():
                if touch_process.stderr is None:
                    return
                for line in touch_process.stderr:
                    if len(touch_stderr) < 20:
                        touch_stderr.append(redact_log_line(line.strip()))
            stderr_reader = threading.Thread(target=drain_touch_stderr, daemon=True)
            stderr_reader.start()
            worker_threads.append(stderr_reader)
            parser = TouchEventParser(clock)
            saver = threading.Thread(target=self._checkpoint_loop,
                                     args=(run_dir, result, collector, vault, session_id), daemon=True)
            saver.start()
            worker_threads.append(saver)
            print("Manual Android capture is ready. Use the app on the phone; type q and Enter to stop.")
            print("Live counts: app interactions and API events will be reported during capture.")
            reader = threading.Thread(
                target=self._touch_loop,
                args=(touch_process, parser, adb, vault, session_id, size, ranges, run_dir), daemon=True,
            )
            reader.start()
            worker_threads.append(reader)
            control_queue: queue.Queue[str] = queue.Queue()
            def wait_for_stop():
                try:
                    control_queue.put(self.input_fn().strip().casefold())
                except (EOFError, KeyboardInterrupt):
                    control_queue.put("interrupt")
            control_thread = threading.Thread(target=wait_for_stop, daemon=True)
            control_thread.start()
            while not self.stop_event.is_set():
                if proxy is not None and proxy.poll() is not None:
                    failed_index = active_source_index if active_source_index is not None else -1
                    self.source_errors.append("proxy: process exited during capture")
                    proxy = None
                    for fallback_index in range(failed_index + 1, len(live_sources)):
                        fallback = live_sources[fallback_index]
                        if fallback == "app_log":
                            if network_export and network_export.is_file():
                                try:
                                    imported_events = parse_network_file(network_export, "json")
                                    for event in imported_events:
                                        if host_allowed(event.get("host", ""), self.profile.allowed_hosts):
                                            with collector.lock:
                                                collector.events.append(event)
                                                if event.get("host"):
                                                    collector.hosts.add(event["host"])
                                    result["network_source"] = "app_log"
                                    active_source_index = fallback_index
                                    break
                                except (OSError, ValueError, TypeError) as exc:
                                    self.source_errors.append("app_log: " + redact_log_line(str(exc)))
                                    continue
                            self.source_errors.append("app_log: no copied export was provided")
                        elif fallback == "logcat":
                            pid = _resolve_pid(adb_path, self.device_id, self.profile.app_id, self.command_runner)
                            if not pid:
                                self.source_errors.append("logcat: unable to resolve app PID after proxy failure")
                                continue
                            try:
                                logcat = ProcessScopedLogcat(
                                    adb_path, self.device_id, self.profile.app_id,
                                    run_dir / "device-logcat-redacted.txt",
                                    on_line=lambda line: self._read_network_log_line(line, collector),
                                ).start()
                                result["network_source"] = "logcat"
                                active_source_index = fallback_index
                                break
                            except Exception as exc:
                                self.source_errors.append("logcat: " + redact_log_line(str(exc)))
                try:
                    command = control_queue.get(timeout=0.25)
                    if command in {"q", "interrupt"}:
                        print("Stop requested; closing capture and saving the session...")
                        if command == "interrupt":
                            result["status"] = "partial"
                            result["failure"] = "Capture interrupted; partial session saved."
                        break
                except queue.Empty:
                    if touch_process.poll() is not None:
                        result["status"] = "partial"
                        detail = " ".join(touch_stderr).casefold()
                        if "permission denied" in detail or "not permitted" in detail:
                            result["failure"] = "Touch-event access was denied by Android; partial session saved."
                        else:
                            result["failure"] = "Phone disconnected or getevent stopped; partial session saved."
                        break
                except KeyboardInterrupt:
                    result["status"] = "partial"
                    result["failure"] = "Capture interrupted; partial session saved."
                    break
            self.stop_event.set()
            touch_process.terminate()
            try:
                touch_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                touch_process.kill()
            reader.join(timeout=3)
            stderr_reader.join(timeout=1)
            foreground_thread.join(timeout=2)
            saver.join(timeout=2)
            if logcat:
                logcat.close()
                logcat = None
            if proxy and proxy.poll() is None:
                proxy.terminate()
                try:
                    proxy.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proxy.kill()
                proxy = None
            collector.accept_events = False

            self._flush_pending_events(vault, session_id, collector)

            markers = [{"step_id": step.get("action_id"), "ts": step.get("ts")} for step in self.steps]
            with collector.lock:
                captured_events = list(collector.events)
            correlated = assign_events_to_markers(markers, captured_events) if markers else [
                {**event, "action_id": None, "correlation_status": "ambiguous",
                 "correlation_reason": "no interaction steps in session"} for event in collector.events
            ]
            for event in correlated:
                vault.conn.execute(
                    "UPDATE session_api_events SET action_id=?, correlation_status=?, entry_point=? "
                    "WHERE session_id=? AND seq=?",
                    (event.get("action_id"), event.get("correlation_status") or "ambiguous",
                     f"Android interaction {event.get('action_id') or 'ambiguous'}", session_id,
                     event.get("event_seq")),
                )
            vault.conn.commit()
            vault.end_session(session_id)
            result.update(
                status=(result.get("status") if result.get("status") == "partial"
                        else "passed" if self.steps else "failed"),
                interaction_steps=len(self.steps), network_events=len(correlated),
                host_inventory=sorted(collector.hosts), host_failures=collector.host_failures,
                proxy_diagnostics=dict(collector.proxy_diagnostics),
                proxy_host_diagnostics={h: dict(c) for h, c in collector.proxy_host_diagnostics.items()},
                source_errors=self.source_errors,
                hierarchy_snapshots=sum(1 for p in (run_dir / "hierarchy").glob("*.xml")),
                missing_ui_evidence=self.missing_ui_evidence,
                foreground_samples=self.foreground_checks,
                touch_event_lines=self.touch_event_lines,
                completed_gestures=self.completed_gestures,
                foreground_filtered_gestures=self.foreground_filtered_gestures,
                duration_seconds=round(time.monotonic() - started_monotonic, 3),
                correlation_summary={
                    "matched": sum(e.get("correlation_status") == "matched" for e in correlated),
                    "ambiguous": sum(e.get("correlation_status") == "ambiguous" for e in correlated),
                },
            )
            if not correlated:
                result["network_warning"] = "No events from the selected source; UI interactions are recorded independently."
            if not self.steps and result.get("status") != "partial":
                result["failure"] = (
                    "No in-app touch interactions were captured. Use the profiled app on the phone and "
                    "check touch_event_lines, completed_gestures, and foreground_filtered_gestures in the summary."
                )
            _write_json(run_dir / "network-events.json", correlated)
            result["warnings"] = self.errors + self.source_errors + result["vault_warnings"]
        except KeyboardInterrupt:
            result["failure"] = "Capture interrupted; partial session saved."
            result["status"] = "partial" if vault is not None else "failed"
        except Exception as exc:
            result["failure"] = redact_log_line(str(exc))
            result["status"] = "partial" if vault is not None else "failed"
            if vault is not None:
                try:
                    vault.end_session(session_id)
                except Exception:
                    pass
        finally:
            self.stop_event.set()
            if touch_process is not None and touch_process.poll() is None:
                try:
                    touch_process.terminate()
                    touch_process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        touch_process.kill()
                    except OSError:
                        pass
            for worker in worker_threads:
                worker.join(timeout=3)
            self._join_snapshot_threads(timeout=2.0)
            if logcat:
                logcat.close()
            if proxy and proxy.poll() is None:
                proxy.terminate()
                try:
                    proxy.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proxy.kill()
            collector.close()
            if vault:
                try:
                    self._flush_pending_events(vault, session_id, collector)
                    markers = [{"step_id": step.get("action_id"), "ts": step.get("ts")}
                               for step in self.steps]
                    with collector.lock:
                        final_events = list(collector.events)
                    final_correlated = assign_events_to_markers(markers, final_events) if markers else [
                        {**event, "action_id": None, "correlation_status": "ambiguous",
                         "correlation_reason": "no interaction steps in session"}
                        for event in final_events
                    ]
                    for event in final_correlated:
                        if event.get("event_seq") is not None:
                            vault.conn.execute(
                                "UPDATE session_api_events SET action_id=?, correlation_status=?, entry_point=? "
                                "WHERE session_id=? AND seq=?",
                                (event.get("action_id"), event.get("correlation_status") or "ambiguous",
                                 f"Android interaction {event.get('action_id') or 'ambiguous'}", session_id,
                                 event["event_seq"]),
                            )
                    vault.conn.commit()
                    _write_json(run_dir / "network-events.json", final_correlated)
                    result["network_events"] = len(final_correlated)
                    result["correlation_summary"] = {
                        "matched": sum(e.get("correlation_status") == "matched" for e in final_correlated),
                        "ambiguous": sum(e.get("correlation_status") == "ambiguous" for e in final_correlated),
                    }
                    vault.end_session(session_id)
                except Exception as exc:
                    self.errors.append(f"Final vault flush failed: {redact_log_line(str(exc))}")
                vault.conn.close()
            result["duration_seconds"] = round(time.monotonic() - started_monotonic, 3)
            result["interaction_steps"] = len(self.steps)
            result["touch_event_lines"] = self.touch_event_lines
            result["completed_gestures"] = self.completed_gestures
            result["foreground_filtered_gestures"] = self.foreground_filtered_gestures
            result["foreground_samples"] = self.foreground_checks
            result["missing_ui_evidence"] = list(dict.fromkeys(self.missing_ui_evidence))
            result["source_errors"] = self.source_errors
            result["host_inventory"] = sorted(collector.hosts)
            result["host_failures"] = dict(collector.host_failures)
            result["proxy_diagnostics"] = dict(collector.proxy_diagnostics)
            result["proxy_host_diagnostics"] = {
                host: dict(counts) for host, counts in collector.proxy_host_diagnostics.items()
            }
            result["warnings"] = self.errors + self.source_errors + result.get("vault_warnings", [])
            if clock is not None:
                result["clock_calibration"] = {
                    "samples": clock.samples,
                    "uptime_offset_seconds": clock.offset_seconds,
                    "getevent_offset_seconds": clock.event_offset_seconds,
                    "getevent_clock_corrected": clock.event_offset_seconds is not None,
                }
            image_suffixes = {
                ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".avif",
                ".mp4", ".mov", ".mkv", ".webm", ".avi", ".3gp", ".m4v",
            }
            result["image_artifacts"] = [str(path.relative_to(run_dir)) for path in run_dir.rglob("*")
                                         if path.is_file() and path.suffix.casefold() in image_suffixes]
            if result["image_artifacts"]:
                result["status"] = "failed"
                result["failure"] = "Image artifacts are forbidden in manual Android capture."
            (run_dir / "commands.log").write_text("\n".join(logs), encoding="utf-8")
            _write_json(run_dir / "summary.json", result)
        return result

    def _touch_loop(self, process, parser, adb, vault, session_id, size, ranges, run_dir):
        for line in process.stdout:
            if self.stop_event.is_set():
                break
            if _EVENT_LINE.search(line):
                self.touch_event_lines += 1
            with self.state_lock:
                snapshot = {
                    "hierarchy": self.latest_snapshot,
                    "foreground": self.foreground,
                    "ime_bounds": self.ime_bounds,
                }
            try:
                gesture = parser.feed(
                    line, snapshot_before_touch=snapshot,
                    received_epoch=time.time() if _EVENT_LINE.search(line) else None,
                )
                if gesture:
                    self.completed_gestures += 1
                    if (gesture.get("snapshot") or {}).get("foreground", (None, None))[0] != self.profile.app_id:
                        self.foreground_filtered_gestures += 1
                    self._add_step(vault, session_id, gesture, size, ranges, snapshot, run_dir,
                                   adb, self.ime_component)
            except Exception as exc:
                self.errors.append(f"Touch event error: {redact_log_line(str(exc))}")

    def _flush_pending_events(self, vault, session_id, collector):
        with collector.lock:
            pending = list(collector.events[self.persisted_event_count:])
        for event in pending:
            seq = self.persisted_event_count + 1
            event["event_seq"] = seq
            vault.ingest({
                "method": event.get("method") or "GET", "url": event["url"],
                "status": event.get("status"), "request_headers": event.get("request_headers") or {},
                "response_headers": event.get("response_headers") or {},
                "payload": event.get("payload", ""), "response_body": event.get("response_body", ""),
                "entry_point": "Android interaction pending correlation",
                "ts": event.get("ts"), "event_seq": seq,
                "action_id": None, "correlation_status": "ambiguous",
            }, session_id)
            self.persisted_event_count += 1

    def _checkpoint_loop(self, run_dir, result, collector, vault, session_id):
        last_progress = (0, 0, 0, 0, 0, 0, 0)
        while not self.stop_event.wait(5):
            try:
                self._flush_pending_events(vault, session_id, collector)
            except Exception as exc:
                self.errors.append(f"Periodic vault flush failed: {redact_log_line(str(exc))}")
            with collector.lock:
                result["network_events"] = len(collector.events)
                result["host_inventory"] = sorted(collector.hosts)
                result["host_failures"] = dict(collector.host_failures)
                result["proxy_diagnostics"] = dict(collector.proxy_diagnostics)
                result["proxy_host_diagnostics"] = {
                    host: dict(counts) for host, counts in collector.proxy_host_diagnostics.items()
                }
            result["interaction_steps"] = len(self.steps)
            result["touch_event_lines"] = self.touch_event_lines
            result["completed_gestures"] = self.completed_gestures
            result["foreground_filtered_gestures"] = self.foreground_filtered_gestures
            result["foreground_samples"] = self.foreground_checks
            proxy_debug = result.get("proxy_diagnostics", {})
            progress = (
                len(self.steps), result["network_events"],
                proxy_debug.get("client_connections", 0),
                proxy_debug.get("client_connections_from_device", 0),
                proxy_debug.get("other_client_connections", 0),
                proxy_debug.get("proxy_connect_requests", 0),
                proxy_debug.get("client_tls_failures", 0),
            )
            if progress != last_progress:
                print(
                    f"Capture progress: {progress[0]} app interactions, {progress[1]} API events; "
                    f"proxy clients total/device/other {progress[2]}/{progress[3]}/{progress[4]}, "
                    f"CONNECTs {progress[5]}, client TLS failures {progress[6]}."
                )
                last_progress = progress
            _write_json(run_dir / "summary.json", result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture manual Android interactions and network events")
    parser.add_argument("--app", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--profiles", type=Path, default=DEFAULT_PROFILE_PATH)
    parser.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--network-export", type=Path,
                        help="Locally copied normalized JSON export from the phone's Downloads folder")
    return parser


def main(argv=None) -> int:
    from capture_app.workflows.android_capture_support import load_profiles
    args = build_parser().parse_args(argv)
    try:
        profiles = load_profiles(args.profiles)
        if args.app not in profiles:
            raise ValueError(f"Unknown app profile {args.app!r}")
        result = ManualAndroidCapture(
            profiles[args.app], device_id=args.device_id, vault_path=args.vault,
            report_dir=args.report_dir,
        ).run(network_export=args.network_export)
    except (ValueError, OSError) as exc:
        print(f"Android manual capture failed: {exc}")
        return 2
    print(f"Android manual capture {result['status']}; artifacts: {result['artifact_dir']}")
    print(f"Recorded {result.get('interaction_steps', 0)} app interactions and "
          f"{result.get('network_events', 0)} API events.")
    if result.get("vault_session_id"):
        print(f"Vault session: {result['vault_session_id']}")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
