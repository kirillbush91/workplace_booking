from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import re
from time import monotonic
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Request,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from .config import Settings
from .telegram_client import TelegramNotifier


LOGGER = logging.getLogger(__name__)

OTP_HINT_SNIPPETS = [
    "one-time code",
    "verification code",
    "otp",
    "\u043e\u0434\u043d\u043e\u0440\u0430\u0437\u043e\u0432",
    "\u044f\u043d\u0434\u0435\u043a\u0441 id",
]

MAP_MARKER_API_PATHS = (
    "/api/web/floor/table_markers",
    "/api/web/floor/room_markers",
)


class BookingError(RuntimeError):
    def __init__(self, message: str, screenshot_path: Path | None = None) -> None:
        super().__init__(message)
        self.screenshot_path = screenshot_path


class DaySkipError(RuntimeError):
    pass


@dataclass(frozen=True)
class DayBookingResult:
    date: str
    status: str
    message: str
    attempt: int
    screenshot_path: Path | None


@dataclass(frozen=True)
class BookingResult:
    started_at: datetime
    finished_at: datetime
    office: str
    seat: str
    screenshot_path: Path | None
    booked_dates: list[str]
    skipped_dates: list[str]
    failed_dates: list[str]
    day_results: list[DayBookingResult]


@dataclass(frozen=True)
class MarkerRequestEvent:
    path: str
    method: str
    date_from: str | None
    date_to: str | None
    floor: str | None
    room_type: str | None
    captured_at: float


@dataclass(frozen=True)
class BookingWindow:
    date_from: str
    date_to: str
    floor: str | None
    room_type: str | None


class BookingBot:
    def __init__(
        self,
        settings: Settings,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self.settings = settings
        self.notifier = notifier
        self._marker_requests: list[MarkerRequestEvent] = []
        self._resolved_target_table_id: str | None = None
        self._api_authorization_header: str | None = None
        self._storage_state_saved_at_least_once = False

    async def book(self) -> BookingResult:
        started_at = datetime.now(timezone.utc)
        browser: Browser | None = None
        context: BrowserContext | None = None
        page: Page | None = None
        screenshot: Path | None = None

        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=self.settings.headless)
                context = await self._new_context(browser)
                page = await context.new_page()
                page.on("close", lambda: LOGGER.debug("Playwright page was closed."))
                self._attach_page_trackers(page)
                page.set_default_timeout(self.settings.default_timeout_ms)

                LOGGER.info("Opening booking page: %s", self.settings.booking_url)
                await page.goto(self.settings.booking_url, wait_until="domcontentloaded")
                await self._pause(page)

                if self.settings.page_ready_selector:
                    await page.locator(self.settings.page_ready_selector).first.wait_for(
                        state="visible", timeout=self.settings.default_timeout_ms
                    )

                await self._perform_pre_login_actions(page)
                await self._login_if_needed(page)
                await self._persist_storage_state_safe(
                    context,
                    phase="post-login",
                    target_closed_level="debug",
                )
                await self._select_office(page)
                target_dates = self._resolve_target_dates()
                if not target_dates:
                    raise RuntimeError("No target dates to process.")
                LOGGER.info(
                    "Target dates: %s",
                    ", ".join(d.strftime(self.settings.booking_date_format) for d in target_dates),
                )

                day_results: list[DayBookingResult] = []
                for target in target_dates:
                    day_result = await self._book_single_date(page, target)
                    day_results.append(day_result)
                    if day_result.screenshot_path is not None:
                        screenshot = day_result.screenshot_path

                booked_dates = [r.date for r in day_results if r.status == "booked"]
                skipped_dates = [r.date for r in day_results if r.status == "skipped"]
                failed_dates = [r.date for r in day_results if r.status == "failed"]
                if failed_dates:
                    raise RuntimeError(
                        "One or more target dates failed. "
                        f"Skipped dates: {', '.join(skipped_dates) or 'n/a'}. "
                        f"Failed dates: {', '.join(failed_dates)}"
                    )
                if not booked_dates and skipped_dates:
                    LOGGER.info(
                        "No new bookings were created; all target dates were skipped "
                        "(already booked/unavailable)."
                    )

                await self._persist_storage_state_safe(
                    context,
                    phase="before-return",
                    target_closed_level="debug",
                )

                finished_at = datetime.now(timezone.utc)
                return BookingResult(
                    started_at=started_at,
                    finished_at=finished_at,
                    office=self.settings.target_office,
                    seat=self.settings.target_seat,
                    screenshot_path=screenshot,
                    booked_dates=booked_dates,
                    skipped_dates=skipped_dates,
                    failed_dates=failed_dates,
                    day_results=day_results,
                )
        except Exception as exc:
            if page is not None and not page.is_closed():
                try:
                    screenshot = await self._capture_screenshot(page, "error")
                except Exception:
                    LOGGER.exception("Failed to capture error screenshot.")
            raise BookingError(
                f"{exc.__class__.__name__}: {exc}",
                screenshot_path=screenshot,
            ) from exc
        finally:
            if context is not None:
                await self._persist_storage_state_safe(
                    context,
                    phase="finalize",
                    target_closed_level=(
                        "debug" if self._storage_state_saved_at_least_once else "warning"
                    ),
                )
                try:
                    await context.close()
                except Exception as exc:
                    if self._is_target_closed_exception(exc):
                        LOGGER.debug("Browser context was already closed.")
                    else:
                        LOGGER.exception("Failed to close browser context cleanly.")
            if browser is not None:
                try:
                    await browser.close()
                except Exception as exc:
                    if self._is_target_closed_exception(exc):
                        LOGGER.debug("Browser was already closed.")
                    else:
                        LOGGER.exception("Failed to close browser cleanly.")

    async def _persist_storage_state_safe(
        self,
        context: BrowserContext,
        *,
        phase: str,
        target_closed_level: str = "warning",
    ) -> bool:
        try:
            await context.storage_state(path=str(self.settings.storage_state_path))
            self._storage_state_saved_at_least_once = True
            LOGGER.debug("Browser storage state persisted (%s): %s", phase, self.settings.storage_state_path)
            return True
        except Exception as exc:
            if self._is_target_closed_exception(exc):
                message = (
                    "Skipping storage state persist because browser context is already closed "
                    f"({phase})."
                )
                if target_closed_level == "debug":
                    LOGGER.debug(message)
                else:
                    LOGGER.warning(message)
                return False
            LOGGER.exception("Failed to persist browser storage state (%s).", phase)
            return False

    async def _new_context(self, browser: Browser) -> BrowserContext:
        if self.settings.storage_state_path.exists():
            LOGGER.info("Using saved auth state: %s", self.settings.storage_state_path)
            try:
                return await browser.new_context(
                    storage_state=str(self.settings.storage_state_path)
                )
            except Exception:
                LOGGER.exception(
                    "Saved auth state is invalid, starting with a fresh browser context."
                )
        return await browser.new_context()

    async def _login_if_needed(self, page: Page) -> None:
        username_entry = await self._first_visible_selector(
            page=page,
            selectors=self.settings.login_username_selectors,
            total_timeout_ms=5_000,
        )
        password_entry = await self._first_visible_selector(
            page=page,
            selectors=self.settings.login_password_selectors,
            total_timeout_ms=5_000,
        )

        if username_entry is None and password_entry is None:
            LOGGER.info("Login form not detected. Continuing with current session.")
            if self.notifier and self.notifier.enabled:
                await asyncio.to_thread(
                    self.notifier.send,
                    "[workplace-booking] Login form not detected. Reusing saved session.",
                )
            return

        username_input = username_entry[1] if username_entry else None
        password_input = password_entry[1] if password_entry else None

        if (
            self.settings.username
            and self.settings.password
            and username_input is not None
            and password_input is not None
        ):
            LOGGER.info("Login form detected. Filling credentials.")
            if self.notifier and self.notifier.enabled:
                await asyncio.to_thread(
                    self.notifier.send,
                    "[workplace-booking] Login form detected. Filling LDAP/password automatically.",
                )
            await username_input.fill(self.settings.username)
            await password_input.fill(self.settings.password)
        else:
            LOGGER.info(
                "Login form detected, trying submit without credential fill "
                "(useful for SSO/pre-filled auth)."
            )

        submit_entry = await self._first_visible_selector(
            page=page,
            selectors=self.settings.login_submit_selectors,
            total_timeout_ms=3_000,
        )
        if submit_entry is not None:
            _, submit_button = submit_entry
            await self._click_locator(page, submit_button)
        elif password_input is not None:
            await password_input.press("Enter")
        else:
            LOGGER.info("Login submit not found, continuing.")

        await page.wait_for_load_state("domcontentloaded")
        await self._pause(page)
        await self._handle_otp_if_needed(page)

        if self.settings.login_success_selector:
            await page.locator(self.settings.login_success_selector).first.wait_for(
                state="visible", timeout=self.settings.default_timeout_ms
            )

    async def _handle_otp_if_needed(self, page: Page) -> None:
        selector = self.settings.otp_code_input_selector
        if not selector:
            return

        otp_input = page.locator(selector).first
        try:
            await otp_input.wait_for(state="visible", timeout=5_000)
        except PlaywrightTimeoutError:
            return

        otp_screen_hints_found = await self._otp_screen_hints_present(page)
        if not otp_screen_hints_found:
            LOGGER.info(
                "OTP selector matched but OTP screen hints were not found. "
                "Skipping OTP handling on this screen."
            )
            return

        # Some pages reuse the same input component for LDAP login and OTP.
        # Treat OTP as valid only after password input disappeared.
        password_still_visible = await self._first_visible_selector(
            page=page,
            selectors=self.settings.login_password_selectors,
            total_timeout_ms=1_200,
        )
        if password_still_visible is not None:
            LOGGER.info(
                "OTP selector matched while password input is still visible. "
                "Skipping OTP handling on this screen."
            )
            return

        LOGGER.info("OTP input detected.")
        if self.settings.otp_code_value:
            # Some OTP pages render one input, some render N inputs.
            # Focusing the first input and typing raw code works for both patterns.
            raw_code = self.settings.otp_code_value.replace(" ", "")
            await otp_input.click()
            await page.keyboard.press("Control+A")
            await page.keyboard.type(raw_code)
        elif self.notifier and self.notifier.enabled:
            code = await asyncio.to_thread(
                self.notifier.wait_for_otp_code,
                max(1, self.settings.otp_wait_timeout_ms // 1000),
            )
            if code:
                await otp_input.click()
                await page.keyboard.press("Control+A")
                await page.keyboard.type(code)
            else:
                LOGGER.warning(
                    "OTP code was not received from Telegram in time. "
                    "Waiting for manual OTP entry in browser."
                )
        else:
            LOGGER.info(
                "Waiting for manual OTP entry up to %s ms.",
                self.settings.otp_wait_timeout_ms,
            )

        try:
            await otp_input.wait_for(
                state="hidden",
                timeout=self.settings.otp_wait_timeout_ms,
            )
        except PlaywrightTimeoutError:
            if "/offices" not in page.url:
                raise RuntimeError(
                    "OTP step did not complete in time. "
                    "Set OTP_CODE_VALUE or increase OTP_WAIT_TIMEOUT_MS."
                ) from None

        await page.wait_for_load_state("domcontentloaded")
        await self._pause(page)

    async def _otp_screen_hints_present(self, page: Page) -> bool:
        try:
            body_text = await page.locator("body").inner_text(timeout=2_500)
        except Exception:
            return False

        normalized = " ".join(body_text.split()).lower()
        return any(hint in normalized for hint in OTP_HINT_SNIPPETS)

    async def _perform_pre_login_actions(self, page: Page) -> None:
        if not self.settings.pre_login_click_selectors and not self.settings.pre_login_click_texts:
            return

        LOGGER.info("Running pre-login click actions (for SSO entry, if visible).")
        timeout_ms = self.settings.pre_login_click_timeout_ms

        for selector in self.settings.pre_login_click_selectors:
            try:
                locator = page.locator(selector).first
                await locator.wait_for(state="visible", timeout=timeout_ms)
                LOGGER.info("Clicking pre-login selector: %s", selector)
                await self._click_locator(page, locator)
                await page.wait_for_load_state("domcontentloaded")
            except Exception:
                continue

        for text in self.settings.pre_login_click_texts:
            try:
                locator = page.get_by_text(text, exact=False).first
                await locator.wait_for(state="visible", timeout=timeout_ms)
                LOGGER.info("Clicking pre-login text: %s", text)
                await self._click_locator(page, locator)
                await page.wait_for_load_state("domcontentloaded")
            except Exception:
                continue

    async def _select_office(self, page: Page) -> None:
        LOGGER.info("Selecting office: %s", self.settings.target_office)
        current_office_id = self._current_office_id_from_map_url(page.url)
        if current_office_id:
            target_office_id = self._target_office_id_from_settings()
            if target_office_id is None or target_office_id == current_office_id:
                LOGGER.info(
                    "Already on map with office_id=%s. Skipping office selection click.",
                    current_office_id,
                )
                await self._wait_for_office_map_ready(page)
                return

            LOGGER.info(
                "Map is already open with office_id=%s, but target office_id=%s. "
                "Navigating to /offices for explicit office selection.",
                current_office_id,
                target_office_id,
            )
            parsed = urlparse(page.url)
            offices_url = urlunparse(parsed._replace(path="/offices", query="", fragment=""))
            await page.goto(offices_url, wait_until="domcontentloaded")
            await self._pause(page)

        if self.settings.office_choose_selector:
            await self._click_selector(page, self.settings.office_choose_selector)
            await self._wait_for_office_map_ready(page)
            return

        if self.settings.office_open_selector:
            await self._click_selector(page, self.settings.office_open_selector)

        if self.settings.office_option_selector_template:
            selector = self._format_selector(self.settings.office_option_selector_template)
            await self._click_selector(page, selector)
            await self._wait_for_office_map_ready(page)
            return

        await self._click_text(page, self.settings.target_office)
        await self._wait_for_office_map_ready(page)

    @staticmethod
    def _current_office_id_from_map_url(url: str) -> str | None:
        parsed = urlparse(url)
        if "/map" not in parsed.path:
            return None
        query = parse_qs(parsed.query)
        raw = query.get("office_id")
        if not raw:
            return None
        value = str(raw[0]).strip()
        return value or None

    def _target_office_id_from_settings(self) -> str | None:
        selector = self.settings.office_choose_selector or ""
        match = re.search(
            r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12})",
            selector,
        )
        if match:
            return match.group(1).lower()
        return None

    async def _book_single_date(self, page: Page, target: date) -> DayBookingResult:
        date_label = target.strftime(self.settings.booking_date_format)
        max_attempts = self.settings.booking_per_date_attempts

        if self.settings.booking_skip_weekends and target.weekday() >= 5:
            result = DayBookingResult(
                date=date_label,
                status="skipped",
                message=(
                    "Weekend date skipped by policy "
                    "(BOOKING_SKIP_WEEKENDS=true)."
                ),
                attempt=1,
                screenshot_path=None,
            )
            await self._notify_day_result(result)
            return result

        for attempt in range(1, max_attempts + 1):
            LOGGER.info("Processing date %s (attempt %s/%s).", date_label, attempt, max_attempts)
            try:
                if self.settings.booking_use_api_submit_fallback:
                    await self._configure_booking_parameters(
                        page,
                        target_date=target,
                        apply_optional_ui=False,
                    )
                    try:
                        await self._submit_booking_via_api(page, target_date=target)
                        screenshot = await self._capture_success_screenshot(
                            page,
                            f"success_api_{target.strftime('%Y%m%d')}",
                            target_date=target,
                        )
                        result = DayBookingResult(
                            date=date_label,
                            status="booked",
                            message="Booking created via API.",
                            attempt=attempt,
                            screenshot_path=screenshot,
                        )
                        await self._notify_day_result(result)
                        return result
                    except DaySkipError:
                        raise
                    except Exception as api_exc:
                        if page.is_closed():
                            raise
                        if self.settings.target_table_id:
                            # If exact table UUID is configured, API path is the source of truth.
                            raise
                        if "set target_table_id" in str(api_exc).lower():
                            raise DaySkipError(str(api_exc)) from api_exc
                        LOGGER.warning(
                            "API booking path failed for %s: %s: %s. "
                            "Falling back to UI flow.",
                            date_label,
                            api_exc.__class__.__name__,
                            api_exc,
                        )

                await self._configure_booking_parameters(
                    page,
                    target_date=target,
                    apply_optional_ui=True,
                )
                await self._select_seat(page)
                await self._submit_booking(page)
                await self._wait_for_success(page)
                screenshot = await self._capture_success_screenshot(
                    page,
                    f"success_{target.strftime('%Y%m%d')}",
                    target_date=target,
                )
                await self._close_success_modal_if_present(page)
                result = DayBookingResult(
                    date=date_label,
                    status="booked",
                    message="Booking created via UI.",
                    attempt=attempt,
                    screenshot_path=screenshot,
                )
                await self._notify_day_result(result)
                return result
            except DaySkipError as exc:
                result = DayBookingResult(
                    date=date_label,
                    status="skipped",
                    message=str(exc),
                    attempt=attempt,
                    screenshot_path=None,
                )
                await self._notify_day_result(result)
                await self._recover_after_day_attempt(page)
                return result
            except Exception as exc:
                if page.is_closed():
                    raise
                LOGGER.warning(
                    "Date %s attempt %s failed: %s: %s",
                    date_label,
                    attempt,
                    exc.__class__.__name__,
                    exc,
                )
                await self._recover_after_day_attempt(page)
                if attempt >= max_attempts:
                    result = DayBookingResult(
                        date=date_label,
                        status="failed",
                        message=f"{exc.__class__.__name__}: {exc}",
                        attempt=attempt,
                        screenshot_path=None,
                    )
                    await self._notify_day_result(result)
                    return result
        # Defensive fallback.
        return DayBookingResult(
            date=date_label,
            status="failed",
            message="Unexpected date processing state.",
            attempt=max_attempts,
            screenshot_path=None,
        )

    async def _notify_day_result(self, result: DayBookingResult) -> None:
        LOGGER.info(
            "Day result: date=%s status=%s attempt=%s message=%s",
            result.date,
            result.status,
            result.attempt,
            result.message,
        )
        if not (self.notifier and self.notifier.enabled):
            return
        icon = {"booked": "✅", "skipped": "⚪", "failed": "❌"}.get(result.status, "ℹ️")
        message = (
            "[workplace-booking] Day result\n"
            f"{icon} Date: {result.date}\n"
            f"Status: {result.status}\n"
            f"Attempt: {result.attempt}\n"
            f"Message: {result.message}"
        )
        await asyncio.to_thread(self.notifier.send, message)

    async def _recover_after_day_attempt(self, page: Page) -> None:
        if page.is_closed():
            return
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await self._pause(page)

    async def _close_success_modal_if_present(self, page: Page) -> None:
        if page.is_closed():
            return
        if self.settings.success_close_selector:
            try:
                await self._click_selector(page, self.settings.success_close_selector)
                return
            except Exception:
                pass
        for text in ("Закрыть", "Close", "Done", "OK", "ОК"):
            try:
                await self._click_text(page, text, timeout_ms=2_000)
                return
            except Exception:
                continue

    async def _configure_booking_parameters(
        self,
        page: Page,
        target_date: date | None = None,
        apply_optional_ui: bool = True,
    ) -> None:
        has_any_param = any(
            [
                self.settings.booking_date_input_selector,
                self.settings.booking_date_values,
                self.settings.booking_date_value,
                self.settings.booking_date_offset_days is not None,
                self.settings.booking_use_url_date_fallback,
                apply_optional_ui
                and self.settings.booking_params_open_selector,
                apply_optional_ui
                and self.settings.booking_type_selector
                and (
                    self.settings.booking_type_option_selector
                    or self.settings.booking_type_value
                ),
                apply_optional_ui
                and self.settings.booking_time_from_selector
                and self.settings.booking_time_from,
                apply_optional_ui
                and self.settings.booking_time_to_selector
                and self.settings.booking_time_to,
            ]
        )
        if not has_any_param:
            return

        LOGGER.info("Configuring booking parameters.")
        if apply_optional_ui and self.settings.booking_params_open_selector:
            try:
                await self._click_selector(page, self.settings.booking_params_open_selector)
            except RuntimeError as exc:
                LOGGER.warning(
                    "Booking params opener did not respond, trying direct controls: %s",
                    exc,
                )
                opened = await self._try_open_date_picker_by_text(page)
                if opened:
                    LOGGER.info("Booking params/date picker opened by fallback text search.")

        resolved_target_date = self._resolve_booking_date(target_date)
        if resolved_target_date:
            selected = await self._select_booking_date(
                page=page,
                target_date=resolved_target_date,
            )
            if not selected:
                raise DaySkipError(
                    f"Date {resolved_target_date.strftime(self.settings.booking_date_format)} "
                    "is not available in calendar."
                )

        if not apply_optional_ui:
            return

        if self.settings.booking_type_selector and (
            self.settings.booking_type_option_selector or self.settings.booking_type_value
        ):
            type_opened = await self._try_click_selector_optional(
                page=page,
                selector=self.settings.booking_type_selector,
                step_name="booking type selector",
            )
            if type_opened:
                if self.settings.booking_type_option_selector:
                    await self._try_click_selector_optional(
                        page=page,
                        selector=self.settings.booking_type_option_selector,
                        step_name="booking type option",
                    )
                elif self.settings.booking_type_value:
                    await self._try_click_text_optional(
                        page=page,
                        text=self.settings.booking_type_value,
                        step_name="booking type value",
                    )

        if self.settings.booking_time_from_selector and self.settings.booking_time_from:
            await self._try_fill_input_optional(
                page=page,
                selector=self.settings.booking_time_from_selector,
                value=self.settings.booking_time_from,
                step_name="booking time from",
            )
        if self.settings.booking_time_to_selector and self.settings.booking_time_to:
            await self._try_fill_input_optional(
                page=page,
                selector=self.settings.booking_time_to_selector,
                value=self.settings.booking_time_to,
                step_name="booking time to",
            )

        if self.settings.booking_params_apply_selector:
            await self._try_click_selector_optional(
                page=page,
                selector=self.settings.booking_params_apply_selector,
                step_name="booking params apply",
            )
        elif self.settings.booking_params_close_selector:
            await self._try_click_selector_optional(
                page=page,
                selector=self.settings.booking_params_close_selector,
                step_name="booking params close",
            )

    async def _select_seat(self, page: Page) -> None:
        LOGGER.info("Selecting seat: %s", self.settings.target_seat)
        if self.settings.seat_search_selector:
            search_input = page.locator(self.settings.seat_search_selector).first
            await search_input.wait_for(
                state="visible", timeout=self.settings.default_timeout_ms
            )
            await search_input.fill(self.settings.target_seat)
            await search_input.press("Enter")
            await self._pause(page)

        if self.settings.seat_selector_template:
            selector = self._format_selector(self.settings.seat_selector_template)
            await self._click_selector(page, selector)
            return

        if (
            self.settings.seat_canvas_selector
            and self.settings.seat_canvas_x is not None
            and self.settings.seat_canvas_y is not None
        ):
            await self._click_seat_canvas(page)
            return

        await self._click_text(page, self.settings.target_seat)

    async def _submit_booking(self, page: Page) -> None:
        LOGGER.info("Submitting booking.")
        if self.settings.book_button_selector:
            await self._click_selector(
                page,
                self.settings.book_button_selector,
                timeout_ms=min(self.settings.default_timeout_ms, 8_000),
            )
            return

        last_exception: Exception | None = None
        for text in self.settings.book_button_texts:
            try:
                await self._click_text(page, text, timeout_ms=2_000)
                return
            except Exception as exc:
                last_exception = exc
        raise DaySkipError(
            "Booking button not found for selected date/seat. "
            "Likely no available slot."
        ) from last_exception

    async def _submit_booking_via_api(self, page: Page, target_date: date) -> None:
        target_iso = target_date.strftime("%Y-%m-%d")
        window = self._resolve_booking_window_for_date(target_date)
        if window is None:
            raise RuntimeError(
                f"Could not determine marker date window for {target_iso}. "
                "Map marker requests were not captured."
            )
        self._assert_booking_window_matches_settings(window, target_date)

        table_id = await self._resolve_target_table_id_for_date(
            page=page,
            target_date=target_date,
            window=window,
        )
        payload = {
            "table": table_id,
            "date_from": window.date_from,
            "date_to": window.date_to,
        }
        LOGGER.info(
            "Submitting booking via API for %s: table=%s date_from=%s date_to=%s",
            target_iso,
            table_id,
            window.date_from,
            window.date_to,
        )
        response = await self._api_fetch(
            page=page,
            path="/api/web/single_booking",
            method="POST",
            body=payload,
        )
        body_text = response.get("text", "")
        status = int(response.get("status", 0))
        if status in {200, 201}:
            LOGGER.info("API booking created successfully (status=%s).", status)
            self._resolved_target_table_id = table_id
            return

        message = self._extract_api_error_message(body_text)
        normalized = message.lower()
        skip_hints = (
            "already",
            "not available",
            "busy",
            "overbook",
            "already booked",
            "занят",
            "недоступ",
            "уже",
            "превыш",
        )
        if status in {400, 404, 409, 422} and any(hint in normalized for hint in skip_hints):
            raise DaySkipError(
                f"Seat {self.settings.target_seat} is unavailable for {target_iso}: "
                f"{message or f'HTTP {status}'}"
            )

        raise RuntimeError(
            "API booking request failed: "
            f"HTTP {status}, response={message or body_text[:300] or '<empty>'}"
        )

    def _resolve_booking_window_for_date(self, target_date: date) -> BookingWindow | None:
        target_iso = target_date.strftime("%Y-%m-%d")
        matching_event = self._find_matching_marker_request(target_iso=target_iso, since=0.0)
        base_event = matching_event

        if base_event is None:
            base_event = self._latest_marker_request_with_window()

        explicit_window = self._configured_booking_window(
            target_date=target_date,
            floor=(base_event.floor if base_event else None),
            room_type=(base_event.room_type if base_event else None),
        )
        if explicit_window is not None:
            self._assert_booking_window_matches_settings(explicit_window, target_date)
            return explicit_window

        if matching_event and matching_event.date_from and matching_event.date_to:
            return BookingWindow(
                date_from=matching_event.date_from,
                date_to=matching_event.date_to,
                floor=matching_event.floor,
                room_type=matching_event.room_type,
            )

        latest = base_event
        if latest and latest.date_from and latest.date_to:
            return BookingWindow(
                date_from=self._replace_iso_date(latest.date_from, target_iso),
                date_to=self._replace_iso_date(latest.date_to, target_iso),
                floor=latest.floor,
                room_type=latest.room_type,
            )
        return None

    def _configured_booking_window(
        self,
        target_date: date,
        floor: str | None,
        room_type: str | None,
    ) -> BookingWindow | None:
        if not self.settings.booking_time_from or not self.settings.booking_time_to:
            return None
        date_from, date_to = self._build_target_utc_window_from_settings(target_date)
        return BookingWindow(
            date_from=date_from,
            date_to=date_to,
            floor=floor,
            room_type=room_type,
        )

    def _latest_marker_request_with_window(self) -> MarkerRequestEvent | None:
        for event in reversed(self._marker_requests):
            if event.path != "/api/web/floor/table_markers":
                continue
            if not event.date_from or not event.date_to:
                continue
            return event
        for event in reversed(self._marker_requests):
            if not event.date_from or not event.date_to:
                continue
            return event
        return None

    @staticmethod
    def _replace_iso_date(value: str, target_iso: str) -> str:
        replaced = re.sub(r"\d{4}-\d{2}-\d{2}", target_iso, value, count=1)
        return replaced if replaced else target_iso

    def _build_target_utc_window_from_settings(self, target_date: date) -> tuple[str, str]:
        start_raw = self.settings.booking_time_from or "10:00"
        end_raw = self.settings.booking_time_to or "19:00"
        offset = self._parse_utc_offset(self.settings.booking_local_utc_offset)

        start_h, start_m = self._parse_hhmm(start_raw)
        end_h, end_m = self._parse_hhmm(end_raw)

        local_start = datetime(
            target_date.year,
            target_date.month,
            target_date.day,
            start_h,
            start_m,
            tzinfo=offset,
        )
        local_end = datetime(
            target_date.year,
            target_date.month,
            target_date.day,
            end_h,
            end_m,
            tzinfo=offset,
        )
        if (end_h, end_m) <= (start_h, start_m):
            local_end += timedelta(days=1)

        utc_start = local_start.astimezone(timezone.utc)
        utc_end = local_end.astimezone(timezone.utc)
        return (
            utc_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            utc_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    def _assert_booking_window_matches_settings(
        self,
        window: BookingWindow,
        target_date: date,
    ) -> None:
        if not self.settings.booking_time_from or not self.settings.booking_time_to:
            return
        expected_from, expected_to = self._build_target_utc_window_from_settings(target_date)
        if window.date_from != expected_from or window.date_to != expected_to:
            raise RuntimeError(
                "Booking window validation failed. "
                f"Expected {expected_from}..{expected_to}, "
                f"got {window.date_from}..{window.date_to}."
            )

    @staticmethod
    def _parse_hhmm(value: str) -> tuple[int, int]:
        match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value or "")
        if not match:
            raise RuntimeError(f"Invalid time format '{value}', expected HH:MM.")
        hour = int(match.group(1))
        minute = int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise RuntimeError(f"Invalid time value '{value}', expected HH:MM.")
        return hour, minute

    @staticmethod
    def _parse_utc_offset(raw_value: str) -> timezone:
        value = (raw_value or "").strip()
        match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", value)
        if not match:
            raise RuntimeError(
                f"Invalid BOOKING_LOCAL_UTC_OFFSET '{raw_value}', expected +HH:MM or -HH:MM."
            )
        sign = 1 if match.group(1) == "+" else -1
        hours = int(match.group(2))
        minutes = int(match.group(3))
        if hours > 23 or minutes > 59:
            raise RuntimeError(
                f"Invalid BOOKING_LOCAL_UTC_OFFSET '{raw_value}', expected +HH:MM or -HH:MM."
            )
        return timezone(sign * timedelta(hours=hours, minutes=minutes))

    async def _resolve_target_table_id_for_date(
        self,
        page: Page,
        target_date: date,
        window: BookingWindow,
    ) -> str:
        if self.settings.target_table_id:
            return self.settings.target_table_id
        if self._resolved_target_table_id:
            return self._resolved_target_table_id

        markers = await self._fetch_table_markers(page=page, window=window)
        target_seat = str(self.settings.target_seat).strip()
        candidates: list[dict] = []
        for marker in markers:
            title = str(marker.get("table_title", "")).strip()
            if title != target_seat:
                continue
            permit_value = marker.get("has_permit_for_booking")
            has_permit = True if permit_value is None else self._to_bool(permit_value)
            if not has_permit:
                continue
            candidates.append(marker)

        by_table_id: dict[str, dict] = {}
        for marker in candidates:
            table_id = str(marker.get("table_id", "")).strip()
            if not table_id:
                continue
            previous = by_table_id.get(table_id)
            if previous is None:
                by_table_id[table_id] = marker
                continue
            if self._to_bool(marker.get("is_available")) and not self._to_bool(
                previous.get("is_available")
            ):
                by_table_id[table_id] = marker

        unique_candidates = list(by_table_id.values())
        available = [
            marker for marker in unique_candidates if self._to_bool(marker.get("is_available"))
        ]
        if not available:
            raise DaySkipError(
                f"Seat {target_seat} is not available for "
                f"{target_date.strftime(self.settings.booking_date_format)}."
            )

        if len(available) == 1:
            resolved = str(available[0].get("table_id"))
            self._resolved_target_table_id = resolved
            return resolved

        if window.room_type:
            same_room_type = [
                marker
                for marker in available
                if str(marker.get("room_type_id", "")).strip() == str(window.room_type).strip()
            ]
            if len(same_room_type) == 1:
                resolved = str(same_room_type[0].get("table_id"))
                self._resolved_target_table_id = resolved
                return resolved

        unique_ids = sorted(
            {str(marker.get("table_id", "")).strip() for marker in available if marker.get("table_id")}
        )
        raise RuntimeError(
            "Seat number is ambiguous across multiple desks. "
            f"Set TARGET_TABLE_ID. Candidates for seat {target_seat}: {', '.join(unique_ids[:8])}"
        )

    async def _fetch_table_markers(self, page: Page, window: BookingWindow) -> list[dict]:
        query: dict[str, str] = {
            "date_from": window.date_from,
            "date_to": window.date_to,
        }
        if window.floor:
            query["floor"] = window.floor
        if window.room_type is not None:
            query["room_type"] = window.room_type

        url = self._build_api_url(page, "/api/web/floor/table_markers", query=query)
        response = await self._api_fetch(
            page=page,
            path="/api/web/floor/table_markers",
            method="GET",
            query=query,
        )
        text = response.get("text", "")
        status = int(response.get("status", 0))
        if status != 200:
            raise RuntimeError(
                "Failed to fetch table markers for target date: "
                f"HTTP {status}, url={url}"
            )
        payload = response.get("json")
        if not isinstance(payload, dict):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise RuntimeError("Table markers response is not valid JSON.") from exc
        markers = payload.get("table_markers")
        if not isinstance(markers, list):
            raise RuntimeError("Table markers response does not contain 'table_markers' list.")
        return markers

    @staticmethod
    def _to_bool(value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return False

    def _build_api_url(
        self,
        page: Page,
        path: str,
        query: dict[str, str] | None = None,
    ) -> str:
        parsed = urlparse(page.url or self.settings.booking_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if query:
            return f"{origin}{path}?{urlencode(query)}"
        return f"{origin}{path}"

    async def _api_fetch(
        self,
        page: Page,
        path: str,
        method: str = "GET",
        query: dict[str, str] | None = None,
        body: dict[str, object] | None = None,
    ) -> dict:
        headers: dict[str, str] = {"accept": "application/json"}
        if self._api_authorization_header:
            headers["authorization"] = self._api_authorization_header

        response = await page.evaluate(
            """async ({path, method, query, body, headers}) => {
              const url = new URL(path, window.location.origin);
              if (query) {
                for (const [key, value] of Object.entries(query)) {
                  if (value === null || value === undefined) continue;
                  url.searchParams.set(key, String(value));
                }
              }
              const init = {
                method: method || "GET",
                credentials: "include",
                headers: { ...(headers || {}) },
              };
              if (body && typeof body === "object") {
                init.headers["content-type"] = "application/json";
                init.body = JSON.stringify(body);
              }
              const res = await fetch(url.toString(), init);
              const text = await res.text();
              let json = null;
              try {
                json = text ? JSON.parse(text) : null;
              } catch (_err) {
                json = null;
              }
              return {
                url: url.toString(),
                ok: res.ok,
                status: res.status,
                text,
                json,
              };
            }""",
            {
                "path": path,
                "method": method,
                "query": query or {},
                "body": body,
                "headers": headers,
            },
        )
        if not isinstance(response, dict):
            raise RuntimeError("Unexpected API fetch response shape.")
        return response

    @staticmethod
    def _extract_api_error_message(raw_text: str) -> str:
        text = (raw_text or "").strip()
        if not text:
            return ""
        try:
            payload = json.loads(text)
        except Exception:
            return text

        if isinstance(payload, dict):
            for key in ("message", "detail", "error", "description"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            nested = payload.get("errors")
            if isinstance(nested, list) and nested:
                first = nested[0]
                if isinstance(first, str):
                    return first
                if isinstance(first, dict):
                    for key in ("message", "detail", "error"):
                        value = first.get(key)
                        if isinstance(value, str) and value.strip():
                            return value.strip()
        return text

    async def _wait_for_success(self, page: Page) -> None:
        LOGGER.info("Waiting for booking success indicator.")
        if self.settings.success_selector:
            await page.locator(self.settings.success_selector).first.wait_for(
                state="visible", timeout=self.settings.default_timeout_ms
            )
            return
        if self.settings.success_text:
            await page.get_by_text(self.settings.success_text, exact=False).first.wait_for(
                state="visible", timeout=self.settings.default_timeout_ms
            )
            return

        await page.wait_for_timeout(2_000)

    def _attach_page_trackers(self, page: Page) -> None:
        page.on("requestfinished", self._on_request_finished)

    def _on_request_finished(self, request: Request) -> None:
        try:
            self._record_marker_request(request)
        except Exception:
            LOGGER.debug("Failed to inspect request for marker sync.", exc_info=True)

    def _record_marker_request(self, request: Request) -> None:
        parsed = urlparse(request.url)
        if parsed.path not in MAP_MARKER_API_PATHS:
            return

        headers: dict[str, str] = {}
        try:
            raw_headers = request.headers
            if isinstance(raw_headers, dict):
                headers = {str(k).lower(): str(v) for k, v in raw_headers.items()}
        except Exception:
            headers = {}
        auth_header = headers.get("authorization")
        if auth_header:
            self._api_authorization_header = auth_header

        query = parse_qs(parsed.query)
        event = MarkerRequestEvent(
            path=parsed.path,
            method=request.method,
            date_from=self._first_query_value(query, "date_from"),
            date_to=self._first_query_value(query, "date_to"),
            floor=self._first_query_value(query, "floor"),
            room_type=self._first_query_value(query, "room_type"),
            captured_at=monotonic(),
        )
        self._marker_requests.append(event)
        if len(self._marker_requests) > 200:
            self._marker_requests = self._marker_requests[-200:]
        LOGGER.debug(
            "Map marker request captured: path=%s date_from=%s date_to=%s floor=%s room_type=%s",
            event.path,
            event.date_from,
            event.date_to,
            event.floor,
            event.room_type,
        )

    @staticmethod
    def _first_query_value(values: dict[str, list[str]], key: str) -> str | None:
        raw = values.get(key)
        if not raw:
            return None
        value = str(raw[0]).strip()
        return value or None

    @staticmethod
    def _extract_iso_date(value: str | None) -> str | None:
        if not value:
            return None
        match = re.search(r"(\d{4}-\d{2}-\d{2})", value)
        if not match:
            return None
        return match.group(1)

    def _find_matching_marker_request(
        self,
        target_iso: str,
        since: float = 0.0,
    ) -> MarkerRequestEvent | None:
        for event in reversed(self._marker_requests):
            if event.captured_at < since:
                break
            date_from_iso = self._extract_iso_date(event.date_from)
            date_to_iso = self._extract_iso_date(event.date_to)
            if date_from_iso == target_iso and date_to_iso == target_iso:
                return event
        return None

    def _current_map_date_from_url(self, page: Page) -> str | None:
        parsed = urlparse(page.url)
        if "/map" not in parsed.path:
            return None
        query = parse_qs(parsed.query)
        return self._extract_iso_date(self._first_query_value(query, "date_from"))

    async def _wait_for_target_date_state(
        self,
        page: Page,
        target_date: date,
        since: float,
        timeout_ms: int | None = None,
    ) -> bool:
        target_iso = target_date.strftime("%Y-%m-%d")
        wait_ms = timeout_ms or self.settings.booking_date_apply_wait_timeout_ms
        deadline = monotonic() + wait_ms / 1000.0

        while monotonic() < deadline:
            marker_event = self._find_matching_marker_request(target_iso=target_iso, since=since)
            if marker_event is not None:
                LOGGER.info(
                    "Date %s confirmed by marker request (%s).",
                    target_iso,
                    marker_event.path,
                )
                return True

            url_iso = self._current_map_date_from_url(page)
            if url_iso == target_iso:
                LOGGER.info("Date %s confirmed by map URL parameters.", target_iso)
                return True

            label_date = await self._read_selected_date_from_ui(page)
            if label_date == target_date:
                LOGGER.info("Date %s confirmed by date label in UI.", target_iso)
                return True

            await page.wait_for_timeout(180)

        LOGGER.warning("Target date %s was not confirmed by UI/network state.", target_iso)
        return False

    async def _read_selected_date_from_ui(self, page: Page) -> date | None:
        text = await self._read_selected_date_text(page)
        if not text:
            return None
        return self._parse_date_text(text)

    async def _read_selected_date_text(self, page: Page) -> str | None:
        selectors: list[str] = []
        if self.settings.booking_date_input_selector:
            selectors.append(self.settings.booking_date_input_selector)
        if self.settings.booking_params_open_selector:
            selectors.append(self.settings.booking_params_open_selector)
        selectors.extend(
            [
                '[role="listbox"] [role="option"][aria-selected="true"] [data-testid="Day"]',
                '[role="listbox"] [role="option"][aria-selected="true"]',
            ]
        )

        for selector in selectors:
            locator = page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=500)
            except Exception:
                continue

            try:
                tag_name = (await locator.evaluate("el => el.tagName.toLowerCase()")).strip()
            except Exception:
                tag_name = ""

            text: str | None = None
            try:
                if tag_name in {"input", "textarea"}:
                    text = await locator.input_value()
                else:
                    text = await locator.inner_text()
            except Exception:
                text = None

            normalized = " ".join((text or "").split())
            if normalized:
                return normalized
        return None

    def _parse_date_text(self, raw_text: str) -> date | None:
        text = " ".join(raw_text.split()).lower()

        dotted_match = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", text)
        if dotted_match:
            day = int(dotted_match.group(1))
            month = int(dotted_match.group(2))
            year = int(dotted_match.group(3))
            try:
                return date(year, month, day)
            except ValueError:
                return None

        month_map = {
            "янв": 1,
            "фев": 2,
            "мар": 3,
            "апр": 4,
            "мая": 5,
            "май": 5,
            "июн": 6,
            "июл": 7,
            "авг": 8,
            "сен": 9,
            "окт": 10,
            "ноя": 11,
            "дек": 12,
        }
        named_match = re.search(r"\b(\d{1,2})\s+([а-яё]{3,})\b", text)
        if not named_match:
            return None

        day = int(named_match.group(1))
        month_token = named_match.group(2)
        month = None
        for prefix, value in month_map.items():
            if month_token.startswith(prefix):
                month = value
                break
        if month is None:
            return None

        today = date.today()
        year = today.year
        if month < today.month - 10:
            year += 1
        elif month > today.month + 10:
            year -= 1

        try:
            return date(year, month, day)
        except ValueError:
            return None

    async def _click_selector(
        self,
        page: Page,
        selector: str,
        timeout_ms: int | None = None,
    ) -> None:
        locator = page.locator(selector).first
        wait_timeout = timeout_ms or self.settings.default_timeout_ms
        try:
            await locator.wait_for(state="visible", timeout=wait_timeout)
            await self._click_locator(page, locator)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(
                f"Selector was not visible/clickable in time: '{selector}' "
                f"(url={page.url})"
            ) from exc

    async def _click_text(
        self,
        page: Page,
        text: str,
        timeout_ms: int | None = None,
    ) -> None:
        wait_timeout = timeout_ms or self.settings.default_timeout_ms
        locator = page.get_by_text(text, exact=False).first
        await locator.wait_for(state="visible", timeout=wait_timeout)
        await self._click_locator(page, locator)

    async def _click_text_exact(
        self,
        page: Page,
        text: str,
        timeout_ms: int | None = None,
    ) -> None:
        wait_timeout = timeout_ms or self.settings.default_timeout_ms
        locator = page.get_by_text(text, exact=True).first
        await locator.wait_for(state="visible", timeout=wait_timeout)
        await self._click_locator(page, locator)

    async def _try_click_selector_optional(
        self,
        page: Page,
        selector: str,
        step_name: str,
    ) -> bool:
        try:
            await self._click_selector(page, selector)
            return True
        except Exception as exc:
            LOGGER.warning(
                "Skipping optional step '%s': selector not usable (%s): %s",
                step_name,
                selector,
                exc,
            )
            return False

    async def _try_click_text_optional(
        self,
        page: Page,
        text: str,
        step_name: str,
        timeout_ms: int | None = None,
    ) -> bool:
        try:
            await self._click_text(page, text, timeout_ms=timeout_ms)
            return True
        except Exception as exc:
            LOGGER.warning(
                "Skipping optional step '%s': text click not usable (%s): %s",
                step_name,
                text,
                exc,
            )
            return False

    async def _try_fill_input_optional(
        self,
        page: Page,
        selector: str,
        value: str,
        step_name: str,
    ) -> bool:
        try:
            await self._fill_input_like_user(page=page, selector=selector, value=value)
            return True
        except Exception as exc:
            LOGGER.warning(
                "Skipping optional step '%s': input not usable (%s): %s",
                step_name,
                selector,
                exc,
            )
            return False

    async def _select_booking_date(self, page: Page, target_date: date) -> bool:
        target_iso = target_date.strftime("%Y-%m-%d")

        current_url_iso = self._current_map_date_from_url(page)
        if current_url_iso == target_iso:
            LOGGER.info("Target date %s is already set in URL.", target_iso)
            return True

        current_ui_date = await self._read_selected_date_from_ui(page)
        if current_ui_date == target_date:
            LOGGER.info("Target date %s is already selected in UI.", target_iso)
            return True

        if self.settings.booking_use_url_date_fallback:
            LOGGER.info("Trying URL date sync first for %s.", target_iso)
            url_set_started = monotonic()
            set_by_url = await self._set_date_via_url(page, target_date)
            if set_by_url:
                applied_by_url = await self._wait_for_target_date_state(
                    page=page,
                    target_date=target_date,
                    since=url_set_started,
                    timeout_ms=max(
                        self.settings.booking_date_apply_wait_timeout_ms,
                        self.settings.office_map_wait_timeout_ms,
                    ),
                )
                if applied_by_url:
                    return True
                LOGGER.warning(
                    "URL date sync did not confirm target date %s. "
                    "Trying calendar click fallback.",
                    target_iso,
                )

        for attempt in range(1, 4):
            attempt_started = monotonic()
            click_status = await self._try_click_target_date_in_calendar(page, target_date)
            if click_status == "disabled":
                LOGGER.info("Target date exists but disabled in calendar: %s", target_iso)
                return False
            if click_status == "selected":
                applied = await self._wait_for_target_date_state(
                    page=page,
                    target_date=target_date,
                    since=attempt_started,
                )
                if applied:
                    return True
                LOGGER.warning(
                    "Date click attempt %s/%s did not apply target date %s.",
                    attempt,
                    3,
                    target_iso,
                )

        return False

    async def _try_click_target_date_in_calendar(
        self,
        page: Page,
        target_date: date,
    ) -> str:
        if not await self._open_date_picker_for_target(page):
            return "not_found"

        if await self._has_listbox_calendar(page):
            if await self._click_listbox_day_option(page, target_date.day):
                LOGGER.info(
                    "Calendar click by listbox day for %s (day %s).",
                    target_date.strftime("%Y-%m-%d"),
                    target_date.day,
                )
                return "selected"

            # For listbox calendars avoid blind month iteration:
            # shift month only when current selected month differs from target.
            current_selected_date = await self._read_selected_date_from_ui(page)
            if current_selected_date and (
                current_selected_date.year != target_date.year
                or current_selected_date.month != target_date.month
            ):
                for _ in range(2):
                    if not await self._calendar_next_month(page):
                        break
                    if await self._click_listbox_day_option(page, target_date.day):
                        LOGGER.info(
                            "Calendar click by listbox day after month shift for %s.",
                            target_date.strftime("%Y-%m-%d"),
                        )
                        return "selected"
            else:
                LOGGER.info(
                    "Listbox calendar is visible and target month matches current; "
                    "skipping month-switch attempts for %s.",
                    target_date.strftime("%Y-%m-%d"),
                )

        iso = target_date.strftime("%Y-%m-%d")
        for month_shift in range(0, 4):
            if await self._click_calendar_iso_cell(page, iso, allow_disabled=False):
                LOGGER.info("Calendar click by ISO selector for %s.", iso)
                return "selected"
            if await self._click_calendar_iso_cell(page, iso, allow_disabled=True):
                return "disabled"
            if month_shift >= 3:
                break
            moved = await self._calendar_next_month(page)
            if not moved:
                break

        if await self._click_calendar_day_with_fallback(page, target_date.day):
            LOGGER.info(
                "Calendar click by day fallback for %s (day %s).",
                iso,
                target_date.day,
            )
            return "selected"
        return "not_found"

    async def _has_listbox_calendar(self, page: Page) -> bool:
        selectors = [
            '[role="listbox"] [role="option"] [data-testid="Day"]',
            '[role="listbox"] [role="option"]',
        ]
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=1_000)
                return True
            except PlaywrightTimeoutError:
                continue
        return False

    async def _set_date_via_url(self, page: Page, target_date: date) -> bool:
        parsed = urlparse(page.url)
        if "/map" not in parsed.path:
            return False

        target_iso = target_date.strftime("%Y-%m-%d")
        latest_window = self._latest_marker_request_with_window()

        explicit_window = self._configured_booking_window(
            target_date=target_date,
            floor=(latest_window.floor if latest_window else None),
            room_type=(latest_window.room_type if latest_window else None),
        )
        if explicit_window is not None:
            query = parse_qs(parsed.query)
            query["date_from"] = [explicit_window.date_from]
            query["date_to"] = [explicit_window.date_to]
            new_query = urlencode(query, doseq=True)
            new_url = urlunparse(parsed._replace(query=new_query))
            LOGGER.info(
                "Reloading map with explicit date/time params: %s (date_from=%s, date_to=%s)",
                target_iso,
                explicit_window.date_from,
                explicit_window.date_to,
            )
            await page.goto(new_url, wait_until="domcontentloaded")
            await self._wait_for_office_map_ready(page)
            return True

        default_date_from = target_iso
        default_date_to = target_iso
        if latest_window and latest_window.date_from and latest_window.date_to:
            default_date_from = self._replace_iso_date(latest_window.date_from, target_iso)
            default_date_to = self._replace_iso_date(latest_window.date_to, target_iso)

        query = parse_qs(parsed.query)
        current_date_from = self._first_query_value(query, "date_from")
        current_date_to = self._first_query_value(query, "date_to")
        query["date_from"] = [
            self._replace_iso_date(current_date_from, target_iso)
            if current_date_from
            else default_date_from
        ]
        query["date_to"] = [
            self._replace_iso_date(current_date_to, target_iso)
            if current_date_to
            else default_date_to
        ]
        new_query = urlencode(query, doseq=True)
        new_url = urlunparse(parsed._replace(query=new_query))

        LOGGER.info(
            "Reloading map with explicit date params: %s (date_from=%s, date_to=%s)",
            target_iso,
            query["date_from"][0],
            query["date_to"][0],
        )
        await page.goto(new_url, wait_until="domcontentloaded")
        await self._wait_for_office_map_ready(page)
        return True

    async def _open_date_picker_for_target(self, page: Page) -> bool:
        if await self._calendar_is_open(page):
            return True
        if self.settings.booking_date_input_selector:
            try:
                await self._click_selector(page, self.settings.booking_date_input_selector)
            except Exception:
                pass
        if await self._calendar_is_open(page):
            return True
        if self.settings.booking_date_input_selector:
            # Some pickers require second click/focus after panel animation.
            try:
                await page.locator(self.settings.booking_date_input_selector).first.click(
                    timeout=2_000
                )
                await self._pause(page)
            except Exception:
                pass
        if await self._calendar_is_open(page):
            return True
        return await self._try_open_date_picker_by_text(page)

    async def _calendar_is_open(self, page: Page) -> bool:
        selectors = [
            ".ant-picker-dropdown .ant-picker-content",
            ".ant-picker-panel",
            '[role="listbox"] [role="option"] [data-testid="Day"]',
            "[class*=\"calendar\"] [role=\"grid\"]",
            "[class*=\"date\"] [role=\"grid\"]",
        ]
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=1_500)
                return True
            except PlaywrightTimeoutError:
                continue
        return False

    async def _click_calendar_iso_cell(
        self,
        page: Page,
        iso_date: str,
        allow_disabled: bool,
    ) -> bool:
        if allow_disabled:
            selectors = [
                f'.ant-picker-dropdown td[title="{iso_date}"]',
                f'.ant-picker-panel td[title="{iso_date}"]',
            ]
        else:
            selectors = [
                (
                    f'.ant-picker-dropdown td[title="{iso_date}"]'
                    ":not(.ant-picker-cell-disabled) .ant-picker-cell-inner"
                ),
                (
                    f'.ant-picker-panel td[title="{iso_date}"]'
                    ":not(.ant-picker-cell-disabled) .ant-picker-cell-inner"
                ),
            ]
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=2_000)
                if allow_disabled:
                    return True
                await self._click_locator(page, locator)
                return True
            except PlaywrightTimeoutError:
                continue
            except Exception:
                continue
        return False

    async def _calendar_next_month(self, page: Page) -> bool:
        selectors: list[str] = []
        if self.settings.booking_calendar_next_selector:
            selectors.append(self.settings.booking_calendar_next_selector)
        selectors.extend([
            ".ant-picker-dropdown .ant-picker-header-next-btn",
            ".ant-picker-panel .ant-picker-header-next-btn",
            "[class*=\"calendar\"] [aria-label*=\"next\"]",
        ])
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=2_000)
                await self._click_locator(page, locator)
                LOGGER.info("Calendar moved to next month.")
                return True
            except PlaywrightTimeoutError:
                continue
            except Exception:
                continue
        return False

    async def _try_open_date_picker_by_text(self, page: Page) -> bool:
        # Handles localized labels like "Ср, 11 февраля" and similar variants.
        date_like_patterns = [
            re.compile(
                r"\b\d{1,2}\s+"
                r"(янв|фев|мар|апр|ма[йя]|июн|июл|авг|сен|окт|ноя|дек)",
                re.IGNORECASE,
            ),
            re.compile(r"\b\d{1,2}[./-]\d{1,2}\b"),
        ]
        for pattern in date_like_patterns:
            locator = page.get_by_text(pattern).first
            try:
                await locator.wait_for(state="visible", timeout=5_000)
                await self._click_locator(page, locator)
                return True
            except PlaywrightTimeoutError:
                continue
            except Exception:
                continue
        return False

    async def _click_calendar_day_with_fallback(self, page: Page, day: int) -> bool:
        day_str = str(day)
        if self.settings.booking_date_day_selector_template:
            try:
                selector = self.settings.booking_date_day_selector_template.format(
                    day=day_str
                )
                locator = page.locator(selector).first
                await locator.wait_for(state="visible", timeout=3_500)
                await self._click_locator(page, locator)
                return True
            except PlaywrightTimeoutError:
                pass
            except Exception:
                pass

        if await self._click_listbox_day_option(page, day):
            return True

        selectors = [
            f'[role="listbox"] [role="option"] [data-testid="Day"] span:has-text("{day_str}")',
            f'[role="listbox"] [role="option"]:has([data-testid="Day"] span:has-text("{day_str}"))',
            ".ant-picker-dropdown .ant-picker-cell:not(.ant-picker-cell-disabled):not(.ant-picker-cell-in-view-false) "
            f".ant-picker-cell-inner:has-text(\"{day_str}\")",
            ".ant-picker-panel .ant-picker-cell:not(.ant-picker-cell-disabled):not(.ant-picker-cell-in-view-false) "
            f".ant-picker-cell-inner:has-text(\"{day_str}\")",
            f"[class*=\"calendar\"] [class*=\"day\"]:has-text(\"{day_str}\")",
        ]

        for selector in selectors:
            locator = page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=3_500)
                await self._click_locator(page, locator)
                return True
            except PlaywrightTimeoutError:
                continue
            except Exception:
                continue

        return False

    async def _click_listbox_day_option(self, page: Page, day: int) -> bool:
        try:
            day_indexes: list[int] = await page.evaluate(
                """(dayValue) => {
                    const normalized = String(dayValue);
                    const options = Array.from(
                      document.querySelectorAll('[role="listbox"] [role="option"]')
                    );
                    const matches = [];
                    for (let i = 0; i < options.length; i += 1) {
                      const option = options[i];
                      const dayNode =
                        option.querySelector('[data-testid="Day"] span') ||
                        option.querySelector('[data-testid="Day"]') ||
                        option.querySelector('span');
                      const text = (dayNode?.textContent || "").replace(/\\s+/g, " ").trim();
                      if (text !== normalized) continue;
                      const classes = `${option.className || ""} ${dayNode?.className || ""}`.toLowerCase();
                      const disabled = option.getAttribute("aria-disabled") === "true" || classes.includes("disabled");
                      if (disabled) continue;
                      matches.push(i);
                    }
                    return matches;
                }""",
                day,
            )
        except Exception:
            return False

        for idx in day_indexes:
            locator = page.locator('[role="listbox"] [role="option"]').nth(idx)
            try:
                await locator.wait_for(state="visible", timeout=2_000)
                await self._click_locator(page, locator)
                LOGGER.info("Calendar day clicked via listbox option index: %s", idx)
                return True
            except Exception:
                continue
        return False

    async def _fill_input_like_user(self, page: Page, selector: str, value: str) -> None:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=self.settings.default_timeout_ms)
            await locator.click()
            try:
                await locator.fill(value)
            except Exception:
                await page.keyboard.press("Control+A")
                await page.keyboard.type(value)
            await page.keyboard.press("Enter")
            await self._pause(page)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(
                f"Input selector was not visible in time: '{selector}' (url={page.url})"
            ) from exc

    async def _click_locator(self, page: Page, locator: Locator) -> None:
        await locator.scroll_into_view_if_needed()
        bounds = await locator.bounding_box()
        if bounds:
            await page.mouse.move(
                bounds["x"] + bounds["width"] / 2,
                bounds["y"] + bounds["height"] / 2,
            )
        await locator.click(timeout=self.settings.default_timeout_ms)
        await self._pause(page)

    async def _click_seat_canvas(self, page: Page) -> None:
        locator = page.locator(self.settings.seat_canvas_selector or "canvas")
        if self.settings.seat_canvas_index is not None:
            locator = locator.nth(self.settings.seat_canvas_index)
        else:
            locator = locator.first

        await locator.wait_for(state="visible", timeout=self.settings.default_timeout_ms)
        await locator.scroll_into_view_if_needed()
        await locator.click(
            position={
                "x": float(self.settings.seat_canvas_x),
                "y": float(self.settings.seat_canvas_y),
            },
            timeout=self.settings.default_timeout_ms,
        )
        await self._pause(page)

    async def _wait_for_office_map_ready(self, page: Page) -> None:
        timeout_ms = self.settings.office_map_wait_timeout_ms
        LOGGER.info(
            "Waiting for office map readiness (timeout=%sms).",
            timeout_ms,
        )
        try:
            await page.wait_for_url("**/map**", timeout=timeout_ms)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(
                "Map page was not opened after office selection. "
                "Check OFFICE_CHOOSE_SELECTOR/OFFICE_OPTION_SELECTOR_TEMPLATE."
            ) from exc

        ready_locator: Locator | None = None
        if self.settings.office_map_ready_selector:
            ready_locator = page.locator(self.settings.office_map_ready_selector).first
        elif self.settings.seat_canvas_selector:
            canvas = page.locator(self.settings.seat_canvas_selector)
            ready_locator = (
                canvas.nth(self.settings.seat_canvas_index)
                if self.settings.seat_canvas_index is not None
                else canvas.first
            )
        elif self.settings.booking_params_open_selector:
            ready_locator = page.locator(self.settings.booking_params_open_selector).first

        if ready_locator is not None:
            try:
                await ready_locator.wait_for(state="visible", timeout=timeout_ms)
            except PlaywrightTimeoutError as exc:
                raise RuntimeError(
                    "Office map did not become ready in time. "
                    "Tune OFFICE_MAP_READY_SELECTOR/OFFICE_MAP_WAIT_TIMEOUT_MS."
                ) from exc

        # Map data is frequently loaded by XHR after URL change, so wait for
        # network quiet as a best-effort sync point.
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeoutError:
            LOGGER.debug("Network idle was not reached quickly; continuing.")

        if self.settings.office_map_loading_selectors:
            observed_loader = False
            for selector in self.settings.office_map_loading_selectors:
                loader = page.locator(selector).first
                try:
                    await loader.wait_for(state="visible", timeout=3_000)
                    observed_loader = True
                    LOGGER.info("Map loader visible: %s", selector)
                    await loader.wait_for(
                        state="hidden",
                        timeout=self.settings.office_map_loading_wait_timeout_ms,
                    )
                    LOGGER.info("Map loader hidden: %s", selector)
                except PlaywrightTimeoutError:
                    continue
            if observed_loader:
                await self._pause(page)

        if self.settings.office_map_extra_wait_ms > 0:
            await page.wait_for_timeout(self.settings.office_map_extra_wait_ms)
        LOGGER.info("Office map is ready: %s", page.url)

    async def _first_visible_selector(
        self,
        page: Page,
        selectors: list[str],
        total_timeout_ms: int,
    ) -> tuple[str, Locator] | None:
        if not selectors:
            return None

        per_selector_timeout_ms = min(750, total_timeout_ms)
        deadline = monotonic() + total_timeout_ms / 1000.0
        while monotonic() < deadline:
            for selector in selectors:
                locator = page.locator(selector).first
                try:
                    await locator.wait_for(
                        state="visible", timeout=per_selector_timeout_ms
                    )
                    return selector, locator
                except PlaywrightTimeoutError:
                    continue
            await page.wait_for_timeout(150)
        return None

    def _format_selector(self, template: str) -> str:
        return template.format(
            office=self.settings.target_office,
            office_name=self.settings.target_office,
            seat=self.settings.target_seat,
            seat_name=self.settings.target_seat,
        )

    def _resolve_target_dates(self) -> list[date]:
        if self.settings.booking_date_values:
            out: list[date] = []
            seen: set[date] = set()
            for raw in self.settings.booking_date_values:
                parsed = datetime.strptime(
                    raw,
                    self.settings.booking_date_format,
                ).date()
                if parsed in seen:
                    continue
                seen.add(parsed)
                out.append(parsed)
            return out

        if self.settings.booking_date_value:
            parsed = datetime.strptime(
                self.settings.booking_date_value,
                self.settings.booking_date_format,
            ).date()
            return [parsed]
        if self.settings.booking_date_offset_days is not None:
            return [date.today() + timedelta(days=self.settings.booking_date_offset_days)]

        today = date.today()
        start = today if self.settings.booking_include_today else today + timedelta(days=1)
        end = today + timedelta(days=self.settings.booking_range_days)
        current = start
        out: list[date] = []
        while current <= end:
            if not (self.settings.booking_skip_weekends and current.weekday() >= 5):
                out.append(current)
            current += timedelta(days=1)
        return out

    def _resolve_booking_date(self, target_date: date | None) -> date | None:
        if target_date is not None:
            return target_date
        if self.settings.booking_date_values:
            first = self.settings.booking_date_values[0]
            return datetime.strptime(
                first,
                self.settings.booking_date_format,
            ).date()
        if self.settings.booking_date_value:
            return datetime.strptime(
                self.settings.booking_date_value,
                self.settings.booking_date_format,
            ).date()
        if self.settings.booking_date_offset_days is not None:
            return date.today() + timedelta(days=self.settings.booking_date_offset_days)
        return None

    @staticmethod
    def _is_target_closed_exception(exc: Exception) -> bool:
        message = str(exc).lower()
        return "target page, context or browser has been closed" in message

    async def _pause(self, page: Page) -> None:
        if self.settings.ui_pause_ms > 0:
            await page.wait_for_timeout(self.settings.ui_pause_ms)

    async def _capture_success_screenshot(
        self,
        page: Page,
        prefix: str,
        target_date: date | None = None,
    ) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_path = self.settings.screenshot_dir / f"{prefix}_{timestamp}.png"

        if not page.is_closed():
            focused = await self._capture_focused_ui_screenshot(page, screenshot_path)
            if focused:
                return screenshot_path

        if target_date is not None and not page.is_closed():
            bookings_shot = await self._capture_bookings_card_screenshot(
                page=page,
                target_date=target_date,
                screenshot_path=screenshot_path,
            )
            if bookings_shot:
                return screenshot_path

        await page.screenshot(path=str(screenshot_path), full_page=True)
        return screenshot_path

    async def _capture_focused_ui_screenshot(
        self,
        page: Page,
        screenshot_path: Path,
    ) -> bool:
        candidates: list[Locator] = []
        if self.settings.success_selector:
            candidates.append(page.locator(self.settings.success_selector).first)
        if self.settings.success_close_selector:
            candidates.append(
                page.locator(f'div:has({self.settings.success_close_selector})').first
            )

        selector_candidates = [
            '[role="dialog"]',
            'div:has(button:has-text("Закрыть"))',
            'div:has(button:has-text("Забронировать"))',
            'div:has-text("Бронирование создано")',
            'div:has-text("17")',
        ]
        for selector in selector_candidates:
            try:
                candidates.append(page.locator(selector).first)
            except Exception:
                continue

        for locator in candidates:
            try:
                await locator.wait_for(state="visible", timeout=800)
                box = await locator.bounding_box()
                if not box:
                    continue
                if box["width"] < 220 or box["height"] < 120:
                    continue
                await locator.screenshot(path=str(screenshot_path))
                return True
            except Exception:
                continue
        return False

    async def _capture_bookings_card_screenshot(
        self,
        page: Page,
        target_date: date,
        screenshot_path: Path,
    ) -> bool:
        tmp_page: Page | None = None
        try:
            tmp_page = await page.context.new_page()
            tmp_page.set_default_timeout(min(self.settings.default_timeout_ms, 20_000))
            bookings_url = self._build_api_url(page, "/bookings")
            await tmp_page.goto(bookings_url, wait_until="domcontentloaded")
            try:
                await tmp_page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass

            date_text_variants = self._build_booking_date_search_variants(target_date)
            time_from = (self.settings.booking_time_from or "").strip()
            time_to = (self.settings.booking_time_to or "").strip()

            selectors = [
                "article",
                '[role="listitem"]',
                '[class*="booking"]',
            ]
            for scroll_step in range(12):
                for selector in selectors:
                    locator = tmp_page.locator(selector)
                    count = min(await locator.count(), 120)
                    for idx in range(count):
                        item = locator.nth(idx)
                        try:
                            await item.scroll_into_view_if_needed(timeout=700)
                            await item.wait_for(state="visible", timeout=700)
                            text = " ".join((await item.inner_text()).split())
                        except Exception:
                            continue
                        if not text:
                            continue
                        text_lower = text.lower()
                        if self.settings.target_seat not in text:
                            continue
                        # Bookings page may show time in GMT0 instead of local office time.
                        # Date + seat is the primary discriminator. Time is best-effort only.
                        if time_from and time_from in text and time_to and time_to in text:
                            pass
                        if not any(v.lower() in text_lower for v in date_text_variants):
                            continue
                        box = await item.bounding_box()
                        if box and box["width"] >= 220 and box["height"] >= 120:
                            await tmp_page.wait_for_timeout(250)
                            await item.screenshot(path=str(screenshot_path))
                            return True
                if scroll_step < 11:
                    await tmp_page.mouse.wheel(0, 1400)
                    await tmp_page.wait_for_timeout(350)

            # Fallback: find target date text, scroll to it, screenshot nearest container.
            if await self._capture_bookings_viewport_near_date(
                page=tmp_page,
                date_text_variants=date_text_variants,
                screenshot_path=screenshot_path,
            ):
                return True

            # Fallback: find target date text, scroll to it, screenshot nearest container.
            for variant in date_text_variants:
                if len(variant.strip()) < 4:
                    continue
                try:
                    date_loc = tmp_page.get_by_text(variant, exact=False).first
                    await date_loc.wait_for(state="visible", timeout=1_500)
                    await date_loc.scroll_into_view_if_needed(timeout=1_500)
                    container = date_loc.locator(
                        'xpath=ancestor::*[self::article or @role="listitem" or contains(@class,"booking")][1]'
                    ).first
                    box = await container.bounding_box()
                    if box and box["width"] >= 220 and box["height"] >= 120:
                        await tmp_page.wait_for_timeout(250)
                        await container.screenshot(path=str(screenshot_path))
                        return True
                except Exception:
                    continue

            # Fallback: capture bookings page viewport instead of map zoom-out.
            main_locator = tmp_page.locator("main").first
            try:
                await main_locator.wait_for(state="visible", timeout=2_000)
                await main_locator.screenshot(path=str(screenshot_path))
                return True
            except Exception:
                pass
            return False
        except Exception:
            LOGGER.debug("Could not capture bookings card screenshot.", exc_info=True)
            return False
        finally:
            if tmp_page is not None:
                try:
                    await tmp_page.close()
                except Exception:
                    pass

    async def _capture_bookings_viewport_near_date(
        self,
        page: Page,
        date_text_variants: list[str],
        screenshot_path: Path,
    ) -> bool:
        for _ in range(16):
            for variant in date_text_variants:
                if len(variant.strip()) < 4:
                    continue
                try:
                    locator = page.get_by_text(variant, exact=False).first
                    await locator.wait_for(state="visible", timeout=600)
                    await locator.scroll_into_view_if_needed(timeout=1_500)
                    await page.wait_for_timeout(350)
                    if await self._capture_viewport_around_locator(page, locator, screenshot_path):
                        return True
                except Exception:
                    continue
            await page.mouse.wheel(0, 1200)
            await page.wait_for_timeout(300)
        return False

    async def _capture_viewport_around_locator(
        self,
        page: Page,
        locator: Locator,
        screenshot_path: Path,
    ) -> bool:
        try:
            box = await locator.bounding_box()
            if not box:
                return False
            viewport = page.viewport_size
            if not viewport:
                viewport = await page.evaluate(
                    "() => ({ width: window.innerWidth || 1280, height: window.innerHeight || 720 })"
                )
            width = int(max(320, min(int(viewport["width"]), 1600)))
            height = int(max(300, min(int(viewport["height"]), 900)))
            clip_y = max(0, float(box["y"]) - 120)
            clip_x = 0.0
            clip = {
                "x": clip_x,
                "y": clip_y,
                "width": float(width),
                "height": float(height),
            }
            await page.screenshot(path=str(screenshot_path), clip=clip)
            return True
        except Exception:
            return False

    @staticmethod
    def _build_booking_date_search_variants(target_date: date) -> list[str]:
        ru_months = [
            "января",
            "февраля",
            "марта",
            "апреля",
            "мая",
            "июня",
            "июля",
            "августа",
            "сентября",
            "октября",
            "ноября",
            "декабря",
        ]
        en_months = [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ]
        month_name = ru_months[target_date.month - 1]
        en_month_name = en_months[target_date.month - 1]
        variants = [
            target_date.strftime("%d.%m.%Y"),
            target_date.strftime("%d.%m"),
            target_date.strftime("%Y-%m-%d"),
            f"{target_date.day} {month_name}",
            f"{target_date.day} {month_name[:3]}",
            f"{target_date.day} {en_month_name}",
            f"{target_date.day} {en_month_name[:3]}",
        ]
        # Preserve order while removing duplicates.
        out: list[str] = []
        seen: set[str] = set()
        for item in variants:
            key = item.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    async def _capture_screenshot(self, page: Page, prefix: str) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_path = self.settings.screenshot_dir / f"{prefix}_{timestamp}.png"
        await page.screenshot(path=str(screenshot_path), full_page=True)
        return screenshot_path
