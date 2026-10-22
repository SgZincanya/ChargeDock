"""Qt supervisor; all USB work stays in the managed child process."""
import json
import sys
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from core import DJI_MODELS, validate_readings, atomic_json
from dji_protocol import Status, readings


class DjiClient(QObject):
    updated = Signal(list, dict)

    def __init__(self, data_dir):
        super().__init__()
        self.folder = Path(data_dir) / ("dji-" + uuid.uuid4().hex)
        self.folder.mkdir(parents=True)
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

    def start(self):
        # Never replay a command after a crash or restart.
        (self.folder / "command.json").unlink(missing_ok=True)
        self.pending_id = None
        (self.folder / "lease").touch()
        args = ["--dji-worker", "--data-dir", str(self.folder)]
        if not getattr(sys, "frozen", False):
            args.insert(0, str(Path(__file__).with_name("chargedock.py")))
        self.process.start(sys.executable, args)
        self.started = time.time()
        self.last_heartbeat = self.started
        self.timer.start(250)

    def tick(self):
        if self.stopping:
            return
        now = time.time()
        if now - self.last_lease > 1:
            try:
                (self.folder / "lease").touch()
                self.last_lease = now
            except OSError:
                self.process.kill()
        if self.process.state() == QProcess.ProcessState.NotRunning:
            if not self.retry_at:
                self.pending_id = None
                self.state = {}
                self.updated.emit(readings(Status(), "read_failed", "worker exited"), {})
                self.retry_at = now + 5
            if now >= self.retry_at:
                self.retry_at = 0
                self.start()
            return
        try:
            path = self.folder / "state.json"
            if path.stat().st_size > 65536:
                raise ValueError("Oversized DJI message")
            raw = path.read_text(encoding="utf-8")
            if raw != self.last_raw:
                state = json.loads(raw)
                values = validate_readings(state["readings"], DJI_MODELS)
                from dji_control import status_from_message
                status_from_message(state)
                heartbeat = state["heartbeat"]
                if type(heartbeat) not in (int, float) or not self.started - 1 <= heartbeat <= now + 1:
                    raise ValueError("Invalid heartbeat")
                self.last_raw, self.last_heartbeat, self.state = raw, heartbeat, state
                result = state.get("command_result") or {}
                if result.get("id") == self.pending_id and result.get("code") not in ("pending", "rebooting"):
                    self.pending_id = None
                self.updated.emit(values, state)
        except (OSError, ValueError, TypeError, KeyError):
            pass
        if now - self.last_heartbeat > 6:
            self.updated.emit(readings(Status(), "stale", "worker timeout"), {})
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
        (self.folder / "refresh").touch()

    def stop(self):
        self.stopping = True
        self.timer.stop()
        (self.folder / "stop").touch()
        if self.process.state() != QProcess.ProcessState.NotRunning:
            if not self.process.waitForFinished(700):
                self.process.kill()
                self.process.waitForFinished(1000)
        # Exact known mailbox files only. Never recursively remove a data dir.
        for name in ("state.json", "state.json.tmp", "command.json", "command.json.tmp", "lease", "stop", "refresh"):
            (self.folder / name).unlink(missing_ok=True)
        try:
            self.folder.rmdir()
        except OSError:
            pass
