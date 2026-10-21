import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core
import hid_win
import rog_proto
from core import Reading, MODELS
from polling import RetrySchedule
from chargedock import arguments


def good(key):
    return Reading(key, key, key, 50)


class EnumerationTests(unittest.TestCase):
    def test_full_round_enumerates_only_once(self):
        with patch.object(hid_win, "enumerate_hid", return_value=[]) as enum:
            readings = core.read_all()
        enum.assert_called_once()
        self.assertEqual([r.key for r in readings], list(MODELS))
        self.assertTrue(all(r.level is None and r.error for r in readings))

    def test_partial_round_enumerates_only_once_and_keeps_order(self):
        with patch.object(hid_win, "enumerate_hid", return_value=[]) as enum:
            readings = core.read_all(["keyboard", "mouse"])
        enum.assert_called_once()
        self.assertEqual([r.key for r in readings], ["keyboard", "mouse"])

    def test_empty_or_invalid_request_does_not_access_devices(self):
        with patch.object(hid_win, "enumerate_hid") as enum:
            self.assertEqual(core.read_all([]), [])
            for keys in (["no-device"], ["mouse", "mouse"]):
                with self.assertRaises(ValueError):
                    core.read_all(keys)
            enum.assert_not_called()

    def test_empty_shared_inventory_is_not_enumerated_again(self):
        with patch.object(hid_win, "enumerate_hid") as enum:
            core.read_headset([])
            rog_proto.read_device("mouse", [])
            enum.assert_not_called()

    def test_inventory_failure_returns_per_device_errors(self):
        with patch.object(hid_win, "enumerate_hid", side_effect=OSError("offline")) as enum:
            readings = core.read_all()
        enum.assert_called_once()
        self.assertEqual(len(readings), 3)
        self.assertTrue(all("offline" in r.error for r in readings))

    def test_individual_read_failure_does_not_discard_other_devices(self):
        def read(kind, inventory):
            if kind == "mouse":
                raise OSError("mouse failed")
            return rog_proto.RogReading("keyboard", 60)
        with patch.object(hid_win, "enumerate_hid", return_value=[]), \
             patch.object(rog_proto, "read_device", side_effect=read):
            result = core.read_all(["mouse", "keyboard"])
        self.assertIsNone(result[0].level)
        self.assertEqual(result[1].level, 60)

    def test_repeated_cli_keys_and_partial_json_roundtrip(self):
        with patch.object(sys, "argv", ["chargedock", "--poll-key", "mouse", "--poll-key", "keyboard"]):
            self.assertEqual(arguments().poll_key, ["mouse", "keyboard"])
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / "result.json"
            with patch.object(core, "read_all", return_value=[good("mouse"), good("keyboard")]) as read:
                core.poll_to_file(path, ["mouse", "keyboard"])
                read.assert_called_once_with(["mouse", "keyboard"])
            self.assertEqual(len(core.read_poll_file(path, ["mouse", "keyboard"])), 2)
            with self.assertRaises(ValueError):
                core.read_poll_file(path, ["headset"])


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.schedule = RetrySchedule(MODELS, lambda: self.now)

    def test_failed_device_exponentially_backs_off_and_caps(self):
        delays = []
        for _ in range(10):
            self.schedule.record([], ["headset"], 60)
            delay = self.schedule.deadlines["headset"] - self.now
            delays.append(delay)
            self.now += delay
        self.assertEqual(delays, [5, 10, 20, 40, 80, 160, 300, 300, 300, 300])

    def test_success_resets_failures_and_returns_to_normal_interval(self):
        self.schedule.record([], ["mouse"], 60)
        self.now = 5
        self.schedule.record([good("mouse")], ["mouse"], 120)
        self.assertEqual(self.schedule.failures["mouse"], 0)
        self.assertEqual(self.schedule.deadlines["mouse"], 125)

    def test_healthy_round_does_not_bypass_other_devices_backoff(self):
        self.schedule.record([], ["headset"], 60)
        for _ in range(6):
            self.now = self.schedule.deadlines["headset"]
            self.schedule.record([], ["headset"], 60)
        deadline = self.schedule.deadlines["headset"]
        self.schedule.record([good("mouse"), good("keyboard")], ["mouse", "keyboard"], 60)
        self.now += 60
        self.assertEqual(self.schedule.due(), ["mouse", "keyboard"])
        self.assertEqual(self.schedule.deadlines["headset"], deadline)

    def test_wake_only_resets_the_corresponding_device(self):
        self.schedule.record([], list(MODELS), 60)
        self.schedule.reset(["mouse"])
        self.assertEqual(self.schedule.due(), ["mouse"])
        self.assertEqual(self.schedule.failures["headset"], 1)
        self.schedule.reset()
        self.assertEqual(self.schedule.due(), list(MODELS))

    def test_interval_change_preserves_failed_device_deadline(self):
        self.schedule.record([good("mouse")], list(MODELS), 60)
        self.schedule.change_interval(300)
        self.assertEqual(self.schedule.deadlines["mouse"], 300)
        self.assertEqual(self.schedule.deadlines["headset"], 5)

    def test_due_keys_obey_deadlines_without_busy_loop(self):
        self.schedule.record([good(k) for k in MODELS], list(MODELS), 60)
        self.assertEqual(self.schedule.due(), [])
        self.assertGreaterEqual(self.schedule.delay_ms(), 60000)
        self.now = 60
        self.assertEqual(self.schedule.due(), list(MODELS))
        self.assertGreaterEqual(self.schedule.delay_ms(), 1)

    def test_invalid_reading_counts_as_failure(self):
        reading = good("mouse"); reading.error = "bad reply"
        self.schedule.record([reading], ["mouse"], 60)
        self.assertEqual(self.schedule.failures["mouse"], 1)


if __name__ == "__main__":
    unittest.main()
