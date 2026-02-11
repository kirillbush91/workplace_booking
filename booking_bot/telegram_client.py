from __future__ import annotations

import json
import logging
from urllib import parse, request


LOGGER = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot_token: str | None, chat_id: str | None) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send(self, message: str) -> None:
        if not self.enabled:
            LOGGER.debug("Telegram disabled because TELEGRAM_BOT_TOKEN/CHAT_ID not set.")
            return

        assert self.bot_token is not None
        assert self.chat_id is not None
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "disable_web_page_preview": "true",
        }
        data = parse.urlencode(payload).encode("utf-8")
        req = request.Request(
            url=url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=20) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"Telegram API HTTP {response.status}: {response.read().decode()}"
                    )
                raw = response.read().decode("utf-8")
                body = json.loads(raw)
                if not body.get("ok"):
                    raise RuntimeError(f"Telegram API error: {raw}")
        except Exception:
            LOGGER.exception("Failed to send Telegram notification.")

