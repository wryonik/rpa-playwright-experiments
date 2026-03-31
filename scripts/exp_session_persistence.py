"""
Experiment 3: Session Persistence & Cookie Reuse
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Simulates what the backend does with RPASession cookies:
  1. Login and save cookies to disk
  2. Close the browser completely
  3. Reopen a new browser, restore cookies
  4. Verify we land on form.html without re-logging in
  5. Measure cold-start time (login) vs warm-start time (cookie restore)

Also tests what happens when cookies expire / are invalid.

This directly validates whether session persistence in RPASession is
worth implementing — and how much login time it saves per workflow.

Usage:
    python exp_session_persistence.py
    python exp_session_persistence.py --runs 5  # repeat 5 times for avg
"""
import argparse
import asyncio
import json
import os
import time

from playwright.async_api import async_playwright

from utils import FORM_URL, LOGIN_URL, SITE_BASE_URL


COOKIE_FILE = "/tmp/portalx_cookies.json"


# ── Cold start: full login ────────────────────────────────────────────────────

async def cold_start(pw, headless: bool) -> dict:
    """Full login from scratch. Returns cookies + timing."""
    t = time.perf_counter()
    browser = await pw.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
    context = await browser.new_context()
    page = await context.new_page()

    page.on("dialog", lambda d: asyncio.create_task(d.accept()))

    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await page.fill("#username", "admin")
    await page.fill("#password", "password123")
    await page.click("#login-btn")
    await page.wait_for_url("**/form.html", timeout=5000)

    login_time = time.perf_counter() - t

    # Save cookies (this is what RPASession.cookies stores)
    cookies = await context.cookies()
    await browser.close()

    return {"cookies": cookies, "login_time_s": login_time}


# ── Warm start: cookie restore ────────────────────────────────────────────────

async def warm_start(pw, cookies: list, headless: bool) -> dict:
    """Open browser with pre-loaded cookies. Measure time to reach form."""
    t = time.perf_counter()
    browser = await pw.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
    context = await browser.new_context()

    # Restore cookies — same as what PlaywrightBrowser.launch() does
    if cookies and cookies != [{}]:
        await context.add_cookies(cookies)

    page = await context.new_page()
    page.on("dialog", lambda d: asyncio.create_task(d.accept()))

    await page.goto(FORM_URL, wait_until="domcontentloaded")

    # Check if we're actually on the form (not redirected to login)
    current_url = page.url
    reached_form = "form.html" in current_url

    restore_time = time.perf_counter() - t
    await browser.close()

    return {"reached_form": reached_form, "restore_time_s": restore_time, "landed_at": current_url}


# ── Expired/invalid cookies ───────────────────────────────────────────────────

async def expired_cookie_start(pw, headless: bool) -> dict:
    """
    Test what happens when saved cookies are expired or tampered.
    Expected: redirect to login.html.
    """
    bad_cookies = [
        {
            "name": "portalx_session",
            "value": "expired_or_invalid_token",
            "domain": "localhost",
            "path": "/",
        }
    ]
    result = await warm_start(pw, bad_cookies, headless)
    result["test"] = "expired_cookies"
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(args):
    headless = args.headless.lower() != "false"
    runs = args.runs

    cold_times = []
    warm_times = []
    warm_success = 0

    print(f"\nRunning {runs} iteration(s)...\n")

    async with async_playwright() as pw:
        for i in range(runs):
            # Cold start
            cold = await cold_start(pw, headless)
            cold_times.append(cold["login_time_s"])
            cookies = cold["cookies"]
            print(f"  Run {i+1} cold start:  {cold['login_time_s']:.2f}s  "
                  f"({len(cookies)} cookies saved)")

            # Warm start with same cookies
            warm = await warm_start(pw, cookies, headless)
            warm_times.append(warm["restore_time_s"])
            if warm["reached_form"]:
                warm_success += 1
            print(f"  Run {i+1} warm start:  {warm['restore_time_s']:.2f}s  "
                  f"reached_form={warm['reached_form']}")

    # Test expired cookies (once)
    async with async_playwright() as pw:
        expired = await expired_cookie_start(pw, headless)
        print(f"\n  Expired cookie test: reached_form={expired['reached_form']}  "
              f"landed_at={expired['landed_at']}")

    # Summary
    avg_cold = sum(cold_times) / len(cold_times)
    avg_warm = sum(warm_times) / len(warm_times)
    savings = avg_cold - avg_warm
    savings_pct = (savings / avg_cold) * 100

    print(f"\n{'='*50}")
    print(f"  Session Persistence Results ({runs} runs)")
    print(f"{'='*50}")
    print(f"  Avg cold start (full login): {avg_cold:.2f}s")
    print(f"  Avg warm start (cookies):    {avg_warm:.2f}s")
    print(f"  Time saved per workflow:     {savings:.2f}s ({savings_pct:.0f}%)")
    print(f"  Warm start success rate:     {warm_success}/{runs}")
    print(f"  Expired cookies redirect:    {'✓ correctly to login' if not expired['reached_form'] else '✗ unexpected'}")

    # Extrapolated savings
    print(f"\n  Extrapolated savings (assuming 80% session reuse):")
    for daily in [500, 2000, 10000]:
        reused = daily * 0.8
        saved_hrs = (reused * savings) / 3600
        print(f"    {daily:>6} workflows/day → ~{saved_hrs:.0f} browser-hours/month saved")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3, help="Number of iterations (default: 3)")
    parser.add_argument("--headless", default="true")
    asyncio.run(main(parser.parse_args()))
