import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import asus_control
from core import AlertPolicy, Reading, defaults, parse_headset, validate_config


def reading(level, key="mouse", charging=False, error=""):
    return Reading(key, key, key, level, charging, error=error)


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.policy, self.config = AlertPolicy(), defaults()

    def test_first_low_reading_and_equality(self):
        for level in (0, 10, 20):
            self.assertEqual(len(AlertPolicy().evaluate([reading(level)], self.config)), 1)
        self.assertEqual(self.policy.evaluate([reading(21)], self.config), [])

    def test_repeated_polls_do_not_repeat_alert(self):
        self.policy.evaluate([reading(20)], self.config)
        for level in (19, 18, 15, 1, 0):
            self.assertEqual(self.policy.evaluate([reading(level)], self.config), [])

    def test_both_thresholds_are_independent(self):
        self.config["alerts"]["mouse"]["threshold"] = 30
        values = self.policy.evaluate([reading(25), reading(25, "keyboard")], self.config)
        self.assertEqual([r.key for r in values], ["mouse"])

    def test_headset_never_alerts(self):
        self.assertEqual(self.policy.evaluate([reading(0, "headset")], self.config), [])

    def test_disabled_and_charging_devices_do_not_alert(self):
        self.config["alerts"]["mouse"]["enabled"] = False
        self.assertEqual(self.policy.evaluate([reading(1)], self.config), [])
        self.assertEqual(self.policy.evaluate([reading(1, "keyboard", True)], self.config), [])

    def test_offline_and_error_do_not_alert(self):
        self.assertEqual(self.policy.evaluate([reading(None), reading(1, error="offline")], self.config), [])

    def test_recovery_hysteresis(self):
        self.policy.evaluate([reading(20)], self.config)
        for level in (21, 24, 25, 20):
            self.assertEqual(self.policy.evaluate([reading(level)], self.config), [])
        self.policy.evaluate([reading(26)], self.config)
        self.assertEqual(len(self.policy.evaluate([reading(20)], self.config)), 1)

    def test_charging_rearms_after_discharge(self):
        self.policy.evaluate([reading(10)], self.config)
        self.policy.evaluate([reading(12, charging=True)], self.config)
        self.assertEqual(len(self.policy.evaluate([reading(12)], self.config)), 1)

    def test_snooze_does_not_lose_pending_alert(self):
        self.policy.snoozed_until = 1800
        self.assertEqual(self.policy.evaluate([reading(10)], self.config, now=100), [])
        self.assertEqual(len(self.policy.evaluate([reading(10)], self.config, now=1801)), 1)

    def test_invalid_settings_are_sanitized(self):
        self.assertEqual(validate_config(None), defaults())
        self.assertEqual(validate_config({"alerts": []}), defaults())
        value = validate_config({"interval": 0, "dismiss_seconds": -1, "alerts": {
            "mouse": {"threshold": 200}, "keyboard": {"threshold": -5}}})
        self.assertEqual(value["interval"], 60)
        self.assertEqual(value["dismiss_seconds"], 12)
        self.assertEqual(value["alerts"]["mouse"]["threshold"], 99)
        self.assertEqual(value["alerts"]["keyboard"]["threshold"], 1)


class HeadsetTests(unittest.TestCase):
    @staticmethod
    def packet(code, state=8):
        result = bytearray(64)
        result[:2] = b"\x06\xb0"
        result[6], result[15] = code, state
        return bytes(result)

    def test_nine_steps(self):
        for raw, percent in enumerate((0, 13, 25, 38, 50, 63, 75, 88, 100)):
            self.assertEqual(parse_headset(self.packet(raw)), (percent, False, ""))

    def test_charging(self):
        self.assertEqual(parse_headset(self.packet(7, 2)), (88, True, ""))

    def test_offline_invalid_and_unrelated(self):
        for raw in (self.packet(7, 1), self.packet(9), self.packet(7, 0), b"\0" * 64,
                    self.packet(7)[:12]):
            self.assertIsNone(parse_headset(raw)[0])
            self.assertTrue(parse_headset(raw)[2])


class AsusTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.exe = self.root / "AcPowerNotification.exe"
        self.backup = self.root / "AcPowerNotification.exe.disabled"
        self.result = self.root / "result.json"
        for name, value in (("ORIGINAL", self.exe), ("BACKUP", self.backup), ("COMPONENT", self.root)):
            self.patch = patch.object(asus_control, name, value)
            self.patch.start()
            self.addCleanup(self.patch.stop)

    def test_disable_and_restore_preserves_file(self):
        self.exe.write_bytes(b"example executable")
        with patch.object(asus_control.subprocess, "run"), patch.object(asus_control.subprocess, "Popen") as launch:
            self.assertEqual(asus_control.helper_action("disable", self.result), 0)
            self.assertFalse(self.exe.exists())
            self.assertEqual(self.backup.read_bytes(), b"example executable")
            self.assertEqual(asus_control.helper_action("enable", self.result), 0)
            self.assertEqual(self.exe.read_bytes(), b"example executable")
            launch.assert_called_once()

    def test_never_overwrites_backup_after_update(self):
        self.exe.write_bytes(b"new")
        self.backup.write_bytes(b"old")
        self.assertEqual(asus_control.helper_action("disable", self.result), 1)
        self.assertEqual(self.exe.read_bytes(), b"new")
        self.assertEqual(self.backup.read_bytes(), b"old")

    def test_permission_error_is_reported(self):
        self.exe.write_bytes(b"example")
        with patch.object(asus_control.subprocess, "run"), patch.object(Path, "rename", side_effect=PermissionError("denied")):
            self.assertEqual(asus_control.helper_action("disable", self.result), 1)
        self.assertIn('"ok": false', self.result.read_text(encoding="utf-8"))
        self.assertTrue(self.exe.exists())


if __name__ == "__main__":
    unittest.main()
