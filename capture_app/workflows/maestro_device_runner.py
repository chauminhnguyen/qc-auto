"""Run manually-authored Maestro flows against an attached Android device."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from capture_app.workflows.android_adb import AdbCommandWrapper


DEFAULT_REPORT_DIR = Path("out/android_runs")
PACKAGE_ID_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")


class PreflightError(RuntimeError):
    """A prerequisite for a local Maestro device run is not satisfied."""


def _flow_path(path: str | Path) -> Path:
    flow_path = Path(path).expanduser().resolve()
    if flow_path.is_file():
        if flow_path.suffix.casefold() not in {".yaml", ".yml"}:
            raise PreflightError(f"Flow file must end in .yaml or .yml: {flow_path}")
        return flow_path
    if flow_path.is_dir():
        flows = [
            candidate for candidate in flow_path.rglob("*")
            if candidate.is_file() and candidate.suffix.casefold() in {".yaml", ".yml"}
        ]
        if not flows:
            raise PreflightError(f"No .yaml or .yml Maestro flows found in: {flow_path}")
        return flow_path
    raise PreflightError(f"Flow path does not exist: {flow_path}")


def _parse_devices(output: str) -> dict[str, str]:
    devices: dict[str, str] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        fields = line.split()
        if len(fields) >= 2:
            devices[fields[0]] = fields[1]
    return devices


class MaestroDeviceRunner:
    """Preflight ADB and the installed app, then run Maestro and save artifacts."""

    def __init__(
        self,
        report_dir: str | Path = DEFAULT_REPORT_DIR,
        *,
        which: Callable[[str], str | None] | None = None,
        command_runner: Callable = subprocess.run,
    ):
        self.report_dir = Path(report_dir)
        self.which = which or shutil.which
        self.command_runner = command_runner

    def _command(self, argv: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        # Running tests should not require Maestro's optional analytics state or network.
        env["MAESTRO_CLI_NO_ANALYTICS"] = "1"
        command = argv
        shell = False
        if os.name == "nt" and Path(argv[0]).suffix.casefold() in {".bat", ".cmd"}:
            command = subprocess.list2cmdline(argv)  # type: ignore[assignment]
            shell = True
        return self.command_runner(
            command, capture_output=True, text=True, timeout=timeout, env=env,
            check=False, shell=shell,
        )

    @staticmethod
    def _record_command(log_parts: list[str], label: str, argv: list[str], result) -> None:
        from capture_app.workflows.android_capture import redact_log_line
        log_parts.extend([
            f"## {label}",
            f"$ {subprocess.list2cmdline(argv)}",
            f"exit_code: {result.returncode}",
            "stdout:",
            redact_log_line(result.stdout or ""),
            "stderr:",
            redact_log_line(result.stderr or ""),
            "",
        ])

    def _checked_command(self, argv: list[str], label: str, log_parts: list[str]):
        try:
            result = self._command(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            raise PreflightError(f"Could not run {label}: {exc}") from exc
        self._record_command(log_parts, label, argv, result)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise PreflightError(f"{label} failed with exit code {result.returncode}. {detail}".strip())
        return result

    def _select_device(self, adb: str, requested_id: str | None, log_parts: list[str]) -> str:
        if not requested_id:
            raise PreflightError("--device-id is required by the Android ADB command allowlist")
        try:
            result = AdbCommandWrapper(adb, requested_id, command_runner=self.command_runner).run("window")
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise PreflightError(f"Could not query device through the ADB allowlist: {exc}") from exc
        self._record_command(
            log_parts, "ADB window preflight",
            [adb, "-s", requested_id, "shell", "dumpsys", "window"], result,
        )
        if result.returncode != 0:
            raise PreflightError(f"Android device {requested_id} is not connected and authorized")
        return requested_id

    def run(
        self,
        flows: str | Path,
        app_id: str,
        *,
        device_id: str | None = None,
        expected_text: str | None = None,
        maestro_permitted: bool = True,
    ) -> dict:
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        started = time.monotonic()
        run_dir = self.report_dir / (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
        )
        run_dir.mkdir(parents=True, exist_ok=False)
        logs: list[str] = []
        result = {
            "started_at": started_at,
            "status": "failed",
            "app_id": app_id,
            "requested_device_id": device_id,
            "device_id": None,
            "flows": str(flows),
            "exit_code": None,
            "failure": None,
            "duration_seconds": 0.0,
            "artifact_dir": str(run_dir),
            "command_log": str(run_dir / "commands.log"),
            "junit_report": str(run_dir / "junit.xml"),
            "maestro_artifacts": str(run_dir / "maestro_artifacts"),
        }

        try:
            if not maestro_permitted:
                raise PreflightError("Maestro test mode is forbidden by this app profile.")
            if not PACKAGE_ID_RE.fullmatch(app_id):
                raise PreflightError("--app-id must be an Android package ID such as com.example.app.")
            flow_path = _flow_path(flows)
            maestro = self.which("maestro")
            if not maestro:
                raise PreflightError("Maestro CLI was not found on PATH. Install Maestro and retry.")
            adb = self.which("adb")
            if not adb:
                raise PreflightError(
                    "ADB was not found on PATH. Install Android SDK Platform Tools and add its platform-tools folder to PATH."
                )

            selected_device = self._select_device(adb, device_id, logs)
            result["device_id"] = selected_device
            package_result = AdbCommandWrapper(
                adb, selected_device, command_runner=self.command_runner
            ).run("package", app_id)
            self._record_command(
                logs, "Installed app check",
                [adb, "-s", selected_device, "shell", "dumpsys", "package", app_id], package_result,
            )
            if package_result.returncode != 0 or app_id not in (package_result.stdout or ""):
                raise PreflightError(
                    f"App {app_id} is not installed on device {selected_device}. Install it and retry."
                )

            artifact_dir = run_dir / "maestro_artifacts"
            artifact_dir.mkdir()
            with tempfile.TemporaryDirectory(prefix="maestro-device-") as temporary_output:
                temporary_artifacts = Path(temporary_output) / "artifacts"
                temporary_artifacts.mkdir()
                command = [maestro, "--device", selected_device, "test"]
                command += [
                    "--format", "JUNIT", "--output", str(run_dir / "junit.xml"),
                    "--test-output-dir", str(temporary_artifacts),
                    "-e", f"APP_ID={app_id}",
                ]
                if expected_text is not None:
                    command += ["-e", f"EXPECTED_TEXT={expected_text}"]
                command.append(str(flow_path))
                try:
                    maestro_result = self._command(command, timeout=1800)
                except (OSError, subprocess.SubprocessError) as exc:
                    raise PreflightError(f"Could not run Maestro: {exc}") from exc
                # Maestro saves automatic failure screenshots. Keep them out of the report tree.
                image_suffixes = {
                    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".avif",
                    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".3gp", ".m4v",
                }
                for source in temporary_artifacts.rglob("*"):
                    if source.is_file() and source.suffix.casefold() not in image_suffixes:
                        destination = artifact_dir / source.relative_to(temporary_artifacts)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, destination)
            self._record_command(logs, "Maestro test", command, maestro_result)
            result["exit_code"] = maestro_result.returncode
            if maestro_result.returncode == 0:
                result["status"] = "passed"
            else:
                detail = (maestro_result.stderr or maestro_result.stdout or "").strip()
                result["failure"] = f"Maestro failed with exit code {maestro_result.returncode}. {detail}".strip()
        except PreflightError as exc:
            result["failure"] = str(exc)
        except OSError as exc:
            result["failure"] = f"Could not prepare run artifacts: {exc}"
        finally:
            result["duration_seconds"] = round(time.monotonic() - started, 3)
            Path(result["command_log"]).write_text("\n".join(logs), encoding="utf-8")
            (run_dir / "summary.json").write_text(
                json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run authored Maestro flows against an Android phone connected through ADB."
    )
    parser.add_argument("--flows", required=True, type=Path, help="Maestro .yaml/.yml flow or directory")
    parser.add_argument("--app-id", required=True, help="Installed Android application package ID")
    parser.add_argument("--device-id", required=True, help="Explicit ADB serial from `adb devices -l`")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR, help="Root folder for run artifacts")
    parser.add_argument(
        "--expected-text",
        help="Set EXPECTED_TEXT for the starter smoke flow's visible-text assertion",
    )
    parser.add_argument("--profiles", type=Path, default=Path("android/apps.yaml"),
                        help="Profiles used to enforce Maestro permission for known app IDs")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    maestro_permitted = True
    try:
        from capture_app.workflows.android_capture_support import load_profiles
        matches = [p for p in load_profiles(args.profiles).values() if p.app_id == args.app_id]
        if matches:
            maestro_permitted = matches[0].maestro_permitted
    except ValueError:
        maestro_permitted = True
    result = MaestroDeviceRunner(args.report_dir).run(
        args.flows,
        args.app_id,
        device_id=args.device_id,
        expected_text=args.expected_text,
        maestro_permitted=maestro_permitted,
    )
    if result["status"] == "passed":
        print(
            f"Maestro device run passed on {result['device_id']}; "
            f"artifacts: {result['artifact_dir']}"
        )
        return 0
    print(
        f"Maestro device run failed: {result['failure']}\n"
        f"Artifacts: {result['artifact_dir']}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
