"""Qt supervisor; all USB work stays in the managed child process."""
import json
import sys
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from core import DJI_MODELS, validate_readings, atomic_json
from dji_protocol import Status, readings
from file_io import read_json_shared


class DjiClient(QObject):
    updated = Signal(list, dict)

    def __init__(self, data_dir):
        super().__init__()
        self.folder = Path(data_dir) / ("dji-" + uuid.uuid4().hex)
        self.process = QProcess(self)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.last_heartbeat = 0
        self.started = 0
        self.retry_at = 0
        self.last_raw = None
        self.stopping = False
        self.state = {}
        self.pending_id = None
        self.last_lease = 0
        self.last_presentation = None
        self.last_io_error = ""

    def notify_failure(self, state, detail):
        from dataclasses import asdict
        values = readings(Status(), state, detail)
        self.state = {"protocol": None, "settings": {}, "tx": {},
                      "readings": [asdict(r) for r in values]}
        self.updated.emit(values, self.state)

    def worker_failure(self):
        try:
            error = read_json_shared(self.folder / "failure.json", 4096)
            if (isinstance(error, dict) and error.get("state") in ("storage_failed", "read_failed")
                    and isinstance(error.get("detail"), str)):
                self.notify_failure(error["state"], error["detail"][:2048])
                return
        except (OSError, ValueError, TypeError):
            pass
        self.notify_failure("storage_failed" if self.last_io_error else "read_failed",
                            self.last_io_error or "worker exited")

    def start(self):
        # Never replay a command after a crash or restart.
        self.pending_id = None
        self.last_presentation = None
        self.last_io_error = ""
        self.timer.start(250)
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            for name in ("command.json", "failure.json"):
                (self.folder / name).unlink(missing_ok=True)
            (self.folder / "lease").touch()
        except OSError as exc:
            self.last_io_error = str(exc)
            self.notify_failure("storage_failed", self.last_io_error)
            self.retry_at = time.time() + 5
            return
        args = ["--dji-worker", "--data-dir", str(self.folder)]
        if not getattr(sys, "frozen", False):
            args.insert(0, str(Path(__file__).with_name("chargedock.py")))
        self.process.start(sys.executable, args)
        self.started = time.time()
        self.last_heartbeat = self.started

    def tick(self):
        if self.stopping:
            return
        now = time.time()
        if now - self.last_lease > 1:
            try:
                (self.folder / "lease").touch()
                self.last_lease = now
            except OSError as exc:
                self.last_io_error = str(exc)
                self.process.kill()
        if self.process.state() == QProcess.ProcessState.NotRunning:
            if not self.retry_at:
                self.pending_id = None
                self.worker_failure()
                self.retry_at = now + 5
            if now >= self.retry_at:
                self.retry_at = 0
                self.start()
            return
        try:
            state = read_json_shared(self.folder / "state.json")
            raw = json.dumps(state, ensure_ascii=False, sort_keys=True)
            if raw != self.last_raw:
                values = validate_readings(state["readings"], DJI_MODELS)
                from dji_control import status_from_message
                status_from_message(state)
                heartbeat = state["heartbeat"]
                if type(heartbeat) not in (int, float) or not self.started - 1 <= heartbeat <= now + 1:
                    raise ValueError("Invalid heartbeat")
                self.last_raw, self.last_heartbeat, self.state = raw, heartbeat, state
                self.last_io_error = ""
                result = state.get("command_result") or {}
                if result.get("id") == self.pending_id and result.get("code") not in ("pending", "rebooting"):
                    self.pending_id = None
                visible = {k: v for k, v in state.items() if k not in ("heartbeat", "readings", "rejected")}
                visible["readings"] = [{k: v for k, v in item.items() if k != "updated"}
                                       for item in state["readings"]]
                signature = json.dumps(visible, ensure_ascii=False, sort_keys=True)
                if signature != self.last_presentation:
                    self.last_presentation = signature
                    self.updated.emit(values, state)
        except OSError as exc:
            if not isinstance(exc, FileNotFoundError):
                self.last_io_error = str(exc)
        except (ValueError, TypeError, KeyError):
            pass
        if now - self.last_heartbeat > 6:
            self.notify_failure("storage_failed" if self.last_io_error else "stale",
                                self.last_io_error or "worker timeout")
            self.process.kill()

    def submit(self, setting, value, unit=None, reboot_confirmed=False):
        if self.pending_id:
            return False
        request = {"id": uuid.uuid4().hex, "setting": setting, "value": value,
                   "unit": unit, "reboot_confirmed": reboot_confirmed}
        atomic_json(self.folder / "command.json", request)
        self.pending_id = request["id"]
        return True

    def refresh(self):
        try:
            (self.folder / "refresh").touch()
        except OSError as exc:
            self.last_io_error = str(exc)
            self.notify_failure("storage_failed", self.last_io_error)

    def stop(self):
        self.stopping = True
        self.timer.stop()
        try:
            (self.folder / "stop").touch()
        except OSError:
            pass
        if self.process.state() != QProcess.ProcessState.NotRunning:
            if not self.process.waitForFinished(700):
                self.process.kill()
                self.process.waitForFinished(1000)
        # Exact known mailbox files only. Never recursively remove a data dir.
        for name in ("state.json", "state.json.tmp", "command.json", "command.json.tmp", "failure.json", "lease", "stop", "refresh"):
            try:
                (self.folder / name).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            self.folder.rmdir()
        except OSError:
            pass
