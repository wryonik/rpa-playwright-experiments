"""
Experiment: Browserbase Session Reuse — Batch Patient Lookup
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Realistic production scenario: login once to PortalX, then loop through
multiple patients checking prior auth status — all within a single
browser session.

This measures:
  - Login cost amortized over N patient lookups
  - Per-lookup marginal time (search → view → back)
  - Long-lived session stability on Browserbase
  - Session duration behavior (PortalX 5-min timeout vs Browserbase 6-hr limit)
  - Comparison: Browserbase cloud vs local Playwright

Flow:
  1. Login → MFA → Terms → NPI selection  (one-time setup, ~8s)
  2. For each patient (N iterations):
     a. Search patient by name/DOB
     b. Select matching row (score ≥ 99)
     c. Navigate to prior auth form
     d. Fill minimal fields + submit → get confirmation ref ID
     e. Click "New Request" → back to patient search
  3. Report per-iteration timing + total session duration

Environment variables:
    PUBLIC_SITE_URL           Publicly reachable PortalX URL
    BROWSERBASE_API_KEY       Browserbase API key
    BROWSERBASE_PROJECT_ID    Browserbase project ID

Usage:
    python exp_browserbase_session_reuse.py --patients 10
    python exp_browserbase_session_reuse.py --patients 20 --local-only
    python exp_browserbase_session_reuse.py --patients 5 --bb-only
"""
import argparse
import asyncio
import json
import os
import statistics
import time

from playwright.async_api import Page, async_playwright

import exp_full_workflow as wf
from utils import CHROME_ARGS, ExperimentMetrics, ResourceSampler, RunResult

PUBLIC_SITE = os.getenv("PUBLIC_SITE_URL", "")
LOCAL_SITE = os.getenv("SITE_URL", "http://localhost:8888")


# ── Patient data pool ────────────────────────────────────────────────────────
# PortalX always returns the same patient (Rivera, Alex) for any search,
# but we vary the search inputs to simulate real batch lookups.
PATIENTS = [
    {"last": "Rivera",   "first": "Alex",    "dob": "06/15/1985"},
    {"last": "Chen",     "first": "Sarah",   "dob": "03/22/1990"},
    {"last": "Johnson",  "first": "Marcus",  "dob": "11/08/1978"},
    {"last": "Patel",    "first": "Priya",   "dob": "07/14/1982"},
    {"last": "Williams", "first": "Denise",  "dob": "01/30/1995"},
    {"last": "Kim",      "first": "David",   "dob": "09/17/1988"},
    {"last": "Garcia",   "first": "Maria",   "dob": "12/03/1975"},
    {"last": "Brown",    "first": "James",   "dob": "04/25/1992"},
    {"last": "Singh",    "first": "Raj",     "dob": "08/11/1986"},
    {"last": "Lee",      "first": "Jennifer","dob": "02/19/1983"},
    {"last": "Taylor",   "first": "Robert",  "dob": "10/06/1979"},
    {"last": "Martinez", "first": "Carlos",  "dob": "05/28/1991"},
    {"last": "Anderson", "first": "Lisa",    "dob": "06/12/1987"},
    {"last": "Thomas",   "first": "Michael", "dob": "03/15/1976"},
    {"last": "Jackson",  "first": "Emily",   "dob": "11/22/1993"},
    {"last": "White",    "first": "Daniel",  "dob": "07/09/1984"},
    {"last": "Harris",   "first": "Amanda",  "dob": "09/01/1989"},
    {"last": "Clark",    "first": "Kevin",   "dob": "01/18/1981"},
    {"last": "Lewis",    "first": "Rachel",  "dob": "04/07/1994"},
    {"last": "Walker",   "first": "Steven",  "dob": "08/30/1977"},
]


# ── One-time login setup ────────────────────────────────────────────────────

async def full_login_setup(page: Page, site: str) -> dict:
    """
    Perform the one-time login flow: login → MFA → terms → NPI.
    Returns timing dict for each step.
    """
    wf.SITE = site
    timings = {}

    page.on("dialog", lambda d: asyncio.create_task(d.accept()))

    t = time.perf_counter()
    elapsed = await wf.step_login(page)
    timings["login_s"] = round(elapsed, 3)

    if "/concurrent" in page.url:
        raise RuntimeError("Concurrent session detected — PortalX rejected login")

    elapsed = await wf.step_mfa(page)
    timings["mfa_s"] = round(elapsed, 3)

    elapsed = await wf.step_terms(page)
    timings["terms_s"] = round(elapsed, 3)

    elapsed = await wf.step_npi(page)
    timings["npi_s"] = round(elapsed, 3)

    timings["total_login_s"] = round(time.perf_counter() - t, 3)
    return timings


# ── Single patient lookup iteration ──────────────────────────────────────────

async def lookup_patient(page: Page, patient: dict, site: str) -> dict:
    """
    From the patient search page: search → select → prior auth → submit → confirm → back.
    Returns timing dict.
    """
    wf.SITE = site
    timings = {}
    t_total = time.perf_counter()

    # Wait for patient search page (we should already be here)
    try:
        await page.wait_for_url(f"{site}/patient-search", timeout=5000)
    except Exception:
        # Maybe we're on a different page — navigate explicitly
        await page.goto(f"{site}/patient-search", wait_until="domcontentloaded")

    # Search for patient
    t = time.perf_counter()
    await page.evaluate(f"""() => {{
        const s = (id, v) => {{ const el = document.getElementById(id); if (el) el.value = v; }};
        s('LastName', '{patient["last"]}');
        s('FirstName', '{patient["first"]}');
        s('DateOfBirth', '{patient["dob"]}');
    }}""")

    async with page.expect_response(lambda r: "/api/patient-search" in r.url) as resp_info:
        await page.click("button[type=submit]", no_wait_after=True)
    await resp_info.value
    await page.wait_for_selector("#results-table", state="visible", timeout=5000)
    timings["search_s"] = round(time.perf_counter() - t, 3)

    # Select best match (score ≥ 99)
    t = time.perf_counter()
    best_row = await page.evaluate("""() => {
        const rows = document.querySelectorAll('#results-tbody tr');
        for (let i = 0; i < rows.length; i++) {
            const score = parseInt((rows[i].cells[5] || {}).innerText || '0');
            if (score >= 99) return i;
        }
        return 0;
    }""")
    await page.locator(f"#results-tbody tr:nth-child({best_row + 1})").click()
    await page.click("#proceed-btn")
    await page.wait_for_url(f"{site}/prior-auth", timeout=6000)
    timings["select_s"] = round(time.perf_counter() - t, 3)

    # Fill prior auth form + submit
    t = time.perf_counter()
    elapsed = await wf.step_prior_auth_form(page)
    timings["prior_auth_s"] = round(elapsed, 3)

    # Get confirmation ref ID
    elapsed, ref_id = await wf.step_confirmation(page)
    timings["confirm_s"] = round(elapsed, 3)
    timings["ref_id"] = ref_id

    # Navigate back to patient search for next iteration
    t = time.perf_counter()
    await page.click(".btn-new")
    await page.wait_for_url(f"{site}/patient-search", timeout=5000)
    timings["back_s"] = round(time.perf_counter() - t, 3)

    timings["total_lookup_s"] = round(time.perf_counter() - t_total, 3)
    return timings


# ── Run batch on a single browser ────────────────────────────────────────────

async def run_batch(page: Page, site: str, n_patients: int, label: str) -> dict:
    """
    Login once, then process N patients sequentially.
    Returns full result dict.
    """
    result = {"label": label, "site": site, "n_patients": n_patients,
              "login": {}, "lookups": [], "errors": []}

    # One-time login
    print(f"\n  [{label}] Logging in...")
    try:
        login_timings = await full_login_setup(page, site)
        result["login"] = login_timings
        print(f"  [{label}] Login complete: {login_timings['total_login_s']:.1f}s "
              f"(login={login_timings['login_s']:.1f}s mfa={login_timings['mfa_s']:.1f}s "
              f"terms={login_timings['terms_s']:.1f}s npi={login_timings['npi_s']:.1f}s)")
    except Exception as e:
        result["errors"].append(f"Login failed: {e}")
        print(f"  [{label}] Login FAILED: {e}")
        return result

    # We should now be on /patient-search (step_npi navigates there)

    # Loop through patients
    for i in range(n_patients):
        patient = PATIENTS[i % len(PATIENTS)]
        try:
            t_iter = time.perf_counter()
            timings = await lookup_patient(page, patient, site)
            result["lookups"].append(timings)
            print(f"  [{label}] Patient {i+1:02d}/{n_patients}: "
                  f"{timings['total_lookup_s']:.2f}s  "
                  f"search={timings['search_s']:.2f}s  "
                  f"auth={timings['prior_auth_s']:.2f}s  "
                  f"ref={timings['ref_id']}")
        except Exception as e:
            err = f"Patient {i+1} ({patient['last']}): {e}"
            result["errors"].append(err)
            print(f"  [{label}] Patient {i+1:02d}/{n_patients}: FAILED — {e}")
            # Try to recover by navigating back to patient search
            try:
                await page.goto(f"{site}/patient-search", wait_until="domcontentloaded")
            except Exception:
                print(f"  [{label}] Cannot recover — stopping batch")
                break

    return result


# ── Print summary ─────────────────────────────────────────────────────────────

def print_batch_summary(result: dict):
    label = result["label"]
    lookups = result["lookups"]
    if not lookups:
        print(f"\n  [{label}] No successful lookups to summarize.")
        return

    login_t = result["login"].get("total_login_s", 0)
    lookup_times = [l["total_lookup_s"] for l in lookups]
    search_times = [l["search_s"] for l in lookups]
    auth_times = [l["prior_auth_s"] for l in lookups]
    total_session = login_t + sum(lookup_times)

    s = sorted(lookup_times)
    p95_idx = min(int(len(s) * 0.95), len(s) - 1)

    print(f"\n  {'─'*60}")
    print(f"  [{label}] Session Reuse Summary")
    print(f"  {'─'*60}")
    print(f"  Patients processed : {len(lookups)}/{result['n_patients']}")
    print(f"  Errors             : {len(result['errors'])}")
    print(f"  Login (one-time)   : {login_t:.2f}s")
    print(f"  Avg lookup time    : {statistics.mean(lookup_times):.2f}s")
    print(f"  p50 lookup time    : {s[len(s)//2]:.2f}s")
    print(f"  p95 lookup time    : {s[p95_idx]:.2f}s")
    print(f"  Avg search time    : {statistics.mean(search_times):.2f}s")
    print(f"  Avg prior auth     : {statistics.mean(auth_times):.2f}s")
    print(f"  Total session time : {total_session:.1f}s ({total_session/60:.1f} min)")
    print(f"  Amortized per-patient: {total_session/len(lookups):.2f}s "
          f"(vs {login_t + statistics.mean(lookup_times):.2f}s if fresh login each time)")
    if len(lookups) >= 2:
        savings_pct = (1 - total_session / (len(lookups) * (login_t + statistics.mean(lookup_times)))) * 100
        print(f"  Session reuse savings: {savings_pct:.0f}%")
    print(f"  {'─'*60}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(args):
    n_patients = args.patients
    run_local = not args.bb_only
    run_bb = not args.local_only

    all_results = []

    # ── Local Playwright ──────────────────────────────────────────────────────
    if run_local:
        site = LOCAL_SITE if not PUBLIC_SITE else PUBLIC_SITE
        print(f"\n{'='*62}")
        print(f"  Mode A: Local Playwright — {n_patients} patient batch")
        print(f"  Site: {site}")
        print(f"{'='*62}")

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=CHROME_ARGS)
            context = await browser.new_context()
            page = await context.new_page()

            result = await run_batch(page, site, n_patients, "LOCAL")
            all_results.append(result)
            print_batch_summary(result)

            await browser.close()

    # ── Browserbase ───────────────────────────────────────────────────────────
    if run_bb:
        api_key = os.getenv("BROWSERBASE_API_KEY", "")
        project_id = os.getenv("BROWSERBASE_PROJECT_ID", "")
        if not api_key:
            print("\n  ⚠  BROWSERBASE_API_KEY not set — skipping Browserbase mode\n")
        elif not PUBLIC_SITE:
            print("\n  ⚠  PUBLIC_SITE_URL not set — Browserbase can't reach the test site\n")
        else:
            print(f"\n{'='*62}")
            print(f"  Mode B: Browserbase — {n_patients} patient batch")
            print(f"  Site: {PUBLIC_SITE}")
            print(f"{'='*62}")

            try:
                from browserbase import AsyncBrowserbase
            except ImportError:
                print("  browserbase SDK not installed: pip install browserbase")
                return

            bb = AsyncBrowserbase(api_key=api_key)
            session = await bb.sessions.create(project_id=project_id)
            session_id = session.id
            print(f"  Session created: {session_id[:16]}…")

            async with async_playwright() as pw:
                t_cdp = time.perf_counter()
                browser = await pw.chromium.connect_over_cdp(session.connect_url)
                print(f"  CDP connected: {time.perf_counter() - t_cdp:.3f}s")

                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await context.new_page()

                result = await run_batch(page, PUBLIC_SITE, n_patients, "BB")
                result["session_id"] = session_id
                all_results.append(result)
                print_batch_summary(result)

                await browser.close()

            await bb.sessions.update(id=session_id, project_id=project_id, status="REQUEST_RELEASE")
            print(f"  Session released: {session_id[:16]}…")

    # ── Comparison ────────────────────────────────────────────────────────────
    if len(all_results) == 2:
        local_r, bb_r = all_results[0], all_results[1]
        if local_r["lookups"] and bb_r["lookups"]:
            l_avg = statistics.mean([l["total_lookup_s"] for l in local_r["lookups"]])
            b_avg = statistics.mean([l["total_lookup_s"] for l in bb_r["lookups"]])
            l_login = local_r["login"].get("total_login_s", 0)
            b_login = bb_r["login"].get("total_login_s", 0)

            print(f"\n{'='*62}")
            print(f"  Local vs Browserbase — Session Reuse Comparison")
            print(f"{'='*62}")
            print(f"  {'':25} {'Local':>10}  {'BB':>10}  {'Δ':>10}")
            print(f"  {'-'*25} {'-'*10}  {'-'*10}  {'-'*10}")
            print(f"  {'Login (one-time)':25} {l_login:>9.2f}s  {b_login:>9.2f}s  {(b_login-l_login)/l_login*100:>+9.1f}%")
            print(f"  {'Avg lookup':25} {l_avg:>9.2f}s  {b_avg:>9.2f}s  {(b_avg-l_avg)/l_avg*100:>+9.1f}%")
            print(f"  {'Patients processed':25} {len(local_r['lookups']):>10}  {len(bb_r['lookups']):>10}")
            print(f"  {'Errors':25} {len(local_r['errors']):>10}  {len(bb_r['errors']):>10}")
            print(f"{'='*62}\n")

    # Save results
    out_path = "/tmp/exp_bb_session_reuse.json"
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"  Results saved to {out_path}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Browserbase session reuse — batch patient lookup benchmark")
    parser.add_argument("--patients", type=int, default=10, help="Number of patients to process per session")
    parser.add_argument("--local-only", action="store_true", help="Skip Browserbase mode")
    parser.add_argument("--bb-only", action="store_true", help="Skip local Playwright mode")
    asyncio.run(main(parser.parse_args()))
