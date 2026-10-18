import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.recovery import (
    MAX_RECOVERY_ATTEMPTS,
    RECOVERY_FAILED,
    RecoveryStore,
)


class RecoveryStoreTests(unittest.TestCase):
    def test_checkpoint_is_atomic_and_preserves_session_time(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recovery.json"
            store = RecoveryStore(str(path))
            checkpoint = store.begin_session("owner-a", 600.0)
            store.mark_disconnect()
            store.mark_recovery_failed()

            restored = RecoveryStore(str(path)).checkpoint
            self.assertEqual(restored.session_id, checkpoint.session_id)
            self.assertEqual(restored.user_id, "owner-a")
            self.assertEqual(restored.remaining_seconds, 600.0)
            self.assertEqual(restored.lifecycle, RECOVERY_FAILED)
            self.assertEqual(list(path.parent.glob(".*.recovery.json.*")), [])

    def test_recovery_budget_is_nine_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecoveryStore(str(Path(directory) / "recovery.json"))
            store.begin_session("owner-a", 120.0)
            with patch("core.recovery.time.time", return_value=1000.0):
                for _ in range(MAX_RECOVERY_ATTEMPTS):
                    self.assertTrue(store.begin_reconnect_attempt())
                self.assertFalse(store.begin_reconnect_attempt())
            self.assertEqual(store.checkpoint.lifecycle, RECOVERY_FAILED)
            self.assertEqual(store.checkpoint.remaining_seconds, 120.0)

    def test_recovery_window_resets_after_three_hours(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecoveryStore(str(Path(directory) / "recovery.json"))
            store.begin_session("owner-a", 120.0)
            with patch("core.recovery.time.time", side_effect=[1000.0] * 9 + [11801.0]):
                for _ in range(MAX_RECOVERY_ATTEMPTS):
                    self.assertTrue(store.begin_reconnect_attempt())
                self.assertTrue(store.begin_reconnect_attempt())
            self.assertEqual(store.checkpoint.recovery_attempts, 1)


if __name__ == "__main__":
    unittest.main()
