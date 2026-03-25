from __future__ import annotations

from datetime import datetime, timezone
import socket
from pathlib import Path
import tempfile
import unittest

from booking_bot.runtime_state import RunHistoryEntry, RuntimeStateStore, SchedulerState


class RuntimeStateStoreTests(unittest.TestCase):
    def test_scheduler_state_roundtrip_and_history_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStateStore(Path(tmp))
            state = SchedulerState(
                last_scheduled_run_local_date="2026-03-02",
                last_run_status="booked",
                last_run_mode="scheduled",
                last_healthcheck_local_date="2026-03-02",
            )
            store.save_scheduler_state(state)
            restored = store.load_scheduler_state()
            self.assertEqual(restored.last_scheduled_run_local_date, "2026-03-02")
            self.assertEqual(restored.last_run_status, "booked")
            self.assertEqual(restored.last_healthcheck_local_date, "2026-03-02")

            now = datetime.now(timezone.utc).isoformat()
            for idx in range(4):
                store.append_run_history(
                    RunHistoryEntry(
                        run_id=f"run-{idx}",
                        mode="manual",
                        started_at_utc=now,
                        finished_at_utc=now,
                        target_date=f"0{idx + 1}.03.2026",
                        seat_attempt_order=["17", "19"],
                        chosen_seat="17",
                        status="booked",
                        summary=f"summary-{idx}",
                        otp_requested=False,
                        otp_received=False,
                        screenshot_path=None,
                    ),
                    limit=3,
                )

            history = store.read_run_history(limit=10)
            self.assertEqual(len(history), 3)
            self.assertEqual(history[0].run_id, "run-1")
            self.assertEqual(history[-1].run_id, "run-3")
            self.assertEqual(store.read_last_history().run_id, "run-3")

    def test_run_lock_can_recover_from_dead_pid_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeStateStore(Path(tmp))
            store.run_lock_path.write_text(
                (
                    '{"run_id":"old","acquired_at_utc":"2026-03-02T10:29:45+00:00",'
                    f'"hostname":"{socket.gethostname()}","pid":99999999'
                    "}\n"
                ),
                encoding="utf-8",
            )
            store.acquire_run_lock("new-run", stale_after_sec=10_000)
            info = store._read_lock_info()
            self.assertIsNotNone(info)
            self.assertEqual(info["run_id"], "new-run")
            store.release_run_lock("new-run")
            self.assertFalse(store.run_lock_path.exists())


if __name__ == "__main__":
    unittest.main()
