"""
Capture raw table marker payloads while a user navigates manually in the browser.

Usage:
  python scripts/seat_id_probe.py --url "https://lemana.simple-office-web.liis.su/"

What it does:
- opens Playwright Chromium in headed mode
- reuses `.state/storage_state.json` if available
- saves every `/api/web/floor/table_markers` response to
  `artifacts/seat_id_probe_YYYYmmdd_HHMMSS/`
- takes a screenshot next to each captured payload for quick visual correlation

How to use:
1) Run the script.
2) In the opened browser, log in if needed.
3) Open the target office/floor and navigate to the desired hard/seat.
4) Close the browser window when done.
5) Share nothing else; the artifact stays in this repo and can be parsed locally.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import BrowserContext, Page, Playwright, Response, sync_playwright


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


class TableMarkerCapture:
    def __init__(self, out_dir: Path, page: Page) -> None:
        self.out_dir = out_dir
        self.page = page
        self.counter = 0

    def on_response(self, response: Response) -> None:
        parsed = urlparse(response.url)
        if parsed.path != "/api/web/floor/table_markers":
            return

        try:
            payload = response.json()
        except Exception as exc:
            self._write_json(
                name=f"table_markers_{self.counter + 1:03d}_error.json",
                payload={
                    "url": response.url,
                    "status": response.status,
                    "error": f"{exc.__class__.__name__}: {exc}",
                },
            )
            return

        self.counter += 1
        base_name = f"table_markers_{self.counter:03d}"
        markers = payload.get("table_markers")
        record = {
            "captured_at_local": datetime.now().isoformat(),
            "url": response.url,
            "status": response.status,
            "query": parse_qs(parsed.query),
            "markers_count": len(markers) if isinstance(markers, list) else None,
            "payload": payload,
            "page_url": self.page.url,
        }
        self._write_json(name=f"{base_name}.json", payload=record)

        screenshot_path = self.out_dir / f"{base_name}.png"
        try:
            self.page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            pass

        short_path = self.out_dir / "latest_capture.txt"
        short_path.write_text(
            "\n".join(
                [
                    f"capture_index={self.counter}",
                    f"url={response.url}",
                    f"page_url={self.page.url}",
                    f"json={self.out_dir / (base_name + '.json')}",
                    f"screenshot={screenshot_path}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        print(
            f"[seat-id-probe] captured #{self.counter}: "
            f"{len(markers) if isinstance(markers, list) else '?'} markers"
        )

    def _write_json(self, name: str, payload: dict[str, Any]) -> None:
        (self.out_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def new_context(playwright: Playwright) -> BrowserContext:
    browser = playwright.chromium.launch(headless=False)
    kwargs: dict[str, Any] = {}
    storage_state_path = Path(".state/storage_state.json")
    if storage_state_path.exists():
        kwargs["storage_state"] = str(storage_state_path)
    return browser.new_context(**kwargs)


def run_probe(args: argparse.Namespace) -> int:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir) / f"seat_id_probe_{ts}"
    ensure_dir(out_dir)

    with sync_playwright() as playwright:
        context = new_context(playwright)
        page = context.new_page()
        page.set_default_timeout(args.default_timeout_ms)
        capture = TableMarkerCapture(out_dir=out_dir, page=page)
        page.on("response", capture.on_response)

        print(f"[seat-id-probe] artifacts: {out_dir}")
        print("[seat-id-probe] browser is opening. Navigate manually, then close the window.")
        page.goto(args.url, wait_until="domcontentloaded")

        page.wait_for_event("close", timeout=args.duration_sec * 1000)

        try:
            context.storage_state(path=str(out_dir / "storage_state_after.json"))
        except Exception:
            pass
        try:
            context.close()
        except Exception:
            pass

    print(f"[seat-id-probe] finished. Artifacts: {out_dir}")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture raw table_markers responses.")
    parser.add_argument(
        "--url",
        default="https://lemana.simple-office-web.liis.su/",
        help="Start URL.",
    )
    parser.add_argument(
        "--out-dir",
        default="artifacts",
        help="Base output directory.",
    )
    parser.add_argument(
        "--duration-sec",
        type=int,
        default=3600,
        help="Maximum duration while waiting for the browser to be closed.",
    )
    parser.add_argument(
        "--default-timeout-ms",
        type=int,
        default=30000,
        help="Playwright default timeout in ms.",
    )
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    return run_probe(args)


if __name__ == "__main__":
    raise SystemExit(main())
