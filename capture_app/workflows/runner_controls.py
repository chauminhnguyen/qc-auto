"""Temporary operating-system controls used while the testcase runner is active."""

from __future__ import annotations

import ctypes
import os
import threading
from contextlib import contextmanager


@contextmanager
def prevent_system_sleep():
    """Ask Windows to keep the system awake for the duration of a runner call."""
    if os.name != "nt":
        yield False
        return

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_execution_state = kernel32.SetThreadExecutionState
    set_execution_state.argtypes = [ctypes.c_uint]
    set_execution_state.restype = ctypes.c_uint
    continuous = 0x80000000
    system_required = 0x00000001
    active = bool(set_execution_state(continuous | system_required))
    try:
        yield active
    finally:
        set_execution_state(continuous)


class CtrlLSkipHotkey:
    """Consume Ctrl+L globally and request a skip at the next safe checkpoint."""

    _WM_HOTKEY = 0x0312
    _WM_QUIT = 0x0012
    _PM_NOREMOVE = 0x0000
    _MOD_CONTROL = 0x0002
    _MOD_NOREPEAT = 0x4000
    _VK_L = 0x4C
    _HOTKEY_ID = 0xCA71

    def __init__(self, active_case: threading.Event, skip_requested: threading.Event):
        self.active_case = active_case
        self.skip_requested = skip_requested
        self.available = False
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._user32 = None
        self._kernel32 = None

    def start(self) -> bool:
        if os.name != "nt":
            return False
        self._thread = threading.Thread(target=self._message_loop, name="testcase-ctrl-l", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2)
        return self.available

    def stop(self) -> None:
        if not self._thread:
            return
        if self._thread_id and self._user32:
            self._user32.PostThreadMessageW(self._thread_id, self._WM_QUIT, 0, 0)
        self._thread.join(timeout=2)

    def _handle_hotkey(self) -> None:
        if self.active_case.is_set():
            self.skip_requested.set()

    def _message_loop(self) -> None:
        from ctypes import wintypes

        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self._thread_id = int(self._kernel32.GetCurrentThreadId())
        self._user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self._user32.RegisterHotKey.restype = wintypes.BOOL
        self._user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self._user32.UnregisterHotKey.restype = wintypes.BOOL
        self._user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        self._user32.GetMessageW.restype = wintypes.BOOL
        self._user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        self._user32.PeekMessageW.restype = wintypes.BOOL
        self._user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self._user32.PostThreadMessageW.restype = wintypes.BOOL

        message = wintypes.MSG()
        self._user32.PeekMessageW(ctypes.byref(message), None, 0, 0, self._PM_NOREMOVE)
        self.available = bool(self._user32.RegisterHotKey(
            None, self._HOTKEY_ID, self._MOD_CONTROL | self._MOD_NOREPEAT, self._VK_L,
        ))
        self._ready.set()
        if not self.available:
            return

        try:
            while self._user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == self._WM_HOTKEY and int(message.wParam) == self._HOTKEY_ID:
                    self._handle_hotkey()
        finally:
            self._user32.UnregisterHotKey(None, self._HOTKEY_ID)
