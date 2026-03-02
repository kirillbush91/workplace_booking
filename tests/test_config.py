from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from booking_bot.config import Settings


class SettingsEnvTests(unittest.TestCase):
    def _base_env(self, state_dir: Path) -> dict[str, str]:
        return {
            "TARGET_OFFICE": "Central Office",
            "TARGET_SEAT": "17",
            "STORAGE_STATE_PATH": str(state_dir / "storage_state.json"),
            "SCREENSHOT_DIR": str(state_dir / "screens"),
        }

    def test_preferred_seats_and_table_ids_support_legacy_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = self._base_env(Path(tmp))
            env.update(
                {
                    "PREFERRED_SEATS": "17|19",
                    "PREFERRED_SEAT_TABLE_IDS": "19:table-19",
                    "TARGET_TABLE_ID": "table-17",
                }
            )
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_env()

        self.assertEqual(settings.preferred_seats, ["17", "19"])
        self.assertEqual(
            settings.preferred_seat_table_ids,
            {"17": "table-17", "19": "table-19"},
        )

    def test_preferred_seats_default_to_target_seat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = self._base_env(Path(tmp))
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_env()

        self.assertEqual(settings.preferred_seats, ["17"])
        self.assertEqual(settings.preferred_seat_table_ids, {})


if __name__ == "__main__":
    unittest.main()
