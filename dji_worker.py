"""Managed single-owner DJI worker. File mailboxes avoid windowed EXE stdio.

The parent supplies a private per-session directory. It never stores device
settings as desired values or changes a driver. A crashed/hung worker is killed
by the parent; the OS releases the WinUSB handle.
"""
import time
import json
from dataclasses import asdict
from pathlib import Path

from core import atomic_json
from dji_protocol import Status, Stream, readings
from dji_control import ControlError, Transaction, validate_request, build_command


def run(folder, connection_factory=None, clock=time.monotonic, sleep=time.sleep):
    from dji_usb import Connection, DeviceError
    folder = Path(folder)
    connection_factory = connection_factory or Connection
    failure, retry_at = 0, 0
    status, stream, connection = Status(), Stream(), None
    last_status = last_emit = opened_at = 0
    current, detail = "connecting", ""
    seq, pending, result, last_command, reconnected = 1, None, None, None, False
    unavailable = set()
    try:
        while not (folder / "stop").exists():
            lease = folder / "lease"
            if lease.exists() and time.time() - lease.stat().st_mtime > 10:
                break  # parent disappeared; release the interface
            now = clock()
            refresh_file = folder / "refresh"
            if refresh_file.exists():
                refresh_file.unlink(missing_ok=True)
                retry_at = 0
            if connection is None and now >= retry_at:
                try:
                    connection = connection_factory()
                    status, stream = Status(), Stream()
                    last_status = 0
                    opened_at = clock()
                    reconnected = pending is not None
                    unavailable.clear()
                    current, detail = "no_frames", ""
                except DeviceError as exc:
                    current, detail = exc.state, exc.detail
                    failure = min(failure + 1, 7)
                    retry_at = now + min(300, 5 * 2 ** (failure - 1))
            command_file = folder / "command.json"
            try:
                if command_file.exists() and command_file.stat().st_size <= 4096:
                    request = validate_request(json.loads(command_file.read_text(encoding="utf-8")))
                    if request["id"] != last_command:
                        last_command = request["id"]
                        if pending:
                            raise ControlError("busy")
                        if not connection or current != "online" or clock() - last_status > 1.5:
                            raise ControlError("unavailable")
                        packet = build_command(status, seq, request["setting"], request["value"],
                            request.get("unit"), request.get("reboot_confirmed", False))
                        try:
                            connection.write(packet)
                        except DeviceError:
                            raise ControlError("write_failed")
                        pending = Transaction(request, seq, clock(), status.receiver_id)
                        seq = (seq + 1) & 0xFFFF
                        reconnected = False
                        result = {"id": request["id"], "code": "rebooting" if pending.reboot else "pending"}
            except ControlError as exc:
                result = {"id": last_command, "code": exc.code}
            except (OSError, ValueError, TypeError):
                pass
            if connection:
                try:
                    for packet in stream.feed(connection.read()):
                        if pending:
                            pending.acknowledge(packet)
                            if pending.ack_status not in (None, 0):
                                unavailable.add(pending.request["setting"] + (":" + str(pending.request["unit"])
                                    if pending.request.get("unit") else ""))
                                result = {"id": pending.request["id"], "code": "rejected",
                                          "ack_status": pending.ack_status}
                                pending = None
                        if status.decode(packet):
                            last_status = clock()
                            current, detail, failure = "online", "", 0
                            if pending and pending.confirmed(status, reconnected):
                                result = {"id": pending.request["id"], "code": "confirmed"}
                                pending = None
                    if status.receiver_product == "DJI Mic Mini 2":
                        unavailable.update(("noise-cancel", "noise-cancel-power"))
                    if last_status and clock() - last_status > 3:
                        current = "stale"
                    # Reopen a silent interface; a bounded wait must recover.
                    if clock() - (last_status or opened_at) > 6:
                        connection.close(); connection = None
                        retry_at = clock() + 5
                except DeviceError as exc:
                    connection.close(); connection = None
                    current, detail = exc.state, exc.detail
                    retry_at = clock() + (0.5 if pending and pending.reboot else 5)
                    if pending and not pending.reboot:
                        result = {"id": pending.request["id"], "code": "disconnected"}
                        pending = None
            else:
                sleep(0.1)
            now = clock()
            if pending and pending.expired(now):
                result = {"id": pending.request["id"], "code": "timeout"}
                pending = None
            if now - last_emit >= 0.5:
                atomic_json(folder / "state.json", {"heartbeat": time.time(),
                    "readings": [asdict(r) for r in readings(status, current, detail,
                        time.time() - (now - last_status) if last_status else 0)],
                    "settings": status.settings, "tx": status.tx,
                    "protocol": status.version, "rejected": stream.rejected,
                    "receiver_product": status.receiver_product,
                    "command_result": result, "unavailable_settings": sorted(unavailable)})
                last_emit = now
    finally:
        if connection:
            connection.close()
    return 0
