"""Validated v2 settings and ACK + fresh reported-state confirmation.

No optimistic state mutations. One pending transaction per receiver owner.
"""
from dataclasses import dataclass

from dji_protocol import frame


@dataclass(frozen=True)
class Setting:
    label: str
    command: int
    target: str  # rx / broadcast / tx
    options: tuple = ((False, "关闭", 0), (True, "开启", 1))
    reboot: bool = False


SETTINGS = {
    "noise-cancel-power": Setting("降噪开关", 0x38, "broadcast"),
    "noise-cancel": Setting("降噪强度", 0x37, "broadcast", (("basic", "基础", 0), ("strong", "强", 1))),
    "low-cut": Setting("低切", 3, "broadcast"),
    "stereo": Setting("声道模式", 8, "rx", (("mono", "单声道", 0), ("stereo", "立体声", 2))),
    "safety-track": Setting("安全音轨", 0x21, "rx"),
    "clip-limiter": Setting("防爆音", 0x1E, "rx"),
    "mic-leds": Setting("指示灯", 0x0A, "broadcast", ((False, "关闭", 2), (True, "开启", 0))),
    "auto-off-15m": Setting("接收器自动关机（15 分钟）", 0x10, "rx"),
    "tx-auto-off-15m": Setting("发射器自动关机（15 分钟）", 0x10, "broadcast"),
    "voice-tone": Setting("音色", 0x29, "tx", (("standard", "标准", 0), ("rich", "饱满", 1), ("bright", "明亮", 2))),
    "noise-cancel-button": Setting("电源键切换降噪", 0x0F, "broadcast"),
    "camera-power": Setting("跟随相机开关机", 0x20, "rx"),
    "plug-free": Setting("免拔插外放", 0x23, "rx", reboot=True),
}


ERRORS = {
    "unavailable": "设备未就绪，请等待有效状态回报", "unsupported": "当前固件或设备不支持此设置",
    "offline": "发射器离线", "invalid": "无效的麦克风设置请求", "busy": "上一项设置仍在确认中",
    "write_failed": "设置发送失败，请检查连接", "timeout": "设置超时，未获得确认的设备回报",
    "disconnected": "设置期间连接中断，结果未确认", "confirmed": "设备已回报并确认设置",
    "rebooting": "接收器正在重启，等待重新连接确认", "failed": "设置失败，设备未确认更改",
    "rejected": "设备返回非成功 ACK，设置未确认；可能受固件能力限制",
}


class ControlError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(ERRORS.get(code, code))


def reported(status, setting_id, unit=None):
    if setting_id == "voice-tone":
        tx = status.tx.get(unit, {})
        if tx.get("product") != "DJI Mic Mini 2":
            return None
        return tx.get("tone")
    if setting_id not in status.settings:
        return None
    if SETTINGS[setting_id].target == "broadcast":
        per_tx = [tx.get("settings", {}).get(setting_id) for tx in status.tx.values()]
        if per_tx and any(value is None for value in per_tx):
            return None
        if per_tx and any(value is not None and value != status.settings[setting_id] for value in per_tx):
            return None  # mixed actual states are not a shared on/off value
    return status.settings[setting_id]


def build_command(status, seq, setting_id, value, unit=None, reboot_confirmed=False):
    if status.version != 2 or not status.status_received:
        raise ControlError("unsupported")
    desc = SETTINGS.get(setting_id)
    if desc is None:
        raise ControlError("invalid")
    if setting_id in ("noise-cancel", "noise-cancel-power") and status.receiver_product == "DJI Mic Mini 2":
        raise ControlError("unsupported")
    option = next((o for o in desc.options if type(o[0]) is type(value) and o[0] == value), None)
    if option is None:
        raise ControlError("invalid")
    if desc.target == "tx":
        if type(unit) is not int or unit not in (1, 2) or unit not in status.tx:
            raise ControlError("offline")
        if status.tx[unit].get("product") != "DJI Mic Mini 2":
            raise ControlError("unsupported")
        target = unit
    else:
        if unit is not None:
            raise ControlError("invalid")
        if desc.target == "broadcast" and not status.tx:
            raise ControlError("offline")
        target = 0xFFFF if desc.target == "broadcast" else 0
    if setting_id not in status.settings and desc.target != "tx":
        raise ControlError("unsupported")
    if desc.reboot and reboot_confirmed is not True:
        raise ControlError("invalid")
    if desc.reboot and not status.receiver_id:
        raise ControlError("unavailable")
    body = bytearray(20)
    body[4:6] = b"\x02\x5a"
    body[6:8] = (seq & 0xFFFF).to_bytes(2, "little")
    body[8:12] = b"\x40\x5b\x01\x02"
    body[12:14] = target.to_bytes(2, "little")
    body[16:18] = desc.command.to_bytes(2, "little")
    body[18:20] = bytes((1, option[2]))
    return frame(body)


class Transaction:
    def __init__(self, request, seq, started, receiver_id=""):
        self.request, self.seq, self.started = request, seq, started
        self.ack = False
        self.ack_status = None
        self.reboot = SETTINGS[request["setting"]].reboot
        self.receiver_id = receiver_id

    def acknowledge(self, packet):
        if len(packet) == 14 and packet[8:11] == b"\x80\x5b\x01" and int.from_bytes(packet[6:8], "little") == self.seq:
            self.ack_status = packet[11]
            self.ack = self.ack_status == 0

    def confirmed(self, status, reconnect=False):
        # Reboots can sever the ACK; a fresh connection's real state is required.
        if not self.ack and not (self.reboot and reconnect):
            return False
        if self.reboot:
            if not reconnect or not self.receiver_id or status.receiver_id != self.receiver_id:
                return False
        key, value = self.request["setting"], self.request["value"]
        actual = reported(status, key, self.request.get("unit"))
        if actual is None or type(actual) is not type(value) or actual != value:
            return False
        if key == "stereo" and value == "stereo" and status.settings.get("safety-track") is not False:
            return False
        if key == "safety-track" and value is True and status.settings.get("stereo") != "mono":
            return False
        return True

    def expired(self, now):
        return now - self.started > (15 if self.reboot else 3)


def validate_request(raw):
    if not isinstance(raw, dict) or set(raw) - {"id", "setting", "value", "unit", "reboot_confirmed"}:
        raise ControlError("invalid")
    if not isinstance(raw.get("id"), str) or not 1 <= len(raw["id"]) <= 64:
        raise ControlError("invalid")
    if not isinstance(raw.get("setting"), str) or raw["setting"] not in SETTINGS or "value" not in raw:
        raise ControlError("invalid")
    return raw


def status_from_message(raw):
    """Validate the control part of a child snapshot before presenting it."""
    from dji_protocol import Status
    if not isinstance(raw, dict) or raw.get("protocol") not in (None, 1, 2):
        raise ValueError("Invalid protocol")
    settings, txs = raw.get("settings", {}), raw.get("tx", {})
    if not isinstance(settings, dict) or not isinstance(txs, dict) or len(txs) > 2:
        raise ValueError("Invalid state")
    def check_settings(values):
        if not isinstance(values, dict):
            raise ValueError("Invalid settings")
        for key, value in values.items():
            if key not in SETTINGS or not any(type(value) is type(o[0]) and value == o[0] for o in SETTINGS[key].options):
                raise ValueError("Invalid setting value")
    check_settings(settings)
    unavailable = raw.get("unavailable_settings", [])
    allowed_unavailable = set(SETTINGS) | {"voice-tone:1", "voice-tone:2"}
    if not isinstance(unavailable, list) or len(unavailable) > 15 or any(not isinstance(k, str) or k not in allowed_unavailable for k in unavailable):
        raise ValueError("Invalid capability result")
    result = raw.get("command_result")
    if result is not None:
        if not isinstance(result, dict) or result.get("code") not in (set(ERRORS) | {"pending"}):
            raise ValueError("Invalid command result")
        if not isinstance(result.get("id"), str) or len(result["id"]) > 64:
            raise ValueError("Invalid command id")
    if raw.get("protocol") is not None and type(raw.get("protocol")) is not int:
        raise ValueError("Invalid protocol type")
    parsed = {}
    for key, tx in txs.items():
        if key not in ("1", "2") or not isinstance(tx, dict):
            raise ValueError("Invalid transmitter")
        if tx.get("tone") not in (None, "standard", "rich", "bright"):
            raise ValueError("Invalid tone")
        if not isinstance(tx.get("product", ""), str) or len(tx.get("product", "")) > 80:
            raise ValueError("Invalid product")
        check_settings(tx.get("settings", {}))
        parsed[int(key)] = tx
    product = raw.get("receiver_product", "")
    if not isinstance(product, str) or len(product) > 80:
        raise ValueError("Invalid receiver model")
    return Status(version=raw.get("protocol"), connected=sum(1 << (u - 1) for u in parsed),
                  tx=parsed, settings=settings, status_received=raw.get("protocol") is not None,
                  receiver_product=product)
