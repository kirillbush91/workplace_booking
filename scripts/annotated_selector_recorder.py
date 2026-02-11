"""
Simple inline click recorder.

Usage:
  python scripts/annotated_selector_recorder.py --url "https://lemana.simple-office-web.liis.su/"

Flow:
1) Browser opens.
2) You click one element in browser.
3) Browser asks annotation via prompt ("What did you do?").
4) Repeat.
5) Finish by closing browser or typing /q in annotation prompt.

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
  window.__annSelectorStop = false;

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
    const selector = uniqueSelector(el);
    const text = textSnippet(el);
    const annotation = window.prompt(
      "What did you do?\\nType /q to finish recording.",
      ""
    );
    if (annotation !== null && annotation.trim() === "/q") {
      window.__annSelectorStop = true;
    }
    const rec = {
      ts: new Date().toISOString(),
      url: location.href,
      selector: selector,
      text: text,
      tag: (el.tagName || "").toLowerCase(),
      role: el.getAttribute("role") || "",
      placeholder: el.getAttribute("placeholder") || "",
      className: String(el.className || "").slice(0, 200),
      clickX: Number(event.clientX || 0),
      clickY: Number(event.clientY || 0),
      offsetX: Number((event.clientX || 0) - rect.left),
      offsetY: Number((event.clientY || 0) - rect.top),
      note: annotation == null ? "" : String(annotation)
    };
    window.__annSelectorRecords.push(rec);
  }, true);
})();
"""


@dataclass
class CaptureItem:
    index: int
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
    print(f"Note:     {rec.get('note', '')}")


def _is_stop_requested(context: BrowserContext) -> bool:
    pages = [p for p in context.pages if not p.is_closed()]
    for page in pages:
        try:
            flag = page.evaluate("() => !!window.__annSelectorStop")
            if flag:
                return True
        except Exception:
            continue
    return False


def _capture_with_inline_annotations(context: BrowserContext) -> list[CaptureItem]:
    offsets: dict[int, int] = {}
    pending: list[dict] = []
    out: list[CaptureItem] = []
    index = 1

    print("Recorder started.")
    print("Click in browser and annotate directly in browser prompt.")
    print("Finish: close browser or type /q in browser prompt.")

    while True:
        _pump_records(context, offsets, pending)

        while pending:
            rec = pending.pop(0)
            _print_record(rec, index)

            out.append(
                CaptureItem(
                    index=index,
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
                    note=str(rec.get("note", "")),
                    recorded_at_utc=datetime.now(timezone.utc).isoformat(),
                )
            )
            index += 1

        if _is_stop_requested(context):
            break

        alive_pages = [p for p in context.pages if not p.is_closed()]
        if not alive_pages:
            break
        time.sleep(0.2)

    return out


def _build_env_lines(items: list[CaptureItem]) -> list[str]:
    last_by_key: dict[str, str] = {}
    seat_canvas_item: CaptureItem | None = None

    for item in items:
        key = item.note.strip()
        if not key:
            continue
        if not re.fullmatch(r"[A-Z0-9_]+", key):
            continue
        if item.selector:
            last_by_key[key] = item.selector
        if key == "SEAT_SELECTOR_TEMPLATE" and (
            item.tag == "canvas" or "canvas" in item.selector
        ):
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
        help="ENV output path (from optional labels).",
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
            items = _capture_with_inline_annotations(context)
        except KeyboardInterrupt:
            print("\nInterrupted.")
        finally:
            browser.close()

    output_path.write_text(
        json.dumps([asdict(item) for item in items], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    env_output_path.write_text("\n".join(_build_env_lines(items)), encoding="utf-8")

    print(f"\nSaved annotations: {output_path}")
    print(f"Saved env lines:   {env_output_path}")
    print("Send me selector_annotations.json; I will map steps and update project.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
