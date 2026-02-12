"""
Behavior probe for workplace booking UI.

Purpose:
- Capture real UI behavior and API traffic while you act manually in browser.
- Produce artifacts to debug date/calendar logic without blind selector clicking.

Usage:
  python scripts/behavior_probe.py --url "https://lemana.simple-office-web.liis.su/"

How to use:
1) Run script (browser opens).
2) Perform full flow manually: login -> OTP -> office -> date -> seat -> booking.
3) Close browser window (or wait until timeout).
4) Share artifacts folder contents.

Outputs (artifacts/behavior_probe_YYYYmmdd_HHMMSS):
- events.jsonl                 timeline of clicks, requests, responses, snapshots, navigations
- summary.json                endpoint counts and session info
- trace.zip                   Playwright trace (open via `playwright show-trace trace.zip`)
- session.har                 HAR with network calls (can be disabled by --no-har)
- screenshots/*.png           navigation snapshots
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import BrowserContext, Page, Playwright, Request, Response, sync_playwright


INJECT_CLICK_CAPTURE = r"""
(() => {
  if (window.__behaviorProbeInstalled) return;
  window.__behaviorProbeInstalled = true;
  window.__behaviorProbeClicks = [];

  const IGNORE_CLASS_PREFIXES = ["ant-", "css-", "sc-", "rc-", "react-", "__"];

  function esc(value) {
    if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(value);
    return String(value).replace(/["\\]/g, "\\$&");
  }

  function stableClasses(el) {
    return Array.from(el.classList || []).filter(
      (cls) => !IGNORE_CLASS_PREFIXES.some((prefix) => cls.startsWith(prefix))
    );
  }

  function attrSelector(el) {
    const dt = el.getAttribute("data-testid");
    if (dt) return `[data-testid="${esc(dt)}"]`;
    const id = el.getAttribute("id");
    if (id && !/^(root|app|main)$/i.test(id)) return `#${esc(id)}`;
    const name = el.getAttribute("name");
    if (name) return `[name="${esc(name)}"]`;
    const role = el.getAttribute("role");
    if (role) return `[role="${esc(role)}"]`;
    return "";
  }

  function baseSelector(el) {
    const tag = (el.tagName || "").toLowerCase() || "*";
    const attr = attrSelector(el);
    const classes = stableClasses(el).slice(0, 2).map((c) => `.${esc(c)}`).join("");
    return `${tag}${attr}${classes}`;
  }

  function uniqueSelector(el) {
    const root = el.ownerDocument || document;
    const base = baseSelector(el);
    try {
      if (base && root.querySelectorAll(base).length === 1) return base;
    } catch (_err) {}

    const parts = [];
    let node = el;
    let depth = 0;
    while (node && node.nodeType === 1 && depth < 6) {
      let part = baseSelector(node);
      const parent = node.parentElement;
      if (parent && !attrSelector(node)) {
        const sameTag = Array.from(parent.children).filter((child) => child.tagName === node.tagName);
        if (sameTag.length > 1) {
          const idx = sameTag.indexOf(node) + 1;
          part += `:nth-of-type(${idx})`;
        }
      }
      parts.unshift(part);
      const candidate = parts.join(" > ");
      try {
        if (candidate && root.querySelectorAll(candidate).length === 1) return candidate;
      } catch (_err) {}
      node = node.parentElement;
      depth += 1;
    }
    return parts.join(" > ") || base || "*";
  }

  function norm(text) {
    return (text || "").replace(/\s+/g, " ").trim();
  }

  document.addEventListener("click", (event) => {
    const el = event.target;
    if (!el || !el.tagName) return;
    const rec = {
      ts: new Date().toISOString(),
      selector: uniqueSelector(el),
      text: norm(el.innerText || el.textContent || "").slice(0, 140),
      tag: String(el.tagName || "").toLowerCase(),
      role: el.getAttribute && (el.getAttribute("role") || ""),
      class_name: el.className || "",
      url: location.href,
      x: event.clientX,
      y: event.clientY
    };
    window.__behaviorProbeClicks.push(rec);
    if (window.__behaviorProbeClicks.length > 400) {
      window.__behaviorProbeClicks = window.__behaviorProbeClicks.slice(-250);
    }
  }, true);
})();
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def clamp_text(value: str | None, limit: int = 1200) -> str | None:
    if value is None:
        return None
    if len(value) <= limit:
        return value
    return value[:limit] + "...<truncated>"


def safe_json_dump(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def collect_ui_snapshot(page: Page) -> dict[str, Any]:
    return page.evaluate(
        """() => {
          const norm = (v) => String(v || "").replace(/\\s+/g, " ").trim();
          const getText = (selectors) => {
            for (const s of selectors) {
              const el = document.querySelector(s);
              if (!el) continue;
              const tag = (el.tagName || "").toLowerCase();
              if (tag === "input" || tag === "textarea") {
                const v = norm(el.value || el.getAttribute("value") || "");
                if (v) return v;
              }
              const text = norm(el.innerText || el.textContent || "");
              if (text) return text;
            }
            return "";
          };

          const listboxOptions = Array.from(
            document.querySelectorAll('[role="listbox"] [role="option"]')
          ).slice(0, 80).map((option) => {
            const dayNode =
              option.querySelector('[data-testid="Day"] span') ||
              option.querySelector('[data-testid="Day"]') ||
              option.querySelector("span");
            return {
              day_text: norm(dayNode?.textContent || ""),
              option_text: norm(option.textContent || ""),
              class_name: option.className || "",
              aria_selected: option.getAttribute("aria-selected") || "",
              aria_disabled: option.getAttribute("aria-disabled") || "",
            };
          });

          const visibleInputs = Array.from(document.querySelectorAll("input, textarea"))
            .filter((el) => {
              const st = window.getComputedStyle(el);
              return st.display !== "none" && st.visibility !== "hidden";
            })
            .slice(0, 20)
            .map((el) => ({
              name: el.getAttribute("name") || "",
              id: el.getAttribute("id") || "",
              placeholder: el.getAttribute("placeholder") || "",
              type: el.getAttribute("type") || "",
              value: norm(el.value || ""),
            }));

          const selectedDateText = getText([
            'div.bWMguj > div.aTSiK > div > div.eNxvnb',
            '[data-testid="date-input"]',
            '.ant-picker-input input',
            '[role="listbox"] [role="option"][aria-selected="true"] [data-testid="Day"]',
          ]);

          const calendarHeader = getText([
            '.ant-picker-header-view',
            '[class*="calendar"] [class*="header"]',
            '[role="listbox"] [class*="month"]',
          ]);

          return {
            href: location.href,
            title: document.title,
            selected_date_text: selectedDateText,
            calendar_header_text: calendarHeader,
            calendar_open:
              !!document.querySelector('.ant-picker-panel') ||
              !!document.querySelector('[role="listbox"] [role="option"]'),
            listbox_options: listboxOptions,
            visible_inputs: visibleInputs,
          };
        }"""
    )


class Recorder:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.screenshots_dir = out_dir / "screenshots"
        ensure_dir(self.screenshots_dir)
        self.events_path = out_dir / "events.jsonl"
        self.events_file = self.events_path.open("w", encoding="utf-8")
        self.api_path_counter: Counter[str] = Counter()
        self.requests_count = 0
        self.responses_count = 0
        self.clicks_count = 0
        self.snapshots_count = 0
        self.navigations_count = 0

    def close(self) -> None:
        self.events_file.close()

    def write(self, kind: str, payload: dict[str, Any]) -> None:
        rec = {"ts_utc": utc_now(), "kind": kind, **payload}
        self.events_file.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.events_file.flush()

    def on_request(self, request: Request) -> None:
        parsed = urlparse(request.url)
        self.requests_count += 1
        if parsed.path.startswith("/api/"):
            self.api_path_counter[parsed.path] += 1
        self.write(
            "request",
            {
                "method": request.method,
                "url": request.url,
                "path": parsed.path,
                "query": parse_qs(parsed.query),
                "resource_type": request.resource_type,
                "post_data": clamp_text(request.post_data),
            },
        )

    def on_response(self, response: Response) -> None:
        parsed = urlparse(response.url)
        self.responses_count += 1
        self.write(
            "response",
            {
                "status": response.status,
                "ok": response.ok,
                "url": response.url,
                "path": parsed.path,
            },
        )

    def on_navigation(self, page: Page) -> None:
        self.navigations_count += 1
        self.write(
            "navigation",
            {
                "url": page.url,
                "title": page.title(),
            },
        )
        shot_path = self.screenshots_dir / f"nav_{self.navigations_count:03d}.png"
        try:
            page.screenshot(path=str(shot_path), full_page=True)
            self.write("screenshot", {"path": str(shot_path)})
        except Exception as exc:
            self.write("screenshot_error", {"error": f"{exc.__class__.__name__}: {exc}"})

    def write_summary(self, started_utc: str, finished_utc: str) -> None:
        summary = {
            "started_utc": started_utc,
            "finished_utc": finished_utc,
            "requests_count": self.requests_count,
            "responses_count": self.responses_count,
            "clicks_count": self.clicks_count,
            "snapshots_count": self.snapshots_count,
            "navigations_count": self.navigations_count,
            "api_paths": self.api_path_counter.most_common(),
            "events_file": str(self.events_path),
        }
        safe_json_dump(self.out_dir / "summary.json", summary)


def new_context(playwright: Playwright, record_har: bool, har_path: Path | None) -> BrowserContext:
    browser = playwright.chromium.launch(headless=False)
    kwargs: dict[str, Any] = {}
    storage_state_path = Path(".state/storage_state.json")
    if storage_state_path.exists():
        kwargs["storage_state"] = str(storage_state_path)
    if record_har and har_path is not None:
        kwargs["record_har_path"] = str(har_path)
        kwargs["record_har_mode"] = "full"
    return browser.new_context(**kwargs)


def run_probe(args: argparse.Namespace) -> int:
    started_utc = utc_now()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir) / f"behavior_probe_{ts}"
    ensure_dir(out_dir)
    recorder = Recorder(out_dir)

    with sync_playwright() as p:
        context = new_context(
            playwright=p,
            record_har=not args.no_har,
            har_path=out_dir / "session.har",
        )
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        context.add_init_script(INJECT_CLICK_CAPTURE)

        page = context.new_page()
        page.set_default_timeout(args.default_timeout_ms)

        page.on("request", recorder.on_request)
        page.on("response", recorder.on_response)
        page.on(
            "framenavigated",
            lambda frame: recorder.on_navigation(page) if frame == page.main_frame else None,
        )

        page.goto(args.url, wait_until="domcontentloaded")
        recorder.on_navigation(page)

        started_mono = time.monotonic()
        next_snapshot = 0.0
        next_click_poll = 0.0

        recorder.write(
            "session_start",
            {
                "url": args.url,
                "duration_sec": args.duration_sec,
                "default_timeout_ms": args.default_timeout_ms,
                "note": "Perform actions manually. Close browser window when done.",
            },
        )

        while time.monotonic() - started_mono < args.duration_sec:
            if page.is_closed():
                break

            now = time.monotonic()
            if now >= next_click_poll:
                try:
                    clicks = page.evaluate(
                        """() => {
                          const out = Array.isArray(window.__behaviorProbeClicks)
                            ? window.__behaviorProbeClicks.slice()
                            : [];
                          window.__behaviorProbeClicks = [];
                          return out;
                        }"""
                    )
                    for click in clicks:
                        recorder.clicks_count += 1
                        recorder.write("click", click)
                except Exception:
                    recorder.write("warning", {"message": "Failed to poll clicks"})
                next_click_poll = now + 0.7

            if now >= next_snapshot:
                try:
                    snap = collect_ui_snapshot(page)
                    recorder.snapshots_count += 1
                    recorder.write("snapshot", snap)
                except Exception as exc:
                    recorder.write(
                        "snapshot_error",
                        {"error": f"{exc.__class__.__name__}: {exc}"},
                    )
                next_snapshot = now + args.snapshot_interval_sec

            time.sleep(0.2)

        finished_utc = utc_now()
        recorder.write("session_finish", {"reason": "timeout_or_browser_closed"})

        trace_path = out_dir / "trace.zip"
        try:
            context.tracing.stop(path=str(trace_path))
            recorder.write("trace", {"path": str(trace_path)})
        except Exception as exc:
            recorder.write("trace_error", {"error": f"{exc.__class__.__name__}: {exc}"})

        try:
            context.storage_state(path=str(out_dir / "storage_state_after.json"))
        except Exception as exc:
            recorder.write(
                "storage_state_error",
                {"error": f"{exc.__class__.__name__}: {exc}"},
            )

        try:
            context.close()
        except Exception:
            pass

    recorder.write_summary(started_utc=started_utc, finished_utc=finished_utc)
    recorder.close()
    print(f"Behavior probe finished. Artifacts: {out_dir}")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture UI/API behavior for booking flow.")
    parser.add_argument(
        "--url",
        default="https://lemana.simple-office-web.liis.su/",
        help="Start URL for probe.",
    )
    parser.add_argument(
        "--out-dir",
        default="artifacts",
        help="Base output directory.",
    )
    parser.add_argument(
        "--duration-sec",
        type=int,
        default=1800,
        help="Maximum probe duration in seconds (default: 1800).",
    )
    parser.add_argument(
        "--snapshot-interval-sec",
        type=float,
        default=2.0,
        help="UI snapshot interval in seconds (default: 2.0).",
    )
    parser.add_argument(
        "--default-timeout-ms",
        type=int,
        default=30000,
        help="Playwright default timeout in ms.",
    )
    parser.add_argument(
        "--no-har",
        action="store_true",
        help="Disable HAR recording.",
    )
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    return run_probe(args)


if __name__ == "__main__":
    raise SystemExit(main())
