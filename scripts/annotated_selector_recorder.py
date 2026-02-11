"""
Annotated selector recorder with in-page overlay.

Usage:
  python scripts/annotated_selector_recorder.py --url "https://lemana.simple-office-web.liis.su/"

Flow:
1) Browser opens.
2) Click an element in the page.
3) Overlay panel appears in the same page with click details.
4) Add free-text note and press Save (or Skip).
5) Press Finish in overlay when done.

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

  const STATE_PREFIX = "__annSelectorState:";
  const STYLE_ID = "__ann_selector_style";
  const PANEL_ID = "__ann_selector_panel";
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

  function parsePersistedState() {
    const raw = window.name || "";
    if (!raw.startsWith(STATE_PREFIX)) return null;
    const encoded = raw.slice(STATE_PREFIX.length);
    try {
      const parsed = JSON.parse(decodeURIComponent(encoded));
      if (!parsed || typeof parsed !== "object") return null;
      return parsed;
    } catch (_err) {
      return null;
    }
  }

  function normalizeState(state) {
    const out = state && typeof state === "object" ? state : {};
    return {
      prevWindowName: typeof out.prevWindowName === "string" ? out.prevWindowName : "",
      records: Array.isArray(out.records) ? out.records : [],
      pending: out.pending && typeof out.pending === "object" ? out.pending : null,
      stop: !!out.stop,
    };
  }

  const persisted = parsePersistedState();
  const state = normalizeState(
    persisted ||
      {
        prevWindowName: window.name || "",
        records: [],
        pending: null,
        stop: false,
      }
  );

  function syncGlobals() {
    window.__annSelectorRecords = state.records;
    window.__annSelectorPending = state.pending;
    window.__annSelectorStop = state.stop;
  }

  function persistState() {
    const payload = {
      prevWindowName: state.prevWindowName,
      records: state.records,
      pending: state.pending,
      stop: state.stop,
    };
    window.name = STATE_PREFIX + encodeURIComponent(JSON.stringify(payload));
    syncGlobals();
  }

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    if (!document.documentElement) return;
    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = `
      #${PANEL_ID} {
        position: fixed;
        right: 12px;
        bottom: 12px;
        width: min(460px, calc(100vw - 24px));
        background: rgba(17, 24, 39, 0.97);
        color: #f8fafc;
        z-index: 2147483647;
        border-radius: 10px;
        box-shadow: 0 10px 30px rgba(0, 0, 0, 0.45);
        font: 13px/1.35 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
        padding: 10px;
      }
      #${PANEL_ID} .ann-title {
        font-weight: 700;
        margin-bottom: 6px;
      }
      #${PANEL_ID} .ann-muted {
        color: #cbd5e1;
      }
      #${PANEL_ID} .ann-row {
        margin-top: 6px;
      }
      #${PANEL_ID} code {
        display: block;
        white-space: pre-wrap;
        word-break: break-word;
        max-height: 80px;
        overflow: auto;
        margin-top: 2px;
        padding: 4px 6px;
        border-radius: 6px;
        background: rgba(15, 23, 42, 0.95);
      }
      #${PANEL_ID} textarea {
        width: 100%;
        min-height: 72px;
        resize: vertical;
        border: 1px solid #475569;
        border-radius: 8px;
        background: #0f172a;
        color: #f8fafc;
        padding: 8px;
        margin-top: 6px;
        box-sizing: border-box;
      }
      #${PANEL_ID} .ann-actions {
        display: flex;
        gap: 8px;
        margin-top: 8px;
      }
      #${PANEL_ID} button {
        border: 1px solid #64748b;
        border-radius: 8px;
        background: #1e293b;
        color: #f8fafc;
        padding: 6px 10px;
        cursor: pointer;
      }
      #${PANEL_ID} button.ann-primary {
        background: #0f766e;
        border-color: #14b8a6;
      }
      #${PANEL_ID} button.ann-danger {
        background: #7f1d1d;
        border-color: #ef4444;
      }
      #${PANEL_ID} .ann-hidden {
        display: none;
      }
    `;
    document.documentElement.appendChild(style);
  }

  function ensurePanel() {
    let panel = document.getElementById(PANEL_ID);
    if (panel) return panel;
    if (!document.documentElement) return null;

    function el(tag, props) {
      const node = document.createElement(tag);
      const cfg = props || {};
      if (cfg.id) node.id = cfg.id;
      if (cfg.className) node.className = cfg.className;
      if (cfg.text !== undefined && cfg.text !== null) node.textContent = String(cfg.text);
      return node;
    }

    function rowWithCode(label, codeId) {
      const row = el("div", { className: "ann-row" });
      row.appendChild(document.createTextNode(label));
      const code = el("code", { id: codeId });
      row.appendChild(code);
      return row;
    }

    panel = document.createElement("div");
    panel.id = PANEL_ID;

    const title = el("div", { className: "ann-title", text: "Selector Recorder" });
    const status = el("div", { className: "ann-muted", id: "__ann_status" });

    const idle = el("div", {
      id: "__ann_idle_block",
      className: "ann-row",
      text: "Click target in page. Then add a note and press Save.",
    });

    const pending = el("div", { id: "__ann_pending_block", className: "ann-hidden" });
    pending.appendChild(rowWithCode("URL", "__ann_url"));
    pending.appendChild(rowWithCode("Selector", "__ann_selector"));
    pending.appendChild(rowWithCode("Text", "__ann_text"));

    const noteRow = el("div", { className: "ann-row" });
    noteRow.appendChild(document.createTextNode("Note"));
    const noteInput = el("textarea", { id: "__ann_note" });
    noteInput.setAttribute("placeholder", "Example: clicked date picker opener");
    noteRow.appendChild(noteInput);
    pending.appendChild(noteRow);

    const actions = el("div", { className: "ann-actions" });
    const saveButton = el("button", {
      id: "__ann_save",
      className: "ann-primary",
      text: "Save",
    });
    saveButton.setAttribute("type", "button");
    const skipButton = el("button", { id: "__ann_skip", text: "Skip" });
    skipButton.setAttribute("type", "button");
    const finishButton = el("button", {
      id: "__ann_finish",
      className: "ann-danger",
      text: "Finish",
    });
    finishButton.setAttribute("type", "button");
    actions.appendChild(saveButton);
    actions.appendChild(skipButton);
    actions.appendChild(finishButton);

    panel.appendChild(title);
    panel.appendChild(status);
    panel.appendChild(idle);
    panel.appendChild(pending);
    panel.appendChild(actions);

    document.documentElement.appendChild(panel);

    saveButton.addEventListener("click", () => {
      if (!state.pending) return;
      const note = String(noteInput.value || "").trim();
      const rec = Object.assign({}, state.pending, { note });
      state.records.push(rec);
      state.pending = null;
      noteInput.value = "";
      persistState();
      renderPanel();
      console.log(`[recorder] saved #${state.records.length}: ${rec.selector}`);
    });

    skipButton.addEventListener("click", () => {
      if (!state.pending) return;
      state.pending = null;
      noteInput.value = "";
      persistState();
      renderPanel();
      console.log("[recorder] pending click skipped");
    });

    finishButton.addEventListener("click", () => {
      state.stop = true;
      persistState();
      renderPanel();
      console.log("[recorder] finish requested");
    });

    noteInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        saveButton.click();
      }
      if (event.key === "Escape") {
        event.preventDefault();
        skipButton.click();
      }
    });

    return panel;
  }

  let lastFocusedPendingId = "";

  function renderPanel() {
    let panel = document.getElementById(PANEL_ID);
    if (!panel) {
      ensureStyle();
      panel = ensurePanel();
    }
    if (!panel) return;
    const statusEl = panel.querySelector("#__ann_status");
    const idleBlock = panel.querySelector("#__ann_idle_block");
    const pendingBlock = panel.querySelector("#__ann_pending_block");
    const urlEl = panel.querySelector("#__ann_url");
    const selectorEl = panel.querySelector("#__ann_selector");
    const textEl = panel.querySelector("#__ann_text");
    const noteInput = panel.querySelector("#__ann_note");
    const saveButton = panel.querySelector("#__ann_save");
    const skipButton = panel.querySelector("#__ann_skip");

    if (
      !statusEl ||
      !idleBlock ||
      !pendingBlock ||
      !urlEl ||
      !selectorEl ||
      !textEl ||
      !noteInput ||
      !saveButton ||
      !skipButton
    ) {
      return;
    }

    statusEl.textContent = state.stop
      ? `Finished. Captured: ${state.records.length}.`
      : `Captured: ${state.records.length}.`;

    const hasPending = !!state.pending;
    idleBlock.classList.toggle("ann-hidden", hasPending);
    pendingBlock.classList.toggle("ann-hidden", !hasPending);

    if (state.stop) {
      panel.style.opacity = "0.88";
      saveButton.disabled = true;
      skipButton.disabled = true;
    } else {
      panel.style.opacity = "1";
      saveButton.disabled = !hasPending;
      skipButton.disabled = !hasPending;
    }

    if (!hasPending) return;

    const pending = state.pending;
    urlEl.textContent = String(pending.url || "");
    selectorEl.textContent = String(pending.selector || "");
    textEl.textContent = String(pending.text || "");

    if (pending.recId && pending.recId !== lastFocusedPendingId) {
      lastFocusedPendingId = pending.recId;
      setTimeout(() => {
        noteInput.focus();
        noteInput.select();
      }, 0);
    }
  }

  document.addEventListener("click", (event) => {
    if (state.stop) return;
    const panel = document.getElementById(PANEL_ID);
    if (panel && panel.contains(event.target)) return;
    if (state.pending) return;

    const el = event.target;
    if (!el || !el.tagName) return;

    const rect = el.getBoundingClientRect();
    state.pending = {
      recId: `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`,
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
      offsetY: Number((event.clientY || 0) - rect.top),
      note: "",
    };

    persistState();
    renderPanel();
  }, true);

  syncGlobals();
  persistState();
  renderPanel();
  console.log("[recorder] overlay ready. Click element, annotate in panel, press Save.");
})();
"""


@dataclass
class CaptureItem:
    index: int
    rec_id: str
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


def _read_snapshot(page: Page) -> dict | None:
    try:
        snapshot = page.evaluate(
            """() => ({
                records: Array.isArray(window.__annSelectorRecords) ? window.__annSelectorRecords : [],
                stop: !!window.__annSelectorStop,
                pending: window.__annSelectorPending || null
            })"""
        )
    except Exception:
        return None

    if not isinstance(snapshot, dict):
        return None
    return snapshot


def _record_identity(rec: dict) -> str:
    rec_id = rec.get("recId")
    if isinstance(rec_id, str) and rec_id:
        return rec_id
    parts = [
        str(rec.get("ts", "")),
        str(rec.get("url", "")),
        str(rec.get("selector", "")),
        str(rec.get("clickX", "")),
        str(rec.get("clickY", "")),
    ]
    return "|".join(parts)


def _print_record(rec: dict, index: int) -> None:
    print("")
    print(f"[{index}] Click saved")
    print(f"URL:      {rec.get('url', '')}")
    print(f"Selector: {rec.get('selector', '')}")
    print(f"Text:     {rec.get('text', '')}")
    print(f"Tag:      {rec.get('tag', '')}")
    print(
        "Offset:   "
        f"x={int(rec.get('offsetX', 0))}, y={int(rec.get('offsetY', 0))}"
    )
    print(f"Note:     {rec.get('note', '')}")


def _capture_with_overlay(context: BrowserContext) -> list[CaptureItem]:
    out: list[CaptureItem] = []
    seen_ids: set[str] = set()
    index = 1
    waiting_pending_hint_shown = False

    print("Recorder started.")
    print("Use page overlay: click element, add note, press Save.")
    print("Press Finish in overlay when done.")

    while True:
        alive_pages = [page for page in context.pages if not page.is_closed()]
        if not alive_pages:
            break

        stop_requested = False
        pending_present = False

        for page in alive_pages:
            _ensure_injected(page)
            snapshot = _read_snapshot(page)
            if snapshot is None:
                continue

            records = snapshot.get("records", [])
            if isinstance(records, list):
                for rec in records:
                    if not isinstance(rec, dict):
                        continue
                    rec_key = _record_identity(rec)
                    if rec_key in seen_ids:
                        continue
                    seen_ids.add(rec_key)
                    _print_record(rec, index)
                    out.append(
                        CaptureItem(
                            index=index,
                            rec_id=str(rec.get("recId", "")),
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

            if snapshot.get("pending") is not None:
                pending_present = True
            if bool(snapshot.get("stop")):
                stop_requested = True

        if pending_present and not waiting_pending_hint_shown:
            print("Pending click detected: add note in overlay and press Save/Skip.")
            waiting_pending_hint_shown = True
        if not pending_present:
            waiting_pending_hint_shown = False

        if stop_requested:
            break

        time.sleep(0.25)

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
        help="ENV output path (from notes that match ENV_KEY style).",
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
            items = _capture_with_overlay(context)
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
