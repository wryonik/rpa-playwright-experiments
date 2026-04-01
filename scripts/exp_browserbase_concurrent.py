"""
Experiment: Concurrent Browserbase Sessions
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Runs N Browserbase CDP sessions in parallel (not sequential) to measure:
  - Whether sessions interfere with each other
  - Throughput vs sequential (wall time)
  - Session creation time under concurrency
  - Per-step latency under concurrent load

Requires:
    PUBLIC_SITE_URL   — publicly reachable PortalX URL (Render)
    BROWSERBASE_API_KEY
    BROWSERBASE_PROJECT_ID

Usage:
    python exp_browserbase_concurrent.py               # 3 concurrent
    python exp_browserbase_concurrent.py --sessions 5
"""
import argparse
import asyncio
import os
import statistics
import time

from playwright.async_api import async_playwright

import exp_full_workflow as wf
from utils import CHROME_ARGS, RunResult

PUBLIC_SITE = os.getenv("PUBLIC_SITE_URL", "")


async def run_one_session(pw, session_idx: int, total: int, results: list, api_key: str, project_id: str):
    """Run a full workflow in one Browserbase session. Appends result to results list."""
    from browserbase import AsyncBrowserbase

    wf.SITE = PUBLIC_SITE
    label = f"S{session_idx:02d}"
    t_start = time.perf_counter()
    timings = {}
    session_id = None

    try:
        bb = AsyncBrowserbase(api_key=api_key)

        t_sess = time.perf_counter()
        session = await bb.sessions.create(project_id=project_id)
        session_id = session.id
        timings["session_create_s"] = round(time.perf_counter() - t_sess, 3)
        print(f"  [{label}] session_create  {timings['session_create_s']:6.3f}s  id={session_id[:8]}…")

        t_cdp = time.perf_counter()
        browser = await pw.chromium.connect_over_cdp(session.connect_url)
        timings["cdp_connect_s"] = round(time.perf_counter() - t_cdp, 3)
        print(f"  [{label}] cdp_connect     {timings['cdp_connect_s']:6.3f}s")

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))

        # Step-by-step
        ok = True
        for step_name, step_fn in [
            ("login",          lambda: wf.step_login(page)),
            ("mfa",            lambda: wf.step_mfa(page)),
            ("terms",          lambda: wf.step_terms(page)),
            ("npi",            lambda: wf.step_npi(page)),
            ("patient_search", lambda: wf.step_patient_search(page)),
            ("prior_auth",     lambda: wf.step_prior_auth_form(page)),
            ("confirmation",   lambda: wf.step_confirmation(page)),
        ]:
            try:
                if step_name == "confirmation":
                    elapsed, ref = await step_fn()
                    timings[f"{step_name}_s"] = round(elapsed, 3)
                    print(f"  [{label}] {step_name:14}  {elapsed:6.3f}s  ref={ref}")
                else:
                    elapsed = await step_fn()
                    timings[f"{step_name}_s"] = round(elapsed, 3)
                    print(f"  [{label}] {step_name:14}  {elapsed:6.3f}s")
            except Exception as e:
                print(f"  [{label}] {step_name:14}  ✗ {str(e)[:80]}")
                ok = False
                break

        await browser.close()
        await bb.sessions.update(id=session_id, project_id=project_id, status="REQUEST_RELEASE")

        total_s = round(time.perf_counter() - t_start, 3)
        print(f"  [{label}] ── total {total_s:.3f}s  {'✓' if ok else '✗'}\n")
        results.append({"session": session_idx, "success": ok, "total_s": total_s, "timings": timings})

    except Exception as e:
        if session_id:
            try:
                bb2 = AsyncBrowserbase(api_key=api_key)
                await bb2.sessions.update(id=session_id, project_id=project_id, status="REQUEST_RELEASE")
            except Exception:
                pass
        total_s = round(time.perf_counter() - t_start, 3)
        print(f"  [S{session_idx:02d}] ✗ FATAL: {e}\n")
        results.append({"session": session_idx, "success": False, "total_s": total_s, "timings": timings, "error": str(e)})


async def main(args):
    n = args.sessions
    api_key    = os.getenv("BROWSERBASE_API_KEY", "")
    project_id = os.getenv("BROWSERBASE_PROJECT_ID", "")

    if not api_key or not project_id:
        print("ERROR: BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID must be set")
        return
    if not PUBLIC_SITE:
        print("ERROR: PUBLIC_SITE_URL must be set to the public Render URL")
        return

    print(f"\n{'='*62}")
    print(f"  Concurrent Browserbase Sessions: {n} in parallel")
    print(f"  Site: {PUBLIC_SITE}")
    print(f"{'='*62}\n")

    results = []
    t_wall_start = time.perf_counter()

    async with async_playwright() as pw:
        tasks = [
            run_one_session(pw, i + 1, n, results, api_key, project_id)
            for i in range(n)
        ]
        await asyncio.gather(*tasks)

    wall_time = round(time.perf_counter() - t_wall_start, 3)

    # ── Summary ───────────────────────────────────────────────────────────────
    successes = [r for r in results if r["success"]]
    total_times = [r["total_s"] for r in results]

    print(f"\n{'='*62}")
    print(f"  Concurrent Browserbase — Results")
    print(f"{'='*62}")
    print(f"  Sessions run       : {n}")
    print(f"  Successful         : {len(successes)} / {n}  ({len(successes)/n*100:.0f}%)")
    print(f"  Wall time (total)  : {wall_time:.3f}s")
    if total_times:
        print(f"  Avg per-session    : {statistics.mean(total_times):.3f}s")
        print(f"  Max per-session    : {max(total_times):.3f}s")
        print(f"  Parallelism gain   : {statistics.mean(total_times)/wall_time:.1f}x")

    # Step breakdown across successful sessions
    if successes:
        step_keys = ["session_create_s", "cdp_connect_s", "login_s", "mfa_s",
                     "terms_s", "npi_s", "patient_search_s", "prior_auth_s", "confirmation_s"]
        print(f"\n  Per-step timing across {len(successes)} successful session(s):")
        print(f"  {'Step':22}  {'avg':>7}  {'min':>7}  {'max':>7}")
        print(f"  {'-'*22}  {'-'*7}  {'-'*7}  {'-'*7}")
        for k in step_keys:
            vals = [r["timings"][k] for r in successes if k in r["timings"]]
            if vals:
                name = k.replace("_s", "")
                print(f"  {name:22}  {statistics.mean(vals):>7.3f}  {min(vals):>7.3f}  {max(vals):>7.3f}")

    # Session create latency under concurrency
    sess_times = [r["timings"].get("session_create_s", 0) for r in results if "session_create_s" in r["timings"]]
    if sess_times:
        print(f"\n  Session creation under {n}-way concurrency:")
        print(f"    avg: {statistics.mean(sess_times):.3f}s   max: {max(sess_times):.3f}s   min: {min(sess_times):.3f}s")

    print(f"\n  Per-session summary:")
    for r in sorted(results, key=lambda x: x["session"]):
        status = "✓" if r["success"] else "✗"
        err = f"  err: {r.get('error','')[:60]}" if not r["success"] else ""
        print(f"    S{r['session']:02d}: {status}  {r['total_s']:.3f}s{err}")

    print(f"{'='*62}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=3,
                        help="Number of concurrent Browserbase sessions (default: 3)")
    asyncio.run(main(parser.parse_args()))
