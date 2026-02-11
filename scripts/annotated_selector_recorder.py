"""
Guided Playwright selector recorder with optional notes.

Default mode is guided: labels are predefined, user does not type label names.

Run:
  python scripts/annotated_selector_recorder.py --url "https://lemana.simple-office-web.liis.su/"

Flow:
  1) Terminal shows a step (label + hint).
  2) Click target in browser.
  3) Confirm in terminal with Enter.
  4) Optional note can be added in plain language.

Commands during guided flow:
  - Enter: continue / save
  - r: retry current step
  - s: skip current step
  - b: go back one step
  - q: finish now

Outputs:
  - artifacts/selector_annotations.json
  - artifacts/selector_annotations.env
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import re
import time

from playwright.sync_api import BrowserContext, Page, sync_playwright


INJECT_SCRIPT = r"""
(() => {
  if (window.__annSelectorInstalled) return;
  window.__annSelectorInstalled = true;
  window.__annSelectorRecords = [];

  const IGNORE_CLASS_PREFIXES = ["ant-", "css-", "sc-", "rc-", "react-", "__"];
  const MAX_TEXT_LENGTH = 120;

  function escapeCss(value) {
    if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(value);
    return String(value).replace(/["\\]/g, "\\$&");
  }

  function normText(text) {
    return (text || "").replace(/\s+/g, " ").trim();
  }

  function textSnippet(el) {
    return normText(el.innerText || el.textContent || "").slice(0, MAX_TEXT_LENGTH);
  }

  function stableClasses(el) {
    return Array.from(el.classList || []).filter(
      (cls) => !IGNORE_CLASS_PREFIXES.some((prefix) => cls.startsWith(prefix))
    );
  }

  function attrSelector(el) {
    const dataTestId = el.getAttribute("data-testid");
    if (dataTestId) return `[data-testid="${escapeCss(dataTestId)}"]`;

    const dataTestIdLegacy = el.getAttribute("data-test-id");
    if (dataTestIdLegacy) return `[data-test-id="${escapeCss(dataTestIdLegacy)}"]`;

    const id = el.getAttribute("id");
    if (id && !/^(root|app|main)$/i.test(id)) return `#${escapeCss(id)}`;

    const name = el.getAttribute("name");
    if (name) return `[name="${escapeCss(name)}"]`;

    const role = el.getAttribute("role");
    if (role) return `[role="${escapeCss(role)}"]`;

    const placeholder = el.getAttribute("placeholder");
    if (placeholder) return `[placeholder="${escapeCss(placeholder)}"]`;

    const title = el.getAttribute("title");
    if (title) return `[title="${escapeCss(title)}"]`;

    return "";
  }

  function baseSelector(el) {
    const tag = (el.tagName || "").toLowerCase() || "*";
    const attr = attrSelector(el);
    const classes = stableClasses(el)
      .slice(0, 2)
      .map((cls) => `.${escapeCss(cls)}`)
      .join("");
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
    while (node && node.nodeType === 1 && depth < 7) {
      let part = baseSelector(node);
      const parent = node.parentElement;
      if (parent && !attrSelector(node)) {
        const sameTagSiblings = Array.from(parent.children).filter(
          (child) => child.tagName === node.tagName
        );
        if (sameTagSiblings.length > 1) {
          const index = sameTagSiblings.indexOf(node) + 1;
          part += `:nth-of-type(${index})`;
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

  document.addEventListener("click", (event) => {
    const el = event.target;
    if (!el || !el.tagName) return;
    const rect = el.getBoundingClientRect();
    const rec = {
      ts: new Date().toISOString(),
      url: location.href,
      selector: uniqueSelector(el),
      text: textSnippet(el),
      tag: (el.tagName || "").toLowerCase(),
      role: el.getAttribute("role") || "",
      placeholder: el.getAttribute("placeholder") || "",
      className: String(el.className || "").slice(0, 200),
      clickX: Number(event.clientX || 0),
      clickY: Number(event.clientY || 0),
      offsetX: Number((event.clientX || 0) - rect.left),
      offsetY: Number((event.clientY || 0) - rect.top)
    };
    window.__annSelectorRecords.push(rec);
  }, true);
})();
"""


@dataclass
class GuidedStep:
    label: str
    hint: str


GUIDED_STEPS = [
    GuidedStep("LOGIN_SSO_BUTTON_SELECTOR", "Login page: SSO button"),
    GuidedStep("LOGIN_SUBMIT_SELECTOR", "SSO page: submit/login button"),
    GuidedStep("OTP_CODE_INPUT_SELECTOR", "OTP page: code input (or first digit input)"),
    GuidedStep("OFFICE_CHOOSE_SELECTOR", "Offices page: choose/open required office"),
    GuidedStep("BOOKING_PARAMS_OPEN_SELECTOR", "Map: open booking/date parameters"),
    GuidedStep("BOOKING_DATE_INPUT_SELECTOR", "Map: date input/button"),
    GuidedStep("BOOKING_TYPE_SELECTOR", "Map: booking type selector (optional)"),
    GuidedStep("BOOKING_TYPE_OPTION_SELECTOR", "Map: booking type option (optional)"),
    GuidedStep("BOOKING_TIME_FROM_SELECTOR", "Map: time start input (optional)"),
    GuidedStep("BOOKING_TIME_TO_SELECTOR", "Map: time end input (optional)"),
    GuidedStep("BOOKING_PARAMS_CLOSE_SELECTOR", "Map: close/apply parameters (optional)"),
    GuidedStep("SEAT_SELECTOR_TEMPLATE", "Map: click target seat (or seat canvas)"),
    GuidedStep("BOOK_BUTTON_SELECTOR", "Modal: book button"),
    GuidedStep("SUCCESS_SELECTOR", "Success modal: success text/title"),
    GuidedStep("SUCCESS_CLOSE_SELECTOR", "Success modal: close button"),
]


@dataclass
class CaptureItem:
    ts: str
    url: str
    selector: str
    text: str
    tag: str
    role: str
    placeholder: str
    class_name: str
    click_x: float
    click_y: float
    click_offset_x: float
    click_offset_y: float
    label: str
    note: str
    recorded_at_utc: str


def _ensure_injected(page: Page) -> None:
    try:
        page.evaluate(INJECT_SCRIPT)
    except Exception:
        pass


def _drain_new_records(page: Page, offset: int) -> tuple[list[dict], int]:
    try:
        total = page.evaluate("() => (window.__annSelectorRecords || []).length")
    except Exception:
        return [], offset

    if not isinstance(total, int):
        return [], offset
    if total < offset:
        offset = 0

    try:
        rows = page.evaluate(
            "(start) => (window.__annSelectorRecords || []).slice(start)",
            offset,
        )
    except Exception:
        return [], offset

    if not isinstance(rows, list):
        return [], offset
    return rows, offset + len(rows)


def _pump_records(
    context: BrowserContext,
    offsets: dict[int, int],
    pending: list[dict],
) -> None:
    pages = [p for p in context.pages if not p.is_closed()]
    for page in pages:
        _ensure_injected(page)
        pid = id(page)
        offset = offsets.get(pid, 0)
        rows, new_offset = _drain_new_records(page, offset)
        offsets[pid] = new_offset
        pending.extend(rows)


def _wait_for_next_record(
    context: BrowserContext,
    offsets: dict[int, int],
    pending: list[dict],
) -> dict:
    while True:
        _pump_records(context, offsets, pending)
        if pending:
            return pending.pop(0)
        time.sleep(0.2)


def _print_record(rec: dict, label: str, hint: str) -> None:
    print("")
    print(f"Captured for: {label}")
    print(f"Hint:         {hint}")
    print(f"URL:          {rec.get('url', '')}")
    print(f"Selector:     {rec.get('selector', '')}")
    print(f"Text:         {rec.get('text', '')}")
    print(f"Tag:          {rec.get('tag', '')}")
    ox = rec.get("offsetX", 0)
    oy = rec.get("offsetY", 0)
    print(f"Offset:       x={ox}, y={oy}")


def _make_item(rec: dict, label: str, note: str) -> CaptureItem:
    return CaptureItem(
        ts=str(rec.get("ts", "")),
        url=str(rec.get("url", "")),
        selector=str(rec.get("selector", "")),
        text=str(rec.get("text", "")),
        tag=str(rec.get("tag", "")),
        role=str(rec.get("role", "")),
        placeholder=str(rec.get("placeholder", "")),
        class_name=str(rec.get("className", "")),
        click_x=float(rec.get("clickX", 0.0)),
        click_y=float(rec.get("clickY", 0.0)),
        click_offset_x=float(rec.get("offsetX", 0.0)),
        click_offset_y=float(rec.get("offsetY", 0.0)),
        label=label,
        note=note,
        recorded_at_utc=datetime.now(timezone.utc).isoformat(),
    )


def _make_skipped_item(label: str, note: str) -> CaptureItem:
    now = datetime.now(timezone.utc).isoformat()
    return CaptureItem(
        ts="",
        url="",
        selector="",
        text="",
        tag="",
        role="",
        placeholder="",
        class_name="",
        click_x=0.0,
        click_y=0.0,
        click_offset_x=0.0,
        click_offset_y=0.0,
        label=label,
        note=note or "skipped",
        recorded_at_utc=now,
    )


def _build_env_lines(items: list[CaptureItem]) -> list[str]:
    last_by_key: dict[str, str] = {}
    seat_canvas_item: CaptureItem | None = None

    for item in items:
        key = item.label.strip()
        if not key:
            continue
        if not re.fullmatch(r"[A-Z0-9_]+", key):
            continue
        if item.selector:
            last_by_key[key] = item.selector

        if key == "SEAT_SELECTOR_TEMPLATE" and item.selector:
            if item.tag == "canvas" or "canvas" in item.selector:
                seat_canvas_item = item

    lines: list[str] = [f"{k}={v}" for k, v in last_by_key.items()]

    if seat_canvas_item is not None:
        lines.append("SEAT_CANVAS_SELECTOR=canvas")
        match = re.search(r"canvas:nth-of-type\((\d+)\)", seat_canvas_item.selector)
        if match:
            lines.append(f"SEAT_CANVAS_INDEX={int(match.group(1)) - 1}")
        lines.append(f"SEAT_CANVAS_X={int(round(seat_canvas_item.click_offset_x))}")
        lines.append(f"SEAT_CANVAS_Y={int(round(seat_canvas_item.click_offset_y))}")

    return lines


def _capture_loop_guided(context: BrowserContext) -> list[CaptureItem]:
    offsets: dict[int, int] = {}
    pending: list[dict] = []
    out: list[CaptureItem] = []
    index = 0

    print("Guided recorder started.")
    print("You do NOT type label names.")
    print("For each step: click element in browser, then confirm in terminal.")

    while index < len(GUIDED_STEPS):
        step = GUIDED_STEPS[index]
        print("")
        print(f"Step {index + 1}/{len(GUIDED_STEPS)}: {step.label}")
        print(f"Hint: {step.hint}")
        command = input("Command [Enter=start, s=skip, b=back, q=finish]: ").strip().lower()
        if command == "q":
            break
        if command == "b":
            if out:
                out.pop()
                index = max(0, index - 1)
            continue
        if command == "s":
            out.append(_make_skipped_item(step.label, "skipped by user"))
            index += 1
            continue

        print("Now click target element in browser...")
        rec = _wait_for_next_record(context, offsets, pending)
        _print_record(rec, step.label, step.hint)
        action = input("Action [Enter=save, r=retry, s=skip, b=back, q=finish]: ").strip().lower()
        if action == "q":
            break
        if action == "b":
            if out:
                out.pop()
                index = max(0, index - 1)
            continue
        if action == "s":
            out.append(_make_skipped_item(step.label, "skipped by user"))
            index += 1
            continue
        if action == "r":
            continue

        note = input("Optional note (Enter to skip): ").strip()
        out.append(_make_item(rec, step.label, note))
        print("Saved.")
        index += 1

    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url",
        default="https://lemana.simple-office-web.liis.su/",
        help="Start URL.",
    )
    parser.add_argument(
        "--output",
        default="artifacts/selector_annotations.json",
        help="JSON output path.",
    )
    parser.add_argument(
        "--env-output",
        default="artifacts/selector_annotations.env",
        help="ENV output path.",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    env_output_path = Path(args.env_output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    env_output_path.parent.mkdir(parents=True, exist_ok=True)

    items: list[CaptureItem] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        context.add_init_script(INJECT_SCRIPT)
        page = context.new_page()
        page.goto(args.url)
        _ensure_injected(page)

        try:
            items = _capture_loop_guided(context)
        except KeyboardInterrupt:
            print("")
            print("Interrupted. Saving collected data.")
        finally:
            browser.close()

    rows = [asdict(item) for item in items]
    output_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    env_lines = _build_env_lines(items)
    env_output_path.write_text("\n".join(env_lines), encoding="utf-8")

    print("")
    print(f"Saved annotations: {output_path}")
    print(f"Saved env lines:   {env_output_path}")
    print("Send me these files or ask me to read them from workspace.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

