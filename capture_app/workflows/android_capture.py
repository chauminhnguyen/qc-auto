"""Profile-based Android UI and network capture using Maestro, ADB, and mitmproxy."""

from __future__ import annotations

import argparse
import json
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from capture_app.storage.vault import _EMAIL_ADDRESS_RE
from capture_app.storage.vault import VaultWriter
from capture_app.workflows.android_adb import AdbCommandWrapper
from capture_app.workflows.android_capture_support import (
    AndroidAppProfile,
    CaptureCollector,
    assign_events_to_markers,
    host_allowed,
    load_profiles,
    parse_network_file,
    sanitize_hierarchy,
    validate_marked_flows,
)
from capture_app.workflows.maestro_device_runner import (
    DEFAULT_REPORT_DIR,
    MaestroDeviceRunner,
    PACKAGE_ID_RE,
    PreflightError,
    _flow_path,
)


DEFAULT_PROFILE_PATH = Path("android/apps.yaml")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b")
_ASSIGNMENT = re.compile(
    r"(?i)([\"']?(?:authorization|cookie|set-cookie|token|password|secret|api[_-]?key|csrf|"
    r"session|email|phone|card|cvv|cvc|address|postal|zip|otp|pin|passcode|verification[_ -]?code)[\"']?)"
    r"(\s*[:=]\s*)(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^,\s;<>{}]+)"
)


def redact_log_line(line: str) -> str:
    line = _BEARER.sub("Bearer [REDACTED]", line)
    line = _JWT.sub("[REDACTED:token]", line)
    def replace_assignment(match):
        value = match.group(3)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'"}:
            value = value[0] + "[REDACTED]" + value[-1]
        else:
            value = "[REDACTED]"
        return match.group(1) + match.group(2) + value
    line = _ASSIGNMENT.sub(replace_assignment, line)
    return _EMAIL_ADDRESS_RE.sub("[REDACTED:email]", line)


def _now_run_dir(root: Path) -> Path:
    run_dir = root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8])
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _write_json(path: Path, value: object):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _capture_device_clock(adb: str, serial: str, command_runner=subprocess.run) -> dict:
    wrapper = AdbCommandWrapper(adb, serial, command_runner=command_runner)
    samples = []
    for _ in range(3):
        before = time.time()
        result = wrapper.run("uptime", timeout=10)
        after = time.time()
        try:
            uptime_seconds = float((result.stdout or "").split()[0])
            if result.returncode == 0:
                samples.append({"uptime_seconds": uptime_seconds,
                                "laptop_midpoint_epoch_seconds": (before + after) / 2,
                                "uncertainty_seconds": (after - before) / 2})
        except (ValueError, IndexError):
            pass
        time.sleep(0.02)
    if not samples:
        return {"samples": [], "offset_seconds": None, "method": "ADB /proc/uptime calibration failed"}
    best = min(samples, key=lambda sample: sample["uncertainty_seconds"])
    return {"samples": samples,
            "offset_seconds": best["laptop_midpoint_epoch_seconds"] - best["uptime_seconds"],
            "uncertainty_seconds": best["uncertainty_seconds"],
            "method": "three ADB /proc/uptime samples; lowest-uncertainty midpoint"}


def _resolve_pid(adb: str, serial: str, app_id: str, command_runner=subprocess.run) -> str | None:
    result = AdbCommandWrapper(adb, serial, command_runner=command_runner).run("activity", timeout=10)
    if result.returncode != 0:
        return None
    match = re.search(rf"\b(\d+):{re.escape(app_id)}(?:/|\s|$)", result.stdout or "")
    return match.group(1) if match else None


class ProcessScopedLogcat:
    """Refreshes --pid logcat when the app process restarts and writes redacted lines only."""

    def __init__(self, adb: str, serial: str, app_id: str, output: Path, on_line=None):
        self.adb, self.serial, self.app_id, self.output = adb, serial, app_id, output
        self.adb_client = AdbCommandWrapper(adb, serial)
        self.on_line = on_line
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.pids: list[str] = []
        self.errors: list[str] = []

    def start(self):
        self.thread = threading.Thread(target=self._supervise, daemon=True)
        self.thread.start()
        return self

    def _supervise(self):
        current_pid = None
        process = None
        reader = None
        with self.output.open("w", encoding="utf-8") as sink:
            while not self.stop_event.is_set():
                try:
                    pid = _resolve_pid(self.adb, self.serial, self.app_id)
                except (OSError, subprocess.SubprocessError) as exc:
                    self.errors.append(str(exc))
                    pid = None
                if pid and pid != current_pid:
                    if process and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                    current_pid = pid
                    self.pids.append(pid)
                    try:
                        process = self.adb_client.popen(
                            "logcat", pid,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace",
                        )
                        reader = threading.Thread(target=self._read_lines,
                                                  args=(process, sink, self.on_line), daemon=True)
                        reader.start()
                    except OSError as exc:
                        self.errors.append(str(exc))
                        process = None
                if process and process.poll() is not None:
                    current_pid = None
                self.stop_event.wait(0.5)
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
            if reader:
                reader.join(timeout=1)

    @staticmethod
    def _read_lines(process, sink, on_line=None):
        if process.stdout is None:
            return
        for line in process.stdout:
            if on_line:
                try:
                    on_line(line)
                except Exception:
                    pass
            sink.write(redact_log_line(line))
            sink.flush()

    @staticmethod
    def command_for_pid(adb: str, serial: str, pid: str) -> list[str]:
        return AdbCommandWrapper(adb, serial).argv("logcat", pid)

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)


class AndroidCaptureRunner:
    def __init__(
        self, profile: AndroidAppProfile, *, report_dir: str | Path = DEFAULT_REPORT_DIR,
        vault_path: str | Path = "./ObsidianVault", device_id: str | None = None,
        expected_text: str | None = None, which=None, command_runner=subprocess.run,
        process_factory=subprocess.Popen,
    ):
        self.profile = profile
        self.report_dir = Path(report_dir)
        self.vault_path = Path(vault_path)
        self.device_id = device_id
        self.expected_text = expected_text or profile.expected_text
        self.which = which or shutil.which
        self.command_runner = command_runner
        self.process_factory = process_factory

    def _run(self, argv: list[str], timeout: int = 60):
        env = os.environ.copy()
        env["MAESTRO_CLI_NO_ANALYTICS"] = "1"
        if os.name == "nt" and Path(argv[0]).suffix.casefold() in {".bat", ".cmd"}:
            return self.command_runner(
                subprocess.list2cmdline(argv), capture_output=True, text=True,
                timeout=timeout, env=env, check=False, shell=True,
            )
        return self.command_runner(
            argv, capture_output=True, text=True, timeout=timeout, env=env,
            check=False, shell=False,
        )

    def _hierarchy(self, maestro: str, serial: str) -> tuple[bool, str]:
        try:
            result = self._run([maestro, "--device", serial, "hierarchy"], timeout=45)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, redact_log_line(str(exc))
        if result.returncode != 0:
            return False, redact_log_line((result.stderr or result.stdout or "hierarchy command failed").strip())
        hierarchy = sanitize_hierarchy(result.stdout or "")
        if not hierarchy.strip():
            return False, "Hierarchy output was empty; the current screen may block accessibility inspection."
        return True, hierarchy

    def _start_proxy(self, collector: CaptureCollector, proxy_path: str, serial: str, logs: list[str]):
        addon = Path(__file__).with_name("mitm_android_addon.py")
        argv = [
            proxy_path, "--quiet", "--listen-host", self.profile.proxy_host,
            "--listen-port", str(self.profile.proxy_port), "-s", str(addon),
            "--set", f"android_collector_url={collector.base_url}",
            "--set", "android_allowed_hosts=" + json.dumps(list(self.profile.allowed_hosts)),
        ]
        serial_host = serial.rsplit(":", 1)[0] if ":" in serial else ""
        if serial_host.startswith("[") and serial_host.endswith("]"):
            serial_host = serial_host[1:-1]
        try:
            client_ip = str(ipaddress.ip_address(serial_host))
        except ValueError:
            client_ip = ""
        if client_ip:
            argv += ["--set", f"android_client_ip={client_ip}"]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path.cwd()) + os.pathsep + env.get("PYTHONPATH", "")
        try:
            process = self.process_factory(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                env=env, cwd=str(Path.cwd()),
            )
        except OSError as exc:
            raise PreflightError(f"Could not start mitmproxy: {exc}") from exc
        logs.append("## managed proxy\n$ " + subprocess.list2cmdline(argv))
        if getattr(process, "stderr", None) is not None:
            def drain_proxy_output():
                for line in process.stderr:
                    safe = redact_log_line(line.rstrip())
                    if safe:
                        logs.append("mitmdump: " + safe)
            reader = threading.Thread(target=drain_proxy_output, daemon=True)
            reader.start()
            try:
                process._android_capture_stderr_reader = reader
            except Exception:
                pass
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise PreflightError(
                    "mitmdump exited before listening. Confirm the capture extra is installed and the proxy port is free."
                )
            try:
                with socket.create_connection(("127.0.0.1", self.profile.proxy_port), timeout=0.2):
                    return process
            except OSError:
                time.sleep(0.1)
        process.terminate()
        raise PreflightError(
            f"mitmproxy did not listen on port {self.profile.proxy_port}; check port conflicts and Android Wi-Fi proxy settings."
        )

    def _maestro_command(self, maestro: str, serial: str, flow_path: Path,
                         temp_dir: Path, marker_url: str, mode: str) -> list[str]:
        command = [maestro, "--device", serial, "test", "--format", "JUNIT",
                   "--output", str(temp_dir / "junit.xml"),
                   "--test-output-dir", str(temp_dir / "artifacts"),
                   "-e", f"APP_ID={self.profile.app_id}",
                   "-e", f"CAPTURE_MARKER_URL={marker_url}",
                   "-e", f"CAPTURE_MODE={mode}",
                   "-e", f"SETTLE_MS={self.profile.settle_ms}"]
        if self.expected_text:
            command += ["-e", f"EXPECTED_TEXT={self.expected_text}"]
        command.append(str(flow_path))
        return command

    @staticmethod
    def _record_log(logs: list[str], label: str, argv: list[str], result):
        logs.extend([
            f"## {label}", f"$ {subprocess.list2cmdline(argv)}",
            f"exit_code: {result.returncode}", "stdout:", redact_log_line(result.stdout or ""),
            "stderr:", redact_log_line(result.stderr or ""), "",
        ])

    def _hierarchy_fallback(self, maestro: str, serial: str, flows: Path,
                            markers: list[dict], output_dir: Path, logs: list[str]) -> dict[str, bool]:
        """Replay linear flow prefixes after Maestro releases UI automation."""
        output_dir.mkdir(parents=True, exist_ok=True)
        files = [flows] if flows.is_file() else sorted(
            path for path in flows.rglob("*") if path.suffix.lower() in {".yaml", ".yml"}
        )
        marker_order = [m for m in markers if m.get("step_id") != "__end__"]
        status: dict[str, bool] = {}
        with tempfile.TemporaryDirectory(prefix="android-hierarchy-") as temp_name:
            temp_root = Path(temp_name)
            for source in files:
                try:
                    docs = list(yaml.safe_load_all(source.read_text(encoding="utf-8")))
                    commands = docs[1] if len(docs) > 1 else None
                    if not isinstance(commands, list):
                        raise ValueError("flow must use a top-level linear command list")
                    profile_markers = []
                    for index, command in enumerate(commands):
                        if not isinstance(command, dict) or "runScript" not in command:
                            continue
                        spec = command["runScript"]
                        env = spec.get("env", {}) if isinstance(spec, dict) else {}
                        step_id = env.get("STEP_ID") if isinstance(env, dict) else None
                        if step_id and step_id != "__end__":
                            action_index = index + 1
                            while (action_index < len(commands)
                                   and isinstance(commands[action_index], dict)
                                   and "waitForAnimationToEnd" in commands[action_index]):
                                action_index += 1
                            if action_index >= len(commands):
                                raise ValueError(f"marker {step_id} has no following action")
                            profile_markers.append((step_id, index))
                    for step_id, marker_index in profile_markers:
                        if not any(m.get("step_id") == step_id for m in marker_order):
                            continue
                        prefix_path = temp_root / f"{re.sub(r'[^A-Za-z0-9_-]', '_', step_id)}.yaml"
                        # A marker is deliberately immediately before the action; the
                        # fallback captures the same pre-action hierarchy as pass one.
                        prefix_commands = commands[:marker_index + 1]
                        for prefix_command in prefix_commands:
                            if isinstance(prefix_command, dict) and isinstance(prefix_command.get("runScript"), dict):
                                script_file = prefix_command["runScript"].get("file")
                                if script_file:
                                    script_path = (source.parent / script_file).resolve()
                                    prefix_command["runScript"]["file"] = str(script_path)
                        prefix_path.write_text(
                            yaml.safe_dump_all([docs[0], prefix_commands],
                                               explicit_start=True, sort_keys=False),
                            encoding="utf-8",
                        )
                        cmd = [maestro, "--device", serial, "test", "--format", "JUNIT",
                               "--output", str(temp_root / f"{prefix_path.stem}.xml"),
                               "-e", f"APP_ID={self.profile.app_id}",
                               "-e", f"SETTLE_MS={self.profile.settle_ms}"]
                        if self.expected_text:
                            cmd += ["-e", f"EXPECTED_TEXT={self.expected_text}"]
                        cmd.append(str(prefix_path))
                        try:
                            replay = self._run(cmd, timeout=900)
                        except (OSError, subprocess.SubprocessError) as exc:
                            logs.append(f"Hierarchy prefix {step_id} error: {exc}")
                            status[step_id] = False
                            continue
                        self._record_log(logs, f"Hierarchy prefix {step_id}", cmd, replay)
                        ok, tree = self._hierarchy(maestro, serial) if replay.returncode == 0 else (False, "prefix replay failed")
                        if ok:
                            target = output_dir / f"{re.sub(r'[^A-Za-z0-9_-]', '_', step_id)}.xml"
                            target.write_text(tree, encoding="utf-8")
                        status[step_id] = ok
                except (OSError, ValueError, yaml.YAMLError) as exc:
                    logs.append(f"Hierarchy fallback unsupported for {source}: {exc}")
                    for marker in marker_order:
                        status.setdefault(marker["step_id"], False)
        return status

    def run(self, mode: str = "results") -> dict:
        started = time.monotonic()
        run_dir = _now_run_dir(self.report_dir)
        hierarchy_dir = run_dir / "hierarchy"
        hierarchy_dir.mkdir()
        artifact_dir = run_dir / "maestro_artifacts"
        artifact_dir.mkdir()
        logs: list[str] = []
        result = {
            "mode": mode, "app": self.profile.name, "app_id": self.profile.app_id,
            "network_source": self.profile.network_source,
            "requested_device_id": self.device_id, "device_id": None,
            "status": "failed", "failure": None, "exit_code": None,
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "duration_seconds": 0.0, "artifact_dir": str(run_dir),
            "vault_session_id": None, "network_events": 0, "marker_count": 0,
            "hierarchy_strategy": "post-run-prefix-replay",
            "proxy_diagnostics": {}, "proxy_host_diagnostics": {}, "host_failures": {},
        }
        proxy = None
        logcat = None
        vault = None
        session_id = None
        session_started = False
        collector = CaptureCollector(self.profile.allowed_hosts)
        maestro_runner = MaestroDeviceRunner(run_dir, which=self.which, command_runner=self.command_runner)
        try:
            if mode not in {"results", "api"}:
                raise PreflightError("Run mode must be results or api")
            if not self.profile.maestro_permitted:
                raise PreflightError(
                    f"Maestro test mode is forbidden by profile {self.profile.name!r}; use manual capture."
                )
            if mode == "api" and self.profile.network_source != "proxy":
                raise PreflightError(
                    f"Profile {self.profile.name!r} uses {self.profile.network_source} network exports; use import-network."
                )
            if not self.profile.allow_prefix_replay:
                raise PreflightError(
                    f"Profile {self.profile.name!r} must set allow_prefix_replay: true after confirming its flow actions are safe to repeat."
                )
            if not PACKAGE_ID_RE.fullmatch(self.profile.app_id):
                raise PreflightError(f"Profile {self.profile.name!r} has an invalid Android package ID")
            if not self.profile.flows.exists():
                raise PreflightError(f"Profile {self.profile.name!r} has a missing flow path")
            flow_path = _flow_path(self.profile.flows)
            expected_step_ids = validate_marked_flows(flow_path)
            maestro = self.which("maestro")
            adb = self.which("adb")
            if not maestro:
                raise PreflightError("Maestro CLI was not found on PATH")
            if not adb:
                raise PreflightError("ADB was not found on PATH; install Android SDK Platform Tools")
            if not self.device_id:
                raise PreflightError("--device-id is required by the Android ADB command allowlist")
            proxy_path = self.which("mitmdump") if mode == "api" else None
            if mode == "api" and not proxy_path:
                raise PreflightError("mitmdump was not found on PATH; install the Android capture extra")

            serial = self.device_id
            result["device_id"] = serial
            adb_client = AdbCommandWrapper(adb, serial, command_runner=self.command_runner)
            device_check = adb_client.run("window", timeout=15)
            logs.extend(["## ADB window preflight", redact_log_line(device_check.stdout or device_check.stderr or "")])
            if device_check.returncode != 0:
                raise PreflightError("ADB could not read the device window; confirm it is connected and authorized.")
            installed = adb_client.run("package", self.profile.app_id, timeout=15)
            logs.extend(["## Installed app check", redact_log_line(installed.stdout or installed.stderr or "")])
            if installed.returncode != 0 or self.profile.app_id not in (installed.stdout or ""):
                raise PreflightError(f"App {self.profile.app_id} is not installed on device {serial}")
            if mode == "results":
                proxy_check = adb_client.run("http_proxy")
                configured_proxy = (proxy_check.stdout or "").strip()
                if proxy_check.returncode == 0 and configured_proxy not in {"", "null", ":0", "0.0.0.0:0"}:
                    raise PreflightError(
                        f"A global Android HTTP proxy is configured ({configured_proxy}); disable it for the results run."
                    )
                result["proxy_mode"] = "no global proxy detected; confirm the active Wi-Fi network also has proxy disabled"
            else:
                result["proxy_mode"] = f"managed mitmdump on port {self.profile.proxy_port}"

            session_id = "android-" + uuid.uuid4().hex
            result["vault_session_id"] = session_id
            vault = VaultWriter(self.vault_path)
            vault.start_session(session_id, f"android://{self.profile.app_id}")
            session_started = True
            clock = _capture_device_clock(adb, serial, self.command_runner)
            _write_json(run_dir / "clock_offset.json", clock)
            logcat = ProcessScopedLogcat(adb, serial, self.profile.app_id, run_dir / "device-logcat-redacted.txt").start()

            collector.start()
            if mode == "api":
                proxy = self._start_proxy(collector, proxy_path, serial, logs)

            with tempfile.TemporaryDirectory(prefix="android-maestro-") as temp_name:
                temp_dir = Path(temp_name)
                command = self._maestro_command(maestro, serial, flow_path, temp_dir, collector.base_url, mode)
                try:
                    maestro_result = self._run(command, timeout=1800)
                except (OSError, subprocess.SubprocessError) as exc:
                    raise PreflightError(f"Could not run Maestro: {exc}") from exc
                self._record_log(logs, "Maestro test", command, maestro_result)
                junit = temp_dir / "junit.xml"
                if junit.is_file():
                    (run_dir / "junit.xml").write_text(redact_log_line(junit.read_text(encoding="utf-8", errors="replace")),
                                                       encoding="utf-8")
            # Maestro's raw screenshots and temporary artifacts are not copied into the report.
            result["exit_code"] = maestro_result.returncode
            if maestro_result.returncode != 0:
                result["failure"] = f"Maestro failed with exit code {maestro_result.returncode}. " + \
                    redact_log_line((maestro_result.stderr or maestro_result.stdout or "").strip())

            markers = sorted(collector.markers, key=lambda item: item["ts"])
            # This Maestro/device combination died when a second hierarchy command
            # connected while a flow was active. Always use the tested sequential
            # prefix pass: finish the flow, release Maestro's driver, then dump trees.
            collector.accept_markers = False
            collector.accept_events = False
            if logcat:
                logcat.close()
            hierarchy_targets = [step_id for step_id in expected_step_ids if step_id != "__end__"]
            fallback = self._hierarchy_fallback(
                maestro, serial, flow_path,
                [{"step_id": step_id} for step_id in hierarchy_targets],
                hierarchy_dir, logs,
            )
            actual_by_id = {marker["step_id"]: marker for marker in markers}
            hierarchy_snapshots = {}
            for step_id in hierarchy_targets:
                marker = actual_by_id.get(step_id)
                safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", step_id)
                if fallback.get(step_id):
                    hierarchy_snapshots[step_id] = {
                        "status": "captured", "file": f"hierarchy/{safe_id}.xml",
                    }
                    if marker:
                        marker["hierarchy_status"] = "captured-fallback"
                        marker["hierarchy_file"] = f"hierarchy/{safe_id}.xml"
                else:
                    hierarchy_snapshots[step_id] = {"status": "failed"}
                    if marker:
                        marker["hierarchy_status"] = "failed"
            result["hierarchy_snapshots"] = hierarchy_snapshots
            reported_markers = list(markers)
            for step_id in expected_step_ids:
                if step_id not in actual_by_id:
                    reported_markers.append({"step_id": step_id, "capture_status": "not_received"})
            for marker in markers:
                marker["capture_status"] = "received"

            result["markers"] = reported_markers
            result["marker_count"] = len(markers)
            result["host_inventory"] = sorted(collector.hosts)
            correlated = assign_events_to_markers(markers, collector.events)
            result["network_events"] = len(correlated)
            result["correlation_summary"] = {
                "matched": sum(e["correlation_status"] == "matched" for e in correlated),
                "ambiguous": sum(e["correlation_status"] == "ambiguous" for e in correlated),
            }
            self._persist_vault(vault, session_id, markers, correlated)
            session_started = False
            _write_json(run_dir / "network-events.json", correlated)
            _write_json(run_dir / "markers.json", reported_markers)
            _write_json(run_dir / "host-inventory.json", result["host_inventory"])
            result["logcat_pids"] = logcat.pids if logcat else []
            result["logcat_errors"] = logcat.errors if logcat else []
            if maestro_result.returncode == 0:
                result["status"] = "passed"
            if not markers:
                result["warnings"] = ["No Maestro step markers were received; add marker.js before each action."]
            if mode == "api" and not collector.events:
                result.setdefault("warnings", []).append(
                    "No proxy-visible HTTP events were captured; the app may ignore the Wi-Fi proxy, reject its CA, or use another protocol."
                )
            actual_step_ids = {m.get("step_id") for m in markers if m.get("step_id") != "__end__"}
            missing_markers = [step_id for step_id in hierarchy_targets if step_id not in actual_step_ids]
            missing_flow_markers = [step_id for step_id in expected_step_ids
                                    if step_id not in {m.get("step_id") for m in markers}]
            missing_snapshots = [step_id for step_id in hierarchy_targets
                                 if hierarchy_snapshots.get(step_id, {}).get("status") != "captured"]
            if result["status"] == "passed" and (
                not hierarchy_targets or missing_snapshots or missing_markers or missing_flow_markers
            ):
                result["status"] = "failed"
                result["failure"] = (
                    "No marked steps were captured" if not hierarchy_targets else
                    "Capture markers missing: " + ", ".join(missing_flow_markers) if missing_flow_markers else
                    "Capture markers missing for steps: " + ", ".join(missing_markers) if missing_markers else
                    "Hierarchy snapshots missing for steps: " + ", ".join(missing_snapshots)
                )
            if result["status"] == "passed" and mode == "api" and not collector.events:
                result["status"] = "failed"
                result["failure"] = "No proxy-visible HTTP events were captured; see warnings for likely causes."
            if mode == "api" and proxy is not None and proxy.poll() is not None:
                result["status"] = "failed"
                result["failure"] = "The managed mitmdump process exited during the capture run."
            if collector.errors:
                result["collector_errors"] = collector.errors
        except Exception as exc:
            result["failure"] = redact_log_line(str(exc))
        finally:
            if logcat:
                logcat.close()
            if proxy:
                if proxy.poll() is None:
                    proxy.terminate()
                    try:
                        proxy.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proxy.kill()
                reader = getattr(proxy, "_android_capture_stderr_reader", None)
                if reader:
                    reader.join(timeout=1)
            collector.close()
            with collector.lock:
                result["proxy_diagnostics"] = dict(collector.proxy_diagnostics)
                result["proxy_host_diagnostics"] = {
                    host: dict(counts) for host, counts in collector.proxy_host_diagnostics.items()
                }
                result["host_failures"] = {
                    host: dict(counts) for host, counts in collector.host_failures.items()
                }
                result["host_inventory"] = sorted(collector.hosts)
            if session_started and vault is not None and session_id:
                try:
                    vault.end_session(session_id)
                except Exception:
                    pass
            if vault is not None:
                vault.conn.close()
            result["duration_seconds"] = round(time.monotonic() - started, 3)
            (run_dir / "commands.log").write_text("\n".join(logs), encoding="utf-8")
            _write_json(run_dir / "summary.json", result)
        return result

    @staticmethod
    def _persist_vault(vault: VaultWriter, session_id: str, markers: list[dict], events: list[dict]):
        marker_ids = set()
        for marker in markers:
            step_id = marker.get("step_id")
            if not step_id or step_id == "__end__" or step_id in marker_ids:
                continue
            marker_ids.add(step_id)
            vault.record_step(session_id, {
                "step_type": marker.get("action_kind") or "tap",
                "selector": marker.get("action_target") or None,
                "label": marker.get("label") or step_id,
                "page_url": vault.conn.execute(
                    "SELECT start_url FROM sessions WHERE session_id=?", (session_id,)
                ).fetchone()[0],
                "ts": marker.get("ts"), "action_id": step_id,
            })
        for seq, event in enumerate(sorted(events, key=lambda item: item.get("ts") or 0), start=1):
            vault.ingest({
                "method": event.get("method") or "GET", "url": event["url"],
                "status": event.get("status"), "request_headers": event.get("request_headers") or {},
                "response_headers": event.get("response_headers") or {},
                "payload": event.get("payload", ""), "response_body": event.get("response_body", ""),
                "entry_point": f"Android step {event.get('action_id') or 'ambiguous'}",
                "ts": event.get("ts"), "event_seq": seq,
                "action_id": event.get("action_id"),
                "correlation_status": event.get("correlation_status"),
            }, session_id)
        vault.end_session(session_id)


def import_network_export(path: Path, file_format: str, vault_path: Path,
                         report_dir: Path, app_name: str, app_id: str,
                         allowed_hosts: tuple[str, ...] = ()) -> dict:
    run_dir = _now_run_dir(report_dir)
    result = {"status": "failed", "app": app_name, "app_id": app_id, "artifact_dir": str(run_dir)}
    vault = None
    try:
        parsed_events = parse_network_file(path, file_format)
        events = [event for event in parsed_events if host_allowed(event.get("host", ""), allowed_hosts)]
        result["host_inventory"] = sorted({event["host"] for event in events if event.get("host")})
        result["filtered_event_count"] = len(parsed_events) - len(events)
        session_id = "android-import-" + uuid.uuid4().hex
        result["vault_session_id"] = session_id
        vault = VaultWriter(vault_path)
        vault.start_session(session_id, f"android://{app_id}")
        for index, event in enumerate(events, start=1):
            event.setdefault("correlation_status", "ambiguous")
            event.setdefault("entry_point", "Android imported network event")
            vault.ingest({
                "method": event["method"], "url": event["url"], "status": event.get("status"),
                "request_headers": event.get("request_headers", {}),
                "response_headers": event.get("response_headers", {}),
                "payload": event.get("payload", ""), "response_body": event.get("response_body", ""),
                "entry_point": event["entry_point"], "ts": event.get("ts"),
                "event_seq": index, "action_id": event.get("action_id"),
                "correlation_status": event.get("correlation_status"),
            }, session_id)
        vault.end_session(session_id)
        result.update(status="passed", event_count=len(events))
        _write_json(run_dir / "network-events.json", events)
    except (OSError, ValueError) as exc:
        result["failure"] = str(exc)
    finally:
        _write_json(run_dir / "summary.json", result)
        if vault is not None:
            vault.conn.close()
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Android Maestro capture or import an app network export")
    parser.add_argument("--profiles", type=Path, default=DEFAULT_PROFILE_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("results", "api"):
        sub = subparsers.add_parser(command, help=f"Run the configured Maestro flow in {command} mode")
        sub.add_argument("--app", required=True, help="App profile key from android/apps.yaml")
        sub.add_argument("--device-id")
        sub.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
        sub.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
        sub.add_argument("--flows", type=Path, help="Override the profile's flow file or directory")
        sub.add_argument("--expected-text")
    manual = subparsers.add_parser("manual", help="Capture live interactions while you use the phone")
    manual.add_argument("--app", required=True, help="App profile key from android/apps.yaml")
    manual.add_argument("--device-id", required=True)
    manual.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    manual.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    manual.add_argument("--network-export", type=Path,
                         help="Locally copied normalized JSON export from the phone's Downloads folder")
    sub = subparsers.add_parser("import-network", help="Import HAR 1.2 or normalized JSON network events")
    sub.add_argument("--app", required=True, help="App profile key from android/apps.yaml")
    sub.add_argument("--input", required=True, type=Path)
    sub.add_argument("--format", choices=("har", "json"), required=True)
    sub.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    sub.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        profiles = load_profiles(args.profiles)
        if args.app not in profiles:
            raise ValueError(f"Unknown app profile {args.app!r}; configured: {', '.join(sorted(profiles))}")
        profile = profiles[args.app]
        if args.command == "import-network":
            result = import_network_export(args.input, args.format, args.vault,
                                           args.report_dir, profile.name, profile.app_id,
                                           profile.allowed_hosts)
        elif args.command == "manual":
            from capture_app.workflows.android_manual_capture import ManualAndroidCapture
            result = ManualAndroidCapture(
                profile, report_dir=args.report_dir, vault_path=args.vault,
                device_id=args.device_id,
            ).run(network_export=args.network_export)
        else:
            if args.flows:
                profile = AndroidAppProfile(
                    name=profile.name, app_id=profile.app_id, flows=args.flows,
                    expected_text=profile.expected_text, network_source=profile.network_source,
                    allow_prefix_replay=profile.allow_prefix_replay,
                    allowed_hosts=profile.allowed_hosts,
                    settle_ms=profile.settle_ms, proxy_host=profile.proxy_host, proxy_port=profile.proxy_port,
                    network_sources=profile.network_sources, ui_snapshots=profile.ui_snapshots,
                    maestro_permitted=profile.maestro_permitted,
                    ui_snapshot_delay_ms=profile.ui_snapshot_delay_ms,
                    ui_snapshot_min_interval_ms=profile.ui_snapshot_min_interval_ms,
                    logcat_json=profile.logcat_json,
                )
            runner = AndroidCaptureRunner(
                profile, report_dir=args.report_dir, vault_path=args.vault,
                device_id=args.device_id, expected_text=args.expected_text,
            )
            result = runner.run(args.command)
    except (ValueError, OSError) as exc:
        print(f"Android capture failed: {exc}", file=sys.stderr)
        return 2
    if result.get("status") == "passed":
        print(f"Android {args.command} capture passed; artifacts: {result['artifact_dir']}")
        if result.get("vault_session_id"):
            print(f"Vault session: {result['vault_session_id']}")
        return 0
    print(f"Android {args.command} capture failed: {result.get('failure')}\nArtifacts: {result['artifact_dir']}",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

