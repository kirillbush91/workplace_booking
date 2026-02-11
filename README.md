# workplace_booking

Autonomous workplace booking bot for `https://lemana.simple-office-web.liis.su/`.

The bot:
- opens the site in a real browser (Playwright),
- logs in if login form is visible,
- selects office and seat by UI interaction,
- books date range from today up to `+7` days by default,
- clicks booking controls in UI,
- sends Telegram notification on success/failure,
- sends confirmation screenshot file to Telegram,
- saves screenshot and browser session state for debugging and re-use.

## 1) Quick start (local)

```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt
python -m playwright install chromium
```

Create config:

```bash
# Windows PowerShell
Copy-Item .env.example .env
# Linux/macOS
cp .env.example .env
```

Fill `.env`:
- `TARGET_OFFICE`, `TARGET_SEAT` are required.
- set `USERNAME` and `PASSWORD` for LDAP form auto-fill.
- add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` for alerts.
- set `OTP_CODE_INPUT_SELECTOR` for OTP screen.  
  If `OTP_CODE_VALUE` is empty, bot will request OTP code in Telegram and wait for your reply.

Run once:

```bash
python -m booking_bot
```

## 1.1) Windows setup without admin rights (recommended flow)

### Step 1. Install Python for current user

1. Download installer: `python-3.12.x-amd64.exe` from `python.org`.
2. Run installer.
3. On first screen:
   - enable `Add python.exe to PATH`;
   - click `Customize installation`.
4. Keep `pip`, `venv`, `py launcher` enabled.
5. In `Advanced Options`:
   - disable `Install for all users`;
   - choose install path under your profile (for example: `%LocalAppData%\Programs\Python\Python312`).
6. Finish install.

Open a new PowerShell and verify:

```powershell
python --version
pip --version
```

If `python` is still not found, run once in current terminal:

```powershell
$env:Path="$env:LocalAppData\Programs\Python\Python312;$env:LocalAppData\Programs\Python\Python312\Scripts;$env:Path"
python --version
```

### Step 2. Prepare project

```powershell
cd "C:\Users\YourUserId\OneDrive - leroymerlin.ru\Documents\workplace_booking"
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Step 3. Install Playwright browser binaries (no admin)

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH="0"
python -m playwright install chromium
```

### Step 4. Configure and run local test

```powershell
Copy-Item .env.example .env
notepad .env
```

Minimum values for first test:
- `TARGET_OFFICE=...`
- `TARGET_SEAT=...`
- `HEADLESS=false`
- `RUN_MODE=once`
- `TELEGRAM_BOT_TOKEN=...`
- `TELEGRAM_CHAT_ID=...`

Run:

```powershell
python -m booking_bot
```

## 1.2) Capture exact selectors from your real UI

1. Open booking page in browser and login manually.
2. Open DevTools Console.
3. Paste content of `scripts/capture_selectors_browser.js`.
4. Start guided mode: `__bookingCapture.lemana()`.
5. For each prompted step do `Ctrl+Shift+Click` on target element.
   Then confirm capture: `__bookingCapture.ok()`.
   If wrong element was captured: `__bookingCapture.retry()`.
   If you need one step back: `__bookingCapture.back()`.
   If a step does not exist in your UI, run `__bookingCapture.skip()`.
   OTP code step is included. You can either enter OTP manually (bot waits) or set `OTP_CODE_VALUE`.
   After redirect to another domain (for example SSO), paste script again in console:
   saved state restores automatically and flow continues from last step.
6. Execute `__bookingCapture.env()` and paste output into `.env`.
7. Stop helper with `__bookingCapture.stop()`.

## 2) How selectors work

Different UI builds can expose different HTML selectors, so most selectors are configurable.

- `PRE_LOGIN_CLICK_SELECTORS`: optional click targets before login form handling (for SSO entry button).
- `OTP_CODE_INPUT_SELECTOR`: OTP input (or first digit input) shown after SSO submit.
- `OTP_CODE_VALUE`: optional static OTP value. Usually keep it empty and reply with code in Telegram.
- `OTP_WAIT_TIMEOUT_MS`: how long to wait for OTP completion.
- `OFFICE_CHOOSE_SELECTOR`: click direct office action on `/offices` page.
- `OFFICE_OPEN_SELECTOR`: click this first if office list is behind dropdown/modal.
- `OFFICE_OPTION_SELECTOR_TEMPLATE`: selector for office option.
- `OFFICE_MAP_READY_SELECTOR`: optional selector that confirms map UI is ready after office click.
- `OFFICE_MAP_WAIT_TIMEOUT_MS`: max wait for map screen after office click.
- `OFFICE_MAP_EXTRA_WAIT_MS`: additional fixed delay after map is ready (for slow UI rendering).
- `OFFICE_MAP_LOADING_SELECTORS`: optional loader selectors to wait for hidden state after map is visible.
- `OFFICE_MAP_LOADING_WAIT_TIMEOUT_MS`: max wait for loader disappearance.
- `BOOKING_PARAMS_OPEN_SELECTOR`: open booking parameters panel on map page.
- `BOOKING_DATE_INPUT_SELECTOR`: date input in booking parameters.
- If `BOOKING_PARAMS_OPEN_SELECTOR` fails, bot now tries a date-like text fallback automatically.
- `BOOKING_DATE_DAY_SELECTOR_TEMPLATE`: optional fallback selector for day cell (supports `{day}`).
- `BOOKING_RANGE_DAYS`: date range size from today (default `7`, inclusive).
- `BOOKING_INCLUDE_TODAY`: include current day in range.
- `BOOKING_SKIP_WEEKENDS`: optional weekend skip in range mode.
- `BOOKING_PER_DATE_ATTEMPTS`: retries per date before marking it failed.
- If `BOOKING_DATE_VALUE` or `BOOKING_DATE_OFFSET_DAYS` is set, bot runs in single-date mode.
- In range mode bot auto-switches calendar month when target date is in next month.
- `BOOKING_TYPE_SELECTOR`: booking type chooser opener.
- `BOOKING_TYPE_OPTION_SELECTOR` or `BOOKING_TYPE_VALUE`: target option in booking type chooser.
- `BOOKING_TIME_FROM_SELECTOR` / `BOOKING_TIME_TO_SELECTOR`: time range inputs.
- `SEAT_SEARCH_SELECTOR`: optional search field before seat click.
- `SEAT_SELECTOR_TEMPLATE`: selector for seat element.
- `SEAT_CANVAS_SELECTOR` + `SEAT_CANVAS_INDEX` + `SEAT_CANVAS_X/Y`: fallback for canvas-based seat maps.
- `BOOK_BUTTON_SELECTOR`: selector for final booking button.
- `SUCCESS_SELECTOR` or `SUCCESS_TEXT`: positive confirmation check.
- `SUCCESS_CLOSE_SELECTOR`: close success modal between dates.

Templates support placeholders:
- `{office}` or `{office_name}` -> value from `TARGET_OFFICE`
- `{seat}` or `{seat_name}` -> value from `TARGET_SEAT`

Example:

```env
OFFICE_OPTION_SELECTOR_TEMPLATE=[data-office-name="{office}"]
SEAT_SELECTOR_TEMPLATE=[data-seat-name="{seat}"]
BOOK_BUTTON_SELECTOR=button[data-testid="book-seat"]
SUCCESS_TEXT=Booking created
```

To discover selectors quickly:

```bash
playwright codegen https://lemana.simple-office-web.liis.su/
```

If you want semantic notes after each click (recommended for tricky date picker):

```bash
python scripts/annotated_selector_recorder.py --url "https://lemana.simple-office-web.liis.su/"
```

Simple mode:
- click in browser,
- press Enter in terminal,
- write free-text note ("what you did").

Useful commands in terminal:
- `Enter` capture next click,
- `p` show pending clicks,
- `q` finish.

Outputs:
- `artifacts/selector_annotations.json`
- `artifacts/selector_annotations.env`

If you cannot run Playwright locally, use browser DevTools helper:

```bash
scripts/capture_selectors_browser.js
```

Open file content, paste it into browser console on the booking page, then run:
- `__bookingCapture.lemana()` for guided SSO/map flow

For each step, do `Ctrl+Shift+Click` and then `__bookingCapture.ok()`.
If a step is not needed, run `__bookingCapture.skip()`.
Then run:
- `__bookingCapture.dump()` (JSON),
- `__bookingCapture.env()` (ready `.env` lines),
- `__bookingCapture.stop()`.

## 3) First login strategy

If SSO/MFA/captcha appears, full unattended login may be blocked by your identity provider.
Recommended setup:
1. configure `USERNAME`, `PASSWORD`, `OTP_CODE_INPUT_SELECTOR`, Telegram bot token/chat id;
2. when OTP page appears, bot sends a Telegram message and waits for your 6-digit reply;
3. bot saves session state to `STORAGE_STATE_PATH` and re-uses it next runs.

If session is still valid, OTP/login steps are skipped automatically.

## 4) Telegram notifications

Create bot in Telegram via `@BotFather`, then set:
- `TELEGRAM_BOT_TOKEN=...`
- `TELEGRAM_CHAT_ID=...`

Messages include:
- start of each attempt,
- status (success/failure) for each attempt,
- per-day status in range mode (booked/skipped/failed),
- retry notification before next attempt,
- OTP request/timeout status when OTP screen is detected,
- attempt number,
- seat/office,
- UTC timestamp,
- screenshot file path.
- screenshot file attachment in Telegram (`sendDocument`).

## 5) Autonomous run modes

- `RUN_MODE=once`: one run, with internal retries (`RETRY_ATTEMPTS`, `RETRY_DELAY_SEC`).
- `RUN_MODE=daemon`: infinite loop, runs every `RUN_INTERVAL_MINUTES`.

## 6) Docker run

```bash
cp .env.example .env
# edit .env
docker compose up -d --build
docker compose logs -f workplace-booking
```

## 7) VPS deployment (recommended for reliability)

Use any Linux VPS:
1. install Docker and Docker Compose plugin;
2. clone repo, create `.env`, set selectors and Telegram;
3. run `docker compose up -d --build`;
4. container restarts automatically on reboot (`restart: unless-stopped`).

If you prefer no daemon loop, set `RUN_MODE=once` and trigger by OS scheduler:
- cron (Linux),
- Task Scheduler (Windows).

Ready-made `systemd` templates are provided:
- `deploy/systemd/workplace-booking.service`
- `deploy/systemd/workplace-booking.timer`

### VPS quick deployment (Ubuntu 22.04/24.04, 4 GB RAM)

1. Connect to VPS:

```bash
ssh <user>@<vps_ip>
```

2. Install prerequisites:

```bash
sudo apt update
sudo apt install -y git docker.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
newgrp docker
```

3. Clone and configure:

```bash
git clone <your_repo_url> workplace_booking
cd workplace_booking
cp .env.example .env
nano .env
```

Set at least:
- `TARGET_OFFICE`
- `TARGET_SEAT`
- `RUN_MODE=daemon`
- `RUN_INTERVAL_MINUTES=30` (or your interval)
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

4. Start bot:

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f workplace-booking
```

5. Auto-restart after reboot is already enabled by Compose (`restart: unless-stopped`).

## 8) Free cloud deployment (typical pattern)

For free tiers, most stable approach is scheduled CI:
- GitHub Actions scheduled workflow runs `python -m booking_bot` on interval,
- repository secrets store `.env` values,
- screenshots can be uploaded as artifacts.

Ready workflow:
- `.github/workflows/booking.yml`

Some PaaS free tiers can also run cron jobs, but limits/availability change often.
For booking-critical flows, VPS is usually more stable.

## 9) Security notes

- Never commit `.env` with credentials.
- Use a dedicated account with minimum required permissions if possible.
- Rotate credentials and Telegram token periodically.
