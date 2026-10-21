"""Reversible control of ASUS battery overlay. Does not alter services or ACLs."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
import winreg
from pathlib import Path

from core import atomic_json

COMPONENT = Path(r"C:\Program Files (x86)\ASUS\ArmouryDevice\dll\AcPowerNotification")
ORIGINAL = COMPONENT / "AcPowerNotification.exe"
BACKUP = COMPONENT / "AcPowerNotification.exe.disabled"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def status() -> str:
    if ORIGINAL.exists() and BACKUP.exists():
        return "conflict"
    if BACKUP.exists():
        return "disabled"
    if ORIGINAL.exists():
        return "enabled"
    return "missing"


def helper_action(action: str, result_path: Path) -> int:
    try:
        if action not in ("enable", "disable"):
            raise ValueError("未知操作")
        if status() == "conflict":
            raise RuntimeError("原程序和备份同时存在，可能是奥创已更新；为保留文件，本次未更改")
        if action == "disable":
            if status() == "missing":
                raise FileNotFoundError("未找到奥创电量通知组件")
            subprocess.run([str(Path(os.environ["SystemRoot"]) / "System32/taskkill.exe"),
                            "/F", "/IM", "AcPowerNotification.exe"], capture_output=True,
                           creationflags=0x08000000, timeout=10)
            if ORIGINAL.exists():
                ORIGINAL.rename(BACKUP)
            if status() != "disabled":
                raise RuntimeError("未能验证关闭结果")
            message = "奥创电量浮窗已关闭"
        else:
            if BACKUP.exists():
                BACKUP.rename(ORIGINAL)
            if status() != "enabled":
                raise FileNotFoundError("未找到可恢复的通知组件")
            # External ASUS software must not inherit the frozen app's Qt DLL
            # search path or its plugin environment.
            external_env = dict(os.environ)
            for key in ("QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "QT_QPA_PLATFORM"):
                external_env.pop(key, None)
            if getattr(sys, "frozen", False):
                extraction_root = str(Path(sys._MEIPASS)).casefold()
                external_env["PATH"] = os.pathsep.join(
                    entry for entry in external_env.get("PATH", "").split(os.pathsep)
                    if not entry.casefold().startswith(extraction_root))
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
                kernel.SetDllDirectoryW(None)
            subprocess.Popen([str(ORIGINAL)], cwd=str(COMPONENT),
                             creationflags=0x00000008 | 0x08000000, env=external_env)
            message = "已恢复并启动奥创电量浮窗"
        atomic_json(result_path, {"ok": True, "message": message})
        return 0
    except Exception as exc:
        atomic_json(result_path, {"ok": False, "message": f"未更改成功：{exc}"})
        return 1


def request_admin(action: str, result_path: Path) -> None:
    args = ["--asus-action", action, "--result", str(result_path)]
    if not getattr(sys, "frozen", False):
        args.insert(0, str(Path(__file__).with_name("chargedock.py")))
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.ShellExecuteW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                    ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
    shell.ShellExecuteW.restype = ctypes.c_void_p
    code = shell.ShellExecuteW(None, "runas", sys.executable,
                              subprocess.list2cmdline(args), None, 0)
    if not code or code <= 32:
        raise PermissionError("管理员授权已取消或未获批准，设置未更改")


def startup_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            command = winreg.QueryValueEx(key, "ChargeDock")[0]
        return str(Path(sys.executable)).lower() in str(command).lower()
    except OSError:
        return False


def set_startup(enabled: bool) -> None:
    if enabled and not getattr(sys, "frozen", False):
        raise RuntimeError("请在 EXE 版本中启用开机启动")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            command = subprocess.list2cmdline([sys.executable, "--background"])
            winreg.SetValueEx(key, "ChargeDock", 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, "ChargeDock")
            except FileNotFoundError:
                pass
