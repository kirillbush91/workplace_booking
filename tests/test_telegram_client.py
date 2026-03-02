from __future__ import annotations

import time
import unittest

from booking_bot.telegram_client import OtpWaitCancelledError, TelegramNotifier


class DummyTelegramNotifier(TelegramNotifier):
    def __init__(self, updates: list[list[dict]], *, otp_reminder_interval_sec: int = 1) -> None:
        super().__init__("token", "123", otp_reminder_interval_sec=otp_reminder_interval_sec)
        self._queued_updates = list(updates)
        self.sent_messages: list[str] = []

    def send(self, message: str, reply_markup: dict[str, object] | None = None) -> bool:
        self.sent_messages.append(message)
        return True

    def _prime_update_offset(self) -> None:
        self._update_offset = 0

    def _get_updates(self, timeout: int) -> list[dict]:
        time.sleep(min(timeout, 1))
        if self._queued_updates:
            return self._queued_updates.pop(0)
        return []


class TelegramNotifierTests(unittest.TestCase):
    def test_wait_for_otp_code_sends_reminder_and_timeout(self) -> None:
        notifier = DummyTelegramNotifier([], otp_reminder_interval_sec=1)
        code = notifier.wait_for_otp_code(timeout_sec=3, poll_timeout_sec=1)
        self.assertIsNone(code)
        joined = "\n".join(notifier.sent_messages)
        self.assertIn("OTP code required", joined)
        self.assertIn("OTP code is still required", joined)
        self.assertIn("OTP code was not received before timeout", joined)

    def test_wait_for_otp_code_can_be_cancelled(self) -> None:
        notifier = DummyTelegramNotifier(
            [
                [
                    {
                        "update_id": 1,
                        "message": {
                            "chat": {"id": "123"},
                            "text": "/cancelotp",
                        },
                    }
                ]
            ],
            otp_reminder_interval_sec=1,
        )
        with self.assertRaises(OtpWaitCancelledError):
            notifier.wait_for_otp_code(timeout_sec=3, poll_timeout_sec=1)
        joined = "\n".join(notifier.sent_messages)
        self.assertIn("OTP wait cancelled", joined)


if __name__ == "__main__":
    unittest.main()
