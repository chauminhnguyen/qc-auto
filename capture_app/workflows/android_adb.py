"""Narrow allowlist for direct ADB subprocesses used by Android capture."""

from __future__ import annotations

import re
import subprocess
from typing import Callable


class AdbCommandRejected(ValueError):
    pass


class AdbCommandWrapper:
    """Build only explicitly approved ADB operations; never accepts shell text."""

    _EVENT_DEVICE = re.compile(r"^/dev/input/event\d+$")
    _POSITIVE_PID = re.compile(r"^[1-9]\d{0,9}$")
    _PACKAGE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")

    def __init__(self, adb: str, serial: str, *, command_runner: Callable = subprocess.run,
                 process_factory: Callable = subprocess.Popen):
        if not serial or any(char.isspace() for char in serial):
            raise AdbCommandRejected("An explicit ADB device serial is required")
        self.adb = adb
        self.serial = serial
        self.command_runner = command_runner
        self.process_factory = process_factory

    def argv(self, operation: str, *args: str) -> list[str]:
        prefix = [self.adb, "-s", self.serial, "shell"]
        if operation == "getevent_caps" and not args:
            command = ["getevent", "-p"]
        elif operation == "getevent_stream" and len(args) <= 1:
            if args and not self._EVENT_DEVICE.fullmatch(args[0]):
                raise AdbCommandRejected("getevent device path is not allowlisted")
            command = ["getevent", "-lt", *args]
            return prefix + ["-t", *command]
        elif operation == "wm_size" and not args:
            command = ["wm", "size"]
        elif operation == "uptime" and not args:
            command = ["cat", "/proc/uptime"]
        elif operation == "hierarchy" and not args:
            command = ["uiautomator", "dump", "/dev/stdout"]
        elif operation == "window" and not args:
            command = ["dumpsys", "window"]
        elif operation == "activity" and not args:
            command = ["dumpsys", "activity"]
        elif operation == "input" and not args:
            command = ["dumpsys", "input"]
        elif operation == "package" and len(args) == 1 and self._PACKAGE.fullmatch(args[0]):
            command = ["dumpsys", "package", args[0]]
        elif operation == "default_ime" and not args:
            command = ["settings", "get", "secure", "default_input_method"]
        elif operation == "http_proxy" and not args:
            command = ["settings", "get", "global", "http_proxy"]
        elif operation == "logcat" and len(args) == 1 and self._POSITIVE_PID.fullmatch(args[0]):
            command = ["logcat", "--pid", args[0], "-v", "epoch"]
        else:
            raise AdbCommandRejected(f"ADB operation is not allowlisted: {operation}")
        return prefix + command

    def run(self, operation: str, *args: str, timeout: int = 15):
        argv = self.argv(operation, *args)
        return self.command_runner(argv, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=timeout,
                                   check=False, shell=False)

    def popen(self, operation: str, *args: str, **kwargs):
        argv = self.argv(operation, *args)
        return self.process_factory(argv, shell=False, **kwargs)


__all__ = ["AdbCommandRejected", "AdbCommandWrapper"]
