"""
Simple free-form Playwright click recorder.

Flow:
1) Run script, browser opens.
2) Click element in browser.
3) In terminal press Enter.
4) Script shows captured selector and asks:
   - "What did you do?" (free text)
   - optional short label (can be empty)
5) Repeat.
6) Type q in terminal to finish.

Outputs:
- artifacts/selector_annotations.json
- artifacts/selector_annotations.env (only from uppercase labels)
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
    note: str
    label: str
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


def _wait_for_record(
    context: BrowserContext,
    offsets: dict[int, int],
    pending: list[dict],
) -> dict:
    while True:
        _pump_records(context, offsets, pending)
        if pending:
            return pending.pop(0)
        time.sleep(0.2)


def _print_record(rec: dict, index: int) -> None:
    print("")
    print(f"[{index}] Click captured")
    print(f"URL:      {rec.get('url', '')}")
    print(f"Selector: {rec.get('selector', '')}")
    print(f"Text:     {rec.get('text', '')}")
    print(f"Tag:      {rec.get('tag', '')}")
    print(
        "Offset:   "
        f"x={int(rec.get('offsetX', 0))}, y={int(rec.get('offsetY', 0))}"
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
        if key == "SEAT_SELECTOR_TEMPLATE" and (item.tag == "canvas" or "canvas" in item.selector):
            seat_canvas_item = item

    lines = [f"{k}={v}" for k, v in last_by_key.items()]

    if seat_canvas_item is not None:
        lines.append("SEAT_CANVAS_SELECTOR=canvas")
        match = re.search(r"canvas:nth-of-type\((\d+)\)", seat_canvas_item.selector)
        if match:
            lines.append(f"SEAT_CANVAS_INDEX={int(match.group(1)) - 1}")
        lines.append(f"SEAT_CANVAS_X={int(round(seat_canvas_item.click_offset_x))}")
        lines.append(f"SEAT_CANVAS_Y={int(round(seat_canvas_item.click_offset_y))}")

    return lines


def _capture_loop(context: BrowserContext) -> list[CaptureItem]:
    offsets: dict[int, int] = {}
    pending: list[dict] = []
    out: list[CaptureItem] = []
    index = 1

    print("Simple recorder started.")
    print("Do action in browser -> press Enter in terminal -> write note.")
    print("Commands:")
    print("  Enter: capture next click")
    print("  p: show number of pending clicks")
    print("  q: finish")

    while True:
        cmd = input("\nCommand [Enter/p/q]: ").strip().lower()
        if cmd == "q":
            break
        if cmd == "p":
            _pump_records(context, offsets, pending)
            print(f"Pending clicks: {len(pending)}")
            continue

        rec = _wait_for_record(context, offsets, pending)
        _print_record(rec, index)
        note = input("What did you do? ").strip()
        label = input("Optional label (empty if you do not need it): ").strip()

        out.append(
            CaptureItem(
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
                note=note,
                label=label,
                recorded_at_utc=datetime.now(timezone.utc).isoformat(),
            )
        )
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
        help="ENV output path (only uppercase labels).",
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
            items = _capture_loop(context)
        except KeyboardInterrupt:
            print("\nInterrupted. Saving collected data.")
        finally:
            browser.close()

    output_path.write_text(
        json.dumps([asdict(item) for item in items], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    env_output_path.write_text("\n".join(_build_env_lines(items)), encoding="utf-8")

    print(f"\nSaved annotations: {output_path}")
    print(f"Saved env lines:   {env_output_path}")
    print("Send me file content and I will map selectors to project config.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

