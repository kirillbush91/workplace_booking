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

    def test_proxy_and_email_settings_are_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = self._base_env(Path(tmp))
            env.update(
                {
                    "TELEGRAM_PROXY_ENABLED": "true",
                    "TELEGRAM_PROXY_URL": "http://10.0.0.2:3128",
                    "EMAIL_FALLBACK_ENABLED": "true",
                    "EMAIL_SMTP_HOST": "smtp.example.com",
                    "EMAIL_SMTP_PORT": "2525",
                    "EMAIL_SMTP_USERNAME": "user",
                    "EMAIL_SMTP_PASSWORD": "pass",
                    "EMAIL_SMTP_FROM": "bot@example.com",
                    "EMAIL_SMTP_TO": "me@example.com",
                    "EMAIL_SMTP_STARTTLS": "false",
                    "HEALTHCHECK_ENABLED": "true",
                    "HEALTHCHECK_TIME_LOCAL": "21:00",
                }
            )
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_env()

        self.assertTrue(settings.telegram_proxy_enabled)
        self.assertEqual(settings.telegram_proxy_url, "http://10.0.0.2:3128")
        self.assertTrue(settings.email_fallback_enabled)
        self.assertEqual(settings.email_smtp_host, "smtp.example.com")
        self.assertEqual(settings.email_smtp_port, 2525)
        self.assertEqual(settings.email_smtp_username, "user")
        self.assertEqual(settings.email_smtp_password, "pass")
        self.assertEqual(settings.email_from, "bot@example.com")
        self.assertEqual(settings.email_to, "me@example.com")
        self.assertFalse(settings.email_smtp_starttls)
        self.assertTrue(settings.healthcheck_enabled)
        self.assertEqual(settings.healthcheck_time_local, "21:00")


if __name__ == "__main__":
    unittest.main()
