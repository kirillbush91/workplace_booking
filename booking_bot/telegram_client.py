from __future__ import annotations

import json
import logging
import re
import time
from urllib import parse, request


LOGGER = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot_token: str | None, chat_id: str | None) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._update_offset: int | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send(self, message: str) -> bool:
        if not self.enabled:
            LOGGER.debug("Telegram disabled because TELEGRAM_BOT_TOKEN/CHAT_ID not set.")
            return False

        try:
            self._api_call(
                method="sendMessage",
                payload={
                    "chat_id": str(self.chat_id),
                    "text": message,
                    "disable_web_page_preview": "true",
                },
                timeout_sec=20,
            )
            return True
        except Exception:
            LOGGER.exception("Failed to send Telegram notification.")
            return False

    def wait_for_otp_code(self, timeout_sec: int, poll_timeout_sec: int = 25) -> str | None:
        if not self.enabled:
            LOGGER.warning(
                "OTP requested via Telegram, but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are missing."
            )
            return None

        timeout_sec = max(1, int(timeout_sec))
        poll_timeout_sec = max(1, int(poll_timeout_sec))
        self._prime_update_offset()

        self.send(
            "[workplace-booking] OTP code required.\n"
            "Reply in this chat with 6 digits in one message."
        )

        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            remaining = max(1, int(deadline - time.monotonic()))
            timeout = min(poll_timeout_sec, remaining)
            updates = self._get_updates(timeout=timeout)
            for update in updates:
                message = update.get("message") or update.get("edited_message")
                if not isinstance(message, dict):
                    continue
                if not self._is_target_chat(message):
                    continue
                text = message.get("text")
                if not isinstance(text, str):
                    continue
                code = self._extract_six_digit_code(text)
                if code:
                    self.send("[workplace-booking] OTP code received. Continuing login.")
                    return code

        self.send(
            "[workplace-booking] OTP code was not received before timeout. "
            "Current run will fail."
        )
        return None

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
        with request.urlopen(req, timeout=timeout_sec) as response:
            raw = response.read().decode("utf-8")
            if response.status != 200:
                raise RuntimeError(f"Telegram API HTTP {response.status}: {raw}")
            body = json.loads(raw)
            if not body.get("ok"):
                raise RuntimeError(f"Telegram API error: {raw}")
            return body.get("result")
