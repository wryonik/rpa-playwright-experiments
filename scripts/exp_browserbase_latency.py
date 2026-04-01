"""
Experiment: Browserbase vs Local Playwright Latency
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Runs the full 7-step PortalX v2 workflow in both modes and compares per-step
latency to isolate WHERE the overhead lives (session creation, navigation,
form interaction, etc.).

Mode A — Local Playwright  : chromium.launch() inside the Docker container
Mode B — Browserbase CDP   : chromium.connect_over_cdp() to a remote session

Step timings printed after every single run so latency spikes are visible
immediately, not just in aggregated averages.

Environment variables:
    SITE_URL          Docker-internal URL (for local mode). Default: http://portalx-site:8888
    PUBLIC_SITE_URL   Publicly reachable URL (for Browserbase mode — ngrok / tunnel).
                      Browserbase sessions run in the cloud and cannot reach Docker LAN.
    BROWSERBASE_API_KEY
    BROWSERBASE_PROJECT_ID

Usage:
    python exp_browserbase_latency.py
    python exp_browserbase_latency.py --iterations 20
    python exp_browserbase_latency.py --local-only
"""
import argparse
import asyncio
import os
import statistics
import time

from playwright.async_api import async_playwright

import exp_full_workflow as wf
from utils import CHROME_ARGS, ExperimentMetrics, ResourceSampler, RunResult

LOCAL_SITE    = os.getenv("SITE_URL", "http://portalx-site:8888")
PUBLIC_SITE   = os.getenv("PUBLIC_SITE_URL", "")

STEPS = [
    "session_create",   # Browserbase only
    "login",
    "mfa",
    "terms",
    "npi",
    "patient_search",
    "prior_auth",
    "confirmation",
]


# ── Single run ────────────────────────────────────────────────────────────────

async def run_workflow(page, label: str, run_idx: int, total: int) -> tuple[bool, dict]:
    """
    Execute all 7 steps against the current page. Returns (success, step_timings).
    Prints a per-step latency line immediately after each step.
    """
    timings = {}
    prefix = f"  [{run_idx:02d}/{total}] {label}"

    page.on("dialog", lambda d: asyncio.create_task(d.accept()))

    try:
        # Check for concurrent session on login redirect
        t = time.perf_counter()
        elapsed = await wf.step_login(page)
        timings["login_s"] = round(elapsed, 3)
        print(f"{prefix}  login       {elapsed:6.3f}s")

        # If we landed on /concurrent, bail
        if "/concurrent" in page.url:
            print(f"{prefix}  ✗ concurrent session — skipping run")
            return False, timings

        elapsed = await wf.step_mfa(page)
        timings["mfa_s"] = round(elapsed, 3)
        print(f"{prefix}  mfa         {elapsed:6.3f}s")

        elapsed = await wf.step_terms(page)
        timings["terms_s"] = round(elapsed, 3)
        print(f"{prefix}  terms       {elapsed:6.3f}s")

        elapsed = await wf.step_npi(page)
        timings["npi_s"] = round(elapsed, 3)
        print(f"{prefix}  npi         {elapsed:6.3f}s")

        elapsed = await wf.step_patient_search(page)
        timings["patient_search_s"] = round(elapsed, 3)
        print(f"{prefix}  pat_search  {elapsed:6.3f}s")

        elapsed = await wf.step_prior_auth_form(page)
        timings["prior_auth_s"] = round(elapsed, 3)
        print(f"{prefix}  prior_auth  {elapsed:6.3f}s")

        elapsed, ref_id = await wf.step_confirmation(page)
        timings["confirmation_s"] = round(elapsed, 3)
        print(f"{prefix}  confirm     {elapsed:6.3f}s   ref={ref_id}")

        return True, timings

    except Exception as e:
        print(f"{prefix}  ✗ ERROR: {e}")
        return False, timings


async def run_local(pw, idx: int, total: int) -> tuple[RunResult, dict]:
    """One full workflow run using local Chromium."""
    wf.SITE = LOCAL_SITE  # ensure correct URL for this mode
    t_start = time.perf_counter()

    try:
        browser = await pw.chromium.launch(headless=True, args=CHROME_ARGS)
        context = await browser.new_context()
        page = await context.new_page()

        ok, timings = await run_workflow(page, "LOCAL", idx, total)
        await browser.close()

        total_s = time.perf_counter() - t_start
        print(f"  [{idx:02d}/{total}] LOCAL  ── total {total_s:.3f}s  {'✓' if ok else '✗'}\n")

        return RunResult(
            worker_id=idx, success=ok,
            duration_s=total_s, peak_memory_mb=0,
            steps_completed=len(timings),
        ), timings

    except Exception as e:
        total_s = time.perf_counter() - t_start
        print(f"  [{idx:02d}/{total}] LOCAL  ✗ FATAL: {e}\n")
        return RunResult(
            worker_id=idx, success=False,
            duration_s=total_s, peak_memory_mb=0,
            steps_completed=0, error=str(e)[:120],
        ), {}


async def run_browserbase(pw, idx: int, total: int) -> tuple[RunResult, dict]:
    """One full workflow run using Browserbase CDP."""
    api_key    = os.getenv("BROWSERBASE_API_KEY", "")
    project_id = os.getenv("BROWSERBASE_PROJECT_ID", "")

    if not PUBLIC_SITE:
        raise RuntimeError(
            "PUBLIC_SITE_URL is not set. Browserbase sessions run in the cloud and "
            "cannot reach the Docker-internal URL. Set PUBLIC_SITE_URL to your ngrok "
            "or tunnel URL, e.g. https://xxxx.ngrok-free.app"
        )

    wf.SITE = PUBLIC_SITE  # point step functions at the publicly reachable URL

    try:
        from browserbase import AsyncBrowserbase
    except ImportError:
        raise RuntimeError("browserbase SDK not installed: pip install browserbase")

    t_start = time.perf_counter()
    timings = {}
    session_id = None

    try:
        bb = AsyncBrowserbase(api_key=api_key)

        # ── Session creation latency ─────────────────────────────────────────
        t_sess = time.perf_counter()
        session = await bb.sessions.create(project_id=project_id)
        session_id = session.id
        sess_s = round(time.perf_counter() - t_sess, 3)
        timings["session_create_s"] = sess_s
        print(f"  [{idx:02d}/{total}] BB     session_create  {sess_s:6.3f}s  id={session_id[:8]}…")

        # ── CDP connect ──────────────────────────────────────────────────────
        t_cdp = time.perf_counter()
        browser = await pw.chromium.connect_over_cdp(session.connect_url)
        cdp_s = round(time.perf_counter() - t_cdp, 3)
        timings["cdp_connect_s"] = cdp_s
        print(f"  [{idx:02d}/{total}] BB     cdp_connect     {cdp_s:6.3f}s")

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()

        ok, step_timings = await run_workflow(page, "BB    ", idx, total)
        timings.update(step_timings)

        await browser.close()
        await bb.sessions.update(id=session_id, project_id=project_id, status="REQUEST_RELEASE")

        total_s = time.perf_counter() - t_start
        print(f"  [{idx:02d}/{total}] BB     ── total {total_s:.3f}s  {'✓' if ok else '✗'}\n")

        return RunResult(
            worker_id=idx, success=ok,
            duration_s=total_s, peak_memory_mb=0,
            steps_completed=len(step_timings),
        ), timings

    except Exception as e:
        if session_id:
            try:
                bb2 = AsyncBrowserbase(api_key=api_key)
                await bb2.sessions.update(id=session_id, project_id=project_id, status="REQUEST_RELEASE")
            except Exception:
                pass
        total_s = time.perf_counter() - t_start
        print(f"  [{idx:02d}/{total}] BB     ✗ FATAL: {e}\n")
        return RunResult(
            worker_id=idx, success=False,
            duration_s=total_s, peak_memory_mb=0,
            steps_completed=0, error=str(e)[:120],
        ), timings


# ── Aggregation helpers ───────────────────────────────────────────────────────

def pct(data: list[float], p: int) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    return s[min(int(len(s) * p / 100), len(s) - 1)]


def agg(all_timings: list[dict], key: str) -> dict:
    vals = [t[key] for t in all_timings if key in t]
    if not vals:
        return {}
    return {
        "n":    len(vals),
        "avg":  round(statistics.mean(vals), 3),
        "min":  round(min(vals), 3),
        "p50":  round(pct(vals, 50), 3),
        "p95":  round(pct(vals, 95), 3),
        "max":  round(max(vals), 3),
    }


def print_step_table(label: str, all_timings: list[dict]):
    keys = [k for k in STEPS if any(f"{k}_s" in t for t in all_timings)]
    # Also include cdp_connect if present
    extra = [k for k in ["cdp_connect"] if any(f"{k}_s" in t for t in all_timings)]
    all_keys = [f"{k}_s" for k in (["session_create"] + extra + keys[1:])]

    print(f"\n  {label} — per-step breakdown (seconds):")
    print(f"  {'Step':22}  {'n':>3}  {'avg':>7}  {'p50':>7}  {'p95':>7}  {'max':>7}")
    print(f"  {'-'*22}  {'-'*3}  {'-'*7}  {'-'*7}  {'-'*7}  {'-'*7}")
    for k in all_keys:
        a = agg(all_timings, k)
        if not a:
            continue
        name = k.replace("_s", "")
        print(f"  {name:22}  {a['n']:>3}  {a['avg']:>7.3f}  {a['p50']:>7.3f}  {a['p95']:>7.3f}  {a['max']:>7.3f}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(args):
    n = args.iterations
    local_only = args.local_only or not os.getenv("BROWSERBASE_API_KEY")

    if local_only and not args.local_only:
        print("  ⚠  BROWSERBASE_API_KEY not set — running local-only")

    # ── Local runs ────────────────────────────────────────────────────────────
    print(f"\n{'='*62}")
    print(f"  Mode A: Local Playwright  ({n} iterations)")
    print(f"  Site: {LOCAL_SITE}")
    print(f"{'='*62}\n")

    local_results, local_timings_all = [], []

    async with async_playwright() as pw:
        async with ResourceSampler() as sampler_local:
            for i in range(1, n + 1):
                r, t = await run_local(pw, i, n)
                local_results.append(r)
                if t:
                    local_timings_all.append(t)

    local_metrics = ExperimentMetrics.from_results(
        "bb_latency_local", local_results,
        resource_summary=sampler_local.summary(),
        extras={"mode": "local_chromium", "site": LOCAL_SITE},
    )

    local_durations = [r.duration_s for r in local_results]
    print(f"\n  Local summary:")
    print(f"    success rate : {local_metrics.success_rate}%")
    print(f"    avg total    : {local_metrics.avg_runtime_sec:.3f}s")
    print(f"    p95 total    : {local_metrics.p95_runtime_sec:.3f}s")
    print_step_table("Local", local_timings_all)
    local_metrics.save("/tmp/exp_local_latency.json")

    # ── Browserbase runs ──────────────────────────────────────────────────────
    bb_metrics = None
    bb_timings_all = []

    if not local_only:
        print(f"\n{'='*62}")
        print(f"  Mode B: Browserbase CDP  ({n} iterations)")
        print(f"  Site: {PUBLIC_SITE}")
        print(f"{'='*62}\n")

        bb_results = []

        async with async_playwright() as pw:
            async with ResourceSampler() as sampler_bb:
                for i in range(1, n + 1):
                    r, t = await run_browserbase(pw, i, n)
                    bb_results.append(r)
                    if t:
                        bb_timings_all.append(t)

        bb_metrics = ExperimentMetrics.from_results(
            "bb_latency_cdp", bb_results,
            resource_summary=sampler_bb.summary(),
            extras={"mode": "browserbase_cdp", "site": PUBLIC_SITE},
        )

        bb_durations = [r.duration_s for r in bb_results]
        print(f"\n  Browserbase summary:")
        print(f"    success rate : {bb_metrics.success_rate}%")
        print(f"    avg total    : {bb_metrics.avg_runtime_sec:.3f}s")
        print(f"    p95 total    : {bb_metrics.p95_runtime_sec:.3f}s")
        print_step_table("Browserbase", bb_timings_all)
        bb_metrics.save("/tmp/exp_bb_latency.json")

    # ── Comparison ────────────────────────────────────────────────────────────
    if bb_metrics and local_timings_all and bb_timings_all:
        la, lp95 = local_metrics.avg_runtime_sec, local_metrics.p95_runtime_sec
        ba, bp95 = bb_metrics.avg_runtime_sec,    bb_metrics.p95_runtime_sec

        print(f"\n{'='*62}")
        print(f"  Local vs Browserbase — comparison")
        print(f"{'='*62}")
        print(f"  {'':22}  {'Local':>10}  {'Browserbase':>12}  {'Δ overhead':>12}")
        print(f"  {'-'*22}  {'-'*10}  {'-'*12}  {'-'*12}")
        print(f"  {'Total avg':22}  {la:>9.3f}s  {ba:>11.3f}s  {((ba-la)/la*100):>+11.1f}%")
        print(f"  {'Total p95':22}  {lp95:>9.3f}s  {bp95:>11.3f}s  {((bp95-lp95)/lp95*100):>+11.1f}%")

        # Per-step comparison
        all_step_keys = sorted(set(k for t in local_timings_all + bb_timings_all for k in t))
        print(f"\n  Per-step overhead:")
        print(f"  {'Step':22}  {'Local avg':>10}  {'BB avg':>10}  {'Δ':>10}")
        print(f"  {'-'*22}  {'-'*10}  {'-'*10}  {'-'*10}")
        for k in all_step_keys:
            la_step = agg(local_timings_all, k)
            ba_step = agg(bb_timings_all, k)
            lv = la_step.get("avg", 0)
            bv = ba_step.get("avg", 0)
            if bv == 0 and lv == 0:
                continue
            if lv > 0:
                diff = f"{(bv - lv) / lv * 100:+.1f}%"
            else:
                diff = "BB only"
            name = k.replace("_s", "")
            print(f"  {name:22}  {lv:>9.3f}s  {bv:>9.3f}s  {diff:>10}")

        # Session overhead breakdown
        if bb_timings_all:
            sess_vals = [t.get("session_create_s", 0) for t in bb_timings_all if "session_create_s" in t]
            cdp_vals  = [t.get("cdp_connect_s", 0)   for t in bb_timings_all if "cdp_connect_s"   in t]
            if sess_vals:
                overhead = statistics.mean(sess_vals) + (statistics.mean(cdp_vals) if cdp_vals else 0)
                print(f"\n  Browserbase session startup overhead:")
                print(f"    session create : {statistics.mean(sess_vals):.3f}s avg  p95={pct(sess_vals,95):.3f}s")
                if cdp_vals:
                    print(f"    cdp connect    : {statistics.mean(cdp_vals):.3f}s avg  p95={pct(cdp_vals,95):.3f}s")
                print(f"    total startup  : {overhead:.3f}s avg")
                print(f"    (startup is {overhead/ba*100:.0f}% of total BB workflow time)")

        print(f"\n  Results saved to /tmp/exp_local_latency.json and /tmp/exp_bb_latency.json")

    print(f"{'='*62}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--local-only", action="store_true")
    asyncio.run(main(parser.parse_args()))
