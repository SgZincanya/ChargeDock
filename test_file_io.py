"""Real Windows sharing locks, recovery, bounded failure and child reporting."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core import atomic_json
from file_io import open_shared_binary, read_json_shared, replace_file


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.path = self.root / "state.json"
        atomic_json(self.path, {"version": "old"})

    @unittest.skipUnless(sys.platform == "win32", "Windows sharing semantics")
    def test_reader_permits_replacement_and_keeps_complete_old_snapshot(self):
        with open_shared_binary(self.path) as reader:
            atomic_json(self.path, {"version": "new"})
            self.assertEqual(json.loads(reader.read()), {"version": "old"})
        self.assertEqual(read_json_shared(self.path), {"version": "new"})

    @unittest.skipUnless(sys.platform == "win32", "Windows sharing semantics")
    def test_locked_target_recovers_after_reader_releases_it(self):
        collision = threading.Event()
        attempts, errors = [], []
        replace = replace_file
        def replacing(path, target):
            attempts.append(True)
            try:
                return replace(path, target)
            except PermissionError:
                collision.set()
                raise
        def writing():
            try:
                atomic_json(self.path, {"version": "new"}, replace_attempts=5)
            except Exception as exc:
                errors.append(exc)
        reader = self.path.open("rb")  # the old client read mode blocks rename
        with patch("core.replace_file", replacing):
            writer = threading.Thread(target=writing)
            writer.start()
            try:
                self.assertTrue(collision.wait(1), "must reproduce a real sharing violation")
                self.assertEqual(read_json_shared(self.path), {"version": "old"})
            finally:
                reader.close()
                writer.join(2)
        self.assertFalse(writer.is_alive())
        self.assertFalse(errors, errors)
        self.assertGreaterEqual(len(attempts), 2)
        self.assertEqual(read_json_shared(self.path), {"version": "new"})
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    @unittest.skipUnless(sys.platform == "win32", "Windows sharing semantics")
    def test_persistent_lock_fails_within_limit_and_preserves_good_file(self):
        with self.path.open("rb"), patch("core.time.sleep") as sleeping:
            with self.assertRaises(PermissionError):
                atomic_json(self.path, {"version": "new"}, replace_attempts=3)
        self.assertEqual(sleeping.call_count, 2)
        self.assertEqual(read_json_shared(self.path), {"version": "old"})
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_bounded_and_invalid_reads(self):
        with self.assertRaises(ValueError):
            read_json_shared(self.path, 2)
        self.path.write_bytes(b"{broken")
        with self.assertRaises(ValueError):
            read_json_shared(self.path)

    def test_worker_reports_permission_failure_without_raising(self):
        import dji_worker
        with patch.object(dji_worker, "run", side_effect=PermissionError("target locked")):
            self.assertEqual(dji_worker.run_managed(self.root), 1)
        failure = read_json_shared(self.root / "failure.json")
        self.assertEqual(failure["state"], "storage_failed")
        self.assertIn("target locked", failure["detail"])

    def test_worker_exits_even_when_diagnostic_file_is_unwritable(self):
        import dji_worker
        with patch.object(dji_worker, "run", side_effect=PermissionError("denied")), \
             patch.object(dji_worker, "atomic_json", side_effect=PermissionError("denied")):
            self.assertEqual(dji_worker.run_managed(self.root), 1)

    def test_retry_limit_cannot_be_unbounded_or_zero(self):
        for value in (0, -1, 9, True, "5"):
            with self.assertRaises(ValueError):
                atomic_json(self.path, {}, replace_attempts=value)


if __name__ == "__main__":
    unittest.main()
