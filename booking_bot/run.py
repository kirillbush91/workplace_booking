from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import date, datetime, time as dt_time, timedelta, timezone
import logging
import os
from pathlib import Path
import re
import traceback
import uuid

from dotenv import dotenv_values

from .booking import (
    AuthRefreshResult,
    BookingBot,
    BookingError,
    BookingResult,
    PreflightResult,
)
from .config import Settings
from .notifier import EmailNotifier, EmailSettings, FallbackNotifier, InteractiveNotifier
from .runtime_state import RunHistoryEntry, RunLockError, RuntimeStateStore, SchedulerState, utc_now
from .telegram_client import TelegramNotifier


LOGGER = logging.getLogger(__name__)


BTN_MENU = "Menu"
BTN_STATUS = "Status"
BTN_RUN_NEXT = "Book +7"
BTN_PICK_DATE = "Pick date"
BTN_PICK_SEAT = "Pick seat"
BTN_RUN_SELECTED = "Run selected"
BTN_RESET_SELECTIONS = "Reset selection"
BTN_BACK = "Back"
BTN_CANCEL = "Cancel"
BTN_ENTER_SEAT = "Enter seat"
BTN_PREFLIGHT = "Preflight"
BTN_REAUTH = "Re-auth"
BTN_LAST_RUN = "Last run"
BTN_HISTORY = "History"


@dataclass
class ServiceUiState:
    selected_date: date | None = None
    selected_seat: str | None = None
    pending_input: str | None = None


@dataclass
class RunOnceOutcome:
    exit_code: int
    attempt: int
    result: BookingResult | None = None
    error_message: str | None = None
    screenshot_path: Path | None = None
    otp_requested: bool = False
    otp_received: bool = False


@dataclass
class PreflightOutcome:
    exit_code: int
    result: PreflightResult | None = None
    error_message: str | None = None


@dataclass
class ReauthOutcome:
    exit_code: int
    result: AuthRefreshResult | None = None
    error_message: str | None = None
    screenshot_path: Path | None = None
    otp_requested: bool = False
    otp_received: bool = False


@dataclass(frozen=True)
class CatchupDecision:
    state: str
    scheduled_run_utc: datetime
    scheduled_local_date: date
    target_date: date
    reason: str


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _load_env_files(shared_path: str | Path = ".env.shared", local_path: str | Path = ".env") -> list[str]:
    loaded: list[str] = []
    protected_keys = set(os.environ.keys())
    for raw_path in (shared_path, local_path):
        path = Path(raw_path)
        if not path.exists():
            continue
        values = dotenv_values(path)
        for key, value in values.items():
            if value is None:
                continue
            if key in protected_keys:
                continue
            os.environ[key] = value
        loaded.append(str(path))
    return loaded


def _state_store(settings: Settings) -> RuntimeStateStore:
    return RuntimeStateStore(settings.storage_state_path.parent)


def _seat_order(settings: Settings) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for seat in settings.preferred_seats:
        value = str(seat).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    if not out:
        out.append(settings.target_seat)
    return out


def _seat_order_label(settings: Settings) -> str:
    return " -> ".join(_seat_order(settings))


def _selected_seat_override(ui_state: ServiceUiState) -> str | None:
    if not ui_state.selected_seat:
        return None
    value = str(ui_state.selected_seat).strip()
    return value or None


def _target_dates_for_settings(settings: Settings) -> list[date]:
    if settings.booking_date_values:
        out: list[date] = []
        seen: set[date] = set()
        for raw in settings.booking_date_values:
            parsed = datetime.strptime(raw, settings.booking_date_format).date()
            if parsed in seen:
                continue
            seen.add(parsed)
            out.append(parsed)
        return out
    if settings.booking_date_value:
        return [datetime.strptime(settings.booking_date_value, settings.booking_date_format).date()]
    if settings.booking_date_offset_days is not None:
        return [date.today() + timedelta(days=settings.booking_date_offset_days)]

    today = date.today()
    start = today if settings.booking_include_today else today + timedelta(days=1)
    end = today + timedelta(days=settings.booking_range_days)
    out: list[date] = []
    current = start
    while current <= end:
        if not (settings.booking_skip_weekends and current.weekday() >= 5):
            out.append(current)
        current += timedelta(days=1)
    return out


def _single_target_label(settings: Settings) -> str | None:
    dates = _target_dates_for_settings(settings)
    if not dates:
        return None
    if len(dates) == 1:
        return dates[0].strftime(settings.booking_date_format)
    return ", ".join(item.strftime(settings.booking_date_format) for item in dates)


def _chosen_seat_from_result(result: BookingResult | None) -> str | None:
    if result is None:
        return None
    for day in result.day_results:
        if day.chosen_seat:
            return day.chosen_seat
    return None


def _result_status(result: BookingResult) -> str:
    if result.failed_dates:
        return "failed"
    if result.booked_dates:
        return "booked"
    return "skipped"


def _result_summary(result: BookingResult) -> str:
    status = _result_status(result)
    chosen_seat = _chosen_seat_from_result(result)
    if status == "booked":
        summary = f"Booked {', '.join(result.booked_dates)}"
        if chosen_seat:
            summary += f" on seat {chosen_seat}"
        return summary
    if status == "skipped":
        return f"Skipped: {', '.join(result.skipped_dates) or 'no target dates'}"
    return f"Failed: {', '.join(result.failed_dates)}"


def _preflight_status(result: PreflightResult) -> str:
    if result.login_required or result.otp_likely or not result.office_map_available:
        return "warning"
    if result.session_valid:
        return "ok"
    return "failed"


def _preflight_summary(result: PreflightResult) -> str:
    parts = [result.message]
    if result.login_required:
        parts.append("login required")
    if result.otp_likely:
        parts.append("otp likely")
    if result.office_map_available:
        parts.append("office map available")
    return "; ".join(dict.fromkeys(part for part in parts if part))


def _reauth_status(result: AuthRefreshResult) -> str:
    if result.session_valid:
        return "ok"
    return "warning"


def _reauth_summary(result: AuthRefreshResult) -> str:
    parts = [result.message]
    if result.login_required:
        parts.append("login required")
    if result.otp_likely:
        parts.append("otp likely")
    if result.office_map_available:
        parts.append("office map available")
    if result.storage_state_saved:
        parts.append("storage saved")
    return "; ".join(dict.fromkeys(part for part in parts if part))


def _build_success_message(result: BookingResult, attempt: int, mode: str) -> str:
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
        extra = f" seat={day.chosen_seat}" if day.chosen_seat else ""
        day_lines.append(f"{icon} {day.date} ({day.status}{extra}): {day.message}")
    days_block = "\n".join(day_lines) if day_lines else "n/a"
    return (
        "[workplace-booking] Booking run finished\n"
        f"Mode: {mode}\n"
        f"Attempt: {attempt}\n"
        f"Summary: {summary}\n"
        f"Office: {result.office}\n"
        f"Seat order: {result.seat}\n"
        f"Booked: {len(result.booked_dates)}\n"
        f"Skipped: {len(result.skipped_dates)}\n"
        f"Failed: {len(result.failed_dates)}\n"
        f"Duration: {duration:.1f}s\n"
        f"Finished UTC: {result.finished_at.isoformat()}\n"
        f"Screenshot: {screenshot}\n"
        "Per-day:\n"
        f"{days_block}"
    )


def _build_start_message(settings: Settings, attempt: int, mode: str) -> str:
    target_label = _single_target_label(settings) or "n/a"
    return (
        "[workplace-booking] Booking started\n"
        f"Mode: {mode}\n"
        f"Attempt: {attempt}/{settings.retry_attempts}\n"
        f"Office: {settings.target_office}\n"
        f"Target date(s): {target_label}\n"
        f"Seat order: {_seat_order_label(settings)}\n"
        f"Time: {settings.booking_time_from}-{settings.booking_time_to}\n"
        f"UTC: {utc_now().isoformat()}"
    )


def _build_error_message(exc: Exception, attempt: int, mode: str) -> str:
    screenshot = "n/a"
    if isinstance(exc, BookingError) and exc.screenshot_path is not None:
        screenshot = str(exc.screenshot_path)
    stack = traceback.format_exception_only(exc.__class__, exc)[-1].strip()
    return (
        "[workplace-booking] Booking failed\n"
        f"Mode: {mode}\n"
        f"Attempt: {attempt}\n"
        f"Error: {stack}\n"
        f"Screenshot: {screenshot}\n"
        f"UTC: {utc_now().isoformat()}"
    )


def _build_preflight_start_message(settings: Settings, mode: str) -> str:
    return (
        "[workplace-booking] Auth preflight started\n"
        f"Mode: {mode}\n"
        f"Office: {settings.target_office}\n"
        f"Seat order: {_seat_order_label(settings)}\n"
        f"UTC: {utc_now().isoformat()}"
    )


def _build_preflight_result_message(result: PreflightResult, mode: str) -> str:
    status = _preflight_status(result)
    storage_age = "n/a"
    if result.storage_state_age_sec is not None:
        storage_age = f"{result.storage_state_age_sec}s"
    next_step = ""
    if status != "ok":
        next_step = "\nNext: send /reauth or tap Re-auth to refresh saved session."
    return (
        "[workplace-booking] Auth preflight finished\n"
        f"Mode: {mode}\n"
        f"Status: {status}\n"
        f"Session valid: {result.session_valid}\n"
        f"Login required: {result.login_required}\n"
        f"OTP likely: {result.otp_likely}\n"
        f"Office map available: {result.office_map_available}\n"
        f"Storage state present: {result.storage_state_present}\n"
        f"Storage state age: {storage_age}\n"
        f"Current URL: {result.current_url}\n"
        f"Summary: {result.message}"
        f"{next_step}"
    )


def _build_preflight_error_message(exc: Exception, mode: str) -> str:
    stack = traceback.format_exception_only(exc.__class__, exc)[-1].strip()
    return (
        "[workplace-booking] Auth preflight failed\n"
        f"Mode: {mode}\n"
        f"Error: {stack}\n"
        f"UTC: {utc_now().isoformat()}"
    )


def _build_reauth_start_message(settings: Settings, mode: str) -> str:
    return (
        "[workplace-booking] Auth refresh started\n"
        f"Mode: {mode}\n"
        f"Office: {settings.target_office}\n"
        f"Storage state: {settings.storage_state_path}\n"
        "Booking will not be attempted in this run.\n"
        f"UTC: {utc_now().isoformat()}"
    )


def _build_reauth_result_message(result: AuthRefreshResult, mode: str) -> str:
    status = _reauth_status(result)
    return (
        "[workplace-booking] Auth refresh finished\n"
        f"Mode: {mode}\n"
        f"Status: {status}\n"
        f"Session valid: {result.session_valid}\n"
        f"Login required: {result.login_required}\n"
        f"OTP likely: {result.otp_likely}\n"
        f"OTP requested/received: {result.otp_requested}/{result.otp_received}\n"
        f"Office map available: {result.office_map_available}\n"
        f"Storage state saved: {result.storage_state_saved}\n"
        f"Current URL: {result.current_url}\n"
        f"Summary: {result.message}"
    )


def _build_reauth_error_message(exc: Exception, mode: str) -> str:
    screenshot = "n/a"
    if isinstance(exc, BookingError) and exc.screenshot_path is not None:
        screenshot = str(exc.screenshot_path)
    stack = traceback.format_exception_only(exc.__class__, exc)[-1].strip()
    return (
        "[workplace-booking] Auth refresh failed\n"
        f"Mode: {mode}\n"
        f"Error: {stack}\n"
        f"Screenshot: {screenshot}\n"
        f"UTC: {utc_now().isoformat()}"
    )


def _send_result_screenshot(notifier: InteractiveNotifier, screenshot_path: Path | None) -> None:
    if not screenshot_path:
        return
    notifier.send_document(
        screenshot_path,
        caption="[workplace-booking] Screenshot",
    )


def _run_lock_stale_after_sec(settings: Settings) -> int:
    otp_wait = max(1, settings.otp_wait_timeout_ms // 1000)
    retry_buffer = settings.retry_attempts * max(1, settings.retry_delay_sec)
    return max(otp_wait + retry_buffer + 1800, 7200)


async def run_once_detailed(
    settings: Settings,
    notifier: InteractiveNotifier,
    *,
    mode: str,
) -> RunOnceOutcome:
    last_outcome = RunOnceOutcome(exit_code=1, attempt=0, error_message="Run did not start.")
    for attempt in range(1, settings.retry_attempts + 1):
        notifier.send(_build_start_message(settings, attempt, mode))
        bot = BookingBot(settings, notifier=notifier)
        try:
            result = await bot.book()
            if result.booked_dates:
                LOGGER.info("Booking created successfully on attempt %s.", attempt)
            else:
                LOGGER.info(
                    "Booking run completed on attempt %s: no new bookings were needed.",
                    attempt,
                )
            notifier.send(_build_success_message(result, attempt, mode), critical=True)
            _send_result_screenshot(notifier, result.screenshot_path)
            return RunOnceOutcome(
                exit_code=0,
                attempt=attempt,
                result=result,
                screenshot_path=result.screenshot_path,
                otp_requested=bot.otp_requested,
                otp_received=bot.otp_received,
            )
        except Exception as exc:
            LOGGER.exception("Booking attempt %s failed.", attempt)
            final_attempt = attempt >= settings.retry_attempts
            notifier.send(
                _build_error_message(exc, attempt, mode),
                critical=final_attempt,
            )
            screenshot_path = exc.screenshot_path if isinstance(exc, BookingError) else None
            if isinstance(exc, BookingError):
                _send_result_screenshot(notifier, exc.screenshot_path)
            last_outcome = RunOnceOutcome(
                exit_code=1,
                attempt=attempt,
                error_message=str(exc),
                screenshot_path=screenshot_path,
                otp_requested=bot.otp_requested,
                otp_received=bot.otp_received,
            )
            if attempt >= settings.retry_attempts:
                return last_outcome
            notifier.send(
                "[workplace-booking] Retrying after failure\n"
                f"Next attempt in {settings.retry_delay_sec} sec."
            )
            await asyncio.sleep(settings.retry_delay_sec)
    return last_outcome


async def run_once(settings: Settings, notifier: InteractiveNotifier) -> int:
    outcome = await run_once_detailed(settings, notifier, mode=settings.run_mode)
    return outcome.exit_code


async def run_preflight_once(
    settings: Settings,
    notifier: InteractiveNotifier,
    *,
    mode: str,
) -> PreflightOutcome:
    notifier.send(_build_preflight_start_message(settings, mode))
    bot = BookingBot(settings, notifier=notifier)
    try:
        result = await bot.preflight()
        notifier.send(_build_preflight_result_message(result, mode))
        return PreflightOutcome(exit_code=0, result=result)
    except Exception as exc:
        LOGGER.exception("Auth preflight failed.")
        notifier.send(_build_preflight_error_message(exc, mode))
        return PreflightOutcome(exit_code=1, error_message=str(exc))


async def run_reauth_once(
    settings: Settings,
    notifier: InteractiveNotifier,
    *,
    mode: str,
) -> ReauthOutcome:
    notifier.send(_build_reauth_start_message(settings, mode))
    bot = BookingBot(settings, notifier=notifier)
    try:
        result = await bot.refresh_auth()
        notifier.send(
            _build_reauth_result_message(result, mode),
            critical=not result.session_valid,
        )
        return ReauthOutcome(
            exit_code=0 if result.session_valid else 1,
            result=result,
            otp_requested=result.otp_requested,
            otp_received=result.otp_received,
        )
    except Exception as exc:
        LOGGER.exception("Auth refresh failed.")
        notifier.send(_build_reauth_error_message(exc, mode), critical=True)
        screenshot_path = exc.screenshot_path if isinstance(exc, BookingError) else None
        if isinstance(exc, BookingError):
            _send_result_screenshot(notifier, exc.screenshot_path)
        return ReauthOutcome(
            exit_code=1,
            error_message=str(exc),
            screenshot_path=screenshot_path,
            otp_requested=bot.otp_requested,
            otp_received=bot.otp_received,
        )


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


def _schedule_timezone(settings: Settings) -> timezone:
    return _parse_utc_offset(settings.schedule_local_utc_offset)


def _schedule_local_now(settings: Settings, now_utc: datetime | None = None) -> datetime:
    now_utc = now_utc or utc_now()
    return now_utc.astimezone(_schedule_timezone(settings))


def _scheduled_local_datetime(settings: Settings, local_day: date) -> datetime:
    schedule_time = _parse_hhmm(settings.schedule_time_local)
    return datetime.combine(local_day, schedule_time, tzinfo=_schedule_timezone(settings))


def _scheduled_target_date(settings: Settings, now_utc: datetime | None = None) -> date:
    local_today = _schedule_local_now(settings, now_utc).date()
    offset_days = settings.booking_date_offset_days
    if offset_days is None:
        offset_days = 7
    return local_today + timedelta(days=offset_days)


def _scheduled_target_date_for_run(settings: Settings, run_at_utc: datetime) -> date:
    return _scheduled_target_date(settings, now_utc=run_at_utc)


def _next_scheduled_run_utc(settings: Settings, now_utc: datetime | None = None) -> datetime:
    now_utc = now_utc or utc_now()
    local_now = _schedule_local_now(settings, now_utc)
    local_candidate = _scheduled_local_datetime(settings, local_now.date())
    if local_candidate <= local_now:
        local_candidate += timedelta(days=1)
    return local_candidate.astimezone(timezone.utc)


def _last_due_scheduled_run_utc(settings: Settings, now_utc: datetime | None = None) -> datetime:
    now_utc = now_utc or utc_now()
    local_now = _schedule_local_now(settings, now_utc)
    local_candidate = _scheduled_local_datetime(settings, local_now.date())
    if local_candidate > local_now:
        local_candidate -= timedelta(days=1)
    return local_candidate.astimezone(timezone.utc)


def _format_local_dt(dt_utc: datetime, offset_raw: str) -> str:
    local_tz = _parse_utc_offset(offset_raw)
    local_dt = dt_utc.astimezone(local_tz)
    return f"{local_dt.strftime('%d.%m.%Y %H:%M')} ({offset_raw})"


def _weekday_short(value: date) -> str:
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return names[value.weekday()]


def _is_weekend_booking_date(settings: Settings, target: date) -> bool:
    return bool(settings.booking_skip_weekends and target.weekday() >= 5)


def _booking_target_rule_label(settings: Settings) -> str:
    offset_days = settings.booking_date_offset_days
    if offset_days is None:
        offset_days = 7
    return f"local run date +{offset_days} day(s)"


def _build_schedule_preview(settings: Settings, start_run_utc: datetime, count: int = 7) -> str:
    lines: list[str] = []
    run_at_utc = start_run_utc
    seat_order = _seat_order_label(settings)
    for _ in range(max(1, count)):
        target = _scheduled_target_date_for_run(settings, run_at_utc)
        target_label = target.strftime(settings.booking_date_format)
        if _is_weekend_booking_date(settings, target):
            suffix = " [weekend skipped]"
        else:
            suffix = f" seats {seat_order}"
        lines.append(
            f"- {_format_local_dt(run_at_utc, settings.schedule_local_utc_offset)} "
            f"-> {target_label} ({_weekday_short(target)}){suffix}"
        )
        run_at_utc = _next_scheduled_run_utc(
            settings,
            now_utc=run_at_utc + timedelta(seconds=1),
        )
    return "\n".join(lines)


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
    mapped = out.preferred_seat_table_ids.get(seat)
    if mapped is None and seat == out.target_seat and out.target_table_id:
        mapped = out.target_table_id
    preferred_map = {seat: mapped} if mapped else {}
    return replace(
        out,
        target_seat=seat,
        preferred_seats=[seat],
        preferred_seat_table_ids=preferred_map,
        target_table_id=mapped,
    )


def _menu_keyboard_rows() -> list[list[str]]:
    return [
        [BTN_RUN_NEXT, BTN_STATUS],
        [BTN_PICK_DATE, BTN_PICK_SEAT],
        [BTN_RUN_SELECTED, BTN_RESET_SELECTIONS],
        [BTN_PREFLIGHT, BTN_REAUTH],
        [BTN_LAST_RUN, BTN_HISTORY],
        ["/help"],
    ]


def _date_keyboard_rows(settings: Settings, days: int = 14) -> list[list[str]]:
    base = _schedule_local_now(settings).date()
    labels: list[str] = []
    offset = 0
    target_count = max(1, days)
    max_scan_days = max(target_count * 3, target_count + 14)
    while len(labels) < target_count and offset < max_scan_days:
        candidate = base + timedelta(days=offset)
        offset += 1
        if _is_weekend_booking_date(settings, candidate):
            continue
        labels.append(candidate.strftime(settings.booking_date_format))
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


def _seat_keyboard_rows(settings: Settings, ui_state: ServiceUiState) -> list[list[str]]:
    current = (_selected_seat_override(ui_state) or settings.target_seat).strip()
    rows: list[list[str]] = []
    if current.isdigit():
        center = int(current)
        values = []
        for seat in range(max(1, center - 8), center + 9):
            values.append(f"Seat {seat}")
        row: list[str] = []
        for value in values:
            row.append(value)
            if len(row) == 4:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
    else:
        rows.append([f"Seat {current}"])
    rows.append([BTN_ENTER_SEAT])
    rows.append([BTN_BACK, BTN_CANCEL])
    return rows


def _effective_selected_seat(settings: Settings, ui_state: ServiceUiState) -> str:
    return (_selected_seat_override(ui_state) or settings.target_seat).strip()


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
    chosen_date = (
        ui_state.selected_date.strftime(settings.booking_date_format)
        if ui_state.selected_date
        else f"not selected (uses +{settings.booking_date_offset_days or 7})"
    )
    selected_override = _selected_seat_override(ui_state)
    chosen_seat = _effective_selected_seat(settings, ui_state)
    if selected_override:
        seat_mode = f"manual seat {chosen_seat} (fallback disabled)"
    else:
        seat_mode = f"default fallback {_seat_order_label(settings)}"
    weekend_note = ""
    if ui_state.selected_date and _is_weekend_booking_date(settings, ui_state.selected_date):
        weekend_note = "\nWarning: selected date is weekend and will be skipped by policy."
    return (
        "[workplace-booking] Manual booking selection\n"
        f"Date: {chosen_date}\n"
        f"Seat mode: {seat_mode}\n"
        f"Time: {settings.booking_time_from}-{settings.booking_time_to}"
        f"{weekend_note}"
    )


def _send_service_menu(
    notifier: InteractiveNotifier,
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
        "/preflight - run auth/session self-check\n"
        "/reauth - refresh saved auth session with login/OTP, without booking\n"
        "/last - show last completed run\n"
        "/history - show last 10 runs\n"
        "/run - run booking now using scheduled +7 logic\n"
        "/booknext - run booking now for date +7\n"
        "/book DD.MM.YYYY - run booking now for exact date\n"
        "/book +N - run booking now for offset days (example /book +7)\n"
        "/seat N - set seat for manual runs (example /seat 17)\n"
        "/cancelotp - cancel active OTP wait if one is in progress\n"
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
        return _schedule_local_now(settings).date() + timedelta(days=offset_days)
    try:
        return datetime.strptime(arg, settings.booking_date_format).date()
    except ValueError:
        return None


def _parse_iso_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _format_history_entry(settings: Settings, entry: RunHistoryEntry) -> str:
    started = _parse_iso_datetime(entry.started_at_utc)
    started_label = _format_local_dt(started, settings.schedule_local_utc_offset) if started else entry.started_at_utc
    seat_order = " -> ".join(entry.seat_attempt_order) if entry.seat_attempt_order else "n/a"
    chosen = entry.chosen_seat or "n/a"
    target = entry.target_date or "n/a"
    otp = "yes" if entry.otp_requested else "no"
    otp_received = "yes" if entry.otp_received else "no"
    return (
        f"- {started_label} | mode={entry.mode} | status={entry.status} | "
        f"target={target} | seats={seat_order} | chosen={chosen} | "
        f"otp={otp}/{otp_received} | {entry.summary}"
    )


def _build_last_run_message(settings: Settings, entry: RunHistoryEntry | None) -> str:
    if entry is None:
        return "[workplace-booking] No run history is available yet."
    return "[workplace-booking] Last run\n" + _format_history_entry(settings, entry)


def _build_history_message(settings: Settings, entries: list[RunHistoryEntry]) -> str:
    if not entries:
        return "[workplace-booking] No run history is available yet."
    lines = ["[workplace-booking] Last runs"]
    for entry in entries[-10:]:
        lines.append(_format_history_entry(settings, entry))
    return "\n".join(lines)


def _compute_catchup_decision(
    settings: Settings,
    state: SchedulerState,
    now_utc: datetime | None = None,
) -> CatchupDecision:
    now_utc = now_utc or utc_now()
    due_run_utc = _last_due_scheduled_run_utc(settings, now_utc)
    due_local_dt = due_run_utc.astimezone(_schedule_timezone(settings))
    due_local_date = due_local_dt.date()
    due_local_iso = due_local_date.isoformat()
    target_date = _scheduled_target_date_for_run(settings, due_run_utc)

    if (
        state.last_scheduled_run_local_date is None
        and state.last_run_started_at_utc is None
        and state.catchup_executed_for_local_date is None
    ):
        return CatchupDecision(
            state="not_needed",
            scheduled_run_utc=due_run_utc,
            scheduled_local_date=due_local_date,
            target_date=target_date,
            reason="scheduler state is empty; skip catch-up on first startup",
        )

    if state.last_scheduled_run_local_date == due_local_iso:
        return CatchupDecision(
            state="not_needed",
            scheduled_run_utc=due_run_utc,
            scheduled_local_date=due_local_date,
            target_date=target_date,
            reason="latest scheduled run already completed",
        )
    if state.catchup_executed_for_local_date == due_local_iso:
        return CatchupDecision(
            state="handled",
            scheduled_run_utc=due_run_utc,
            scheduled_local_date=due_local_date,
            target_date=target_date,
            reason="latest missed run already handled after restart",
        )

    now_local = _schedule_local_now(settings, now_utc)
    window_end = due_local_dt + timedelta(minutes=settings.schedule_catchup_window_minutes)
    if now_local <= window_end:
        return CatchupDecision(
            state="pending",
            scheduled_run_utc=due_run_utc,
            scheduled_local_date=due_local_date,
            target_date=target_date,
            reason="latest scheduled run was missed and is still inside catch-up window",
        )
    return CatchupDecision(
        state="expired",
        scheduled_run_utc=due_run_utc,
        scheduled_local_date=due_local_date,
        target_date=target_date,
        reason="latest scheduled run was missed and catch-up window already expired",
    )


def _is_preflight_due(
    settings: Settings,
    state: SchedulerState,
    now_utc: datetime | None = None,
) -> tuple[bool, date | None]:
    if not settings.auth_preflight_enabled:
        return False, None
    now_local = _schedule_local_now(settings, now_utc)
    preflight_time = _parse_hhmm(settings.auth_preflight_time_local)
    preflight_dt = datetime.combine(now_local.date(), preflight_time, tzinfo=now_local.tzinfo)
    if now_local < preflight_dt:
        return False, None
    local_date = now_local.date()
    if state.last_preflight_local_date == local_date.isoformat():
        return False, local_date
    return True, local_date


def _scheduled_preflight_local_date(settings: Settings, scheduled_local_date: date) -> date:
    schedule_time = _parse_hhmm(settings.schedule_time_local)
    preflight_time = _parse_hhmm(settings.auth_preflight_time_local)
    if preflight_time <= schedule_time:
        return scheduled_local_date
    return scheduled_local_date - timedelta(days=1)


def _scheduled_auth_block_message(
    settings: Settings,
    state: SchedulerState,
    scheduled_local_date: date,
) -> str | None:
    if not settings.auth_preflight_enabled:
        return None
    expected_preflight_date = _scheduled_preflight_local_date(settings, scheduled_local_date)
    if state.last_preflight_local_date != expected_preflight_date.isoformat():
        return None
    if (state.last_preflight_status or "").lower() == "ok":
        return None

    status = state.last_preflight_status or "unknown"
    detail = state.last_preflight_message or "Auth preflight did not finish cleanly."
    return (
        f"Scheduled run blocked because auth preflight on "
        f"{expected_preflight_date.strftime(settings.booking_date_format)} finished with "
        f"status={status}: {detail}. Send /reauth or tap Re-auth, then run booking manually."
    )


def _is_healthcheck_due(
    settings: Settings,
    state: SchedulerState,
    now_utc: datetime | None = None,
) -> tuple[bool, date | None]:
    if not settings.healthcheck_enabled:
        return False, None
    now_local = _schedule_local_now(settings, now_utc)
    healthcheck_time = _parse_hhmm(settings.healthcheck_time_local)
    healthcheck_dt = datetime.combine(
        now_local.date(),
        healthcheck_time,
        tzinfo=now_local.tzinfo,
    )
    if now_local < healthcheck_dt:
        return False, None
    local_date = now_local.date()
    if state.last_healthcheck_local_date == local_date.isoformat():
        return False, local_date
    return True, local_date


def _build_healthcheck_message(settings: Settings, now_utc: datetime | None = None) -> str:
    now_utc = now_utc or utc_now()
    route = "proxy" if settings.telegram_proxy_enabled and settings.telegram_proxy_url else "direct"
    proxy_label = settings.telegram_proxy_url or "disabled"
    return (
        "[workplace-booking] Daily Telegram health check\n"
        f"Mode: {settings.run_mode}\n"
        f"Route: {route}\n"
        f"Proxy URL: {proxy_label}\n"
        f"Office: {settings.target_office}\n"
        f"Next scheduled booking: {settings.schedule_time_local} ({settings.schedule_local_utc_offset})\n"
        f"UTC: {now_utc.isoformat()}"
    )


def _build_status_message(
    settings: Settings,
    store: RuntimeStateStore,
    next_run_utc: datetime,
    *,
    now_utc: datetime | None = None,
) -> str:
    now_utc = now_utc or utc_now()
    state = store.load_scheduler_state()
    last_entry = store.read_last_history()
    catchup = _compute_catchup_decision(settings, state, now_utc)
    scheduled_target = _scheduled_target_date_for_run(settings, next_run_utc)
    weekend_policy = (
        "skip Saturday/Sunday targets (no booking attempt)"
        if settings.booking_skip_weekends
        else "weekend targets are allowed"
    )
    preflight_policy = (
        f"daily at {settings.auth_preflight_time_local} ({settings.schedule_local_utc_offset})"
        if settings.auth_preflight_enabled
        else "disabled"
    )
    healthcheck_policy = (
        f"daily at {settings.healthcheck_time_local} ({settings.schedule_local_utc_offset})"
        if settings.healthcheck_enabled
        else "disabled"
    )
    next_run_utc_label = next_run_utc.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M")
    if catchup.state == "pending":
        catchup_label = (
            "yes: "
            f"{catchup.scheduled_local_date.strftime(settings.booking_date_format)} "
            f"-> {catchup.target_date.strftime(settings.booking_date_format)} "
            f"({_weekday_short(catchup.target_date)})"
        )
    elif catchup.state == "expired":
        catchup_label = (
            "missed/outside window: "
            f"{catchup.scheduled_local_date.strftime(settings.booking_date_format)} "
            f"-> {catchup.target_date.strftime(settings.booking_date_format)}"
        )
    else:
        catchup_label = "no"

    last_run_block = _build_last_run_message(settings, last_entry).removeprefix("[workplace-booking] Last run\n")
    in_progress = "yes" if state.in_progress_run_id else "no"
    return (
        "[workplace-booking] Status\n"
        f"Mode: {settings.run_mode}\n"
        f"Office: {settings.target_office}\n"
        f"Preferred seats: {_seat_order_label(settings)}\n"
        f"Booking window (local): {settings.booking_time_from}-{settings.booking_time_to}\n"
        f"Office timezone: {settings.booking_local_utc_offset}\n"
        f"Scheduler: every day at {settings.schedule_time_local} ({settings.schedule_local_utc_offset})\n"
        f"Target date rule: {_booking_target_rule_label(settings)}\n"
        f"Weekend policy: {weekend_policy}\n"
        f"Catch-up policy: {settings.schedule_catchup_window_minutes} minute window after missed scheduled run\n"
        f"Auth preflight: {preflight_policy}\n"
        f"Telegram health check: {healthcheck_policy}\n"
        f"OTP policy: wait {settings.otp_wait_timeout_ms // 60000} min, remind every {settings.otp_reminder_interval_sec // 60} min\n"
        f"Run in progress: {in_progress}\n"
        f"Next scheduled run (local): {_format_local_dt(next_run_utc, settings.schedule_local_utc_offset)}\n"
        f"Next scheduled run (UTC): {next_run_utc_label} (+00:00)\n"
        f"Next scheduled target: {scheduled_target.strftime(settings.booking_date_format)} ({_weekday_short(scheduled_target)})\n"
        f"Pending catch-up: {catchup_label}\n"
        "Last completed run:\n"
        f"{last_run_block}\n"
        "Upcoming schedule preview:\n"
        f"{_build_schedule_preview(settings, next_run_utc, count=7)}"
    )

def _build_run_history_entry(
    *,
    run_id: str,
    mode: str,
    started_at_utc: datetime,
    finished_at_utc: datetime,
    target_date: str | None,
    seat_attempt_order: list[str],
    chosen_seat: str | None,
    status: str,
    summary: str,
    otp_requested: bool,
    otp_received: bool,
    screenshot_path: Path | None,
) -> RunHistoryEntry:
    return RunHistoryEntry(
        run_id=run_id,
        mode=mode,
        started_at_utc=started_at_utc.isoformat(),
        finished_at_utc=finished_at_utc.isoformat(),
        target_date=target_date,
        seat_attempt_order=seat_attempt_order,
        chosen_seat=chosen_seat,
        status=status,
        summary=summary,
        otp_requested=otp_requested,
        otp_received=otp_received,
        screenshot_path=str(screenshot_path) if screenshot_path else None,
    )


def _record_non_booking_result(
    settings: Settings,
    store: RuntimeStateStore,
    *,
    mode: str,
    target_date: date | None,
    status: str,
    summary: str,
    scheduled_local_date: date | None = None,
    mark_scheduled_executed: bool = False,
    mark_catchup_handled: bool = False,
    chosen_seat: str | None = None,
) -> None:
    now = utc_now()
    state = store.load_scheduler_state()
    state.last_run_started_at_utc = now.isoformat()
    state.last_run_finished_at_utc = now.isoformat()
    state.last_run_status = status
    state.last_run_mode = mode
    state.last_run_message = summary
    if mark_scheduled_executed and scheduled_local_date is not None:
        state.last_scheduled_run_local_date = scheduled_local_date.isoformat()
        if target_date is not None:
            state.last_scheduled_target_date = target_date.strftime(settings.booking_date_format)
    if mark_catchup_handled and scheduled_local_date is not None:
        state.catchup_executed_for_local_date = scheduled_local_date.isoformat()
    state.in_progress_run_id = None
    store.save_scheduler_state(state)
    store.append_run_history(
        _build_run_history_entry(
            run_id=uuid.uuid4().hex,
            mode=mode,
            started_at_utc=now,
            finished_at_utc=now,
            target_date=(target_date.strftime(settings.booking_date_format) if target_date else None),
            seat_attempt_order=_seat_order(settings),
            chosen_seat=chosen_seat,
            status=status,
            summary=summary,
            otp_requested=False,
            otp_received=False,
            screenshot_path=None,
        ),
        limit=settings.run_history_limit,
    )


async def _execute_booking_run(
    settings: Settings,
    notifier: InteractiveNotifier,
    store: RuntimeStateStore,
    *,
    mode: str,
    scheduled_local_date: date | None = None,
    target_date: date | None = None,
) -> RunOnceOutcome:
    run_id = uuid.uuid4().hex
    started_at = utc_now()
    state = store.load_scheduler_state()
    try:
        store.acquire_run_lock(run_id, stale_after_sec=_run_lock_stale_after_sec(settings))
    except RunLockError as exc:
        LOGGER.warning("Could not acquire run lock: %s", exc)
        notifier.send(f"[workplace-booking] Run skipped: {exc}")
        return RunOnceOutcome(exit_code=1, attempt=0, error_message=str(exc))

    state.in_progress_run_id = run_id
    state.last_run_started_at_utc = started_at.isoformat()
    state.last_run_mode = mode
    store.save_scheduler_state(state)

    try:
        outcome = await run_once_detailed(settings, notifier, mode=mode)
        finished_at = utc_now()
        if outcome.result is not None:
            status = _result_status(outcome.result)
            summary = _result_summary(outcome.result)
            chosen_seat = _chosen_seat_from_result(outcome.result)
            screenshot_path = outcome.result.screenshot_path
            seat_attempt_order = _seat_order(settings)
            target_label = _single_target_label(settings)
        else:
            status = "failed"
            summary = outcome.error_message or "Booking run failed."
            chosen_seat = None
            screenshot_path = outcome.screenshot_path
            seat_attempt_order = _seat_order(settings)
            target_label = _single_target_label(settings)

        latest_state = store.load_scheduler_state()
        latest_state.last_run_started_at_utc = started_at.isoformat()
        latest_state.last_run_finished_at_utc = finished_at.isoformat()
        latest_state.last_run_status = status
        latest_state.last_run_mode = mode
        latest_state.last_run_message = summary
        if scheduled_local_date is not None:
            latest_state.last_scheduled_run_local_date = scheduled_local_date.isoformat()
            if target_date is not None:
                latest_state.last_scheduled_target_date = target_date.strftime(settings.booking_date_format)
            if mode == "scheduled_catchup":
                latest_state.catchup_executed_for_local_date = scheduled_local_date.isoformat()
        latest_state.in_progress_run_id = None
        store.save_scheduler_state(latest_state)

        store.append_run_history(
            _build_run_history_entry(
                run_id=run_id,
                mode=mode,
                started_at_utc=started_at,
                finished_at_utc=finished_at,
                target_date=target_label,
                seat_attempt_order=seat_attempt_order,
                chosen_seat=chosen_seat,
                status=status,
                summary=summary,
                otp_requested=outcome.otp_requested,
                otp_received=outcome.otp_received,
                screenshot_path=screenshot_path,
            ),
            limit=settings.run_history_limit,
        )
        return outcome
    finally:
        latest_state = store.load_scheduler_state()
        if latest_state.in_progress_run_id == run_id:
            latest_state.in_progress_run_id = None
            store.save_scheduler_state(latest_state)
        store.release_run_lock(run_id)


async def _execute_preflight(
    settings: Settings,
    notifier: InteractiveNotifier,
    store: RuntimeStateStore,
    *,
    mode: str,
    local_date: date | None,
) -> PreflightOutcome:
    run_id = uuid.uuid4().hex
    started_at = utc_now()
    state = store.load_scheduler_state()
    try:
        store.acquire_run_lock(run_id, stale_after_sec=_run_lock_stale_after_sec(settings))
    except RunLockError as exc:
        LOGGER.warning("Could not acquire run lock for preflight: %s", exc)
        notifier.send(f"[workplace-booking] Preflight skipped: {exc}")
        return PreflightOutcome(exit_code=1, error_message=str(exc))

    state.in_progress_run_id = run_id
    state.last_run_started_at_utc = started_at.isoformat()
    state.last_run_mode = mode
    store.save_scheduler_state(state)

    try:
        outcome = await run_preflight_once(settings, notifier, mode=mode)
        finished_at = utc_now()
        if outcome.result is not None:
            status = _preflight_status(outcome.result)
            summary = _preflight_summary(outcome.result)
        else:
            status = "failed"
            summary = outcome.error_message or "Preflight failed."

        latest_state = store.load_scheduler_state()
        latest_state.last_run_started_at_utc = started_at.isoformat()
        latest_state.last_run_finished_at_utc = finished_at.isoformat()
        latest_state.last_run_status = status
        latest_state.last_run_mode = mode
        latest_state.last_run_message = summary
        if local_date is not None:
            latest_state.last_preflight_local_date = local_date.isoformat()
            latest_state.last_preflight_status = status
            latest_state.last_preflight_message = summary
        latest_state.in_progress_run_id = None
        store.save_scheduler_state(latest_state)

        store.append_run_history(
            _build_run_history_entry(
                run_id=run_id,
                mode=mode,
                started_at_utc=started_at,
                finished_at_utc=finished_at,
                target_date=None,
                seat_attempt_order=_seat_order(settings),
                chosen_seat=None,
                status=status,
                summary=summary,
                otp_requested=False,
                otp_received=False,
                screenshot_path=None,
            ),
            limit=settings.run_history_limit,
        )
        return outcome
    finally:
        latest_state = store.load_scheduler_state()
        if latest_state.in_progress_run_id == run_id:
            latest_state.in_progress_run_id = None
            store.save_scheduler_state(latest_state)
        store.release_run_lock(run_id)


async def _execute_reauth(
    settings: Settings,
    notifier: InteractiveNotifier,
    store: RuntimeStateStore,
    *,
    mode: str,
) -> ReauthOutcome:
    run_id = uuid.uuid4().hex
    started_at = utc_now()
    state = store.load_scheduler_state()
    try:
        store.acquire_run_lock(run_id, stale_after_sec=_run_lock_stale_after_sec(settings))
    except RunLockError as exc:
        LOGGER.warning("Could not acquire run lock for auth refresh: %s", exc)
        notifier.send(f"[workplace-booking] Auth refresh skipped: {exc}")
        return ReauthOutcome(exit_code=1, error_message=str(exc))

    state.in_progress_run_id = run_id
    state.last_run_started_at_utc = started_at.isoformat()
    state.last_run_mode = mode
    store.save_scheduler_state(state)

    try:
        outcome = await run_reauth_once(settings, notifier, mode=mode)
        finished_at = utc_now()
        if outcome.result is not None:
            status = _reauth_status(outcome.result)
            summary = _reauth_summary(outcome.result)
        else:
            status = "failed"
            summary = outcome.error_message or "Auth refresh failed."

        latest_state = store.load_scheduler_state()
        latest_state.last_run_started_at_utc = started_at.isoformat()
        latest_state.last_run_finished_at_utc = finished_at.isoformat()
        latest_state.last_run_status = status
        latest_state.last_run_mode = mode
        latest_state.last_run_message = summary
        auth_local_date = _schedule_local_now(settings, finished_at).date()
        latest_state.last_preflight_local_date = auth_local_date.isoformat()
        latest_state.last_preflight_status = status
        latest_state.last_preflight_message = summary
        latest_state.in_progress_run_id = None
        store.save_scheduler_state(latest_state)

        store.append_run_history(
            _build_run_history_entry(
                run_id=run_id,
                mode=mode,
                started_at_utc=started_at,
                finished_at_utc=finished_at,
                target_date=None,
                seat_attempt_order=_seat_order(settings),
                chosen_seat=None,
                status=status,
                summary=summary,
                otp_requested=outcome.otp_requested,
                otp_received=outcome.otp_received,
                screenshot_path=outcome.screenshot_path,
            ),
            limit=settings.run_history_limit,
        )
        return outcome
    finally:
        latest_state = store.load_scheduler_state()
        if latest_state.in_progress_run_id == run_id:
            latest_state.in_progress_run_id = None
            store.save_scheduler_state(latest_state)
        store.release_run_lock(run_id)


async def _run_manual_booking_for_date(
    base_settings: Settings,
    notifier: InteractiveNotifier,
    store: RuntimeStateStore,
    target_date: date,
    target_seat: str | None = None,
) -> int:
    if _is_weekend_booking_date(base_settings, target_date):
        summary = (
            f"Manual booking skipped by policy for {target_date.strftime(base_settings.booking_date_format)} "
            f"({_weekday_short(target_date)}): weekend bookings are disabled."
        )
        notifier.send(f"[workplace-booking] {summary}")
        _record_non_booking_result(
            base_settings,
            store,
            mode="manual",
            target_date=target_date,
            status="skipped",
            summary=summary,
            mark_scheduled_executed=False,
        )
        return 0

    manual_settings = _settings_for_manual_request(
        base_settings,
        target=target_date,
        target_seat=target_seat,
    )
    notifier.send(
        "[workplace-booking] Manual booking requested\n"
        f"Date: {target_date.strftime(base_settings.booking_date_format)}\n"
        f"Seat mode: {_seat_order_label(manual_settings)}\n"
        f"Time: {base_settings.booking_time_from}-{base_settings.booking_time_to}"
    )
    outcome = await _execute_booking_run(
        manual_settings,
        notifier,
        store,
        mode="manual",
        target_date=target_date,
    )
    return outcome.exit_code

async def _handle_service_command(
    text: str,
    settings: Settings,
    notifier: InteractiveNotifier,
    store: RuntimeStateStore,
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

    if normalized in {BTN_STATUS} or normalized.startswith("/status") or normalized.startswith("/schedule"):
        notifier.send_reply_keyboard(
            _build_status_message(settings, store, next_run_utc)
            + "\n\n"
            + _build_selection_summary(settings, ui_state),
            _menu_keyboard_rows(),
        )
        return False, next_run_utc

    if normalized == BTN_PREFLIGHT or normalized.startswith("/preflight"):
        local_date = _schedule_local_now(settings).date()
        await _execute_preflight(settings, notifier, store, mode="preflight", local_date=local_date)
        return True, next_run_utc

    if (
        normalized == BTN_REAUTH
        or lowered.startswith("/reauth")
        or lowered.startswith("/auth")
    ):
        await _execute_reauth(settings, notifier, store, mode="reauth")
        return True, next_run_utc

    if normalized == BTN_LAST_RUN or normalized.startswith("/last"):
        notifier.send_reply_keyboard(
            _build_last_run_message(settings, store.read_last_history()),
            _menu_keyboard_rows(),
        )
        return False, next_run_utc

    if normalized == BTN_HISTORY or normalized.startswith("/history"):
        notifier.send_reply_keyboard(
            _build_history_message(settings, store.read_run_history(limit=10)),
            _menu_keyboard_rows(),
        )
        return False, next_run_utc

    if normalized.startswith("/cancelotp"):
        notifier.send_reply_keyboard(
            "[workplace-booking] No OTP wait is active right now. The command only affects a run that is currently waiting for OTP.",
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
            _seat_keyboard_rows(settings, ui_state),
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
            message=f"[workplace-booking] Seat selected\nSeat: {seat} (manual selection disables fallback)",
        )
        return False, next_run_utc

    if normalized.startswith("/run") and not normalized.startswith("/runtime"):
        target = _scheduled_target_date(settings)
        await _run_manual_booking_for_date(
            settings,
            notifier,
            store,
            target,
            target_seat=_selected_seat_override(ui_state),
        )
        return True, next_run_utc

    if normalized.startswith("/booknext") or normalized == BTN_RUN_NEXT:
        target = _scheduled_target_date(settings)
        await _run_manual_booking_for_date(
            settings,
            notifier,
            store,
            target,
            target_seat=_selected_seat_override(ui_state),
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
            store,
            target,
            target_seat=_selected_seat_override(ui_state),
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
            store,
            target,
            target_seat=_selected_seat_override(ui_state),
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

    alias_map = {
        "date": BTN_PICK_DATE,
        "seat": BTN_PICK_SEAT,
        "status": BTN_STATUS,
        "menu": BTN_MENU,
        "preflight": BTN_PREFLIGHT,
        "reauth": BTN_REAUTH,
        "auth": BTN_REAUTH,
        "last": BTN_LAST_RUN,
        "history": BTN_HISTORY,
    }
    if lowered in alias_map:
        return await _handle_service_command(
            alias_map[lowered],
            settings,
            notifier,
            store,
            next_run_utc,
            ui_state,
        )

    return False, next_run_utc

async def run_daemon(settings: Settings, notifier: InteractiveNotifier) -> int:
    store = _state_store(settings)
    LOGGER.info(
        "Running in daemon mode, interval %s minute(s).",
        settings.run_interval_minutes,
    )
    while True:
        await _execute_booking_run(settings, notifier, store, mode="daemon")
        sleep_seconds = settings.run_interval_minutes * 60
        LOGGER.info("Sleeping for %s seconds before next run.", sleep_seconds)
        await asyncio.sleep(sleep_seconds)


async def run_service(settings: Settings, notifier: InteractiveNotifier) -> int:
    store = _state_store(settings)
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
            f"Scheduled target date: {_scheduled_target_date_for_run(settings, next_run_utc).strftime(settings.booking_date_format)}\n"
            f"Preferred seats: {_seat_order_label(settings)}\n"
            "Use the buttons below or send /help for commands.",
            _menu_keyboard_rows(),
        )
    else:
        LOGGER.warning("Service mode is running without Telegram notifications/commands.")

    while True:
        now_utc = utc_now()
        state = store.load_scheduler_state()

        healthcheck_due, healthcheck_local_date = _is_healthcheck_due(settings, state, now_utc)
        if healthcheck_due:
            notifier.send(
                _build_healthcheck_message(settings, now_utc),
                critical=False,
            )
            latest_state = store.load_scheduler_state()
            if healthcheck_local_date is not None:
                latest_state.last_healthcheck_local_date = healthcheck_local_date.isoformat()
                store.save_scheduler_state(latest_state)
            await asyncio.sleep(1)
            continue

        preflight_due, preflight_local_date = _is_preflight_due(settings, state, now_utc)
        if preflight_due:
            await _execute_preflight(
                settings,
                notifier,
                store,
                mode="preflight",
                local_date=preflight_local_date,
            )
            continue

        if now_utc >= next_run_utc:
            scheduled_local_date = next_run_utc.astimezone(_schedule_timezone(settings)).date()
            target = _scheduled_target_date_for_run(settings, next_run_utc)
            if _is_weekend_booking_date(settings, target):
                summary = (
                    f"Scheduled run skipped by policy for {target.strftime(settings.booking_date_format)} "
                    f"({_weekday_short(target)}): weekend bookings are disabled."
                )
                LOGGER.info(summary)
                notifier.send(f"[workplace-booking] {summary}")
                _record_non_booking_result(
                    settings,
                    store,
                    mode="scheduled",
                    target_date=target,
                    status="skipped",
                    summary=summary,
                    scheduled_local_date=scheduled_local_date,
                    mark_scheduled_executed=True,
                )
            else:
                latest_state = store.load_scheduler_state()
                auth_block = _scheduled_auth_block_message(
                    settings,
                    latest_state,
                    scheduled_local_date,
                )
                if auth_block:
                    LOGGER.warning(auth_block)
                    notifier.send(f"[workplace-booking] {auth_block}", critical=True)
                    _record_non_booking_result(
                        settings,
                        store,
                        mode="scheduled",
                        target_date=target,
                        status="blocked",
                        summary=auth_block,
                        scheduled_local_date=scheduled_local_date,
                        mark_scheduled_executed=True,
                    )
                else:
                    scheduled_settings = _settings_for_single_date(settings, target)
                    await _execute_booking_run(
                        scheduled_settings,
                        notifier,
                        store,
                        mode="scheduled",
                        scheduled_local_date=scheduled_local_date,
                        target_date=target,
                    )
            next_run_utc = _next_scheduled_run_utc(settings, now_utc=utc_now() + timedelta(seconds=1))
            continue

        catchup = _compute_catchup_decision(settings, state, now_utc)
        if catchup.state == "pending":
            LOGGER.info(
                "Executing scheduled catch-up for local run date %s and target %s.",
                catchup.scheduled_local_date.isoformat(),
                catchup.target_date.strftime(settings.booking_date_format),
            )
            if _is_weekend_booking_date(settings, catchup.target_date):
                summary = (
                    f"Scheduled catch-up skipped by policy for {catchup.target_date.strftime(settings.booking_date_format)} "
                    f"({_weekday_short(catchup.target_date)}): weekend bookings are disabled."
                )
                notifier.send(f"[workplace-booking] {summary}")
                _record_non_booking_result(
                    settings,
                    store,
                    mode="scheduled_catchup",
                    target_date=catchup.target_date,
                    status="skipped",
                    summary=summary,
                    scheduled_local_date=catchup.scheduled_local_date,
                    mark_scheduled_executed=True,
                    mark_catchup_handled=True,
                )
            else:
                latest_state = store.load_scheduler_state()
                auth_block = _scheduled_auth_block_message(
                    settings,
                    latest_state,
                    catchup.scheduled_local_date,
                )
                if auth_block:
                    LOGGER.warning(auth_block)
                    notifier.send(f"[workplace-booking] {auth_block}", critical=True)
                    _record_non_booking_result(
                        settings,
                        store,
                        mode="scheduled_catchup",
                        target_date=catchup.target_date,
                        status="blocked",
                        summary=auth_block,
                        scheduled_local_date=catchup.scheduled_local_date,
                        mark_scheduled_executed=True,
                        mark_catchup_handled=True,
                    )
                else:
                    catchup_settings = _settings_for_single_date(settings, catchup.target_date)
                    await _execute_booking_run(
                        catchup_settings,
                        notifier,
                        store,
                        mode="scheduled_catchup",
                        scheduled_local_date=catchup.scheduled_local_date,
                        target_date=catchup.target_date,
                    )
            next_run_utc = _next_scheduled_run_utc(settings, now_utc=utc_now() + timedelta(seconds=1))
            continue

        if catchup.state == "expired":
            summary = (
                f"Missed scheduled run for {catchup.scheduled_local_date.strftime(settings.booking_date_format)} "
                f"(target {catchup.target_date.strftime(settings.booking_date_format)}) and catch-up window expired."
            )
            LOGGER.warning(summary)
            notifier.send(f"[workplace-booking] {summary}")
            _record_non_booking_result(
                settings,
                store,
                mode="scheduled_catchup",
                target_date=catchup.target_date,
                status="missed",
                summary=summary,
                scheduled_local_date=catchup.scheduled_local_date,
                mark_scheduled_executed=False,
                mark_catchup_handled=True,
            )
            continue

        if notifier.enabled:
            seconds_until_schedule = max(0.0, (next_run_utc - now_utc).total_seconds())
            poll_timeout = min(
                settings.telegram_command_poll_timeout_sec,
                max(1, int(seconds_until_schedule) if seconds_until_schedule else 1),
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
                    store=store,
                    next_run_utc=next_run_utc,
                    ui_state=ui_state,
                )
                if handled:
                    LOGGER.info("Handled Telegram command: %s", text)
        else:
            sleep_seconds = min(
                10,
                max(1, int((next_run_utc - now_utc).total_seconds())),
            )
            await asyncio.sleep(sleep_seconds)


def main() -> int:
    _load_env_files()
    settings = Settings.from_env()
    _configure_logging(settings.log_level)

    email_notifier = EmailNotifier(
        EmailSettings(
            smtp_host=settings.email_smtp_host,
            smtp_port=settings.email_smtp_port,
            smtp_username=settings.email_smtp_username,
            smtp_password=settings.email_smtp_password,
            email_from=settings.email_from,
            email_to=settings.email_to,
            starttls=settings.email_smtp_starttls,
        )
    )
    primary_notifier = TelegramNotifier(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        otp_reminder_interval_sec=settings.otp_reminder_interval_sec,
        proxy_enabled=settings.telegram_proxy_enabled,
        proxy_url=settings.telegram_proxy_url,
    )
    notifier = FallbackNotifier(
        primary_notifier,
        email_notifier,
        email_fallback_enabled=settings.email_fallback_enabled,
    )
    primary_notifier.transport_issue_reporter = notifier.report_transport_issue

    LOGGER.info("Telegram notifications enabled: %s", notifier.enabled)
    LOGGER.info(
        "Telegram proxy enabled: %s",
        bool(settings.telegram_proxy_enabled and settings.telegram_proxy_url),
    )
    LOGGER.info(
        "Email fallback enabled: %s",
        bool(settings.email_fallback_enabled and email_notifier.enabled),
    )

    if settings.run_mode == "daemon":
        return asyncio.run(run_daemon(settings, notifier))
    if settings.run_mode == "service":
        return asyncio.run(run_service(settings, notifier))
    store = _state_store(settings)
    return asyncio.run(_execute_booking_run(settings, notifier, store, mode="once")).exit_code


if __name__ == "__main__":
    raise SystemExit(main())
