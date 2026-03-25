from __future__ import annotations

import unittest

from booking_bot.notifier import FallbackNotifier


class FakePrimaryNotifier:
    def __init__(self) -> None:
        self.enabled = True
        self.sent_messages: list[str] = []
        self.reply_messages: list[str] = []
        self.documents: list[str] = []
        self.otp_code: str | None = "123456"
        self.poll_exception: Exception | None = None

    def send(
        self,
        message: str,
        reply_markup: dict[str, object] | None = None,
        *,
        critical: bool = False,
    ) -> bool:
        self.sent_messages.append(message)
        return True

    def send_reply_keyboard(
        self,
        message: str,
        rows: list[list[str]],
        *,
        resize_keyboard: bool = True,
        one_time_keyboard: bool = False,
        selective: bool = False,
        critical: bool = False,
    ) -> bool:
        self.reply_messages.append(message)
        return True

    def send_remove_keyboard(self, message: str, *, critical: bool = False) -> bool:
        self.reply_messages.append(message)
        return True

    def wait_for_otp_code(
        self,
        timeout_sec: int,
        poll_timeout_sec: int = 25,
        *,
        context_message: str | None = None,
    ) -> str | None:
        return self.otp_code

    def poll_text_messages(self, timeout_sec: int = 10) -> list[dict[str, object]]:
        if self.poll_exception is not None:
            raise self.poll_exception
        return [{"text": "/ping"}]

    def send_document(self, path, caption: str | None = None, *, critical: bool = False) -> bool:
        self.documents.append(str(path))
        return True


class FakeEmailNotifier:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.messages: list[tuple[str, str]] = []

    def send_message(self, subject: str, body: str) -> bool:
        self.messages.append((subject, body))
        return True


class FallbackNotifierTests(unittest.TestCase):
    def test_critical_message_is_duplicated_to_email(self) -> None:
        primary = FakePrimaryNotifier()
        email = FakeEmailNotifier()
        notifier = FallbackNotifier(primary, email, email_fallback_enabled=True)

        notifier.send("[workplace-booking] Booking run finished", critical=True)

        self.assertEqual(len(email.messages), 1)
        self.assertIn("Booking run finished", email.messages[0][0])

    def test_noncritical_message_is_not_sent_to_email(self) -> None:
        primary = FakePrimaryNotifier()
        email = FakeEmailNotifier()
        notifier = FallbackNotifier(primary, email, email_fallback_enabled=True)

        notifier.send("[workplace-booking] Status")

        self.assertEqual(email.messages, [])

    def test_otp_wait_sends_email_for_required_and_timeout(self) -> None:
        primary = FakePrimaryNotifier()
        primary.otp_code = None
        email = FakeEmailNotifier()
        notifier = FallbackNotifier(primary, email, email_fallback_enabled=True)

        code = notifier.wait_for_otp_code(30, context_message="Office: office")

        self.assertIsNone(code)
        self.assertEqual(len(email.messages), 2)
        self.assertIn("OTP code required", email.messages[0][0])
        self.assertIn("OTP code timeout", email.messages[1][0])

    def test_repeated_poll_failures_trigger_transport_alert(self) -> None:
        primary = FakePrimaryNotifier()
        primary.poll_exception = RuntimeError("boom")
        email = FakeEmailNotifier()
        notifier = FallbackNotifier(
            primary,
            email,
            email_fallback_enabled=True,
            transport_alert_threshold=3,
        )

        for _ in range(3):
            with self.assertRaises(RuntimeError):
                notifier.poll_text_messages(timeout_sec=1)

        self.assertEqual(len(email.messages), 1)
        self.assertIn("Telegram polling failed repeatedly", email.messages[0][0])

    def test_disabled_primary_does_not_emit_transport_alert_for_normal_send(self) -> None:
        primary = FakePrimaryNotifier()
        primary.enabled = False
        email = FakeEmailNotifier()
        notifier = FallbackNotifier(primary, email, email_fallback_enabled=True)

        notifier.send("[workplace-booking] status")

        self.assertEqual(email.messages, [])


if __name__ == "__main__":
    unittest.main()
