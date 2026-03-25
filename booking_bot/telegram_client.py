from __future__ import annotations

import json
import logging
import mimetypes
from pathlib import Path
import re
import time
from typing import Callable
from urllib import parse, request


LOGGER = logging.getLogger(__name__)
OTP_REMINDER_INTERVAL_SEC = 60 * 60


class OtpWaitCancelledError(RuntimeError):
    pass


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str | None,
        chat_id: str | None,
        otp_reminder_interval_sec: int = OTP_REMINDER_INTERVAL_SEC,
        *,
        proxy_enabled: bool = False,
        proxy_url: str | None = None,
        transport_issue_reporter: Callable[[str, str], None] | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._update_offset: int | None = None
        self.otp_reminder_interval_sec = max(1, int(otp_reminder_interval_sec))
        self.proxy_enabled = bool(proxy_enabled and proxy_url)
        self.proxy_url = proxy_url.strip() if proxy_url else None
        self.transport_issue_reporter = transport_issue_reporter
        self._proxy_opener = self._build_proxy_opener()

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send(
        self,
        message: str,
        reply_markup: dict[str, object] | None = None,
        *,
        critical: bool = False,
    ) -> bool:
        if not self.enabled:
            LOGGER.debug("Telegram disabled because TELEGRAM_BOT_TOKEN/CHAT_ID not set.")
            return False

        payload = {
            "chat_id": str(self.chat_id),
            "text": message,
            "disable_web_page_preview": "true",
        }
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

        for attempt in range(1, 4):
            try:
                self._api_call(
                    method="sendMessage",
                    payload=payload,
                    timeout_sec=20,
                )
                return True
            except Exception:
                if attempt >= 3:
                    LOGGER.exception("Failed to send Telegram notification.")
                    self._report_transport_issue(
                        "Telegram delivery failed",
                        "Telegram sendMessage failed after retries.\n\n"
                        f"Message:\n{message}",
                    )
                    return False
                LOGGER.warning(
                    "Telegram sendMessage failed on attempt %s/3, retrying.",
                    attempt,
                    exc_info=True,
                )
                time.sleep(attempt)
        return False

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
        keyboard = [[{"text": str(item)} for item in row] for row in rows if row]
        return self.send(
            message,
            reply_markup={
                "keyboard": keyboard,
                "resize_keyboard": bool(resize_keyboard),
                "one_time_keyboard": bool(one_time_keyboard),
                "selective": bool(selective),
            },
            critical=critical,
        )

    def send_remove_keyboard(self, message: str, *, critical: bool = False) -> bool:
        return self.send(
            message,
            reply_markup={
                "remove_keyboard": True,
            },
            critical=critical,
        )

    def wait_for_otp_code(
        self,
        timeout_sec: int,
        poll_timeout_sec: int = 25,
        *,
        context_message: str | None = None,
    ) -> str | None:
        if not self.enabled:
            LOGGER.warning(
                "OTP requested via Telegram, but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are missing."
            )
            return None

        timeout_sec = max(1, int(timeout_sec))
        poll_timeout_sec = max(1, int(poll_timeout_sec))
        self._prime_update_offset()

        message = (
            "[workplace-booking] OTP code required.\n"
            "Reply in this chat with 6 digits in one message.\n"
            "Send /cancelotp to cancel the current run."
        )
        if context_message:
            message = f"{message}\n{context_message}"
        self.send(message, critical=True)

        deadline = time.monotonic() + timeout_sec
        next_reminder_at = time.monotonic() + self.otp_reminder_interval_sec
        consecutive_poll_failures = 0
        polling_alert_sent = False
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_reminder_at:
                remaining_sec = max(0, int(deadline - now))
                remaining_min = max(1, remaining_sec // 60) if remaining_sec else 0
                reminder = (
                    "[workplace-booking] OTP code is still required.\n"
                    "Reply in this chat with 6 digits to continue the booking run.\n"
                    "Send /cancelotp to cancel the current run.\n"
                    f"Remaining wait time: ~{remaining_min} min."
                )
                if context_message:
                    reminder = f"{reminder}\n{context_message}"
                self.send(reminder)
                while next_reminder_at <= now:
                    next_reminder_at += self.otp_reminder_interval_sec

            remaining = max(1, int(deadline - time.monotonic()))
            timeout = min(poll_timeout_sec, remaining)
            try:
                updates = self._get_updates(timeout=timeout)
                consecutive_poll_failures = 0
            except Exception:
                consecutive_poll_failures += 1
                LOGGER.warning(
                    "Telegram getUpdates failed during OTP wait; retrying.",
                    exc_info=True,
                )
                if consecutive_poll_failures >= 3 and not polling_alert_sent:
                    self._report_transport_issue(
                        "Telegram polling failed repeatedly",
                        "Telegram getUpdates failed repeatedly while waiting for OTP.",
                    )
                    polling_alert_sent = True
                time.sleep(min(2, remaining))
                continue
            for update in updates:
                message = update.get("message") or update.get("edited_message")
                if not isinstance(message, dict):
                    continue
                if not self._is_target_chat(message):
                    continue
                text = message.get("text")
                if not isinstance(text, str):
                    continue
                if text.strip().lower() == "/cancelotp":
                    self.send("[workplace-booking] OTP wait cancelled. Current run will stop.")
                    raise OtpWaitCancelledError("OTP wait cancelled from Telegram.")
                code = self._extract_six_digit_code(text)
                if code:
                    self.send("[workplace-booking] OTP code received. Continuing login.")
                    return code

        self.send(
            "[workplace-booking] OTP code was not received before timeout. "
            "Current run will fail.",
            critical=True,
        )
        return None

    def poll_text_messages(self, timeout_sec: int = 10) -> list[dict[str, object]]:
        if not self.enabled:
            return []
        timeout_sec = max(0, int(timeout_sec))
        self._prime_update_offset()
        try:
            updates = self._get_updates(timeout=timeout_sec)
        except Exception as exc:
            self._report_transport_issue(
                "Telegram polling failed",
                "Telegram getUpdates raised while polling service commands.\n\n"
                f"Error: {exc.__class__.__name__}: {exc}",
            )
            raise
        out: list[dict[str, object]] = []
        for update in updates:
            message = update.get("message") or update.get("edited_message")
            if not isinstance(message, dict):
                continue
            if not self._is_target_chat(message):
                continue
            text = message.get("text")
            if not isinstance(text, str):
                continue
            out.append(
                {
                    "text": text.strip(),
                    "message_id": message.get("message_id"),
                    "date": message.get("date"),
                    "from_id": (
                        message.get("from", {}).get("id")
                        if isinstance(message.get("from"), dict)
                        else None
                    ),
                }
            )
        return out

    def send_document(
        self,
        path: Path,
        caption: str | None = None,
        *,
        critical: bool = False,
    ) -> bool:
        if not self.enabled:
            LOGGER.debug("Telegram disabled because TELEGRAM_BOT_TOKEN/CHAT_ID not set.")
            return False
        if not path.exists():
            LOGGER.warning("Telegram document path does not exist: %s", path)
            return False

        assert self.bot_token is not None
        assert self.chat_id is not None
        url = f"https://api.telegram.org/bot{self.bot_token}/sendDocument"

        boundary = f"----WorkplaceBooking{int(time.time() * 1000)}"
        data = self._encode_multipart_formdata(
            boundary=boundary,
            fields={
                "chat_id": str(self.chat_id),
                "caption": caption or "",
            },
            file_field_name="document",
            file_path=path,
        )
        for attempt in range(1, 4):
            req = request.Request(
                url=url,
                data=data,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
            try:
                with self._urlopen(req, timeout=40) as response:
                    raw = response.read().decode("utf-8")
                    if response.status != 200:
                        raise RuntimeError(f"Telegram API HTTP {response.status}: {raw}")
                    body = json.loads(raw)
                    if not body.get("ok"):
                        raise RuntimeError(f"Telegram API error: {raw}")
                    return True
            except Exception:
                if attempt >= 3:
                    LOGGER.exception("Failed to send Telegram document: %s", path)
                    self._report_transport_issue(
                        "Telegram document delivery failed",
                        "Telegram sendDocument failed after retries.\n\n"
                        f"Document: {path}\nCaption: {caption or ''}",
                    )
                    return False
                LOGGER.warning(
                    "Telegram sendDocument failed on attempt %s/3, retrying: %s",
                    attempt,
                    path,
                    exc_info=True,
                )
                time.sleep(attempt)
        return False

    def _extract_six_digit_code(self, text: str) -> str | None:
        direct = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
        if direct:
            return direct.group(1)

        digits = re.sub(r"\D", "", text)
        if len(digits) == 6:
            return digits
        return None

    def _is_target_chat(self, message: dict) -> bool:
        if not self.chat_id:
            return False
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return False
        chat_id = chat.get("id")
        return str(chat_id) == str(self.chat_id)

    def _prime_update_offset(self) -> None:
        if self._update_offset is not None:
            return

        updates = self._api_call(
            method="getUpdates",
            payload={
                "timeout": "0",
                "limit": "100",
                "allowed_updates": json.dumps(["message", "edited_message"]),
            },
            timeout_sec=20,
        )
        if not isinstance(updates, list) or not updates:
            self._update_offset = 0
            return

        ids = [
            int(item.get("update_id", 0))
            for item in updates
            if isinstance(item, dict)
        ]
        self._update_offset = (max(ids) + 1) if ids else 0

    def _get_updates(self, timeout: int) -> list[dict]:
        payload: dict[str, str] = {
            "timeout": str(timeout),
            "allowed_updates": json.dumps(["message", "edited_message"]),
        }
        if self._update_offset is not None:
            payload["offset"] = str(self._update_offset)

        updates = self._api_call(
            method="getUpdates",
            payload=payload,
            timeout_sec=max(30, timeout + 10),
        )
        if not isinstance(updates, list):
            return []

        for item in updates:
            if not isinstance(item, dict):
                continue
            update_id = item.get("update_id")
            if isinstance(update_id, int):
                self._update_offset = update_id + 1
        return updates

    def _api_call(self, method: str, payload: dict[str, str], timeout_sec: int) -> object:
        assert self.bot_token is not None
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        data = parse.urlencode(payload).encode("utf-8")
        req = request.Request(
            url=url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with self._urlopen(req, timeout=timeout_sec) as response:
            raw = response.read().decode("utf-8")
            if response.status != 200:
                raise RuntimeError(f"Telegram API HTTP {response.status}: {raw}")
            body = json.loads(raw)
            if not body.get("ok"):
                raise RuntimeError(f"Telegram API error: {raw}")
            return body.get("result")

    def _build_proxy_opener(self):
        if not self.proxy_enabled or not self.proxy_url:
            return None
        return request.build_opener(
            request.ProxyHandler(
                {
                    "http": self.proxy_url,
                    "https": self.proxy_url,
                }
            )
        )

    def _urlopen(self, req: request.Request, timeout: int):
        if self._proxy_opener is not None:
            return self._proxy_opener.open(req, timeout=timeout)
        return request.urlopen(req, timeout=timeout)

    def _report_transport_issue(self, subject: str, body: str) -> None:
        if self.transport_issue_reporter is None:
            return
        try:
            self.transport_issue_reporter(subject, body)
        except Exception:
            LOGGER.exception("Transport issue reporter failed.")

    def _encode_multipart_formdata(
        self,
        boundary: str,
        fields: dict[str, str],
        file_field_name: str,
        file_path: Path,
    ) -> bytes:
        lines: list[bytes] = []
        for name, value in fields.items():
            lines.append(f"--{boundary}".encode("utf-8"))
            lines.append(
                f'Content-Disposition: form-data; name="{name}"'.encode("utf-8")
            )
            lines.append(b"")
            lines.append(value.encode("utf-8"))

        mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        file_bytes = file_path.read_bytes()
        lines.append(f"--{boundary}".encode("utf-8"))
        lines.append(
            (
                f'Content-Disposition: form-data; name="{file_field_name}"; '
                f'filename="{file_path.name}"'
            ).encode("utf-8")
        )
        lines.append(f"Content-Type: {mime_type}".encode("utf-8"))
        lines.append(b"")
        lines.append(file_bytes)
        lines.append(f"--{boundary}--".encode("utf-8"))
        lines.append(b"")
        return b"\r\n".join(lines)
