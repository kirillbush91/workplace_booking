from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import re
from time import monotonic

from playwright.async_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from .config import Settings
from .telegram_client import TelegramNotifier


LOGGER = logging.getLogger(__name__)


class BookingError(RuntimeError):
    def __init__(self, message: str, screenshot_path: Path | None = None) -> None:
        super().__init__(message)
        self.screenshot_path = screenshot_path


@dataclass(frozen=True)
class BookingResult:
    started_at: datetime
    finished_at: datetime
    office: str
    seat: str
    screenshot_path: Path | None


class BookingBot:
    def __init__(
        self,
        settings: Settings,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self.settings = settings
        self.notifier = notifier

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
                page.on("close", lambda: LOGGER.error("Playwright page was closed unexpectedly."))
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
                await self._select_office(page)
                await self._configure_booking_parameters(page)
                await self._select_seat(page)
                await self._submit_booking(page)
                await self._wait_for_success(page)

                screenshot = await self._capture_screenshot(page, "success")
                finished_at = datetime.now(timezone.utc)
                return BookingResult(
                    started_at=started_at,
                    finished_at=finished_at,
                    office=self.settings.target_office,
                    seat=self.settings.target_seat,
                    screenshot_path=screenshot,
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
                try:
                    await context.storage_state(path=str(self.settings.storage_state_path))
                except Exception:
                    LOGGER.exception("Failed to persist browser storage state.")
                try:
                    await context.close()
                except Exception:
                    LOGGER.exception("Failed to close browser context cleanly.")
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    LOGGER.exception("Failed to close browser cleanly.")

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
            if not code:
                raise RuntimeError("OTP code was not received from Telegram.")
            await otp_input.click()
            await page.keyboard.press("Control+A")
            await page.keyboard.type(code)
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

    async def _configure_booking_parameters(self, page: Page) -> None:
        has_any_param = any(
            [
                self.settings.booking_params_open_selector,
                self.settings.booking_date_input_selector,
                self.settings.booking_type_selector
                and (
                    self.settings.booking_type_option_selector
                    or self.settings.booking_type_value
                ),
                self.settings.booking_time_from_selector and self.settings.booking_time_from,
                self.settings.booking_time_to_selector and self.settings.booking_time_to,
            ]
        )
        if not has_any_param:
            return

        LOGGER.info("Configuring booking parameters.")
        if self.settings.booking_params_open_selector:
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

        target_date = self._resolve_booking_date()
        if self.settings.booking_date_input_selector and target_date:
            try:
                await self._fill_input_like_user(
                    page=page,
                    selector=self.settings.booking_date_input_selector,
                    value=target_date,
                )
            except Exception:
                target_day = self._target_day_of_month(target_date)
                if target_day is None:
                    raise
                LOGGER.info(
                    "Date input fill failed, trying calendar day click: %s",
                    target_day,
                )
                if self.settings.booking_date_day_selector_template:
                    await self._click_selector(
                        page,
                        self.settings.booking_date_day_selector_template.format(
                            day=target_day
                        ),
                    )
                else:
                    clicked = await self._click_calendar_day_with_fallback(page, target_day)
                    if not clicked:
                        raise RuntimeError(
                            f"Failed to click calendar day '{target_day}'. "
                            "Set BOOKING_DATE_DAY_SELECTOR_TEMPLATE to explicit selector."
                        )

        if self.settings.booking_type_selector and (
            self.settings.booking_type_option_selector or self.settings.booking_type_value
        ):
            await self._click_selector(page, self.settings.booking_type_selector)
            if self.settings.booking_type_option_selector:
                await self._click_selector(page, self.settings.booking_type_option_selector)
            elif self.settings.booking_type_value:
                await self._click_text(page, self.settings.booking_type_value)

        if self.settings.booking_time_from_selector and self.settings.booking_time_from:
            await self._fill_input_like_user(
                page=page,
                selector=self.settings.booking_time_from_selector,
                value=self.settings.booking_time_from,
            )
        if self.settings.booking_time_to_selector and self.settings.booking_time_to:
            await self._fill_input_like_user(
                page=page,
                selector=self.settings.booking_time_to_selector,
                value=self.settings.booking_time_to,
            )

        if self.settings.booking_params_apply_selector:
            await self._click_selector(page, self.settings.booking_params_apply_selector)
        elif self.settings.booking_params_close_selector:
            await self._click_selector(page, self.settings.booking_params_close_selector)

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
            await self._click_selector(page, self.settings.book_button_selector)
            return

        last_exception: Exception | None = None
        for text in self.settings.book_button_texts:
            try:
                await self._click_text(page, text, timeout_ms=2_000)
                return
            except Exception as exc:
                last_exception = exc
        raise RuntimeError(
            "Booking button not found. Configure BOOK_BUTTON_SELECTOR "
            "or BOOK_BUTTON_TEXTS."
        ) from last_exception

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

    async def _click_selector(self, page: Page, selector: str) -> None:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=self.settings.default_timeout_ms)
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
        selectors = [
            ".ant-picker-dropdown .ant-picker-cell:not(.ant-picker-cell-disabled) "
            f".ant-picker-cell-inner:has-text(\"{day_str}\")",
            ".ant-picker-panel .ant-picker-cell:not(.ant-picker-cell-disabled) "
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

        try:
            await self._click_text_exact(page, day_str, timeout_ms=3_500)
            return True
        except Exception:
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

    def _resolve_booking_date(self) -> str | None:
        if self.settings.booking_date_value:
            return self.settings.booking_date_value
        if self.settings.booking_date_offset_days is not None:
            date_value = datetime.now() + timedelta(days=self.settings.booking_date_offset_days)
            return date_value.strftime(self.settings.booking_date_format)
        return None

    def _target_day_of_month(self, value: str) -> int | None:
        try:
            return datetime.strptime(value, self.settings.booking_date_format).day
        except ValueError:
            return None

    async def _pause(self, page: Page) -> None:
        if self.settings.ui_pause_ms > 0:
            await page.wait_for_timeout(self.settings.ui_pause_ms)

    async def _capture_screenshot(self, page: Page, prefix: str) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_path = self.settings.screenshot_dir / f"{prefix}_{timestamp}.png"
        await page.screenshot(path=str(screenshot_path), full_page=True)
        return screenshot_path
