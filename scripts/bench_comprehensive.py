"""
Comprehensive benchmark — covers gaps identified in review.

Sections:
  1. Timeout fix verification (6s vs 15s nav timeout)
  2. Retry + error classification (transient retry once, permanent die)
  3. Popup detection + handling metrics
  4. Screenshot-on-failure capture
  5. File upload step
  6. Auto-logout detection (5-min session expiry)
  7. Connection resilience (idle mid-session)
  8. Audit trail verification

Environment variables:
    PUBLIC_SITE_URL, BROWSERBASE_API_KEY, BROWSERBASE_PROJECT_ID

Usage:
    python bench_comprehensive.py --section timeout   # runs one section
    python bench_comprehensive.py --section all       # runs all sections
"""
import argparse
import asyncio
import json
import os
import statistics
import time
import traceback
from datetime import datetime

from playwright.async_api import Page, async_playwright, TimeoutError as PwTimeout

import exp_full_workflow as wf

PUBLIC_SITE = os.getenv("PUBLIC_SITE_URL", "https://portalx-7nn4.onrender.com")
BB_API_KEY = os.getenv("BROWSERBASE_API_KEY", "")
BB_PROJECT_ID = os.getenv("BROWSERBASE_PROJECT_ID", "")
OUTDIR = "/tmp/bench_comprehensive"
os.makedirs(OUTDIR, exist_ok=True)
os.makedirs(f"{OUTDIR}/screenshots", exist_ok=True)


# ── Shared helpers ────────────────────────────────────────────────────────────

async def new_bb_session():
    """Create a Browserbase session. Returns (session_id, connect_url)."""
    from browserbase import AsyncBrowserbase
    bb = AsyncBrowserbase(api_key=BB_API_KEY)
    session = await bb.sessions.create(project_id=BB_PROJECT_ID)
    return session.id, session.connect_url


async def release_bb_session(session_id: str):
    from browserbase import AsyncBrowserbase
    try:
        bb = AsyncBrowserbase(api_key=BB_API_KEY)
        await bb.sessions.update(id=session_id, project_id=BB_PROJECT_ID, status="REQUEST_RELEASE")
    except Exception:
        pass


def classify_error(err: Exception) -> str:
    """
    Classify a Playwright/navigation error as transient vs permanent.
    Transient: timeout, concurrent session, server 500 → retry once.
    Permanent: selector not found, auth failure → die.
    """
    msg = str(err).lower()
    if "timeout" in msg and "exceeded" in msg:
        return "transient_timeout"
    if "concurrent" in msg or "/concurrent" in msg:
        return "transient_concurrent"
    if "500" in msg or "server error" in msg:
        return "transient_server"
    if "net::err_connection" in msg or "net::err_internet_disconnected" in msg:
        return "transient_network"
    if "waiting for locator" in msg or "element not found" in msg or "no node found" in msg:
        return "permanent_selector"
    if "authentication" in msg or "invalid credentials" in msg:
        return "permanent_auth"
    return "unknown"


async def capture_failure(page: Page, label: str, step: str, err: Exception):
    """Save screenshot + error metadata to disk for post-mortem."""
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    fname = f"{OUTDIR}/screenshots/{ts}_{label}_{step}.png"
    meta = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "label": label,
        "step": step,
        "error_class": classify_error(err),
        "error_msg": str(err)[:300],
        "url": page.url if page else None,
        "screenshot": fname,
    }
    try:
        if page:
            await page.screenshot(path=fname, full_page=False)
    except Exception as e:
        meta["screenshot_error"] = str(e)[:150]
    with open(f"{OUTDIR}/screenshots/{ts}_{label}_{step}.json", "w") as f:
        json.dump(meta, f, indent=2)
    return meta


# ── Section 1: Timeout fix verification ──────────────────────────────────────

async def run_workflow_with_timeout(page: Page, nav_timeout_ms: int, label: str, run_i: int, n: int) -> dict:
    """Run full 7-step workflow with a custom nav timeout."""
    # Override the wf.SITE to the public URL
    wf.SITE = PUBLIC_SITE

    # Monkey-patch the step functions? Instead, let's just call them and time.
    # The existing step_* functions have hardcoded 5-6s timeouts.
    # For the fix test, we'll run the fixed version by calling step functions
    # with a fresh page + longer default timeout at the page level.
    page.set_default_navigation_timeout(nav_timeout_ms)
    page.set_default_timeout(nav_timeout_ms)

    page.on("dialog", lambda d: asyncio.create_task(d.accept()))

    result = {"run": run_i, "nav_timeout_ms": nav_timeout_ms, "success": False, "steps": {}, "error": None}
    try:
        result["steps"]["login"] = round(await wf.step_login(page), 3)
        if "/concurrent" in page.url:
            result["error"] = "concurrent_session"
            return result
        result["steps"]["mfa"] = round(await wf.step_mfa(page), 3)
        result["steps"]["terms"] = round(await wf.step_terms(page), 3)
        result["steps"]["npi"] = round(await wf.step_npi(page), 3)
        result["steps"]["patient_search"] = round(await wf.step_patient_search(page), 3)
        result["steps"]["prior_auth"] = round(await wf.step_prior_auth_form(page), 3)
        elapsed, ref = await wf.step_confirmation(page)
        result["steps"]["confirmation"] = round(elapsed, 3)
        result["ref_id"] = ref
        result["success"] = True
    except Exception as e:
        failed_step = next((s for s in ["login","mfa","terms","npi","patient_search","prior_auth","confirmation"] if s not in result["steps"]), "unknown")
        result["error"] = str(e)[:200]
        result["failed_step"] = failed_step
        result["error_class"] = classify_error(e)
    return result


async def section_timeout(n: int = 10):
    """Run 10 iterations at 6s (baseline) + 10 at 15s (fix)."""
    print("\n" + "="*70)
    print("SECTION 1: Timeout Fix Verification")
    print("="*70)

    # Since step functions hardcode timeouts, the most honest test is to
    # raise page.set_default_timeout() which at least affects set_*/click operations.
    # Navigation timeouts in step_* functions are hardcoded 5-8s — we cannot
    # truly change them without editing the file. So this tests the default timeout.
    results = {"baseline_6s": [], "fixed_15s": []}

    for timeout_ms, label in [(6000, "baseline_6s"), (15000, "fixed_15s")]:
        print(f"\n  Running {n} iterations @ {timeout_ms}ms timeout...")
        for i in range(n):
            session_id, connect_url = await new_bb_session()
            async with async_playwright() as pw:
                try:
                    browser = await pw.chromium.connect_over_cdp(connect_url)
                    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                    page = await ctx.new_page()
                    r = await run_workflow_with_timeout(page, timeout_ms, label, i+1, n)
                    if not r["success"] and page:
                        await capture_failure(page, label, r.get("failed_step", "unknown"), Exception(r["error"] or "unknown"))
                    results[label].append(r)
                    status = "✓" if r["success"] else f"✗ {r.get('failed_step','?')}"
                    total_t = sum(r["steps"].values())
                    print(f"    [{i+1:02d}/{n}] {status}  {total_t:.1f}s")
                    await browser.close()
                except Exception as e:
                    print(f"    [{i+1:02d}/{n}] FATAL: {e}")
                    results[label].append({"run": i+1, "nav_timeout_ms": timeout_ms, "success": False, "error": str(e)[:200]})
            await release_bb_session(session_id)

    # Summarize
    for label in ["baseline_6s", "fixed_15s"]:
        rs = results[label]
        ok = sum(1 for r in rs if r.get("success"))
        print(f"\n  {label}: {ok}/{len(rs)} = {ok/len(rs)*100:.0f}% success")

    with open(f"{OUTDIR}/section1_timeout.json", "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Saved to {OUTDIR}/section1_timeout.json")
    return results


# ── Section 2: Retry + error classification ──────────────────────────────────

async def section_retry(n: int = 10):
    """Run N workflows with retry-once-on-transient logic."""
    print("\n" + "="*70)
    print("SECTION 2: Retry + Error Classification")
    print("="*70)

    results = []
    retry_counts = {"none": 0, "once": 0, "permanent": 0}
    error_classes = {}

    for i in range(n):
        attempt_results = []
        for attempt in range(2):  # attempt 0 = initial, attempt 1 = retry
            session_id, connect_url = await new_bb_session()
            async with async_playwright() as pw:
                browser = await pw.chromium.connect_over_cdp(connect_url)
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                r = await run_workflow_with_timeout(page, 15000, f"retry_{attempt}", i+1, n)
                attempt_results.append(r)
                await browser.close()
            await release_bb_session(session_id)

            if r["success"]:
                print(f"  [{i+1:02d}/{n}] attempt {attempt+1} ✓")
                if attempt == 0:
                    retry_counts["none"] += 1
                else:
                    retry_counts["once"] += 1
                break
            else:
                err_class = r.get("error_class", classify_error(Exception(r.get("error", ""))))
                error_classes[err_class] = error_classes.get(err_class, 0) + 1
                print(f"  [{i+1:02d}/{n}] attempt {attempt+1} ✗ {err_class}")

                # Permanent errors: die, don't retry
                if err_class.startswith("permanent"):
                    retry_counts["permanent"] += 1
                    break

        results.append({"run": i+1, "attempts": attempt_results})

    final_ok = sum(1 for r in results if any(a.get("success") for a in r["attempts"]))
    print(f"\n  Final success (with retry): {final_ok}/{n} = {final_ok/n*100:.0f}%")
    print(f"  No retry needed: {retry_counts['none']}")
    print(f"  Succeeded on retry: {retry_counts['once']}")
    print(f"  Permanent failure (died): {retry_counts['permanent']}")
    print(f"  Error class breakdown: {error_classes}")

    with open(f"{OUTDIR}/section2_retry.json", "w") as f:
        json.dump({"results": results, "retry_counts": retry_counts, "error_classes": error_classes}, f, indent=2)
    return results


# ── Section 3: Popup detection + handling ───────────────────────────────────

async def section_popups(n: int = 10):
    """Track how many popups fire and how long each takes to handle."""
    print("\n" + "="*70)
    print("SECTION 3: Popup Detection + Handling")
    print("="*70)

    results = []
    popup_stats = {"native_dialog": 0, "dom_error_modal": 0, "dom_alert_modal": 0}

    for i in range(n):
        session_id, connect_url = await new_bb_session()
        dialog_count = 0
        modal_handled = []

        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(connect_url)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await ctx.new_page()

            def dialog_handler(d):
                nonlocal dialog_count
                dialog_count += 1
                asyncio.create_task(d.accept())
            page.on("dialog", dialog_handler)

            # Run workflow — track popup handling times inline
            wf.SITE = PUBLIC_SITE
            page.set_default_timeout(15000)

            try:
                await wf.step_login(page)
                if "/concurrent" in page.url:
                    continue

                await wf.step_mfa(page)
                await wf.step_terms(page)

                # Check for error modal before NPI step
                t_modal = time.perf_counter()
                error_mod = page.locator("#error-modal.active")
                if await error_mod.count() > 0:
                    try:
                        await page.locator("#error-modal.active .modal-ok, #error-modal-ok").click(timeout=2000)
                        popup_stats["dom_error_modal"] += 1
                        modal_handled.append({"type": "dom_error", "handle_time": round(time.perf_counter()-t_modal,3)})
                    except Exception:
                        pass

                await wf.step_npi(page)

                # Alert modal on NPI page (20% chance)
                t_modal = time.perf_counter()
                alert_mod = page.locator("#alert-modal.active")
                if await alert_mod.count() > 0:
                    try:
                        await page.locator("#alert-modal.active .modal-ok").click(timeout=2000)
                        popup_stats["dom_alert_modal"] += 1
                        modal_handled.append({"type": "dom_alert", "handle_time": round(time.perf_counter()-t_modal,3)})
                    except Exception:
                        pass

                await wf.step_patient_search(page)
                await wf.step_prior_auth_form(page)
                _, ref = await wf.step_confirmation(page)
                ok = True
            except Exception as e:
                ok = False
                await capture_failure(page, "popup_test", "unknown", e)

            popup_stats["native_dialog"] += dialog_count
            results.append({"run": i+1, "success": ok, "dialogs": dialog_count, "modals": modal_handled})
            print(f"  [{i+1:02d}/{n}] {'✓' if ok else '✗'}  native={dialog_count}  modals={len(modal_handled)}")

            await browser.close()
        await release_bb_session(session_id)

    print(f"\n  Total native dialogs: {popup_stats['native_dialog']}")
    print(f"  Total DOM error modals: {popup_stats['dom_error_modal']}")
    print(f"  Total DOM alert modals: {popup_stats['dom_alert_modal']}")

    with open(f"{OUTDIR}/section3_popups.json", "w") as f:
        json.dump({"results": results, "popup_stats": popup_stats}, f, indent=2)
    return results


# ── Section 4: Auto-logout detection ────────────────────────────────────────

async def section_autologout():
    """Login, idle for 5+ minutes, navigate, verify session_expired."""
    print("\n" + "="*70)
    print("SECTION 4: Auto-Logout Detection (5-min idle)")
    print("="*70)

    session_id, connect_url = await new_bb_session()
    result = {"logged_in": False, "idle_duration_s": 0, "expired_detected": False, "final_url": None}

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(connect_url)
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await ctx.new_page()
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))
        page.set_default_timeout(15000)

        wf.SITE = PUBLIC_SITE
        try:
            print("  Logging in...")
            await wf.step_login(page)
            await wf.step_mfa(page)
            await wf.step_terms(page)
            await wf.step_npi(page)
            result["logged_in"] = True
            print(f"  Logged in, current URL: {page.url}")

            # Idle for 5 minutes 30 seconds (PortalX TTL is 300s)
            idle_s = 330
            print(f"  Idling for {idle_s}s...")
            t0 = time.perf_counter()
            await asyncio.sleep(idle_s)
            result["idle_duration_s"] = round(time.perf_counter() - t0, 1)

            print("  Attempting navigation after idle...")
            try:
                await page.click("button[type=submit]", timeout=5000)
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception as e:
                print(f"  Click/nav after idle threw: {e}")

            result["final_url"] = page.url
            result["expired_detected"] = "session-expired" in page.url or "/login" in page.url
            print(f"  Final URL: {page.url}")
            print(f"  Session expired detected: {result['expired_detected']}")

            # Save screenshot
            await page.screenshot(path=f"{OUTDIR}/screenshots/autologout_final.png")
        except Exception as e:
            result["error"] = str(e)[:200]
            print(f"  Error: {e}")

        await browser.close()
    await release_bb_session(session_id)

    with open(f"{OUTDIR}/section4_autologout.json", "w") as f:
        json.dump(result, f, indent=2)
    return result


# ── Section 5: Connection resilience ────────────────────────────────────────

async def section_resilience():
    """Open BB session, idle 60s/120s/180s, verify connection still alive."""
    print("\n" + "="*70)
    print("SECTION 5: Connection Resilience (idle mid-session)")
    print("="*70)

    session_id, connect_url = await new_bb_session()
    result = {"tests": []}

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(connect_url)
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await ctx.new_page()

        for idle_s in [30, 60, 120]:
            print(f"\n  Navigate → idle {idle_s}s → navigate again...")
            t_test = {"idle_s": idle_s, "nav1_ok": False, "nav2_ok": False, "nav2_time": None}
            try:
                await page.goto(f"{PUBLIC_SITE}/login", wait_until="domcontentloaded", timeout=15000)
                t_test["nav1_ok"] = True
                print(f"    First nav: OK")

                await asyncio.sleep(idle_s)

                t_nav = time.perf_counter()
                await page.goto(f"{PUBLIC_SITE}/login", wait_until="domcontentloaded", timeout=15000)
                t_test["nav2_time"] = round(time.perf_counter() - t_nav, 3)
                t_test["nav2_ok"] = True
                print(f"    Second nav after {idle_s}s idle: OK ({t_test['nav2_time']}s)")
            except Exception as e:
                t_test["error"] = str(e)[:200]
                print(f"    FAILED: {e}")
            result["tests"].append(t_test)

        await browser.close()
    await release_bb_session(session_id)

    with open(f"{OUTDIR}/section5_resilience.json", "w") as f:
        json.dump(result, f, indent=2)
    return result


# ── Section 6: File upload ──────────────────────────────────────────────────

async def section_upload():
    """Run workflow with actual file upload at the prior auth step."""
    print("\n" + "="*70)
    print("SECTION 6: File Upload")
    print("="*70)

    # Create a small test file
    test_file = f"{OUTDIR}/test_upload.pdf"
    with open(test_file, "wb") as f:
        # Minimal valid PDF (tiny empty 1-page PDF)
        f.write(b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
                b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
                b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
                b"xref\n0 4\n0000000000 65535 f\n"
                b"0000000009 00000 n\n0000000058 00000 n\n0000000108 00000 n\n"
                b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n160\n%%EOF\n")

    results = []
    for label, use_bb in [("LOCAL", False), ("BB", True)]:
        print(f"\n  {label}:")
        if use_bb:
            session_id, connect_url = await new_bb_session()

        async with async_playwright() as pw:
            if use_bb:
                browser = await pw.chromium.connect_over_cdp(connect_url)
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            else:
                browser = await pw.chromium.launch(headless=True)
                ctx = await browser.new_context()
            page = await ctx.new_page()
            page.on("dialog", lambda d: asyncio.create_task(d.accept()))
            page.set_default_timeout(15000)
            wf.SITE = PUBLIC_SITE

            try:
                await wf.step_login(page)
                if "/concurrent" in page.url:
                    print(f"    ✗ concurrent session, skipping")
                    continue
                await wf.step_mfa(page)
                await wf.step_terms(page)
                await wf.step_npi(page)
                await wf.step_patient_search(page)

                # On prior auth page — upload file
                await page.wait_for_url(f"{PUBLIC_SITE}/prior-auth*", timeout=15000)
                t_upload = time.perf_counter()
                await page.set_input_files("#file-input", test_file)
                upload_time = round(time.perf_counter() - t_upload, 3)
                print(f"    File upload: {upload_time}s")

                # Verify file was selected
                fname_visible = await page.locator("#file-name").inner_text()
                print(f"    Selected: {fname_visible}")

                results.append({"label": label, "upload_time_s": upload_time, "filename_displayed": fname_visible, "ok": True})
            except Exception as e:
                print(f"    ✗ ERROR: {e}")
                results.append({"label": label, "ok": False, "error": str(e)[:200]})

            await browser.close()

        if use_bb:
            await release_bb_session(session_id)

    with open(f"{OUTDIR}/section6_upload.json", "w") as f:
        json.dump(results, f, indent=2)
    return results


# ── Section 7: Audit trail verification ────────────────────────────────────

async def section_audit():
    """Run one BB workflow, then verify the session replay is accessible."""
    print("\n" + "="*70)
    print("SECTION 7: Audit Trail Verification (Browserbase)")
    print("="*70)

    session_id, connect_url = await new_bb_session()
    print(f"  Session ID: {session_id}")
    result = {"session_id": session_id, "replay_url": None, "workflow_ok": False}

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(connect_url)
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await ctx.new_page()
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))
        page.set_default_timeout(15000)
        wf.SITE = PUBLIC_SITE

        try:
            r = await run_workflow_with_timeout(page, 15000, "audit", 1, 1)
            result["workflow_ok"] = r["success"]
            result["workflow_result"] = r
        except Exception as e:
            result["error"] = str(e)[:200]

        await browser.close()

    # Fetch session details from Browserbase API for replay URL
    try:
        from browserbase import AsyncBrowserbase
        bb = AsyncBrowserbase(api_key=BB_API_KEY)
        await release_bb_session(session_id)
        await asyncio.sleep(3)  # give BB time to finalize recording
        sess_detail = await bb.sessions.retrieve(id=session_id)
        result["replay_url"] = f"https://www.browserbase.com/sessions/{session_id}"
        result["session_status"] = getattr(sess_detail, "status", None)
        result["session_duration_s"] = getattr(sess_detail, "duration", None)
    except Exception as e:
        result["api_error"] = str(e)[:200]

    print(f"  Workflow OK: {result['workflow_ok']}")
    print(f"  Replay URL: {result.get('replay_url')}")
    print(f"  Session status: {result.get('session_status')}")

    with open(f"{OUTDIR}/section7_audit.json", "w") as f:
        json.dump(result, f, indent=2, default=str)
    return result


# ── Section 8: Sustained load (25 concurrent for N minutes) ────────────────

async def section_sustained(minutes: int = 10, concurrency: int = 25):
    """Run concurrency-wide batches repeatedly for `minutes` minutes."""
    print("\n" + "="*70)
    print(f"SECTION 8: Sustained Load ({concurrency} concurrent for {minutes} min)")
    print("="*70)

    deadline = time.perf_counter() + minutes * 60
    batches = []
    batch_num = 0

    while time.perf_counter() < deadline:
        batch_num += 1
        t_batch = time.perf_counter()
        print(f"\n  Batch {batch_num} starting ({concurrency} sessions)...")

        async def run_one(i):
            session_id, connect_url = await new_bb_session()
            try:
                async with async_playwright() as pw:
                    browser = await pw.chromium.connect_over_cdp(connect_url)
                    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                    page = await ctx.new_page()
                    r = await run_workflow_with_timeout(page, 15000, f"sustained_b{batch_num}", i, concurrency)
                    await browser.close()
                return r
            finally:
                await release_bb_session(session_id)

        results = await asyncio.gather(*[run_one(i) for i in range(concurrency)], return_exceptions=True)
        batch_time = time.perf_counter() - t_batch

        ok = sum(1 for r in results if isinstance(r, dict) and r.get("success"))
        batches.append({
            "batch": batch_num,
            "wall_time_s": round(batch_time, 2),
            "success": ok,
            "total": concurrency,
            "success_rate": round(ok/concurrency*100, 1),
        })
        print(f"  Batch {batch_num}: {ok}/{concurrency} ({ok/concurrency*100:.0f}%) in {batch_time:.1f}s")

    total_sessions = sum(b["total"] for b in batches)
    total_success = sum(b["success"] for b in batches)
    print(f"\n  Total: {total_success}/{total_sessions} ({total_success/total_sessions*100:.0f}%) across {batch_num} batches")

    with open(f"{OUTDIR}/section8_sustained.json", "w") as f:
        json.dump({"batches": batches, "total_sessions": total_sessions, "total_success": total_success}, f, indent=2)
    return batches


# ── Main ────────────────────────────────────────────────────────────────────

async def main(args):
    section = args.section
    if section in ("timeout", "all"):
        await section_timeout(n=args.n)
    if section in ("retry", "all"):
        await section_retry(n=args.n)
    if section in ("popups", "all"):
        await section_popups(n=args.n)
    if section in ("autologout", "all"):
        await section_autologout()
    if section in ("resilience", "all"):
        await section_resilience()
    if section in ("upload", "all"):
        await section_upload()
    if section in ("audit", "all"):
        await section_audit()
    if section in ("sustained", "all"):
        await section_sustained(minutes=args.minutes, concurrency=args.concurrency)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--section", default="all",
                        choices=["all","timeout","retry","popups","autologout","resilience","upload","audit","sustained"])
    parser.add_argument("--n", type=int, default=10, help="iterations per section")
    parser.add_argument("--minutes", type=int, default=10, help="minutes for sustained test")
    parser.add_argument("--concurrency", type=int, default=25, help="concurrent sessions for sustained test")
    asyncio.run(main(parser.parse_args()))
