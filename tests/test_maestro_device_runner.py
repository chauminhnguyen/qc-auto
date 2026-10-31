import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from capture_app.workflows.maestro_device_runner import MaestroDeviceRunner, main


class MaestroDeviceRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.flow = self.root / "smoke.yaml"
        self.flow.write_text("appId: ${APP_ID}\n---\n- launchApp\n", encoding="utf-8")
        self.report_dir = self.root / "reports"
        self.commands = []
        self.window_output = "mFocusedApp=AppWindowToken{Token{u0 com.example.app/.MainActivity}}\n"
        self.app_output = "Packages:\n  Package [com.example.app]\n"
        self.maestro_returncode = 0
        self.maestro_stderr = ""
        self.adb_available = True
        self.maestro_available = True

    def tearDown(self):
        self.temp.cleanup()

    def _which(self, name):
        if name == "adb" and self.adb_available:
            return "adb.exe"
        if name == "maestro" and self.maestro_available:
            return "maestro"
        return None

    def _command(self, command, **kwargs):
        self.commands.append((command, kwargs))
        parts = command if isinstance(command, list) else [command]
        rendered = " ".join(str(part) for part in parts)
        if "dumpsys window" in rendered:
            return CompletedProcess(command, 0, self.window_output, "")
        if "dumpsys package" in rendered:
            return CompletedProcess(command, 0, self.app_output, "")
        if "test" in rendered:
            output_dir = Path(parts[parts.index("--test-output-dir") + 1])
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "failure.png").write_bytes(b"test image")
            (output_dir / "maestro.log").write_text("text log", encoding="utf-8")
            return CompletedProcess(command, self.maestro_returncode, "Maestro output", self.maestro_stderr)
        self.fail(f"Unexpected command: {command!r}")

    def _runner(self):
        return MaestroDeviceRunner(
            self.report_dir, which=self._which, command_runner=self._command,
        )

    def _run(self, **kwargs):
        kwargs.setdefault("device_id", "phone-123")
        return self._runner().run(self.flow, "com.example.app", **kwargs)

    def test_missing_maestro_is_reported_and_saved(self):
        self.maestro_available = False

        result = self._run()

        self.assertEqual(result["status"], "failed")
        self.assertIn("Maestro CLI was not found", result["failure"])
        self.assertTrue(Path(result["command_log"]).is_file())
        self.assertTrue((Path(result["artifact_dir"]) / "summary.json").is_file())
        self.assertEqual(self.commands, [])

    def test_missing_adb_has_path_setup_guidance(self):
        self.adb_available = False

        result = self._run()

        self.assertEqual(result["status"], "failed")
        self.assertIn("Android SDK Platform Tools", result["failure"])
        self.assertEqual(self.commands, [])

    def test_device_id_is_required_by_adb_allowlist(self):
        result = self._runner().run(self.flow, "com.example.app")
        self.assertIn("--device-id is required", result["failure"])
        self.assertEqual(self.commands, [])

    def test_missing_app_is_reported_before_maestro_starts(self):
        self.app_output = ""

        result = self._run()

        self.assertIn("com.example.app is not installed", result["failure"])
        self.assertEqual(len(self.commands), 2)

    def test_success_runs_flow_and_saves_summary_and_logs(self):
        result = self._run(expected_text="Home")

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["device_id"], "phone-123")
        self.assertEqual(result["exit_code"], 0)
        artifact_dir = Path(result["artifact_dir"])
        summary = json.loads((artifact_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "passed")
        self.assertIn("Maestro test", (artifact_dir / "commands.log").read_text(encoding="utf-8"))
        self.assertTrue(Path(result["maestro_artifacts"]).is_dir())
        artifacts = Path(result["maestro_artifacts"])
        self.assertTrue((artifacts / "maestro.log").is_file())
        self.assertFalse(any(path.suffix.casefold() in {".png", ".jpg", ".mp4"}
                             for path in Path(result["artifact_dir"]).rglob("*")))
        maestro_command = self.commands[-1][0]
        self.assertIn("--device", maestro_command)
        self.assertIn("phone-123", maestro_command)
        self.assertIn("APP_ID=com.example.app", maestro_command)
        self.assertIn("EXPECTED_TEXT=Home", maestro_command)
        self.assertIn("--format", maestro_command)
        self.assertIn("JUNIT", maestro_command)

    def test_maestro_failure_returns_failed_result_and_exit_code(self):
        self.maestro_returncode = 3
        self.maestro_stderr = "assertion failed"

        result = self._run()

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 3)
        self.assertIn("assertion failed", result["failure"])
        self.assertFalse(any(path.suffix.casefold() in {".png", ".jpg", ".mp4"}
                             for path in Path(result["artifact_dir"]).rglob("*")))
        self.assertEqual(
            json.loads((Path(result["artifact_dir"]) / "summary.json").read_text(encoding="utf-8"))["status"],
            "failed",
        )

    def test_invalid_app_id_fails_before_commands(self):
        result = self._runner().run(self.flow, "not a package id")

        self.assertIn("--app-id must be an Android package ID", result["failure"])
        self.assertEqual(self.commands, [])

    def test_flow_directory_must_contain_yaml(self):
        empty_dir = self.root / "empty"
        empty_dir.mkdir()

        result = self._runner().run(empty_dir, "com.example.app")

        self.assertIn("No .yaml or .yml Maestro flows", result["failure"])
        self.assertEqual(self.commands, [])

    def test_cli_returns_nonzero_when_runner_fails(self):
        stderr = io.StringIO()
        failed = {"status": "failed", "failure": "device disconnected", "artifact_dir": "run-artifacts"}

        with patch.object(MaestroDeviceRunner, "run", return_value=failed):
            with contextlib.redirect_stderr(stderr):
                exit_code = main(["--flows", str(self.flow), "--app-id", "com.example.app",
                                  "--device-id", "phone-123"])

        self.assertEqual(exit_code, 1)
        self.assertIn("device disconnected", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
