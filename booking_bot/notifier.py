from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
import logging
import smtplib
from typing import Protocol


LOGGER = logging.getLogger(__name__)


class InteractiveNotifier(Protocol):
    @property
    def enabled(self) -> bool:
        ...

    def send(
        self,
        message: str,
        reply_markup: dict[str, object] | None = None,
        *,
        critical: bool = False,
    ) -> bool:
        ...

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
        ...

    def send_remove_keyboard(
        self,
        message: str,
        *,
        critical: bool = False,
    ) -> bool:
        ...

    def wait_for_otp_code(
        self,
        timeout_sec: int,
        poll_timeout_sec: int = 25,
        *,
        context_message: str | None = None,
    ) -> str | None:
        ...

    def poll_text_messages(self, timeout_sec: int = 10) -> list[dict[str, object]]:
        ...

    def send_document(
        self,
        path,
        caption: str | None = None,
        *,
        critical: bool = False,
    ) -> bool:
        ...


@dataclass(frozen=True)
class EmailSettings:
    smtp_host: str | None
    smtp_port: int
    smtp_username: str | None
    smtp_password: str | None
    email_from: str | None
    email_to: str | None
    starttls: bool


class EmailNotifier:
    def __init__(self, settings: EmailSettings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.smtp_host
            and self.settings.smtp_port
            and self.settings.email_from
            and self.settings.email_to
        )

    def send_message(self, subject: str, body: str) -> bool:
        if not self.enabled:
            LOGGER.debug("Email fallback disabled because SMTP settings are incomplete.")
            return False

        assert self.settings.smtp_host is not None
        assert self.settings.email_from is not None
        assert self.settings.email_to is not None

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.settings.email_from
        message["To"] = self.settings.email_to
        message.set_content(body)

        try:
            with smtplib.SMTP(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=20,
            ) as smtp:
                smtp.ehlo()
                if self.settings.starttls:
                    smtp.starttls()
                    smtp.ehlo()
                if self.settings.smtp_username and self.settings.smtp_password:
                    smtp.login(
                        self.settings.smtp_username,
                        self.settings.smtp_password,
                    )
                smtp.send_message(message)
            return True
        except Exception:
            LOGGER.exception("Failed to send email fallback notification.")
            return False


class FallbackNotifier:
    def __init__(
        self,
        primary: InteractiveNotifier,
        email: EmailNotifier | None = None,
        *,
        email_fallback_enabled: bool = False,
        transport_alert_threshold: int = 3,
    ) -> None:
        self.primary = primary
        self.email = email
        self.email_fallback_enabled = bool(email_fallback_enabled)
        self.transport_alert_threshold = max(1, int(transport_alert_threshold))
        self._poll_failure_count = 0

    @property
    def enabled(self) -> bool:
        return bool(self.primary.enabled)

    def send(
        self,
        message: str,
        reply_markup: dict[str, object] | None = None,
        *,
        critical: bool = False,
    ) -> bool:
        primary_ok = self.primary.send(
            message,
            reply_markup=reply_markup,
            critical=critical,
        )
        if critical:
            self._send_email_copy(message)
        if self.primary.enabled and not primary_ok:
            self._send_transport_alert(
                "Telegram delivery failed",
                "Telegram sendMessage failed after retries.\n\n"
                f"Original message:\n{message}",
            )
        return primary_ok

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
        primary_ok = self.primary.send_reply_keyboard(
            message,
            rows,
            resize_keyboard=resize_keyboard,
            one_time_keyboard=one_time_keyboard,
            selective=selective,
            critical=critical,
        )
        if critical:
            self._send_email_copy(message)
        if self.primary.enabled and not primary_ok:
            self._send_transport_alert(
                "Telegram delivery failed",
                "Telegram reply keyboard delivery failed after retries.\n\n"
                f"Original message:\n{message}",
            )
        return primary_ok

    def send_remove_keyboard(
        self,
        message: str,
        *,
        critical: bool = False,
    ) -> bool:
        primary_ok = self.primary.send_remove_keyboard(message, critical=critical)
        if critical:
            self._send_email_copy(message)
        if self.primary.enabled and not primary_ok:
            self._send_transport_alert(
                "Telegram delivery failed",
                "Telegram removeKeyboard delivery failed after retries.\n\n"
                f"Original message:\n{message}",
            )
        return primary_ok

    def wait_for_otp_code(
        self,
        timeout_sec: int,
        poll_timeout_sec: int = 25,
        *,
        context_message: str | None = None,
    ) -> str | None:
        if self.email_fallback_enabled and self.email is not None and self.email.enabled:
            message = (
                "[workplace-booking] OTP code required.\n"
                "Reply in Telegram with the 6-digit code to continue the current run."
            )
            if context_message:
                message = f"{message}\n{context_message}"
            self.email.send_message(
                "[workplace-booking] OTP code required",
                self._email_body(message),
            )

        code = self.primary.wait_for_otp_code(
            timeout_sec,
            poll_timeout_sec=poll_timeout_sec,
            context_message=context_message,
        )
        if code is None and self.email_fallback_enabled and self.email is not None and self.email.enabled:
            self.email.send_message(
                "[workplace-booking] OTP code timeout",
                self._email_body(
                    "[workplace-booking] OTP code was not received before timeout. "
                    "The current run will fail."
                ),
            )
        return code

    def poll_text_messages(self, timeout_sec: int = 10) -> list[dict[str, object]]:
        try:
            messages = self.primary.poll_text_messages(timeout_sec=timeout_sec)
        except Exception as exc:
            self._poll_failure_count += 1
            if self._poll_failure_count >= self.transport_alert_threshold:
                self._send_transport_alert(
                    "Telegram polling failed repeatedly",
                    "Telegram getUpdates failed repeatedly while polling for service "
                    f"commands.\n\nLast error: {exc.__class__.__name__}: {exc}",
                )
            raise
        self._poll_failure_count = 0
        return messages

    def send_document(
        self,
        path,
        caption: str | None = None,
        *,
        critical: bool = False,
    ) -> bool:
        primary_ok = self.primary.send_document(path, caption=caption, critical=critical)
        if self.primary.enabled and not primary_ok:
            self._send_transport_alert(
                "Telegram document delivery failed",
                "Telegram sendDocument failed after retries.\n\n"
                f"Document: {path}\nCaption: {caption or ''}",
            )
        return primary_ok

    def report_transport_issue(self, subject: str, body: str) -> None:
        self._send_transport_alert(subject, body)

    def _send_email_copy(self, message: str) -> None:
        if not self.email_fallback_enabled or self.email is None or not self.email.enabled:
            return
        self.email.send_message(
            self._subject_for_message(message),
            self._email_body(message),
        )

    def _send_transport_alert(self, subject: str, body: str) -> None:
        if not self.email_fallback_enabled or self.email is None or not self.email.enabled:
            return
        self.email.send_message(
            f"[workplace-booking] {subject}",
            self._email_body(body),
        )

    @staticmethod
    def _subject_for_message(message: str) -> str:
        first_line = (message or "").splitlines()[0].strip() if message else ""
        if not first_line:
            return "[workplace-booking] Critical notification"
        return first_line[:160]

    @staticmethod
    def _email_body(message: str) -> str:
        timestamp = datetime.now(timezone.utc).isoformat()
        return f"{message}\n\nUTC: {timestamp}\n"
