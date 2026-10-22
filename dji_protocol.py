"""DJI Mic Mini protocol, independently ported from DJI-Mic-Control.

Reference commit 9ba76880807a71d4eaba74c785dbee186a98f43b (Unlicense).
Battery gauges are categories, never percentages. See ATTRIBUTION.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def crc8(data):
    value = 0x77  # reflected register form of the documented 0xEE seed
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0x8C if value & 1 else 0)
    return value


def crc16(data, initial=0x3692):
    value = initial
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0x8408 if value & 1 else 0)
    return value


def frame(body, version=2):
    body = bytearray(body)
    body[:3] = bytes((0x55, len(body) + 2, 4))
    body[3] = crc8(body[:3])
    checksum = crc16(body, 0x3692 if version == 2 else 0)
    # Two zero-residue bytes are simply the current little-endian CRC.
    if version == 2:
        return bytes(body) + checksum.to_bytes(2, "little")
    for suffix in range(65536):
        tail = suffix.to_bytes(2, "little")
        if crc16(tail, checksum) == 0xBB01:
            return bytes(body) + tail
    raise ValueError("CRC")


class Stream:
    """Bounded incremental framing, resynchronizing after corrupt packets."""
    def __init__(self):
        self.buffer = bytearray()
        self.rejected = 0

    def feed(self, data):
        self.buffer.extend(data)
        result = []
        while len(self.buffer) >= 4:
            b = self.buffer
            if b[0] != 0x55 or b[2] != 4 or not 14 <= b[1] <= 255 or crc8(b[:3]) != b[3]:
                del b[0]; self.rejected += 1
                continue
            length = b[1]
            if len(b) < length:
                break
            candidate = bytes(b[:length])
            v1 = candidate[8:12] == b"\0\x5b\x03\0"
            initial, residue = (0, 0xBB01) if v1 else (0x3692, 0)
            if crc16(candidate, initial) != residue:
                del b[0]; self.rejected += 1
                continue
            del b[:length]
            result.append(candidate)
        return result


GAUGE_TEXT = {1: "满电", 2: "电量充足", 3: "电量充足", 4: "电量充足",
              5: "电量偏低", 6: "电量很低", 7: "即将耗尽"}
# Illustrative categories, not measured or linearly interpolated percentages.
GAUGE_BAR = {1: 100, 2: 80, 3: 80, 4: 80, 5: 35, 6: 15, 7: 5}
STATE_TEXT = {
    "receiver_missing": "未找到大疆接收器", "driver_missing": "MI_06 缺少 WinUSB 驱动",
    "interface_missing": "找不到 MI_06 数据接口", "interface_busy": "大疆接口被占用，请退出其他麦克风控制软件",
    "multiple_receivers": "检测到多个大疆接收器，暂不支持选择", "wrong_interface": "大疆数据接口不匹配",
    "no_frames": "未收到有效状态报文", "read_failed": "大疆读取失败", "offline": "发射器离线",
    "unsupported": "v1 电量读取暂不支持", "unknown": "电量未知", "online": "已连接",
    "stale": "上次读取", "connecting": "正在连接设备…",
}


@dataclass
class Status:
    version: int | None = None
    connected: int = 0
    tx: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    status_received: bool = False
    receiver_id: str = ""
    receiver_product: str = ""

    def decode(self, packet):
        if len(packet) < 14:
            return False
        if packet[8:11] != b"\0\x5b\x03":
            return False
        marker = packet[11]
        if marker == 0:
            # v1 uses two variable-length slots, NEVER v2 battery offsets.
            off, connected = 14, 0
            for unit in (1, 2):
                if off + 9 > len(packet) - 2:
                    return False
                present = bool(packet[off + 1] & 0x20)
                length = 23 if present else 9
                if off + length > len(packet) - 2:
                    return False
                connected |= (1 << (unit - 1)) if present else 0
                off += length
            if off + 22 != len(packet) - 2:
                return False
            self.version, self.connected, self.status_received = 1, connected, True
            self.tx = {unit: {} for unit in (1, 2) if connected & (1 << (unit - 1))}
            self.settings = {}
            return True
        if marker != 3:
            return False
        # Status subtype 03 at +14, exact 52-byte header + up to two slots.
        if len(packet) in (54, 86, 118) and packet[14:20] == b"\x03\0\0\0\0\x20":
            mask = packet[44]
            if mask & ~3:
                return False
            entries = {}
            for off in range(52, len(packet) - 2, 32):
                unit = packet[off + 1]
                if unit not in (1, 2) or unit in entries:
                    return False
                entries[unit] = packet[off:off + 32]
            self.version, self.connected, self.status_received = 2, mask, True
            self.tx = {unit: dict(self.tx.get(unit, {})) for unit in (1, 2)
                       if mask & (1 << (unit - 1))}
            for unit, tx in self.tx.items():
                # Do not retain a stale gauge when a connected slot is absent.
                tx.update(gauge=None, charging=False, tone=None)
                tx.pop("settings", None)
                if unit in entries:
                    slot = entries[unit]
                    gauge = (slot[7] >> 2) & 7
                    tx.update(gauge=gauge or None, charging=bool(slot[7] & 2),
                              tone="bright" if slot[9] & 0x80 else "rich" if slot[9] & 0x40 else "standard")
                    tx["settings"] = {"mic-leds": not bool(slot[6] & 2),
                        "tx-auto-off-15m": bool(slot[6] & 0x10), "noise-cancel-button": bool(slot[6] & 0x80),
                        "low-cut": bool(slot[9] & 0x20), "noise-cancel-power": bool(slot[7] & 1),
                        "noise-cancel": "strong" if slot[6] & 0x20 else "basic"}
            f1, f2, f3 = packet[20], packet[21], packet[48]
            self.settings = {"noise-cancel": "strong" if f1 & 8 else "basic",
                "noise-cancel-power": bool(f1 & 0x20), "camera-power": bool(f1 & 0x80),
                "auto-off-15m": bool(f2 & 1), "stereo": "stereo" if f2 & 4 else "mono",
                "plug-free": bool(f3 & 2), "clip-limiter": bool(f3 & 0x10), "safety-track": bool(f3 & 0x40)}
            if entries:
                slot = next(iter(entries.values()))
                self.settings.update({"mic-leds": not bool(slot[6] & 2),
                    "tx-auto-off-15m": bool(slot[6] & 0x10), "noise-cancel-button": bool(slot[6] & 0x80),
                    "low-cut": bool(slot[9] & 0x20)})
                # Mini 2 receiver does not implement NC command 37/38. Its
                # reported TX state still lives in each slot (see attribution).
                if self.receiver_product == "DJI Mic Mini 2":
                    first = next((t for t in self.tx.values() if t.get("settings")), None)
                    if first:
                        for key in ("noise-cancel", "noise-cancel-power"):
                            self.settings[key] = first["settings"][key]
            return True
        # Identity records supply capability, not authoritative connectivity.
        off, records = 14, []
        while off + 6 <= len(packet) - 2:
            tag, unit, length = packet[off], packet[off + 1], packet[off + 5]
            end = off + 6 + length
            if end > len(packet) - 2:
                return False
            records.append((tag, unit, packet[off + 6:end]))
            off = end
        if off != len(packet) - 2:
            return False
        for tag, unit, data in records:
            if unit == 0 and tag == 1 and len(data) > 4:
                self.receiver_id = data[4:].split(b"\0")[0].decode("ascii", errors="replace")
            if unit == 0 and tag == 6:
                self.receiver_product = data.split(b"\0")[0].decode("ascii", errors="replace")
            if unit in self.tx and tag == 6:
                self.tx[unit]["product"] = data.split(b"\0")[0].decode("ascii", errors="replace")
        return False


def readings(status, state="online", detail="", updated=0):
    from core import Reading, DJI_MODELS
    result = []
    for unit, (key, names) in enumerate(DJI_MODELS.items(), 1):
        tx = status.tx.get(unit, {})
        current = state
        if state == "online":
            current = "offline" if not status.connected & (1 << (unit - 1)) else (
                "unsupported" if status.version == 1 else "online" if tx.get("gauge") else "unknown")
        result.append(Reading(key, *names, charging=tx.get("charging", False),
            error="" if current in ("online", "unknown", "offline", "unsupported") else STATE_TEXT.get(current, current),
            updated=updated, battery_kind="gauge", gauge=tx.get("gauge"), state=current,
            detail=detail, protocol=status.version))
    return result
