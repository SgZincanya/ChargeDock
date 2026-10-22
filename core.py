"""ChargeDock domain logic; no UI and no network access."""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from file_io import replace_file


@dataclass
class Reading:
    key: str
    title: str
    model: str
    level: int | None = None
    charging: bool = False
    approximate: bool = False
    error: str = ""
    updated: float = 0
    battery_kind: str = "percent"
    gauge: int | None = None
    state: str = ""
    detail: str = ""
    protocol: int | None = None


MODELS = {
    "mouse": ("魔刃 X", "ROG CHAKRAM X"),
    "keyboard": ("夜魔", "ROG AZOTH"),
    "headset": ("赛睿耳机", "ARCTIS NOVA PRO WIRELESS"),
}
DJI_MODELS = {"mic1": ("麦克风 1", "DJI Mic Mini 2"),
              "mic2": ("麦克风 2", "DJI Mic Mini 2")}
ALL_MODELS = {**MODELS, **DJI_MODELS}


def atomic_json(path: Path, value, *, replace_attempts=1) -> None:
    if type(replace_attempts) is not int or not 1 <= replace_attempts <= 8:
        raise ValueError("Invalid replace attempt limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(replace_attempts):
            try:
                replace_file(temporary, path)
                return
            except OSError as exc:
                transient = (getattr(exc, "winerror", None) in (5, 32, 33)
                             or isinstance(exc, PermissionError))
                if not transient or attempt + 1 == replace_attempts:
                    raise
                # Only worker callers opt in; UI writes remain fail-fast.
                time.sleep(min(0.02 * 2 ** attempt, 0.16))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass  # third-party locks must not hide the original failure


def defaults() -> dict:
    return {"interval": 60, "dismiss_seconds": 12, "sound": False, "language": "system",
            "alerts": {"mouse": {"enabled": True, "threshold": 20},
                       "keyboard": {"enabled": True, "threshold": 20}}}


def validate_config(value) -> dict:
    result = defaults()
    if not isinstance(value, dict):
        return result
    from i18n import LANGUAGES
    if isinstance(value.get("language"), str) and value["language"] in LANGUAGES:
        result["language"] = value["language"]
    if value.get("interval") in (30, 60, 120, 300):
        result["interval"] = value["interval"]
    if value.get("dismiss_seconds") in (8, 12, 20, 30):
        result["dismiss_seconds"] = value["dismiss_seconds"]
    result["sound"] = value.get("sound") is True
    alerts = value.get("alerts", {})
    for key in ("mouse", "keyboard"):
        item = alerts.get(key, {}) if isinstance(alerts, dict) else {}
        if not isinstance(item, dict):
            continue
        result["alerts"][key]["enabled"] = item.get("enabled", True) is True
        threshold = item.get("threshold", 20)
        if type(threshold) is int:
            result["alerts"][key]["threshold"] = max(1, min(99, threshold))
    return result


def load_config(path: Path) -> dict:
    try:
        return validate_config(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return defaults()


class AlertPolicy:
    """One reminder per low-battery episode, with 5% recovery hysteresis."""
    def __init__(self):
        self.notified: set[str] = set()
        self.snoozed_until = 0.0

    def evaluate(self, readings: list[Reading], config: dict, now: float | None = None) -> list[Reading]:
        now = time.time() if now is None else now
        notifications = []
        for reading in readings:
            if reading.key not in ("mouse", "keyboard"):
                continue
            item = config["alerts"][reading.key]
            if not item["enabled"]:
                self.notified.discard(reading.key)
                continue
            if reading.level is None or reading.error:
                continue
            threshold = item["threshold"]
            if reading.charging or reading.level > min(99, threshold + 5):
                self.notified.discard(reading.key)
                continue
            if now < self.snoozed_until:
                continue
            if reading.level <= threshold and reading.key not in self.notified:
                notifications.append(reading)
                self.notified.add(reading.key)
        return notifications


def parse_headset(data: bytes) -> tuple[int | None, bool, str]:
    # Independently implemented from HeadsetControl's Nova Pro Wireless protocol.
    # https://github.com/Sapd/HeadsetControl/blob/master/lib/devices/steelseries_arctis_nova_pro_wireless.hpp
    if len(data) < 16 or data[:2] != b"\x06\xb0":
        return None, False, "非电量回复"
    state, code = data[15], data[6]
    if state == 1:
        return None, False, "耳机未开机或超出连接范围"
    if state not in (2, 8) or code > 8:
        return None, False, "耳机返回了无法识别的状态"
    return int(code * 100 / 8 + 0.5), state == 2, ""


def read_headset(interfaces=None) -> Reading:
    import hid_win as hw
    result = Reading("headset", *MODELS["headset"], approximate=True)
    for iface in hw.enumerate_hid() if interfaces is None else interfaces:
        if not (iface.vid == 0x1038 and iface.pid in (0x12E0, 0x12E5)
                and iface.usage_page == 0xFFC0 and iface.usage == 1
                and iface.output_len == 64 and iface.input_len == 64):
            continue
        try:
            with hw.HidDevice(iface.path, write=True) as device:
                if not device.write_access:
                    result.error = "电量接口被占用，请稍后重试"
                    return result
                device.drain()
                device.write_output(b"\x06\xb0".ljust(64, b"\0"), use_hidd=False)
                deadline = time.monotonic() + 0.8
                while time.monotonic() < deadline:
                    data = device.read_input(timeout_ms=120)
                    if not data or data[:2] != b"\x06\xb0":
                        continue
                    result.level, result.charging, result.error = parse_headset(data)
                    result.updated = time.time()
                    return result
            result.error = "耳机未回复，请检查基站连接"
        except OSError as exc:
            result.error = f"耳机读取失败：{exc}"
        return result
    result.error = "未找到耳机基站"
    return result


def read_all(keys=None) -> list[Reading]:
    import rog_proto
    import hid_win as hw
    keys = list(MODELS) if keys is None else list(keys)
    if any(key not in MODELS for key in keys) or len(set(keys)) != len(keys):
        raise ValueError("Unknown or duplicate device keys")
    if not keys:
        return []
    try:
        interfaces = hw.enumerate_hid()
    except Exception as exc:
        return [Reading(key, *MODELS[key], error=f"暂时无法读取：{exc}") for key in keys]
    result = []
    for key in keys:
        try:
            if key == "headset":
                reading = read_headset(interfaces)
            else:
                value = rog_proto.read_device(key, interfaces)
                reading = Reading(key, *MODELS[key], value.level, value.charging,
                                  error=value.error, updated=time.time())
        except Exception as exc:
            reading = Reading(key, *MODELS[key], error=f"暂时无法读取：{exc}")
        result.append(reading)
    return result


def validate_readings(raw, expected_keys):
    if not isinstance(raw, list) or len(raw) != len(expected_keys):
        raise ValueError("Incomplete device results")
    readings = [Reading(**item) for item in raw]
    if [r.key for r in readings] != list(expected_keys):
        raise ValueError("Unexpected devices")
    for r in readings:
        if type(r.charging) is not bool or type(r.approximate) is not bool:
            raise ValueError("Invalid flags")
        if type(r.updated) not in (int, float) or not 0 <= r.updated < 1e12:
            raise ValueError("Invalid timestamp")
        if not all(isinstance(v, str) and len(v) <= 2048 for v in (r.title, r.model, r.error, r.detail, r.state)):
            raise ValueError("Invalid text")
        if r.battery_kind not in ("percent", "gauge"):
            raise ValueError("Invalid battery kind")
        if r.battery_kind == "gauge":
            from dji_protocol import STATE_TEXT
            if r.key not in DJI_MODELS or r.level is not None or r.approximate:
                raise ValueError("Gauge is not a percentage")
            if r.gauge is not None and (type(r.gauge) is not int or r.gauge not in range(1, 8)):
                raise ValueError("Invalid gauge")
            if r.state not in STATE_TEXT or (r.protocol is not None and (type(r.protocol) is not int or r.protocol not in (1, 2))):
                raise ValueError("Invalid DJI state")
            if r.protocol != 2 and r.gauge is not None:
                raise ValueError("Unsupported gauge protocol")
        elif r.key in DJI_MODELS or r.gauge is not None:
            raise ValueError("Invalid percentage device")
        if r.level is not None and (type(r.level) is not int or not 0 <= r.level <= 100):
            raise ValueError("Invalid battery level")
    return readings


def poll_to_file(path: Path, key=None) -> int:
    keys = [key] if isinstance(key, str) else key
    atomic_json(path, [asdict(r) for r in read_all(keys)])
    return 0


def read_poll_file(path: Path, expected_keys=None) -> list[Reading]:
    expected_keys = list(MODELS) if expected_keys is None else expected_keys
    raw = json.loads(path.read_text(encoding="utf-8"))
    return validate_readings(raw, expected_keys)


def user_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "ChargeDock"
