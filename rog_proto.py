"""ROG 电量查询。仅发送读取命令，不更改灯效、按键或设备设置。

协议参考（独立实现）：
https://github.com/seerge/g-helper/blob/main/app/Peripherals/Mouse/AsusMouse.cs
https://github.com/seerge/g-helper/blob/main/app/Peripherals/Mouse/Models/ChakramX.cs
https://github.com/seerge/g-helper/blob/main/app/Peripherals/Keyboard/AsusKeyboard.cs
https://github.com/seerge/g-helper/blob/main/app/Peripherals/Keyboard/Models/Azoth.cs
"""
from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass
from typing import Optional

import hid_win as hw


@dataclass(frozen=True)
class DeviceSpec:
    kind: str
    title: str
    pid: int
    path_fragment: str
    usage_page: int
    report_id: int
    report_len: int
    command: int
    level_offset: int
    charging_offset: int


DEVICES = (
    DeviceSpec("mouse", "ROG 魔刃 X", 0x1A1A, "mi_00", 0xFF01, 0, 65, 7, 5, 10),
    DeviceSpec("mouse", "ROG 魔刃 X", 0x1A18, "mi_00", 0xFF01, 0, 65, 7, 5, 10),
    DeviceSpec("keyboard", "ROG 夜魔", 0x1ACE, "mi_02&col02", 0xFF00, 2, 64, 1, 6, 9),
    DeviceSpec("keyboard", "ROG 夜魔", 0x1A83, "mi_01", 0xFF00, 0, 65, 1, 6, 9),
    DeviceSpec("keyboard", "ROG 夜魔", 0x1A85, "mi_01", 0xFF00, 0, 65, 1, 6, 9),
)


@dataclass
class RogReading:
    title: str
    level: Optional[int] = None
    charging: bool = False
    detail: str = ""
    error: str = ""
    raw: bytes = b""


def match_device(iface: hw.HidInterface) -> Optional[DeviceSpec]:
    return next((s for s in DEVICES if iface.vid == 0x0B05
                 and iface.pid == s.pid and s.path_fragment in iface.path.lower()
                 and iface.usage_page == s.usage_page and iface.usage == 1
                 and iface.output_len == s.report_len
                 and iface.input_len == s.report_len), None)


def parse_reply(spec: DeviceSpec, reply: bytes) -> Optional[RogReading]:
    if len(reply) != spec.report_len or reply[:3] != bytes((spec.report_id, 0x12, spec.command)):
        return None
    level = reply[spec.level_offset]
    charging = reply[spec.charging_offset] > 0 if spec.kind == "mouse" else reply[spec.charging_offset] == 1
    if level > 100:
        return RogReading(spec.title, error="设备返回无效电量", raw=reply)
    # ROG 鼠标休眠时可能返回 0，不能误显示为电池耗尽。
    if spec.kind == "mouse" and level == 0 and not charging:
        return RogReading(spec.title, error="鼠标休眠或未连接，请动一下鼠标后刷新", raw=reply)
    return RogReading(spec.title, level, charging, raw=reply)


def read_interface(iface: hw.HidInterface, timeout: float = 0.65) -> Optional[RogReading]:
    spec = match_device(iface)
    if spec is None:
        return None
    result = RogReading(spec.title, detail=f"{iface.vid_pid} · ROG HID 电量协议")
    # 面板和托盘分别运行时，也不能在同一设备上交错请求/抢走回复。
    kernel = hw.kernel32
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.ReleaseMutex.restype = ctypes.c_int
    kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
    mutex = kernel.CreateMutexW(None, False, f"Local\\RogBattery_{spec.pid:04X}_{spec.kind}")
    if not mutex:
        result.error = "无法创建设备读取锁"
        return result
    acquired = False
    try:
        acquired = kernel.WaitForSingleObject(mutex, 2000) in (0, 0x80)
        if not acquired:
            result.error = "设备正在被另一窗口读取，请稍后刷新"
            return result
        with hw.HidDevice(iface.path, write=True) as dev:
            if not dev.write_access:
                result.error = "无法写入查询命令，请关闭占用设备的程序后重试"
                return result
            for _ in range(2):
                dev.drain()
                packet = bytes((spec.report_id, 0x12, spec.command)).ljust(spec.report_len, b"\0")
                dev.write_output(packet, use_hidd=False)
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    reply = dev.read_input(timeout_ms=max(1, min(120, int((deadline - time.monotonic()) * 1000))))
                    if not reply:
                        continue
                    parsed = parse_reply(spec, reply)
                    if parsed is not None:
                        parsed.detail = result.detail
                        return parsed
        result.error = "设备未回复，请唤醒设备并检查连接；如有占用请退出 Armoury Crate 后重试"
    except OSError as exc:
        result.error = f"读取失败：{exc}"
    finally:
        if acquired:
            kernel.ReleaseMutex(mutex)
        kernel.CloseHandle(mutex)
    return result


def read_device(kind: str, interfaces=None) -> RogReading:
    if kind not in ("mouse", "keyboard"):
        raise ValueError("未知设备类型")
    title = "ROG 魔刃 X" if kind == "mouse" else "ROG 夜魔"
    last = RogReading(title, error="未找到支持的接收器/USB 接口，请检查连接（不支持蓝牙）")
    for iface in hw.enumerate_hid() if interfaces is None else interfaces:
        spec = match_device(iface)
        if spec is not None and spec.kind == kind:
            reading = read_interface(iface)
            if reading is not None:
                if reading.level is not None:
                    return reading
                last = reading
    return last
