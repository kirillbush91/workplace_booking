from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
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
    summary = (
        "New bookings created"
        if result.booked_dates
        else "No new bookings (all target dates skipped)"
    )
    day_lines = []
    for day in result.day_results:
        icon = {"booked": "[OK]", "skipped": "[SKIP]", "failed": "[FAIL]"}.get(
            day.status,
            "[INFO]",
        )
        day_lines.append(f"{icon} {day.date} ({day.status}): {day.message}")
    days_block = "\n".join(day_lines) if day_lines else "n/a"
    return (
        "[workplace-booking] Booking run finished\n"
        f"Attempt: {attempt}\n"
        f"Summary: {summary}\n"
        f"Office: {result.office}\n"
        f"Seat: {result.seat}\n"
        f"Booked: {len(result.booked_dates)}\n"
        f"Skipped: {len(result.skipped_dates)}\n"
        f"Failed: {len(result.failed_dates)}\n"
        f"Duration: {duration:.1f}s\n"
        f"Finished UTC: {result.finished_at.isoformat()}\n"
        f"Screenshot: {screenshot}\n"
        "Per-day:\n"
        f"{days_block}"
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


def _send_result_screenshot(notifier: TelegramNotifier, screenshot_path: Path | None) -> None:
    if not screenshot_path:
        return
    notifier.send_document(
        screenshot_path,
        caption="[workplace-booking] Screenshot",
    )


async def run_once(settings: Settings, notifier: TelegramNotifier) -> int:
    for attempt in range(1, settings.retry_attempts + 1):
        notifier.send(_build_start_message(settings, attempt))
        try:
            result = await BookingBot(settings, notifier=notifier).book()
            if result.booked_dates:
                LOGGER.info("Booking created successfully on attempt %s.", attempt)
            else:
                LOGGER.info(
                    "Booking run completed on attempt %s: no new bookings were needed.",
                    attempt,
                )
            notifier.send(_build_success_message(result, attempt))
            _send_result_screenshot(notifier, result.screenshot_path)
            return 0
        except Exception as exc:
            LOGGER.exception("Booking attempt %s failed.", attempt)
            notifier.send(_build_error_message(exc, attempt))
            if isinstance(exc, BookingError):
                _send_result_screenshot(notifier, exc.screenshot_path)
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
