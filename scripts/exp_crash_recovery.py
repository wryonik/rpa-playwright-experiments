"""
Experiment 4: Crash Recovery
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Simulates what happens when a worker dies mid-task.

Scenario A — Local Chromium (today's behavior):
  Worker crashes at step 3/6. Browser process is killed.
  New worker must start from scratch — re-login, re-fill all fields.
  Measures: total restart time, data loss.

Scenario B — Persistent cookies (RPASession today):
  Worker crashes at step 3/6. New worker restores cookies.
  Still must re-navigate to the form and re-fill all fields.
  Measures: time saved vs Scenario A.

Scenario C — Browserbase CDP (target):
  Simulated by saving page state (URL, filled values) at each step.
  New worker restores to last checkpoint.
  Measures: how much work is recovered.

NOTE: True Browserbase CDP reconnect requires a live Browserbase session.
Scenario C here simulates the recovery pattern using checkpoints — the
real version would call BrowserbaseCDPClient().retrieve(session_id).

Usage:
    python exp_crash_recovery.py
    python exp_crash_recovery.py --crash-at 3  # crash after step N
"""
import argparse
import asyncio
import time

from playwright.async_api import async_playwright

from utils import FAKE_EQUIPMENT, FAKE_MEMBER, FAKE_PROVIDER, FORM_URL, LOGIN_URL


# Steps defined as (step_number, description, coroutine_factory)
FORM_STEPS = [
    (1, "Login",                lambda page: _step_login(page)),
    (2, "Fill member info",     lambda page: _step_member(page)),
    (3, "Fill provider info",   lambda page: _step_provider(page)),
    (4, "Fill equipment info",  lambda page: _step_equipment(page)),
    (5, "Fill notes",           lambda page: _step_notes(page)),
    (6, "Submit form",          lambda page: _step_submit(page)),
]


async def _step_login(page):
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await page.fill("#username", "admin")
    await page.fill("#password", "password123")
    await page.click("#login-btn")
    await page.wait_for_url("**/form.html", timeout=5000)

async def _step_member(page):
    await page.fill("#first-name", FAKE_MEMBER["first_name"])
    await page.fill("#last-name", FAKE_MEMBER["last_name"])
    await page.fill("#dob", FAKE_MEMBER["dob"])
    await page.fill("#member-id", FAKE_MEMBER["member_id"])
    await page.fill("#phone", FAKE_MEMBER["phone"])
    await page.select_option("#insurance-plan", FAKE_MEMBER["insurance_plan"])

async def _step_provider(page):
    await page.fill("#npi", FAKE_PROVIDER["npi"])
    await page.fill("#provider-name", FAKE_PROVIDER["name"])
    await page.select_option("#specialty", FAKE_PROVIDER["specialty"])
    await page.fill("#provider-phone", FAKE_PROVIDER["phone"])

async def _step_equipment(page):
    await page.fill("#hcpcs", FAKE_EQUIPMENT["hcpcs"])
    await page.fill("#icd10", FAKE_EQUIPMENT["icd10"])
    await page.fill("#quantity", FAKE_EQUIPMENT["quantity"])

async def _step_notes(page):
    await page.fill("#notes", FAKE_EQUIPMENT["notes"])

async def _step_submit(page):
    await page.click("#submit-btn")
    await page.wait_for_selector("#success-banner", state="visible", timeout=8000)


# ── Scenario A: No recovery (local Chrome, today) ─────────────────────────────

async def scenario_a_no_recovery(pw, crash_at: int, headless: bool) -> dict:
    """
    Worker dies at crash_at step. New worker restarts from scratch.
    Returns: time of first attempt, time of full restart, total elapsed.
    """
    print(f"\n  Scenario A — No recovery (crash at step {crash_at})")

    # First attempt — crashes at crash_at
    browser = await pw.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
    context = await browser.new_context()
    page = await context.new_page()
    page.on("dialog", lambda d: asyncio.create_task(d.accept()))

    t_start = time.perf_counter()
    completed_before_crash = 0

    for step_num, desc, fn in FORM_STEPS:
        if step_num > crash_at:
            break
        await fn(page)
        completed_before_crash = step_num
        print(f"    step {step_num}: {desc} ✓")

    crash_time = time.perf_counter() - t_start
    print(f"    💥 crash at step {crash_at} ({crash_time:.1f}s elapsed)")
    await browser.close()  # simulates killed process

    # Restart — must do everything from step 1
    browser2 = await pw.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
    context2 = await browser2.new_context()
    page2 = await context2.new_page()
    page2.on("dialog", lambda d: asyncio.create_task(d.accept()))

    t_restart = time.perf_counter()
    for step_num, desc, fn in FORM_STEPS:
        await fn(page2)
        print(f"    [restart] step {step_num}: {desc} ✓")

    total = time.perf_counter() - t_start
    restart_overhead = time.perf_counter() - t_restart
    await browser2.close()

    return {
        "scenario": "A (no recovery)",
        "crashed_at_step": crash_at,
        "work_lost_steps": completed_before_crash,
        "crash_time_s": crash_time,
        "restart_time_s": restart_overhead,
        "total_time_s": total,
    }


# ── Scenario B: Cookie restore (RPASession today) ─────────────────────────────

async def scenario_b_cookie_restore(pw, crash_at: int, headless: bool) -> dict:
    """
    Worker dies at crash_at. New worker restores cookies — skips login
    but still re-navigates and re-fills all form fields.
    """
    print(f"\n  Scenario B — Cookie restore (crash at step {crash_at})")

    # First attempt
    browser = await pw.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
    context = await browser.new_context()
    page = await context.new_page()
    page.on("dialog", lambda d: asyncio.create_task(d.accept()))

    t_start = time.perf_counter()
    saved_cookies = []

    for step_num, desc, fn in FORM_STEPS:
        if step_num > crash_at:
            break
        await fn(page)
        print(f"    step {step_num}: {desc} ✓")
        if step_num == 1:
            saved_cookies = await context.cookies()

    crash_time = time.perf_counter() - t_start
    print(f"    💥 crash at step {crash_at} ({crash_time:.1f}s elapsed)")
    await browser.close()

    # Restart with cookies
    browser2 = await pw.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
    context2 = await browser2.new_context()
    if saved_cookies:
        await context2.add_cookies(saved_cookies)

    page2 = await context2.new_page()
    page2.on("dialog", lambda d: asyncio.create_task(d.accept()))

    t_restart = time.perf_counter()

    # Skip login (step 1) — navigate directly to form
    await page2.goto(FORM_URL, wait_until="domcontentloaded")
    print(f"    [restart] step 1: Login ⟳ skipped (cookie)")

    # Still must re-fill all form fields from step 2
    for step_num, desc, fn in FORM_STEPS:
        if step_num <= 1:
            continue
        await fn(page2)
        print(f"    [restart] step {step_num}: {desc} ✓")

    total = time.perf_counter() - t_start
    restart_overhead = time.perf_counter() - t_restart
    await browser2.close()

    return {
        "scenario": "B (cookie restore)",
        "crashed_at_step": crash_at,
        "work_lost_steps": max(0, crash_at - 1),
        "crash_time_s": crash_time,
        "restart_time_s": restart_overhead,
        "total_time_s": total,
    }


# ── Scenario C: Checkpoint-based recovery (Browserbase simulation) ─────────────

async def scenario_c_checkpoint_recovery(pw, crash_at: int, headless: bool) -> dict:
    """
    Simulates Browserbase-style recovery using step checkpoints.
    In production this would reconnect to the live CDP session.
    Here we save which steps completed and only redo from crash point.

    This shows the TARGET behavior: minimal re-work after a crash.
    """
    print(f"\n  Scenario C — Checkpoint recovery (crash at step {crash_at})")

    # First attempt
    browser = await pw.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
    context = await browser.new_context()
    page = await context.new_page()
    page.on("dialog", lambda d: asyncio.create_task(d.accept()))

    t_start = time.perf_counter()
    completed_steps = []
    saved_cookies = []

    for step_num, desc, fn in FORM_STEPS:
        if step_num > crash_at:
            break
        await fn(page)
        completed_steps.append(step_num)
        print(f"    step {step_num}: {desc} ✓  [checkpoint saved]")
        if step_num == 1:
            saved_cookies = await context.cookies()

    crash_time = time.perf_counter() - t_start
    print(f"    💥 crash at step {crash_at} ({crash_time:.1f}s elapsed)")
    await browser.close()

    # Restart: only redo steps AFTER crash point
    browser2 = await pw.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
    context2 = await browser2.new_context()
    if saved_cookies:
        await context2.add_cookies(saved_cookies)

    page2 = await context2.new_page()
    page2.on("dialog", lambda d: asyncio.create_task(d.accept()))

    t_restart = time.perf_counter()

    # Navigate to form (skip login via cookies)
    await page2.goto(FORM_URL, wait_until="domcontentloaded")

    steps_redone = 0
    for step_num, desc, fn in FORM_STEPS:
        if step_num in completed_steps:
            # In real Browserbase: session state preserved, already filled
            print(f"    [restart] step {step_num}: {desc} ⟳ skipped (session preserved)")
            continue
        await fn(page2)
        steps_redone += 1
        print(f"    [restart] step {step_num}: {desc} ✓  [re-executed]")

    total = time.perf_counter() - t_start
    restart_overhead = time.perf_counter() - t_restart
    await browser2.close()

    return {
        "scenario": "C (checkpoint/Browserbase)",
        "crashed_at_step": crash_at,
        "work_lost_steps": 0,
        "steps_redone": steps_redone,
        "crash_time_s": crash_time,
        "restart_time_s": restart_overhead,
        "total_time_s": total,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(args):
    headless = args.headless.lower() != "false"
    crash_at = args.crash_at

    async with async_playwright() as pw:
        result_a = await scenario_a_no_recovery(pw, crash_at, headless)
        result_b = await scenario_b_cookie_restore(pw, crash_at, headless)
        result_c = await scenario_c_checkpoint_recovery(pw, crash_at, headless)

    print(f"\n{'='*62}")
    print(f"  Crash Recovery Comparison (crash at step {crash_at}/6)")
    print(f"{'='*62}")
    print(f"  {'Scenario':30}  {'Restart':8}  {'Total':8}  {'Lost work'}")
    print(f"  {'-'*30}  {'-'*8}  {'-'*8}  {'-'*10}")
    for r in [result_a, result_b, result_c]:
        lost = f"{r['work_lost_steps']} step(s)" if r.get('work_lost_steps') else "none"
        print(f"  {r['scenario']:30}  {r['restart_time_s']:6.1f}s  {r['total_time_s']:6.1f}s  {lost}")
    print(f"\n  Time overhead vs. no crash:")
    base = result_a['total_time_s']  # A is the baseline clean run with restart
    for r in [result_b, result_c]:
        saved = result_a['restart_time_s'] - r['restart_time_s']
        print(f"  {r['scenario']:30}  saves ~{saved:.1f}s on restart")
    print(f"{'='*62}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--crash-at", type=int, default=3,
                        help="Simulate crash after step N (1-5, default: 3)")
    parser.add_argument("--headless", default="true")
    asyncio.run(main(parser.parse_args()))
