from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from booking_bot.telegram_client import OtpWaitCancelledError, TelegramNotifier


class DummyTelegramNotifier(TelegramNotifier):
    def __init__(self, updates: list[list[dict]], *, otp_reminder_interval_sec: int = 1) -> None:
        super().__init__("token", "123", otp_reminder_interval_sec=otp_reminder_interval_sec)
        self._queued_updates = list(updates)
        self.sent_messages: list[str] = []

    def send(
        self,
        message: str,
        reply_markup: dict[str, object] | None = None,
        *,
        critical: bool = False,
    ) -> bool:
        self.sent_messages.append(message)
        return True

    def _prime_update_offset(self) -> None:
        self._update_offset = 0

    def _get_updates(self, timeout: int) -> list[dict]:
        time.sleep(min(timeout, 1))
        if self._queued_updates:
            return self._queued_updates.pop(0)
        return []


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self.status = 200
        self._raw = json.dumps({"ok": True, "result": payload}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self) -> bytes:
        return self._raw


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

    def test_send_uses_proxy_opener_when_enabled(self) -> None:
        notifier = TelegramNotifier(
            "token",
            "123",
            proxy_enabled=True,
            proxy_url="http://10.0.0.2:3128",
        )
        opener = Mock()
        opener.open.return_value = _FakeResponse(True)
        notifier._proxy_opener = opener

        sent = notifier.send("hello")

        self.assertTrue(sent)
        opener.open.assert_called()

    def test_poll_text_messages_uses_proxy_opener_when_enabled(self) -> None:
        notifier = TelegramNotifier(
            "token",
            "123",
            proxy_enabled=True,
            proxy_url="http://10.0.0.2:3128",
        )
        opener = Mock()
        opener.open.side_effect = [
            _FakeResponse([]),
            _FakeResponse(
                [
                    {
                        "update_id": 1,
                        "message": {
                            "chat": {"id": "123"},
                            "text": "/status",
                            "message_id": 7,
                            "date": 111,
                        },
                    }
                ]
            ),
        ]
        notifier._proxy_opener = opener

        updates = notifier.poll_text_messages(timeout_sec=0)

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["text"], "/status")
        self.assertEqual(opener.open.call_count, 2)

    def test_send_document_uses_proxy_opener_when_enabled(self) -> None:
        notifier = TelegramNotifier(
            "token",
            "123",
            proxy_enabled=True,
            proxy_url="http://10.0.0.2:3128",
        )
        opener = Mock()
        opener.open.return_value = _FakeResponse(True)
        notifier._proxy_opener = opener

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shot.txt"
            path.write_text("ok", encoding="utf-8")
            sent = notifier.send_document(path, caption="cap")

        self.assertTrue(sent)
        opener.open.assert_called()


if __name__ == "__main__":
    unittest.main()
