from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


DEFAULT_LOGIN_USERNAME_SELECTORS = [
    'input[type="email"]',
    'input[name="username"]',
    'input[id*="user"]',
    'input[type="text"]',
]

DEFAULT_LOGIN_PASSWORD_SELECTORS = [
    'input[type="password"]',
]

DEFAULT_LOGIN_SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("Sign in")',
    'button:has-text("Log in")',
    'button:has-text("Continue")',
]

DEFAULT_PRE_LOGIN_CLICK_TEXTS = [
    "Log in using SAML",
    "Sign in",
]

DEFAULT_BOOK_BUTTON_TEXTS = [
    "Book",
    "Reserve",
    "Confirm",
]


def _env_optional(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_required(name: str) -> str:
    value = _env_optional(name)
    if not value:
        raise ValueError(f"Environment variable {name} is required.")
    return value


def _env_bool(name: str, default: bool) -> bool:
    value = _env_optional(name)
    if value is None:
        return default
    normalized = value.lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Environment variable {name} must be boolean.")


def _env_int(name: str, default: int, min_value: int = 0) -> int:
    value = _env_optional(name)
    if value is None:
        return default
    parsed = int(value)
    if parsed < min_value:
        raise ValueError(f"Environment variable {name} must be >= {min_value}.")
    return parsed


def _env_optional_int(name: str, min_value: int | None = None) -> int | None:
    value = _env_optional(name)
    if value is None:
        return None
    parsed = int(value)
    if min_value is not None and parsed < min_value:
        raise ValueError(f"Environment variable {name} must be >= {min_value}.")
    return parsed


def _env_list(name: str, default: list[str]) -> list[str]:
    value = _env_optional(name)
    if value is None:
        return list(default)
    items = [item.strip() for item in value.split("|") if item.strip()]
    return items or list(default)


@dataclass(frozen=True)
class Settings:
    booking_url: str
    username: str | None
    password: str | None
    target_office: str
    target_seat: str

    pre_login_click_selectors: list[str]
    pre_login_click_texts: list[str]
    pre_login_click_timeout_ms: int
    otp_code_input_selector: str | None
    otp_code_value: str | None
    otp_wait_timeout_ms: int

    office_choose_selector: str | None
    office_open_selector: str | None
    office_option_selector_template: str | None
    office_map_ready_selector: str | None
    office_map_wait_timeout_ms: int
    office_map_extra_wait_ms: int
    office_map_loading_selectors: list[str]
    office_map_loading_wait_timeout_ms: int

    booking_params_open_selector: str | None
    booking_params_apply_selector: str | None
    booking_params_close_selector: str | None
    booking_date_input_selector: str | None
    booking_date_day_selector_template: str | None
    booking_calendar_next_selector: str | None
    booking_date_values: list[str]
    booking_date_value: str | None
    booking_date_offset_days: int | None
    booking_range_days: int
    booking_include_today: bool
    booking_skip_weekends: bool
    booking_per_date_attempts: int
    booking_date_apply_wait_timeout_ms: int
    booking_use_url_date_fallback: bool
    booking_date_format: str
    booking_type_selector: str | None
    booking_type_option_selector: str | None
    booking_type_value: str | None
    booking_time_from_selector: str | None
    booking_time_to_selector: str | None
    booking_time_from: str | None
    booking_time_to: str | None
    booking_local_utc_offset: str

    seat_search_selector: str | None
    target_table_id: str | None
    booking_use_api_submit_fallback: bool
    seat_selector_template: str | None
    seat_canvas_selector: str | None
    seat_canvas_index: int | None
    seat_canvas_x: int | None
    seat_canvas_y: int | None
    book_button_selector: str | None
    book_button_texts: list[str]
    success_selector: str | None
    success_close_selector: str | None
    success_text: str | None

    login_username_selectors: list[str]
    login_password_selectors: list[str]
    login_submit_selectors: list[str]
    login_success_selector: str | None
    page_ready_selector: str | None

    headless: bool
    default_timeout_ms: int
    ui_pause_ms: int
    retry_attempts: int
    retry_delay_sec: int

    run_mode: str
    run_interval_minutes: int
    schedule_time_local: str
    schedule_local_utc_offset: str
    telegram_command_poll_timeout_sec: int

    storage_state_path: Path
    screenshot_dir: Path

    telegram_bot_token: str | None
    telegram_chat_id: str | None
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        run_mode = _env_optional("RUN_MODE") or "once"
        if run_mode not in {"once", "daemon", "service"}:
            raise ValueError("RUN_MODE must be one of: once, daemon, service")

        storage_state_path = Path(
            _env_optional("STORAGE_STATE_PATH") or ".state/storage_state.json"
        )
        screenshot_dir = Path(_env_optional("SCREENSHOT_DIR") or ".state/screenshots")
        storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_dir.mkdir(parents=True, exist_ok=True)

        return cls(
            booking_url=_env_optional("BOOKING_URL")
            or "https://lemana.simple-office-web.liis.su/",
            username=_env_optional("USERNAME"),
            password=_env_optional("PASSWORD"),
            target_office=_env_required("TARGET_OFFICE"),
            target_seat=_env_required("TARGET_SEAT"),
            pre_login_click_selectors=_env_list("PRE_LOGIN_CLICK_SELECTORS", default=[]),
            pre_login_click_texts=_env_list(
                "PRE_LOGIN_CLICK_TEXTS",
                default=DEFAULT_PRE_LOGIN_CLICK_TEXTS,
            ),
            pre_login_click_timeout_ms=_env_int(
                "PRE_LOGIN_CLICK_TIMEOUT_MS",
                4_000,
                min_value=0,
            ),
            otp_code_input_selector=_env_optional("OTP_CODE_INPUT_SELECTOR"),
            otp_code_value=_env_optional("OTP_CODE_VALUE"),
            otp_wait_timeout_ms=_env_int(
                "OTP_WAIT_TIMEOUT_MS",
                120_000,
                min_value=1_000,
            ),
            office_choose_selector=_env_optional("OFFICE_CHOOSE_SELECTOR"),
            office_open_selector=_env_optional("OFFICE_OPEN_SELECTOR"),
            office_option_selector_template=_env_optional(
                "OFFICE_OPTION_SELECTOR_TEMPLATE"
            ),
            office_map_ready_selector=_env_optional("OFFICE_MAP_READY_SELECTOR"),
            office_map_wait_timeout_ms=_env_int(
                "OFFICE_MAP_WAIT_TIMEOUT_MS",
                60_000,
                min_value=1_000,
            ),
            office_map_extra_wait_ms=_env_int(
                "OFFICE_MAP_EXTRA_WAIT_MS",
                0,
                min_value=0,
            ),
            office_map_loading_selectors=_env_list(
                "OFFICE_MAP_LOADING_SELECTORS",
                default=[],
            ),
            office_map_loading_wait_timeout_ms=_env_int(
                "OFFICE_MAP_LOADING_WAIT_TIMEOUT_MS",
                60_000,
                min_value=1_000,
            ),
            booking_params_open_selector=_env_optional("BOOKING_PARAMS_OPEN_SELECTOR"),
            booking_params_apply_selector=_env_optional("BOOKING_PARAMS_APPLY_SELECTOR"),
            booking_params_close_selector=_env_optional("BOOKING_PARAMS_CLOSE_SELECTOR"),
            booking_date_input_selector=_env_optional("BOOKING_DATE_INPUT_SELECTOR"),
            booking_date_day_selector_template=_env_optional(
                "BOOKING_DATE_DAY_SELECTOR_TEMPLATE"
            ),
            booking_calendar_next_selector=_env_optional(
                "BOOKING_CALENDAR_NEXT_SELECTOR"
            ),
            booking_date_values=_env_list("BOOKING_DATE_VALUES", default=[]),
            booking_date_value=_env_optional("BOOKING_DATE_VALUE"),
            booking_date_offset_days=_env_optional_int(
                "BOOKING_DATE_OFFSET_DAYS",
                min_value=0,
            ),
            booking_range_days=_env_int(
                "BOOKING_RANGE_DAYS",
                7,
                min_value=0,
            ),
            booking_include_today=_env_bool("BOOKING_INCLUDE_TODAY", True),
            booking_skip_weekends=_env_bool("BOOKING_SKIP_WEEKENDS", True),
            booking_per_date_attempts=_env_int(
                "BOOKING_PER_DATE_ATTEMPTS",
                2,
                min_value=1,
            ),
            booking_date_apply_wait_timeout_ms=_env_int(
                "BOOKING_DATE_APPLY_WAIT_TIMEOUT_MS",
                15_000,
                min_value=1_000,
            ),
            booking_use_url_date_fallback=_env_bool(
                "BOOKING_USE_URL_DATE_FALLBACK",
                True,
            ),
            booking_date_format=_env_optional("BOOKING_DATE_FORMAT") or "%d.%m.%Y",
            booking_type_selector=_env_optional("BOOKING_TYPE_SELECTOR"),
            booking_type_option_selector=_env_optional("BOOKING_TYPE_OPTION_SELECTOR"),
            booking_type_value=_env_optional("BOOKING_TYPE_VALUE"),
            booking_time_from_selector=_env_optional("BOOKING_TIME_FROM_SELECTOR"),
            booking_time_to_selector=_env_optional("BOOKING_TIME_TO_SELECTOR"),
            booking_time_from=_env_optional("BOOKING_TIME_FROM"),
            booking_time_to=_env_optional("BOOKING_TIME_TO"),
            booking_local_utc_offset=(
                _env_optional("BOOKING_LOCAL_UTC_OFFSET") or "+03:00"
            ),
            seat_search_selector=_env_optional("SEAT_SEARCH_SELECTOR"),
            target_table_id=_env_optional("TARGET_TABLE_ID"),
            booking_use_api_submit_fallback=_env_bool(
                "BOOKING_USE_API_SUBMIT_FALLBACK",
                True,
            ),
            seat_selector_template=_env_optional("SEAT_SELECTOR_TEMPLATE"),
            seat_canvas_selector=_env_optional("SEAT_CANVAS_SELECTOR"),
            seat_canvas_index=_env_optional_int("SEAT_CANVAS_INDEX", min_value=0),
            seat_canvas_x=_env_optional_int("SEAT_CANVAS_X", min_value=0),
            seat_canvas_y=_env_optional_int("SEAT_CANVAS_Y", min_value=0),
            book_button_selector=_env_optional("BOOK_BUTTON_SELECTOR"),
            book_button_texts=_env_list(
                "BOOK_BUTTON_TEXTS", default=DEFAULT_BOOK_BUTTON_TEXTS
            ),
            success_selector=_env_optional("SUCCESS_SELECTOR"),
            success_close_selector=_env_optional("SUCCESS_CLOSE_SELECTOR"),
            success_text=_env_optional("SUCCESS_TEXT"),
            login_username_selectors=_env_list(
                "LOGIN_USERNAME_SELECTORS", default=DEFAULT_LOGIN_USERNAME_SELECTORS
            ),
            login_password_selectors=_env_list(
                "LOGIN_PASSWORD_SELECTORS", default=DEFAULT_LOGIN_PASSWORD_SELECTORS
            ),
            login_submit_selectors=_env_list(
                "LOGIN_SUBMIT_SELECTORS", default=DEFAULT_LOGIN_SUBMIT_SELECTORS
            ),
            login_success_selector=_env_optional("LOGIN_SUCCESS_SELECTOR"),
            page_ready_selector=_env_optional("PAGE_READY_SELECTOR"),
            headless=_env_bool("HEADLESS", True),
            default_timeout_ms=_env_int("DEFAULT_TIMEOUT_MS", 30_000, min_value=1000),
            ui_pause_ms=_env_int("UI_PAUSE_MS", 500, min_value=0),
            retry_attempts=_env_int("RETRY_ATTEMPTS", 3, min_value=1),
            retry_delay_sec=_env_int("RETRY_DELAY_SEC", 30, min_value=0),
            run_mode=run_mode,
            run_interval_minutes=_env_int("RUN_INTERVAL_MINUTES", 30, min_value=1),
            schedule_time_local=_env_optional("SCHEDULE_TIME_LOCAL") or "00:01",
            schedule_local_utc_offset=(
                _env_optional("SCHEDULE_LOCAL_UTC_OFFSET")
                or (_env_optional("BOOKING_LOCAL_UTC_OFFSET") or "+03:00")
            ),
            telegram_command_poll_timeout_sec=_env_int(
                "TELEGRAM_COMMAND_POLL_TIMEOUT_SEC",
                12,
                min_value=1,
            ),
            storage_state_path=storage_state_path,
            screenshot_dir=screenshot_dir,
            telegram_bot_token=_env_optional("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_env_optional("TELEGRAM_CHAT_ID"),
            log_level=(_env_optional("LOG_LEVEL") or "INFO").upper(),
        )
