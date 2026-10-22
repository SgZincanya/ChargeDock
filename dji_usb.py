"""Read-only discovery and bounded WinUSB I/O for DJI's MI_06 only.

No driver installation, registry writes, HID enumeration or other interface claims.
Device-interface GUIDs come from the installed function driver's registration.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import re
import uuid
import winreg


class DeviceError(OSError):
    def __init__(self, state, detail=""):
        self.state = state
        self.detail = detail
        super().__init__(state + (": " + detail if detail else ""))


class GUID(C.Structure):
    _fields_ = [("data", C.c_ubyte * 16)]


class DevInfo(C.Structure):
    _fields_ = [("size", W.DWORD), ("guid", GUID), ("devinst", W.DWORD),
                ("reserved", C.c_void_p)]


class InterfaceDescriptor(C.Structure):
    _pack_ = 1
    _fields_ = [(name, C.c_ubyte) for name in (
        "length", "type", "number", "alternate", "endpoints", "cls",
        "subclass", "protocol", "string")]


class Pipe(C.Structure):
    _fields_ = [("type", C.c_int), ("id", C.c_ubyte), ("max_packet", W.USHORT),
                ("interval", C.c_ubyte)]


def bind(dll, name, args, result=W.BOOL):
    fn = getattr(dll, name)
    fn.argtypes, fn.restype = args, result
    return fn


def enumerate_interfaces():
    """Return only present 2CA3:4011 MI_06 function interface paths.

    SetupAPI supplies present instance IDs; Configuration Manager resolves the
    GUIDs registered on that exact function. Never guess a USB symbolic link.
    """
    setup = C.WinDLL("setupapi", use_last_error=True)
    cm = C.WinDLL("cfgmgr32", use_last_error=True)
    get = bind(setup, "SetupDiGetClassDevsW", [C.c_void_p, W.LPCWSTR, C.c_void_p, W.DWORD], C.c_void_p)
    enum = bind(setup, "SetupDiEnumDeviceInfo", [C.c_void_p, W.DWORD, C.POINTER(DevInfo)])
    instance = bind(setup, "SetupDiGetDeviceInstanceIdW", [C.c_void_p, C.POINTER(DevInfo), W.LPWSTR, W.DWORD, C.c_void_p])
    destroy = bind(setup, "SetupDiDestroyDeviceInfoList", [C.c_void_p])
    size = bind(cm, "CM_Get_Device_Interface_List_SizeW", [C.POINTER(W.ULONG), C.POINTER(GUID), W.LPCWSTR, W.ULONG], W.ULONG)
    listing = bind(cm, "CM_Get_Device_Interface_ListW", [C.POINTER(GUID), W.LPCWSTR, W.LPWSTR, W.ULONG, W.ULONG], W.ULONG)
    handle = get(None, "USB", None, 0x02 | 0x04)
    if handle == C.c_void_p(-1).value:
        raise DeviceError("read_failed", str(C.WinError(C.get_last_error())))
    found, paths, missing = False, [], False
    try:
        index = 0
        while True:
            info = DevInfo(); info.size = C.sizeof(info)
            if not enum(handle, index, C.byref(info)):
                if C.get_last_error() != 259:
                    raise DeviceError("read_failed", str(C.WinError(C.get_last_error())))
                break
            index += 1
            buf = C.create_unicode_buffer(512)
            if not instance(handle, C.byref(info), buf, len(buf), None):
                continue
            ident = buf.value
            if not re.match(r"^USB\\VID_2CA3&PID_4011&MI_06\\", ident, re.I):
                continue
            found = True
            root = "SYSTEM\\CurrentControlSet\\Enum\\" + ident
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as key:
                    service = winreg.QueryValueEx(key, "Service")[0]
                if service.lower() != "winusb":
                    missing = True
                    continue
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root + "\\Device Parameters") as key:
                    guids = winreg.QueryValueEx(key, "DeviceInterfaceGUIDs")[0]
                if isinstance(guids, str):
                    guids = [guids]
                for value in guids:
                    guid = GUID.from_buffer_copy(uuid.UUID(value.strip("{} ")).bytes_le)
                    needed = W.ULONG()
                    if size(C.byref(needed), C.byref(guid), ident, 0) != 0:
                        continue
                    buffer = C.create_unicode_buffer(needed.value)
                    if listing(C.byref(guid), ident, buffer, needed.value, 0) == 0:
                        paths.extend(p for p in buffer[:].split("\0") if p)
            except (OSError, ValueError):
                missing = True
    finally:
        destroy(handle)
    if not found:
        raise DeviceError("receiver_missing")
    paths = sorted(set(paths))
    if not paths:
        raise DeviceError("driver_missing" if missing else "interface_missing")
    if len(paths) != 1:
        raise DeviceError("multiple_receivers")
    return paths


class Connection:
    """One owner, one interface; synchronous transfers bounded by pipe policy."""
    def __init__(self, path=None):
        self.file = self.usb = None
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.winusb = C.WinDLL("winusb", use_last_error=True)
        self.close_file = bind(self.kernel, "CloseHandle", [C.c_void_p])
        self.free = bind(self.winusb, "WinUsb_Free", [C.c_void_p])
        self.read_pipe = bind(self.winusb, "WinUsb_ReadPipe", [C.c_void_p, C.c_ubyte, C.c_void_p, W.ULONG, C.POINTER(W.ULONG), C.c_void_p])
        self.write_pipe = bind(self.winusb, "WinUsb_WritePipe", [C.c_void_p, C.c_ubyte, C.c_void_p, W.ULONG, C.POINTER(W.ULONG), C.c_void_p])
        create = bind(self.kernel, "CreateFileW", [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, C.c_void_p], C.c_void_p)
        init = bind(self.winusb, "WinUsb_Initialize", [C.c_void_p, C.POINTER(C.c_void_p)])
        query = bind(self.winusb, "WinUsb_QueryInterfaceSettings", [C.c_void_p, C.c_ubyte, C.POINTER(InterfaceDescriptor)])
        pipe_info = bind(self.winusb, "WinUsb_QueryPipe", [C.c_void_p, C.c_ubyte, C.c_ubyte, C.POINTER(Pipe)])
        policy = bind(self.winusb, "WinUsb_SetPipePolicy", [C.c_void_p, C.c_ubyte, W.ULONG, W.ULONG, C.c_void_p])
        path = path or enumerate_interfaces()[0]
        if not re.search(r"vid_2ca3&pid_4011&mi_06#", path, re.I):
            raise DeviceError("wrong_interface")
        self.file = create(path, 0xC0000000, 3, None, 3, 0x40000000, None)
        if self.file == C.c_void_p(-1).value:
            self.file = None
            self._raise()
        try:
            usb = C.c_void_p()
            if not init(self.file, C.byref(usb)):
                self._raise()
            self.usb = usb
            desc = InterfaceDescriptor()
            if not query(usb, 0, C.byref(desc)) or desc.number != 6:
                raise DeviceError("wrong_interface")
            endpoints = set()
            for index in range(desc.endpoints):
                pipe = Pipe()
                if not pipe_info(usb, 0, index, C.byref(pipe)):
                    self._raise()
                if pipe.type == 2:
                    endpoints.add(pipe.id)
            if not {0x86, 0x06} <= endpoints:
                raise DeviceError("wrong_interface")
            timeout = W.ULONG(200)
            for endpoint in (0x86, 0x06):
                if not policy(usb, endpoint, 3, C.sizeof(timeout), C.byref(timeout)):
                    self._raise()
        except BaseException:
            self.close()
            raise

    def _raise(self):
        code = C.get_last_error()
        state = "interface_busy" if code in (5, 32, 170) else "read_failed"
        raise DeviceError(state, str(C.WinError(code)))

    def read(self):
        buf, count = C.create_string_buffer(512), W.ULONG()
        if not self.read_pipe(self.usb, 0x86, buf, len(buf), C.byref(count), None):
            if C.get_last_error() == 121:
                return b""
            self._raise()
        return buf.raw[:count.value]

    def write(self, data):
        count = W.ULONG()
        if not self.write_pipe(self.usb, 0x06, data, len(data), C.byref(count), None):
            self._raise()
        if count.value != len(data):
            raise DeviceError("write_failed", "short write")

    def close(self):
        if self.usb:
            self.free(self.usb); self.usb = None
        if self.file:
            self.close_file(self.file); self.file = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
