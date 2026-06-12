"""Capture full-page screenshots of every app screen for the UI redesign record.

Used by redesign Phase 0 (docs/redesign/before/ — the pre-redesign overlay UI)
and Phase 10 (docs/redesign/after/ — the History-API SPA this script targets).
Drives the locally-running app (python app.py, port 8000) through the installed
Google Chrome via Playwright — no bundled browser download needed.

Usage:
    .venv/bin/python scripts/capture_screens.py --out docs/redesign/after \
        --email redesign.shots@local.test --password Shots-2026-local \
        [--customer-email plain.shots@local.test --customer-password Shots-2026-local]

For full coverage the --email account should be BOTH is_admin (its email in
$ADMIN_EMAILS — the 内部工具 nav + founder pages render) AND on 企业版 (the
schedule editors render instead of the upsell). The optional second account
should be a plain free customer — it captures the gated/upsell states (D3
schedule pitch, free-quota account page). Accounts must already exist
(create with agent_team.customers.CustomerStore; subscribe via metrics_store).
"""
import argparse
import os
import sys

from playwright.sync_api import sync_playwright

# (path, slug) — every customer route the History-API router serves (plan §5).
CUSTOMER_PAGES = [
    ("/",         "workbench"),
    ("/new",      "report-new"),
    ("/reports",  "reports"),
    ("/schedule", "schedule"),
    ("/brain",    "brain"),
    ("/account",  "account"),
]
FOUNDER_PAGES = [
    ("/founder/team",   "founder-team"),
    ("/founder/costs",  "founder-costs"),
    ("/founder/models", "founder-models"),
    ("/founder/fusion", "founder-fusion"),
    ("/founder/api",    "founder-api"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--out", required=True)
    ap.add_argument("--email", required=True, help="is_admin + 企业版 account")
    ap.add_argument("--password", required=True)
    ap.add_argument("--customer-email", help="plain free customer (gated states)")
    ap.add_argument("--customer-password")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    saved, failed = [], []
    counter = {"n": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = ctx.new_page()

        def shot(slug, locator=None, full=True):
            counter["n"] += 1
            name = f"{counter['n']:02d}-{slug}.png"
            path = os.path.join(args.out, name)
            (locator or page).screenshot(path=path, **({} if locator else {"full_page": full}))
            saved.append(name)
            print("saved", path)

        def login(email, password):
            status = page.evaluate(
                """async ([email, password]) => {
                     const r = await fetch('/api/login', {method: 'POST',
                       headers: {'Content-Type': 'application/json'},
                       body: JSON.stringify({email, password})});
                     return r.status;
                   }""", [email, password])
            if status != 200:
                print(f"FATAL: login {email} failed with HTTP {status}", file=sys.stderr)
                sys.exit(1)

        def logout():
            page.evaluate("() => fetch('/api/logout', {method: 'POST'})")

        def goto(path, settle=900):
            page.goto(args.base + path)
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(settle)  # skeletons → data; sparklines render

        def close_wizard_if_open():
            try:
                v = page.locator("#onboardView[aria-hidden='false']")
                if v.count():
                    page.click("#onboardClose")
                    page.wait_for_timeout(300)
            except Exception:
                pass

        # 1. Logged-out landing (the marketing page IS the "/" route pre-login).
        goto("/")
        shot("landing")

        # 2. Founder/企业版 account → every customer page (#view = the routed page).
        login(args.email, args.password)
        for path, slug in CUSTOMER_PAGES:
            try:
                goto(path)
                close_wizard_if_open()
                shot(slug, locator=page.locator("#view"))
            except Exception as e:
                failed.append((slug, str(e)))

        # 3. The newest viewable web report (/reports/<id>) if one exists.
        try:
            run_id = page.evaluate(
                """async () => {
                     const d = await (await fetch('/api/reports')).json();
                     const row = (d.reports || []).find((r) => r.has_content);
                     return row ? row.id : null;
                   }""")
            if run_id is not None:
                goto(f"/reports/{run_id}", settle=1200)
                shot("report-view", locator=page.locator("#view"))
            else:
                failed.append(("report-view", "no viewable report on this account"))
        except Exception as e:
            failed.append(("report-view", str(e)))

        # 4. The 6-step wizard (?onboarding=demo deep link opens it at step 2).
        try:
            goto("/?onboarding=demo", settle=1200)
            wizard = page.locator("#onboardView[aria-hidden='false']")
            if wizard.count():
                shot("wizard", locator=page.locator("#onboardView"))
                page.click("#onboardClose")
            else:
                failed.append(("wizard", "overlay did not open"))
        except Exception as e:
            failed.append(("wizard", str(e)))

        # 5. The founder machinery views (is_admin only — the 内部工具 nav).
        for path, slug in FOUNDER_PAGES:
            try:
                goto(path)
                shot(slug, locator=page.locator("#view"))
            except Exception as e:
                failed.append((slug, str(e)))

        # 6. Plain-customer pass — the gated/upsell states (D3) + free quota.
        if args.customer_email and args.customer_password:
            logout()
            login(args.customer_email, args.customer_password)
            for path, slug in [("/schedule", "schedule-upsell"),
                               ("/account", "account-free"),
                               ("/", "workbench-free")]:
                try:
                    goto(path)
                    close_wizard_if_open()
                    shot(slug, locator=page.locator("#view"))
                except Exception as e:
                    failed.append((slug, str(e)))

        browser.close()

    print(f"\n{len(saved)} screenshots saved to {args.out}")
    if failed:
        print("FAILED:", failed)
        sys.exit(2)


if __name__ == "__main__":
    main()
