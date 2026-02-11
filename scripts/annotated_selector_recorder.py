"""
Interactive Playwright click recorder with semantic annotations.

How to use:
1) Activate venv with Playwright installed.
2) Run:
   python scripts/annotated_selector_recorder.py --url "https://lemana.simple-office-web.liis.su/"
3) In opened browser, perform your flow manually.
4) After each click in browser, terminal asks for:
   - label (example: BOOKING_PARAMS_OPEN_SELECTOR)
   - note (human description)
5) Type /done in label prompt to finish.

Outputs:
- artifacts/selector_annotations.json
- artifacts/selector_annotations.env
"""

from __future__ import annotations

from dataclasses import dataclass
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
    const rec = {
      ts: new Date().toISOString(),
      url: location.href,
      selector: uniqueSelector(el),
      text: textSnippet(el),
      tag: (el.tagName || "").toLowerCase(),
      role: el.getAttribute("role") || "",
      placeholder: el.getAttribute("placeholder") || "",
      className: String(el.className || "").slice(0, 200),
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


def _build_env_lines(items: list[CaptureItem]) -> list[str]:
    last_by_key: dict[str, str] = {}
    for item in items:
        key = item.label.strip()
        if not key:
            continue
        if not re.fullmatch(r"[A-Z0-9_]+", key):
            continue
        last_by_key[key] = item.selector
    return [f"{k}={v}" for k, v in last_by_key.items()]


def _print_record(rec: dict, index: int) -> None:
    print("")
    print(f"[{index}] Click captured")
    print(f"URL: {rec.get('url', '')}")
    print(f"Selector: {rec.get('selector', '')}")
    print(f"Text: {rec.get('text', '')}")
    print(f"Tag: {rec.get('tag', '')}")


def _capture_loop(context: BrowserContext) -> list[CaptureItem]:
    offsets: dict[int, int] = {}
    out: list[CaptureItem] = []
    click_index = 0
    done = False

    print("Recorder started. Perform clicks in browser.")
    print("After each click, enter label/note in terminal.")
    print("Label examples: BOOKING_PARAMS_OPEN_SELECTOR, BOOKING_DATE_INPUT_SELECTOR")
    print("Type /done in label prompt to finish.")

    while not done:
        pages = [p for p in context.pages if not p.is_closed()]
        for page in pages:
            _ensure_injected(page)
            pid = id(page)
            offset = offsets.get(pid, 0)
            rows, new_offset = _drain_new_records(page, offset)
            offsets[pid] = new_offset

            for rec in rows:
                click_index += 1
                _print_record(rec, click_index)
                label = input("Label (or /done): ").strip()
                if label == "/done":
                    done = True
                    break
                note = input("Note (what this click means): ").strip()

                item = CaptureItem(
                    ts=str(rec.get("ts", "")),
                    url=str(rec.get("url", "")),
                    selector=str(rec.get("selector", "")),
                    text=str(rec.get("text", "")),
                    tag=str(rec.get("tag", "")),
                    role=str(rec.get("role", "")),
                    placeholder=str(rec.get("placeholder", "")),
                    class_name=str(rec.get("className", "")),
                    label=label,
                    note=note,
                    recorded_at_utc=datetime.now(timezone.utc).isoformat(),
                )
                out.append(item)
                print("Saved.")
            if done:
                break
        if not done:
            time.sleep(0.2)
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
        help="ENV output path (labels in UPPER_CASE only).",
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
            print("")
            print("Interrupted. Saving collected data.")
        finally:
            browser.close()

    rows = [item.__dict__ for item in items]
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
