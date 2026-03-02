from __future__ import annotations

from datetime import date, datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from booking_bot.config import Settings
from booking_bot.run import (
    _build_schedule_preview,
    _compute_catchup_decision,
    _scheduled_target_date_for_run,
    _settings_for_manual_request,
)
from booking_bot.runtime_state import SchedulerState


class RunHelperTests(unittest.TestCase):
    def _settings(self) -> Settings:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = {
            "TARGET_OFFICE": "Central Office",
            "TARGET_SEAT": "17",
            "PREFERRED_SEATS": "17|19",
            "TARGET_TABLE_ID": "table-17",
            "STORAGE_STATE_PATH": str(Path(tmp.name) / "storage_state.json"),
            "SCREENSHOT_DIR": str(Path(tmp.name) / "screens"),
            "RUN_MODE": "service",
            "SCHEDULE_TIME_LOCAL": "00:01",
            "SCHEDULE_LOCAL_UTC_OFFSET": "+03:00",
            "BOOKING_DATE_OFFSET_DAYS": "7",
            "BOOKING_DATE_FORMAT": "%d.%m.%Y",
            "BOOKING_SKIP_WEEKENDS": "true",
            "AUTH_PREFLIGHT_ENABLED": "true",
            "AUTH_PREFLIGHT_TIME_LOCAL": "23:50",
            "SCHEDULE_CATCHUP_WINDOW_MINUTES": "360",
        }
        with patch.dict(os.environ, env, clear=True):
            return Settings.from_env()

    def test_scheduled_target_date_matches_2_mar_to_9_mar_example(self) -> None:
        settings = self._settings()
        run_at_utc = datetime(2026, 3, 1, 21, 1, tzinfo=timezone.utc)
        target = _scheduled_target_date_for_run(settings, run_at_utc)
        self.assertEqual(target.strftime("%d.%m.%Y"), "09.03.2026")

    def test_schedule_preview_marks_weekends(self) -> None:
        settings = self._settings()
        start_run_utc = datetime(2026, 2, 26, 21, 1, tzinfo=timezone.utc)
        preview = _build_schedule_preview(settings, start_run_utc, count=4)
        self.assertIn("06.03.2026 (Fri)", preview)
        self.assertIn("07.03.2026 (Sat) [weekend skipped]", preview)
        self.assertIn("08.03.2026 (Sun) [weekend skipped]", preview)
        self.assertIn("09.03.2026 (Mon)", preview)

    def test_catchup_skips_first_startup_without_scheduler_history(self) -> None:
        settings = self._settings()
        now_utc = datetime(2026, 3, 2, 0, 30, tzinfo=timezone.utc)
        decision = _compute_catchup_decision(settings, SchedulerState(), now_utc)
        self.assertEqual(decision.state, "not_needed")

    def test_manual_request_disables_fallback_for_explicit_seat(self) -> None:
        settings = self._settings()
        target = date(2026, 3, 5)
        manual = _settings_for_manual_request(settings, target, target_seat="19")
        self.assertEqual(manual.preferred_seats, ["19"])
        self.assertEqual(manual.target_seat, "19")
        self.assertIsNone(manual.target_table_id)


if __name__ == "__main__":
    unittest.main()
