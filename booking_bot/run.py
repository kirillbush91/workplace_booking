from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import traceback

from dotenv import load_dotenv

from .booking import BookingBot, BookingError, BookingResult
from .config import Settings
from .telegram_client import TelegramNotifier


LOGGER = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _build_success_message(result: BookingResult, attempt: int) -> str:
    duration = (result.finished_at - result.started_at).total_seconds()
    screenshot = str(result.screenshot_path) if result.screenshot_path else "n/a"
    return (
        "[workplace-booking] Booking success\n"
        f"Attempt: {attempt}\n"
        f"Office: {result.office}\n"
        f"Seat: {result.seat}\n"
        f"Duration: {duration:.1f}s\n"
        f"Finished UTC: {result.finished_at.isoformat()}\n"
        f"Screenshot: {screenshot}"
    )


def _build_start_message(settings: Settings, attempt: int) -> str:
    return (
        "[workplace-booking] Booking started\n"
        f"Attempt: {attempt}/{settings.retry_attempts}\n"
        f"Office: {settings.target_office}\n"
        f"Seat: {settings.target_seat}\n"
        f"UTC: {datetime.now(timezone.utc).isoformat()}"
    )


def _build_error_message(exc: Exception, attempt: int) -> str:
    screenshot = "n/a"
    if isinstance(exc, BookingError) and exc.screenshot_path is not None:
        screenshot = str(exc.screenshot_path)
    stack = traceback.format_exception_only(exc.__class__, exc)[-1].strip()
    return (
        "[workplace-booking] Booking failed\n"
        f"Attempt: {attempt}\n"
        f"Error: {stack}\n"
        f"Screenshot: {screenshot}\n"
        f"UTC: {datetime.now(timezone.utc).isoformat()}"
    )


async def run_once(settings: Settings, notifier: TelegramNotifier) -> int:
    for attempt in range(1, settings.retry_attempts + 1):
        notifier.send(_build_start_message(settings, attempt))
        try:
            result = await BookingBot(settings, notifier=notifier).book()
            LOGGER.info("Booking created successfully on attempt %s.", attempt)
            notifier.send(_build_success_message(result, attempt))
            return 0
        except Exception as exc:
            LOGGER.exception("Booking attempt %s failed.", attempt)
            notifier.send(_build_error_message(exc, attempt))
            if attempt >= settings.retry_attempts:
                return 1
            notifier.send(
                "[workplace-booking] Retrying after failure\n"
                f"Next attempt in {settings.retry_delay_sec} sec."
            )
            await asyncio.sleep(settings.retry_delay_sec)
    return 1


async def run_daemon(settings: Settings, notifier: TelegramNotifier) -> int:
    LOGGER.info(
        "Running in daemon mode, interval %s minute(s).",
        settings.run_interval_minutes,
    )
    while True:
        await run_once(settings, notifier)
        sleep_seconds = settings.run_interval_minutes * 60
        LOGGER.info("Sleeping for %s seconds before next run.", sleep_seconds)
        await asyncio.sleep(sleep_seconds)


def main() -> int:
    load_dotenv()
    settings = Settings.from_env()
    _configure_logging(settings.log_level)

    notifier = TelegramNotifier(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
    )
    LOGGER.info("Telegram notifications enabled: %s", notifier.enabled)

    if settings.run_mode == "daemon":
        return asyncio.run(run_daemon(settings, notifier))
    return asyncio.run(run_once(settings, notifier))


if __name__ == "__main__":
    raise SystemExit(main())
