"""Device activity notifications. Reads RAWINPUTHEADER only, never key contents."""
from __future__ import annotations

import ctypes
import re
import time
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter


class WakeGate:
    def __init__(self, idle_seconds=20, cooldown=5):
        self.idle_seconds, self.cooldown = idle_seconds, cooldown
        self.activity, self.triggered = {}, {}

    def observe(self, key, missing=False, polling=False, now=None):
        now = time.monotonic() if now is None else now
        previous = self.activity.get(key)
        self.activity[key] = now
        # Vendor responses to our own headset query must not cause a loop.
        if key == "headset" and polling:
            return False
        idle = previous is None or now - previous >= self.idle_seconds
        if (idle or missing) and now - self.triggered.get(key, -1e10) >= self.cooldown:
            self.triggered[key] = now
            return True
        return False


def device_kind(path, raw_type=None):
    match = re.search(r"vid_([0-9a-f]{4})&pid_([0-9a-f]{4})", path.lower())
    if not match:
        return None
    vid, pid = (int(value, 16) for value in match.groups())
    if vid == 0x1038 and pid in (0x12E0, 0x12E5):
        return "headset"
    if vid != 0x0B05:
        return None
    if pid in (0x1A1A, 0x1A18) and raw_type in (None, 0):
        return "mouse"
    if pid in (0x1ACE, 0x1A83, 0x1A85) and raw_type in (None, 1):
        return "keyboard"
    return None


class RawInputDevice(ctypes.Structure):
    _fields_ = [("page", wintypes.USHORT), ("usage", wintypes.USHORT),
                ("flags", wintypes.DWORD), ("target", wintypes.HWND)]


class RawInputHeader(ctypes.Structure):
    _fields_ = [("kind", wintypes.DWORD), ("size", wintypes.DWORD),
                ("device", wintypes.HANDLE), ("wparam", wintypes.WPARAM)]


class WakeMonitor(QAbstractNativeEventFilter):
    USAGES = ((1, 2), (1, 6), (0xFFC0, 1))

    def __init__(self, app, hwnd, callback, readings, polling):
        super().__init__()
        self.app, self.callback = app, callback
        self.readings, self.polling = readings, polling
        self.gate = WakeGate()
        self.devices = {}
        self.registration_ok = False
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RawInputDevice), wintypes.UINT, wintypes.UINT]
        self.user32.RegisterRawInputDevices.restype = wintypes.BOOL
        self.user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p,
                                               ctypes.POINTER(wintypes.UINT), wintypes.UINT]
        self.user32.GetRawInputData.restype = wintypes.UINT
        self.user32.GetRawInputDeviceInfoW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                                      ctypes.c_void_p, ctypes.POINTER(wintypes.UINT)]
        self.user32.GetRawInputDeviceInfoW.restype = wintypes.UINT
        devices = (RawInputDevice * len(self.USAGES))(
            *(RawInputDevice(page, usage, 0x2100, hwnd) for page, usage in self.USAGES))
        self.registration_ok = bool(self.user32.RegisterRawInputDevices(devices, len(devices), ctypes.sizeof(RawInputDevice)))
        app.installNativeEventFilter(self)

    def lookup(self, handle, raw_type=None):
        if not handle:
            return None
        if handle not in self.devices:
            count = wintypes.UINT()
            if self.user32.GetRawInputDeviceInfoW(handle, 0x20000007, None, ctypes.byref(count)) == 0xFFFFFFFF:
                return None
            name = ctypes.create_unicode_buffer(count.value + 1)
            if self.user32.GetRawInputDeviceInfoW(handle, 0x20000007, name, ctypes.byref(count)) == 0xFFFFFFFF:
                return None
            self.devices[handle] = name.value
        return device_kind(self.devices[handle], raw_type)

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            return False, 0
        try:
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0xFF:  # WM_INPUT; do not read the payload.
                header = RawInputHeader()
                size = wintypes.UINT(ctypes.sizeof(header))
                count = self.user32.GetRawInputData(msg.lParam, 0x10000005,
                    ctypes.byref(header), ctypes.byref(size), ctypes.sizeof(header))
                if count == 0xFFFFFFFF:
                    return False, 0
                key = self.lookup(header.device, header.kind)
                if key:
                    current = self.readings().get(key)
                    missing = current is None or current.level is None
                    if self.gate.observe(key, missing, self.polling()):
                        self.callback()
            elif msg.message == 0xFE:  # WM_INPUT_DEVICE_CHANGE
                key = self.lookup(msg.lParam)
                if int(msg.wParam) == 2:
                    self.devices.pop(msg.lParam, None)
                if key:
                    self.callback()
            elif msg.message == 0x218 and int(msg.wParam) in (7, 18):
                self.gate.activity.clear()
                self.callback()
        except (OSError, ValueError, TypeError):
            pass
        # Normal Windows/Qt input handling remains intact, including cleanup.
        return False, 0

    def close(self):
        self.app.removeNativeEventFilter(self)
        if self.registration_ok:
            devices = (RawInputDevice * len(self.USAGES))(
                *(RawInputDevice(page, usage, 1, None) for page, usage in self.USAGES))
            self.user32.RegisterRawInputDevices(devices, len(devices), ctypes.sizeof(RawInputDevice))
