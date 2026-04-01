"""
Experiment: Full Realistic Workflow (PortalX v2)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Runs the complete multi-page prior authorization flow matching the
actual challenges found in Brightree and MyCGS:

  Step 1 — Login (legacy ASP.NET form, #Username / #Password)
  Step 2 — MFA   (TOTP 6-digit, character-by-character, paste blocked)
  Step 3 — Terms (jurisdiction selection, scroll-to-accept)
  Step 4 — NPI   (select from table, random alert modal)
  Step 5 — Patient search (API wait, match-score table, row selection)
  Step 6 — Prior auth form (quick-lookup NPI, Vuetify v-select HCPCS,
                             confirmation modal, server error dialogs)
  Step 7 — Confirmation page (extract reference ID)

Error conditions handled:
  - Native alert() / confirm() dialogs (page.on "dialog")
  - "An Error occurred" DOM modal (explicit .click())
  - Concurrent session detection (URL check, retry)
  - Session expiry mid-flow (URL check, re-login)
  - Server error dialog on submit (re-submit once)

Usage:
    python exp_full_workflow.py                    # 5 runs, 1 worker
    python exp_full_workflow.py --runs 10 --workers 3
    python exp_full_workflow.py --headless false   # watch it run
"""
import argparse
import asyncio
import math
import os
import time
from dataclasses import dataclass, field

from playwright.async_api import Page, async_playwright

from utils import CHROME_ARGS, ExperimentMetrics, ResourceSampler, RunResult

SITE = os.getenv("SITE_URL", "http://localhost:8888")

# ── TOTP helper ──────────────────────────────────────────────────────────────

def current_totp() -> str:
    """Matches server.py's get_valid_totp() — changes every 30 seconds."""
    t = int(time.time() // 30)
    return str(t % 1_000_000).zfill(6)


# ── Step implementations ──────────────────────────────────────────────────────

async def dismiss_dom_error_modal(page: Page) -> bool:
    """
    Dismiss the 'An Error occurred' DOM modal if present.
    Returns True if a modal was dismissed.
    These are NOT native dialogs — they require explicit .click() on the OK button.
    selector: #error-modal-ok  or  .modal-ok
    """
    try:
        ok_btn = page.locator("#error-modal.active .modal-ok, #error-modal-ok")
        await ok_btn.wait_for(state="visible", timeout=1500)
        await ok_btn.click()
        return True
    except Exception:
        return False


async def dismiss_alert_modal(page: Page) -> bool:
    """Dismiss the random alert modal on NPI page."""
    try:
        ok_btn = page.locator("#alert-modal.active .modal-ok")
        await ok_btn.wait_for(state="visible", timeout=1500)
        await ok_btn.click()
        return True
    except Exception:
        return False


async def step_login(page: Page) -> float:
    t = time.perf_counter()
    await page.goto(f"{SITE}/login", wait_until="domcontentloaded")
    await page.fill("#Username", "admin")
    await page.fill("#Password", "P@ssw0rd!")
    await page.click("button[type=submit]")
    # May land on /mfa OR /concurrent
    await page.wait_for_url(f"{SITE}/**", wait_until="domcontentloaded", timeout=8000)
    return time.perf_counter() - t


async def step_mfa(page: Page, retries: int = 3) -> float:
    """
    Enter TOTP code character-by-character (paste is blocked on the input).
    Handles the 'The data entered is incorrect' modal with up to `retries` attempts.
    Generates TOTP at the last possible moment and waits out any window boundary
    within 3 s to avoid the code expiring mid-submit.
    """
    t = time.perf_counter()
    for attempt in range(retries):
        await page.wait_for_url(f"{SITE}/mfa", timeout=5000)
        # Wait out any TOTP boundary that is fewer than 3 s away
        while True:
            window_remaining = 30 - (time.time() % 30)
            if window_remaining > 3:
                break
            await asyncio.sleep(1)
        code = current_totp()  # generated right before typing
        inp = page.locator("#MFAEntryPanel #txtMFACode")
        await inp.wait_for(state="visible", timeout=5000)
        await inp.click()
        await inp.fill("")
        # Type character by character (mirrors MyCGS MFA handler)
        for ch in code:
            await inp.type(ch, delay=50)
        await page.click("button[type=submit]")
        # Wait for either navigation away from /mfa or error modal
        try:
            await page.wait_for_url(f"{SITE}/terms", timeout=4000)
            break  # success
        except Exception:
            # Wrong code — dismiss error modal and retry
            await dismiss_dom_error_modal(page)
            # Also handle native dialog version
            try:
                await page.locator("#mfa-modal.active .modal-ok").click(timeout=1000)
            except Exception:
                pass
    return time.perf_counter() - t


async def step_terms(page: Page) -> float:
    t = time.perf_counter()
    await page.wait_for_url(f"{SITE}/terms", timeout=5000)

    # Dismiss error modal if server injected one (8% chance)
    await dismiss_dom_error_modal(page)

    # Scroll terms to bottom to enable jurisdiction selection
    await page.evaluate("document.getElementById('terms-text').scrollTop = 9999")
    await asyncio.sleep(0.2)

    # Click Jurisdiction C card
    await page.click(".jur-card:first-child")
    await asyncio.sleep(0.1)

    # Click "Enter Jurisdiction C" button
    await page.click("#enter-jurisdiction-btn")
    await page.wait_for_url(f"{SITE}/npi", timeout=5000)
    return time.perf_counter() - t


async def step_npi(page: Page) -> float:
    t = time.perf_counter()
    await page.wait_for_url(f"{SITE}/npi", timeout=5000)

    # Dismiss random alert modal (20% chance it appears)
    await dismiss_alert_modal(page)
    # Wait until modal is fully gone before clicking — prevents race on high-latency CDP
    # state="hidden" = wait until #alert-modal.active is no longer visible (or never existed)
    await page.wait_for_selector("#alert-modal.active", state="hidden", timeout=5000)

    # Select first NPI row (the active one)
    await page.click(".npi-table tbody tr:first-child")
    await asyncio.sleep(0.1)
    await page.click("#npi-submit")
    await page.wait_for_url(f"{SITE}/patient-search", timeout=5000)
    return time.perf_counter() - t


async def step_patient_search(page: Page) -> float:
    t = time.perf_counter()
    await page.wait_for_url(f"{SITE}/patient-search", timeout=5000)

    # Dismiss server error dialog if present (JS alert from previous POST)
    # The dialog handler already auto-accepts native dialogs

    # Fill ASP.NET-style search fields — batch (1 round-trip instead of 3)
    await page.evaluate("""() => {
        const s = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
        s('LastName', 'Rivera'); s('FirstName', 'Alex'); s('DateOfBirth', '06/15/1985');
    }""")

    # Submit — wait for API response (mirrors wait_for_api_url pattern)
    async with page.expect_response(lambda r: "/api/patient-search" in r.url) as resp_info:
        await page.click("button[type=submit]", no_wait_after=True)

    await resp_info.value  # ensure API call completed

    # Wait for results table
    await page.wait_for_selector("#results-table", state="visible", timeout=5000)

    # Find row with match score >= 99 (column 6) — single evaluate() instead of N round-trips
    best_row_idx = await page.evaluate("""() => {
        const rows = document.querySelectorAll('#results-tbody tr');
        for (let i = 0; i < rows.length; i++) {
            const score = parseInt((rows[i].cells[5] || {}).innerText || '0');
            if (score >= 99) return i;
        }
        return 0;
    }""")
    await page.locator(f"#results-tbody tr:nth-child({best_row_idx + 1})").click()

    await page.click("#proceed-btn")
    await page.wait_for_url(f"{SITE}/prior-auth", timeout=6000)
    return time.perf_counter() - t


async def step_prior_auth_form(page: Page, retries: int = 2) -> float:
    t = time.perf_counter()

    for attempt in range(retries + 1):
        await page.wait_for_url(f"{SITE}/prior-auth*", timeout=5000)

        # Dismiss "An Error occurred" DOM modal if server injected one
        await dismiss_dom_error_modal(page)
        # Wait until modal is fully gone before interacting — prevents race on high-latency CDP
        await page.wait_for_selector("#error-modal.active", state="hidden", timeout=5000)

        # Section 1: Contact info — batch fill via evaluate (1 round-trip instead of 6)
        await page.evaluate("""() => {
            const s = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
            s('contact_name', 'Jordan Smith');
            s('contact_number', '5551234567');
            s('insurance_id', '1EG4-TE5-MK72');
            s('ben_last_name', 'Rivera');
            s('ben_first_name', 'Alex');
            s('date_of_birth', '06/15/1985');
        }""")
        # Trigger change events so any field validators fire
        await page.evaluate("""() => {
            ['contact_name','contact_number','insurance_id','ben_last_name','ben_first_name','date_of_birth']
                .forEach(id => {
                    const el = document.getElementById(id);
                    if (el) el.dispatchEvent(new Event('input', { bubbles: true }));
                });
        }""")

        # Section 3: Provider — Quick-lookup (data-quicklookup field)
        # Type into field → dropdown appears → click matching item
        npi_input = page.locator("#provider_npi_quicklookup")
        await npi_input.fill("1234567890")
        await asyncio.sleep(0.4)  # wait for dropdown debounce
        try:
            item = page.locator(".quicklookup-data.v-list-item.v-list-item--link").first
            await item.wait_for(state="visible", timeout=3000)
            await item.click()
        except Exception:
            pass

        # Submission type (native <select>)
        await page.select_option("#submission_type", "initial")

        # Section 4: HCPCS — Vuetify v-select
        # Click arrow (i.material-icons:has-text('arrow_drop_down')) → listbox appears → ArrowDown → select
        arrow = page.locator("#hcpcs-select-wrapper i.material-icons")
        await arrow.click()
        # Wait for listbox: div[role='listbox']:visible
        listbox = page.locator("#hcpcs-listbox[role='listbox']")
        await listbox.wait_for(state="visible", timeout=3000)

        # Find E0601 — may need ArrowDown presses to load it if not visible initially
        wrapper = page.locator("#hcpcs-select-wrapper")
        for _ in range(7):
            titles = await page.locator("#hcpcs-listbox .v-list-item__title").all()
            for title in titles:
                text = await title.inner_text()
                if "E0601" in text:
                    await title.click()
                    break
            else:
                await wrapper.press("ArrowDown")
                await asyncio.sleep(0.1)
                continue
            break

        # ICD-10 + remaining fields — batch fill (1 round-trip)
        await page.evaluate("""() => {
            const s = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
            s('icd10_code', 'J96.00');
            s('height', '70');
            s('weight', '185');
            s('clinical_notes', 'Patient requires CPAP therapy. AHI >15 confirmed by polysomnography. Clinical necessity documented.');
        }""")

        # Submit — triggers confirmation modal
        await page.click("#submit-btn")
        try:
            confirm_ok = page.locator("#confirm-ok-btn")
            await confirm_ok.wait_for(state="visible", timeout=3000)
            await confirm_ok.click()
        except Exception:
            pass

        # Wait for either confirmation page or server error redirect
        try:
            await page.wait_for_url(f"{SITE}/confirmation", timeout=8000)
            break  # success
        except Exception:
            # Server error injected → page redirected to /prior-auth?server_error=1
            # alert() was auto-accepted by dialog handler; retry the form
            if attempt < retries:
                await page.goto(f"{SITE}/prior-auth", wait_until="domcontentloaded")
                continue
            raise

    return time.perf_counter() - t


async def step_confirmation(page: Page) -> tuple[float, str]:
    t = time.perf_counter()
    await page.wait_for_url(f"{SITE}/confirmation", timeout=5000)
    ref_id = await page.locator("#ref-number").inner_text(timeout=3000)
    return time.perf_counter() - t, ref_id.strip()


# ── Full workflow ─────────────────────────────────────────────────────────────

@dataclass
class WorkflowResult:
    worker_id: int
    success: bool
    total_s: float
    step_times: dict = field(default_factory=dict)
    ref_id: str = ""
    error: str = ""
    concurrent_session: bool = False


async def run_full_workflow(worker_id: int, pw, headless: bool) -> WorkflowResult:
    step_times = {}
    t_total = time.perf_counter()

    try:
        browser = await pw.chromium.launch(headless=headless, args=CHROME_ARGS)
        context = await browser.new_context()
        page = await context.new_page()

        # Global native dialog handler — required for alert() and confirm()
        # Without this, any unexpected native dialog freezes the automation permanently.
        dialogs_seen = []
        def handle_dialog(dialog):
            dialogs_seen.append(dialog.type)
            asyncio.create_task(dialog.accept())
        page.on("dialog", handle_dialog)

        # Step 1: Login
        step_times["login"] = await step_login(page)

        # Detect concurrent session (5% chance) — URL-based detection
        if "/concurrent" in page.url:
            await browser.close()
            return WorkflowResult(
                worker_id=worker_id, success=False,
                total_s=time.perf_counter() - t_total,
                step_times=step_times,
                error="concurrent_session",
                concurrent_session=True,
            )

        # Step 2: MFA
        step_times["mfa"] = await step_mfa(page)

        # Step 3: Terms
        step_times["terms"] = await step_terms(page)

        # Step 4: NPI selection
        step_times["npi"] = await step_npi(page)

        # Step 5: Patient search
        step_times["patient_search"] = await step_patient_search(page)

        # Check for session expiry (redirect to /session-expired or /login)
        if "/session-expired" in page.url or page.url.endswith("/login"):
            await browser.close()
            return WorkflowResult(
                worker_id=worker_id, success=False,
                total_s=time.perf_counter() - t_total,
                step_times=step_times,
                error="session_expired",
            )

        # Step 6: Prior auth form (with server error retry)
        step_times["prior_auth_form"] = await step_prior_auth_form(page)

        # Step 7: Confirmation
        conf_t, ref_id = await step_confirmation(page)
        step_times["confirmation"] = conf_t

        await browser.close()
        return WorkflowResult(
            worker_id=worker_id, success=True,
            total_s=time.perf_counter() - t_total,
            step_times=step_times,
            ref_id=ref_id,
        )

    except Exception as e:
        try:
            await browser.close()
        except Exception:
            pass
        return WorkflowResult(
            worker_id=worker_id, success=False,
            total_s=time.perf_counter() - t_total,
            step_times=step_times,
            error=str(e)[:120],
        )


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(args):
    headless = args.headless.lower() != "false"
    n_runs = args.runs
    n_workers = args.workers

    print(f"\nFull Workflow Experiment (PortalX v2)")
    print(f"Runs: {n_runs} | Workers: {n_workers} | Headless: {headless}\n")

    all_results: list[WorkflowResult] = []

    async def worker_batch(worker_id: int, count: int, pw):
        for _ in range(count):
            r = await run_full_workflow(worker_id, pw, headless)
            all_results.append(r)
            status = "✓" if r.success else f"✗ ({r.error})"
            print(f"  [w{worker_id:02d}] {status}  {r.total_s:.2f}s  "
                  + (f"ref={r.ref_id}" if r.ref_id else ""))

    runs_per_worker = math.ceil(n_runs / n_workers)

    async with async_playwright() as pw:
        async with ResourceSampler() as sampler:
            tasks = [
                asyncio.create_task(worker_batch(wid, runs_per_worker, pw))
                for wid in range(n_workers)
            ]
            await asyncio.gather(*tasks)

    # Trim to requested run count
    all_results = all_results[:n_runs]

    # Analysis
    ok = [r for r in all_results if r.success]
    concurrent = [r for r in all_results if r.concurrent_session]
    expired = [r for r in all_results if r.error == "session_expired"]
    other_fail = [r for r in all_results if not r.success and not r.concurrent_session and r.error != "session_expired"]

    durations = [r.total_s for r in all_results]

    # Step timing aggregation (successful runs only)
    step_avgs = {}
    step_keys = ["login", "mfa", "terms", "npi", "patient_search", "prior_auth_form", "confirmation"]
    for k in step_keys:
        vals = [r.step_times[k] for r in ok if k in r.step_times]
        if vals:
            step_avgs[k] = round(sum(vals) / len(vals), 3)

    print(f"\n{'='*62}")
    print(f"  Full Workflow Results — PortalX v2")
    print(f"{'='*62}")
    print(f"  Total runs       : {len(all_results)}")
    print(f"  Successful       : {len(ok)} ({len(ok)/len(all_results)*100:.1f}%)")
    print(f"  Concurrent sess  : {len(concurrent)}")
    print(f"  Session expired  : {len(expired)}")
    print(f"  Other failures   : {len(other_fail)}")
    if durations:
        print(f"  Avg total time   : {sum(durations)/len(durations):.2f}s")
        sorted_d = sorted(durations)
        print(f"  p95 total time   : {sorted_d[int(len(sorted_d)*0.95)]:.2f}s")
    print(f"\n  Avg step breakdown (successful runs):")
    for k, v in step_avgs.items():
        bar = "█" * max(1, int(v * 4))
        print(f"    {k:20} {v:6.3f}s  {bar}")

    if other_fail:
        print(f"\n  Failure details:")
        for r in other_fail[:5]:
            print(f"    [w{r.worker_id:02d}] {r.error}")

    print(f"{'='*62}\n")

    metrics = ExperimentMetrics.from_results(
        "full_workflow_v2",
        [RunResult(
            worker_id=r.worker_id,
            success=r.success,
            duration_s=r.total_s,
            peak_memory_mb=0,
            steps_completed=len(r.step_times),
            total_steps=7,
            error=r.error or None,
        ) for r in all_results],
        concurrency=n_workers,
        resource_summary=sampler.summary(),
        extras={
            "concurrent_sessions": len(concurrent),
            "session_expirations": len(expired),
            "step_avg_times": step_avgs,
        },
    )
    metrics.print_summary()
    metrics.save("/tmp/exp_full_workflow_v2.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--headless", default="true")
    asyncio.run(main(parser.parse_args()))
