"""Capture full-page screenshots of every app screen for the UI redesign record.

Used by redesign Phase 0 (docs/redesign/before/) and Phase 10 (docs/redesign/after/).
Drives the locally-running app (python app.py, port 8000) through the installed
Google Chrome via Playwright — no bundled browser download needed.

Usage:
    .venv/bin/python scripts/capture_screens.py --out docs/redesign/before \
        --email redesign.shots@local.test --password Shots-2026-local

The login account must already exist (create one with agent_team.customers.CustomerStore).
"""
import argparse
import os
import sys

from playwright.sync_api import sync_playwright

VIEWS = ["chatbot", "fusion", "team", "models", "unified", "brain"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--out", required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    saved, failed = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = ctx.new_page()

        def shot(name, full=True):
            path = os.path.join(args.out, name)
            page.screenshot(path=path, full_page=full)
            saved.append(name)
            print("saved", path)

        # 1. Logged-out landing.
        page.goto(args.base)
        page.wait_for_load_state("networkidle")
        shot("01-landing.png")

        # 2. Log in via the API (sets the session cookie in this browser context).
        status = page.evaluate(
            """async ([email, password]) => {
                 const r = await fetch('/api/login', {method: 'POST',
                   headers: {'Content-Type': 'application/json'},
                   body: JSON.stringify({email, password})});
                 return r.status;
               }""",
            [args.email, args.password])
        if status != 200:
            print(f"FATAL: login failed with HTTP {status}", file=sys.stderr)
            sys.exit(1)
        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(800)

        # 3. Onboarding wizard (auto-opens on first login while not completed).
        try:
            if page.locator("#onboardView[aria-hidden='false']").count():
                shot("02-wizard.png", full=False)
                page.click("#onboardClose")
            else:
                page.click("#menuBtn")
                page.wait_for_timeout(300)
                if page.locator("#menuRectOnboard:visible").count():
                    page.click("#menuRectOnboard")
                    page.wait_for_timeout(500)
                    shot("02-wizard.png", full=False)
                    page.click("#onboardClose")
                else:
                    page.keyboard.press("Escape")
                    failed.append(("wizard", "menu item not visible"))
            page.wait_for_timeout(300)
        except Exception as e:
            failed.append(("wizard", str(e)))
            page.keyboard.press("Escape")

        # 4. Each drawer view inside the detail window.
        for i, view in enumerate(VIEWS, start=3):
            name = f"{i:02d}-{view}.png"
            try:
                page.click("#menuBtn")
                page.wait_for_timeout(300)
                page.click(f".menu-rect[data-view='{view}']")
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(900)
                shot(name)
                page.click("#detailClose")
                page.wait_for_timeout(200)
            except Exception as e:
                failed.append((view, str(e)))
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass

        # 5. Logged-in home (hero + account widget) for completeness.
        try:
            page.wait_for_timeout(300)
            shot("09-home-logged-in.png")
        except Exception as e:
            failed.append(("home-logged-in", str(e)))

        browser.close()

    print(f"\n{len(saved)} screenshots saved to {args.out}")
    if failed:
        print("FAILED:", failed)
        sys.exit(2)


if __name__ == "__main__":
    main()
