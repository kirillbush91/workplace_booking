from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import date, datetime, time as dt_time, timedelta, timezone
import logging
from pathlib import Path
import re
import traceback

from dotenv import load_dotenv

from .booking import BookingBot, BookingError, BookingResult
from .config import Settings
from .telegram_client import TelegramNotifier


LOGGER = logging.getLogger(__name__)


BTN_MENU = "📋 Меню"
BTN_STATUS = "📊 Статус"
BTN_RUN_NEXT = "🚀 Забронировать +7"
BTN_PICK_DATE = "📅 Выбрать дату"
BTN_PICK_SEAT = "💺 Выбрать место"
BTN_RUN_SELECTED = "✅ Запустить выбранное"
BTN_RESET_SELECTIONS = "♻️ Сбросить выбор"
BTN_BACK = "↩️ Назад"
BTN_CANCEL = "❌ Отмена"
BTN_ENTER_SEAT = "⌨️ Ввести номер места"


@dataclass
class ServiceUiState:
    selected_date: date | None = None
    selected_seat: str | None = None
    pending_input: str | None = None


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


def _parse_utc_offset(value: str) -> timezone:
    raw = (value or "").strip()
    match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", raw)
    if not match:
        raise ValueError(f"Invalid UTC offset '{value}', expected +HH:MM or -HH:MM")
    sign = 1 if match.group(1) == "+" else -1
    hours = int(match.group(2))
    minutes = int(match.group(3))
    if hours > 23 or minutes > 59:
        raise ValueError(f"Invalid UTC offset '{value}', expected +HH:MM or -HH:MM")
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def _parse_hhmm(value: str) -> dt_time:
    raw = (value or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not match:
        raise ValueError(f"Invalid time '{value}', expected HH:MM")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid time '{value}', expected HH:MM")
    return dt_time(hour=hour, minute=minute)


def _next_scheduled_run_utc(settings: Settings, now_utc: datetime | None = None) -> datetime:
    now_utc = now_utc or datetime.now(timezone.utc)
    local_tz = _parse_utc_offset(settings.schedule_local_utc_offset)
    local_now = now_utc.astimezone(local_tz)
    schedule_time = _parse_hhmm(settings.schedule_time_local)
    local_candidate = local_now.replace(
        hour=schedule_time.hour,
        minute=schedule_time.minute,
        second=0,
        microsecond=0,
    )
    if local_candidate <= local_now:
        local_candidate += timedelta(days=1)
    return local_candidate.astimezone(timezone.utc)


def _format_local_dt(dt_utc: datetime, offset_raw: str) -> str:
    local_tz = _parse_utc_offset(offset_raw)
    local_dt = dt_utc.astimezone(local_tz)
    return f"{local_dt.strftime('%d.%m.%Y %H:%M')} ({offset_raw})"


def _scheduled_target_date(settings: Settings, now_utc: datetime | None = None) -> date:
    now_utc = now_utc or datetime.now(timezone.utc)
    local_tz = _parse_utc_offset(settings.schedule_local_utc_offset)
    local_today = now_utc.astimezone(local_tz).date()
    offset_days = settings.booking_date_offset_days
    if offset_days is None:
        offset_days = 7
    return local_today + timedelta(days=offset_days)


def _scheduled_target_date_for_run(settings: Settings, run_at_utc: datetime) -> date:
    # Service mode runs after local midnight (e.g. 00:01), so the target date must be
    # calculated from the local date of that scheduled run, not from "now".
    return _scheduled_target_date(settings, now_utc=run_at_utc)


def _settings_for_single_date(settings: Settings, target: date) -> Settings:
    formatted = target.strftime(settings.booking_date_format)
    return replace(
        settings,
        booking_date_values=[formatted],
        booking_date_value=None,
        booking_date_offset_days=None,
        booking_range_days=0,
    )


def _settings_for_manual_request(
    settings: Settings,
    target: date,
    target_seat: str | None = None,
) -> Settings:
    out = _settings_for_single_date(settings, target)
    if target_seat is None:
        return out
    seat = str(target_seat).strip()
    if not seat:
        return out
    if seat == out.target_seat:
        return out
    # Exact table UUID is seat-specific. When seat is changed interactively, resolve by UI/API
    # markers again instead of reusing the fixed table id for the default seat.
    return replace(out, target_seat=seat, target_table_id=None)


def _menu_keyboard_rows() -> list[list[str]]:
    return [
        [BTN_RUN_NEXT, BTN_STATUS],
        [BTN_PICK_DATE, BTN_PICK_SEAT],
        [BTN_RUN_SELECTED, BTN_RESET_SELECTIONS],
        ["/help", "/ping"],
    ]


def _date_keyboard_rows(settings: Settings, days: int = 14) -> list[list[str]]:
    local_tz = _parse_utc_offset(settings.schedule_local_utc_offset)
    base = datetime.now(timezone.utc).astimezone(local_tz).date()
    labels = [
        (base + timedelta(days=offset)).strftime(settings.booking_date_format)
        for offset in range(0, max(1, days))
    ]
    rows: list[list[str]] = []
    row: list[str] = []
    for label in labels:
        row.append(label)
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([BTN_BACK, BTN_CANCEL])
    return rows


def _seat_keyboard_rows(settings: Settings) -> list[list[str]]:
    current = settings.target_seat.strip()
    rows: list[list[str]] = []
    if current.isdigit():
        center = int(current)
        values = []
        for seat in range(max(1, center - 8), center + 9):
            values.append(f"💺 {seat}")
        row: list[str] = []
        for value in values:
            row.append(value)
            if len(row) == 4:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
    else:
        rows.append([f"💺 {current}"])
    rows.append([BTN_ENTER_SEAT])
    rows.append([BTN_BACK, BTN_CANCEL])
    return rows


def _effective_selected_seat(settings: Settings, ui_state: ServiceUiState) -> str:
    return (ui_state.selected_seat or settings.target_seat).strip()


def _parse_date_text(text: str, settings: Settings) -> date | None:
    raw = (text or "").strip()
    try:
        return datetime.strptime(raw, settings.booking_date_format).date()
    except ValueError:
        return None


def _parse_seat_text(text: str) -> str | None:
    raw = (text or "").strip()
    if raw.startswith("/seat"):
        parts = raw.split(maxsplit=1)
        if len(parts) == 2:
            raw = parts[1].strip()
        else:
            return None
    match = re.search(r"(?<!\d)(\d{1,4})(?!\d)", raw)
    if not match:
        return None
    return match.group(1)


def _build_selection_summary(settings: Settings, ui_state: ServiceUiState) -> str:
    chosen_date = ui_state.selected_date.strftime(settings.booking_date_format) if ui_state.selected_date else "not selected (uses +7)"
    chosen_seat = _effective_selected_seat(settings, ui_state)
    seat_note = ""
    if chosen_seat != settings.target_seat and settings.target_table_id:
        seat_note = "\nNote: exact TARGET_TABLE_ID is only configured for default seat."
    return (
        "[workplace-booking] Manual booking selection\n"
        f"Date: {chosen_date}\n"
        f"Seat: {chosen_seat}\n"
        f"Time: {settings.booking_time_from}-{settings.booking_time_to}"
        f"{seat_note}"
    )


def _send_service_menu(
    notifier: TelegramNotifier,
    settings: Settings,
    ui_state: ServiceUiState,
    *,
    message: str | None = None,
) -> None:
    text = message or _build_selection_summary(settings, ui_state)
    notifier.send_reply_keyboard(text, _menu_keyboard_rows())


def _build_service_help() -> str:
    return (
        "[workplace-booking] Telegram commands\n"
        "/help - show commands\n"
        "/menu - show buttons/menu\n"
        "/status - show bot status and next scheduled run\n"
        "/run - run booking now using scheduled +7 logic\n"
        "/booknext - run booking now for date +7\n"
        "/book DD.MM.YYYY - run booking now for exact date\n"
        "/book +N - run booking now for offset days (example /book +7)\n"
        "/seat N - set seat for manual runs (example /seat 17)\n"
        "/ping - health check"
    )


def _parse_manual_book_date(command_text: str, settings: Settings) -> date | None:
    text = command_text.strip()
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        return None
    arg = parts[1].strip()
    if not arg:
        return None
    if re.fullmatch(r"\+\d{1,3}", arg):
        offset_days = int(arg[1:])
        local_tz = _parse_utc_offset(settings.schedule_local_utc_offset)
        base_date = datetime.now(timezone.utc).astimezone(local_tz).date()
        return base_date + timedelta(days=offset_days)
    try:
        return datetime.strptime(arg, settings.booking_date_format).date()
    except ValueError:
        return None


def _build_status_message(settings: Settings, next_run_utc: datetime) -> str:
    local_tz = settings.schedule_local_utc_offset
    scheduled_target = _scheduled_target_date_for_run(settings, next_run_utc)
    return (
        "[workplace-booking] Status\n"
        f"Mode: {settings.run_mode}\n"
        f"Office: {settings.target_office}\n"
        f"Seat: {settings.target_seat}\n"
        f"Target time (local): {settings.booking_time_from}-{settings.booking_time_to}\n"
        f"Office timezone: {settings.booking_local_utc_offset}\n"
        f"Next scheduled run: {_format_local_dt(next_run_utc, local_tz)}\n"
        f"Scheduled target date (+7): {scheduled_target.strftime(settings.booking_date_format)}"
    )


async def _run_manual_booking_for_date(
    base_settings: Settings,
    notifier: TelegramNotifier,
    target_date: date,
    target_seat: str | None = None,
) -> int:
    manual_settings = _settings_for_manual_request(
        base_settings,
        target=target_date,
        target_seat=target_seat,
    )
    notifier.send(
        "[workplace-booking] Manual booking requested\n"
        f"Date: {target_date.strftime(base_settings.booking_date_format)}\n"
        f"Seat: {manual_settings.target_seat}\n"
        f"Time: {base_settings.booking_time_from}-{base_settings.booking_time_to}"
    )
    return await run_once(manual_settings, notifier)


async def _handle_service_command(
    text: str,
    settings: Settings,
    notifier: TelegramNotifier,
    next_run_utc: datetime,
    ui_state: ServiceUiState,
) -> tuple[bool, datetime]:
    normalized = (text or "").strip()
    if not normalized:
        return False, next_run_utc
    lowered = normalized.lower()

    if normalized in {BTN_CANCEL, BTN_BACK}:
        ui_state.pending_input = None
        _send_service_menu(
            notifier,
            settings,
            ui_state,
            message="[workplace-booking] Selection menu",
        )
        return False, next_run_utc

    if normalized.startswith("/start") or normalized.startswith("/menu") or normalized == BTN_MENU:
        ui_state.pending_input = None
        _send_service_menu(notifier, settings, ui_state)
        return False, next_run_utc

    if normalized.startswith("/help"):
        notifier.send_reply_keyboard(_build_service_help(), _menu_keyboard_rows())
        return False, next_run_utc

    if (
        normalized.startswith("/status")
        or normalized.startswith("/schedule")
        or normalized == BTN_STATUS
    ):
        notifier.send_reply_keyboard(
            _build_status_message(settings, next_run_utc)
            + "\n\n"
            + _build_selection_summary(settings, ui_state),
            _menu_keyboard_rows(),
        )
        return False, next_run_utc

    if normalized == BTN_PICK_DATE:
        ui_state.pending_input = "date"
        notifier.send_reply_keyboard(
            "[workplace-booking] Choose booking date",
            _date_keyboard_rows(settings, days=14),
        )
        return False, next_run_utc

    if normalized == BTN_PICK_SEAT:
        ui_state.pending_input = "seat"
        notifier.send_reply_keyboard(
            "[workplace-booking] Choose seat number",
            _seat_keyboard_rows(settings),
        )
        return False, next_run_utc

    if normalized == BTN_ENTER_SEAT:
        ui_state.pending_input = "seat"
        notifier.send_reply_keyboard(
            "[workplace-booking] Send seat number as text (example: 17).",
            [[BTN_BACK, BTN_CANCEL]],
        )
        return False, next_run_utc

    chosen_date = _parse_date_text(normalized, settings)
    if chosen_date is not None and ui_state.pending_input in {None, "date"}:
        ui_state.selected_date = chosen_date
        ui_state.pending_input = None
        _send_service_menu(
            notifier,
            settings,
            ui_state,
            message=(
                "[workplace-booking] Date selected\n"
                f"Date: {chosen_date.strftime(settings.booking_date_format)}"
            ),
        )
        return False, next_run_utc

    if normalized.startswith("/seat") or (
        ui_state.pending_input == "seat" and normalized and not normalized.startswith("/")
    ):
        seat = _parse_seat_text(normalized)
        if seat is None:
            notifier.send_reply_keyboard(
                "[workplace-booking] Invalid seat number. Send only the seat number, for example: 17",
                [[BTN_BACK, BTN_CANCEL]],
            )
            return False, next_run_utc
        ui_state.selected_seat = seat
        ui_state.pending_input = None
        _send_service_menu(
            notifier,
            settings,
            ui_state,
            message=f"[workplace-booking] Seat selected\nSeat: {seat}",
        )
        return False, next_run_utc

    if normalized.startswith("/run") and not normalized.startswith("/runtime"):
        target = _scheduled_target_date(settings)
        await _run_manual_booking_for_date(
            settings,
            notifier,
            target,
            target_seat=_effective_selected_seat(settings, ui_state),
        )
        return True, next_run_utc

    if normalized.startswith("/booknext") or normalized == BTN_RUN_NEXT:
        target = _scheduled_target_date(settings)
        await _run_manual_booking_for_date(
            settings,
            notifier,
            target,
            target_seat=_effective_selected_seat(settings, ui_state),
        )
        return True, next_run_utc

    if normalized == BTN_RESET_SELECTIONS:
        ui_state.selected_date = None
        ui_state.selected_seat = None
        ui_state.pending_input = None
        _send_service_menu(
            notifier,
            settings,
            ui_state,
            message="[workplace-booking] Manual selection reset. Default settings will be used.",
        )
        return False, next_run_utc

    if normalized == BTN_RUN_SELECTED:
        target = ui_state.selected_date or _scheduled_target_date(settings)
        await _run_manual_booking_for_date(
            settings,
            notifier,
            target,
            target_seat=_effective_selected_seat(settings, ui_state),
        )
        return True, next_run_utc

    if normalized.startswith("/book"):
        target = _parse_manual_book_date(normalized, settings)
        if target is None:
            notifier.send(
                "[workplace-booking] Invalid /book command.\n"
                f"Use /book {datetime.now().strftime(settings.booking_date_format)} or /book +7"
            )
            return False, next_run_utc
        await _run_manual_booking_for_date(
            settings,
            notifier,
            target,
            target_seat=_effective_selected_seat(settings, ui_state),
        )
        return True, next_run_utc

    if normalized.startswith("/ping"):
        notifier.send_reply_keyboard("[workplace-booking] pong", _menu_keyboard_rows())
        return False, next_run_utc

    if ui_state.pending_input == "date":
        notifier.send_reply_keyboard(
            (
                "[workplace-booking] Invalid date format.\n"
                f"Use {datetime.now().strftime(settings.booking_date_format)} (DD.MM.YYYY)"
            ),
            _date_keyboard_rows(settings, days=14),
        )
        return False, next_run_utc

    if lowered in {"дата", "место", "статус", "меню"}:
        mapping = {
            "дата": BTN_PICK_DATE,
            "место": BTN_PICK_SEAT,
            "статус": BTN_STATUS,
            "меню": BTN_MENU,
        }
        return await _handle_service_command(
            mapping[lowered],
            settings,
            notifier,
            next_run_utc,
            ui_state,
        )

    return False, next_run_utc


async def run_service(settings: Settings, notifier: TelegramNotifier) -> int:
    next_run_utc = _next_scheduled_run_utc(settings)
    ui_state = ServiceUiState()
    LOGGER.info(
        "Running in service mode. Next scheduled run at %s UTC (%s local).",
        next_run_utc.isoformat(),
        _format_local_dt(next_run_utc, settings.schedule_local_utc_offset),
    )
    if notifier.enabled:
        notifier.send_reply_keyboard(
            "[workplace-booking] Service mode started\n"
            f"Next scheduled run: {_format_local_dt(next_run_utc, settings.schedule_local_utc_offset)}\n"
            f"Scheduled target date (+7): {_scheduled_target_date_for_run(settings, next_run_utc).strftime(settings.booking_date_format)}\n"
            "Use the buttons below or send /help for commands.",
            _menu_keyboard_rows(),
        )
    else:
        LOGGER.warning("Service mode is running without Telegram notifications/commands.")

    while True:
        now_utc = datetime.now(timezone.utc)
        if now_utc >= next_run_utc:
            target = _scheduled_target_date(settings, now_utc=now_utc)
            scheduled_settings = _settings_for_single_date(settings, target)
            notifier.send(
                "[workplace-booking] Scheduled run started\n"
                f"Target date: {target.strftime(settings.booking_date_format)}\n"
                f"Seat: {settings.target_seat}\n"
                f"Time: {settings.booking_time_from}-{settings.booking_time_to}"
            )
            await run_once(scheduled_settings, notifier)
            next_run_utc = _next_scheduled_run_utc(
                settings,
                now_utc=datetime.now(timezone.utc) + timedelta(seconds=1),
            )
            continue

        if notifier.enabled:
            seconds_until_schedule = max(0.0, (next_run_utc - now_utc).total_seconds())
            poll_timeout = min(
                settings.telegram_command_poll_timeout_sec,
                max(1, int(seconds_until_schedule)),
            )
            try:
                messages = await asyncio.to_thread(notifier.poll_text_messages, poll_timeout)
            except Exception:
                LOGGER.exception("Telegram command polling failed.")
                await asyncio.sleep(2)
                continue

            for message in messages:
                text = str(message.get("text") or "").strip()
                handled, next_run_utc = await _handle_service_command(
                    text=text,
                    settings=settings,
                    notifier=notifier,
                    next_run_utc=next_run_utc,
                    ui_state=ui_state,
                )
                if handled:
                    # Keep existing schedule; manual runs should not shift the nightly trigger.
                    LOGGER.info("Handled Telegram command: %s", text)
        else:
            sleep_seconds = min(
                10,
                max(1, int((next_run_utc - now_utc).total_seconds())),
            )
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
    if settings.run_mode == "service":
        return asyncio.run(run_service(settings, notifier))
    return asyncio.run(run_once(settings, notifier))


if __name__ == "__main__":
    raise SystemExit(main())
