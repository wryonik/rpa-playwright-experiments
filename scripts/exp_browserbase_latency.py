"""
Experiment 5: Browserbase vs Local Playwright Latency
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Runs the same full workflow (login → form fill → submit) 10+ times each:
  - Mode A: Local Playwright (chromium.launch)
  - Mode B: Browserbase CDP  (chromium.connect_over_cdp)

Measures per-run and per-step timing to isolate where the difference lives.

Requires BROWSERBASE_API_KEY, BROWSERBASE_PROJECT_ID, BROWSERBASE_REGION
to be set in environment for Mode B. If not set, Mode B is skipped and
the script reports local-only results with a note.

Usage:
    python exp_browserbase_latency.py
    python exp_browserbase_latency.py --iterations 20
    python exp_browserbase_latency.py --local-only     # skip Browserbase
"""
import argparse
import asyncio
import os
import statistics
import time

from playwright.async_api import async_playwright

from utils import (
    CHROME_ARGS,
    ExperimentMetrics,
    ResourceSampler,
    RunResult,
    fill_form,
    login,
    print_summary,
)


# ── Single run helpers ────────────────────────────────────────────────────────

async def run_local(pw, headless: bool) -> tuple[RunResult, dict]:
    """One full workflow run using local Chromium."""
    t_start = time.perf_counter()
    step_timings = {}
    steps = 0

    try:
        browser = await pw.chromium.launch(headless=headless, args=CHROME_ARGS)
        context = await browser.new_context()
        page = await context.new_page()
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))

        t_login = time.perf_counter()
        ok = await login(page)
        step_timings["login_s"] = round(time.perf_counter() - t_login, 3)
        steps = 1 if ok else 0

        if ok:
            form_steps, form_timings = await fill_form(page)
            steps += form_steps
            step_timings.update(form_timings)

        await browser.close()
        total = time.perf_counter() - t_start
        return RunResult(
            worker_id=0, success=steps >= 6,
            duration_s=total, peak_memory_mb=0, steps_completed=steps,
        ), step_timings

    except Exception as e:
        return RunResult(
            worker_id=0, success=False,
            duration_s=time.perf_counter() - t_start,
            peak_memory_mb=0, steps_completed=steps, error=str(e)[:100],
        ), step_timings


async def run_browserbase(pw, headless: bool) -> tuple[RunResult, dict]:
    """One full workflow run using Browserbase CDP."""
    api_key = os.getenv("BROWSERBASE_API_KEY", "")
    project_id = os.getenv("BROWSERBASE_PROJECT_ID", "")
    region = os.getenv("BROWSERBASE_REGION", "us-east-1")

    # Lazy import — only needed for Browserbase mode
    try:
        from browserbase import AsyncBrowserbase
    except ImportError:
        return RunResult(
            worker_id=0, success=False, duration_s=0,
            peak_memory_mb=0, steps_completed=0,
            error="browserbase SDK not installed (pip install browserbase)",
        ), {}

    t_start = time.perf_counter()
    step_timings = {}
    steps = 0
    session_id = None

    try:
        bb = AsyncBrowserbase(api_key=api_key)

        t_session = time.perf_counter()
        session = await bb.sessions.create(project_id=project_id, region=region)
        session_id = session.id
        step_timings["session_create_s"] = round(time.perf_counter() - t_session, 3)

        browser = await pw.chromium.connect_over_cdp(session.connect_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))

        t_login = time.perf_counter()
        ok = await login(page)
        step_timings["login_s"] = round(time.perf_counter() - t_login, 3)
        steps = 1 if ok else 0

        if ok:
            form_steps, form_timings = await fill_form(page)
            steps += form_steps
            step_timings.update(form_timings)

        await browser.close()
        # Release session
        await bb.sessions.update(id=session_id, project_id=project_id, status="REQUEST_RELEASE")

        total = time.perf_counter() - t_start
        return RunResult(
            worker_id=0, success=steps >= 6,
            duration_s=total, peak_memory_mb=0, steps_completed=steps,
        ), step_timings

    except Exception as e:
        if session_id:
            try:
                bb2 = AsyncBrowserbase(api_key=api_key)
                await bb2.sessions.update(id=session_id, project_id=project_id, status="REQUEST_RELEASE")
            except Exception:
                pass
        return RunResult(
            worker_id=0, success=False,
            duration_s=time.perf_counter() - t_start,
            peak_memory_mb=0, steps_completed=steps, error=str(e)[:100],
        ), step_timings


# ── Aggregation ───────────────────────────────────────────────────────────────

def aggregate_step_timings(all_timings: list[dict]) -> dict:
    keys = set(k for t in all_timings for k in t)
    out = {}
    for k in sorted(keys):
        vals = [t[k] for t in all_timings if k in t]
        if vals:
            out[k] = {
                "avg_s": round(statistics.mean(vals), 3),
                "p95_s": round(sorted(vals)[int(len(vals) * 0.95)], 3),
            }
    return out


def percentile(data: list[float], p: int) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    return s[min(int(len(s) * p / 100), len(s) - 1)]


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(args):
    headless = args.headless.lower() != "false"
    n = args.iterations
    local_only = args.local_only or not os.getenv("BROWSERBASE_API_KEY")

    if local_only and not args.local_only:
        print("  ⚠ BROWSERBASE_API_KEY not set — running local-only mode")

    # ── Local runs ──
    print(f"\n── Mode A: Local Playwright ({n} iterations) ──")
    local_results, local_timings_all = [], []

    async with async_playwright() as pw:
        async with ResourceSampler() as sampler:
            for i in range(n):
                r, timings = await run_local(pw, headless)
                r.worker_id = i
                local_results.append(r)
                if timings:
                    local_timings_all.append(timings)
                status = "✓" if r.success else "✗"
                print(f"  [{i+1:02d}/{n}] {status} {r.duration_s:.2f}s")

    local_durations = [r.duration_s for r in local_results]
    local_metrics = ExperimentMetrics.from_results(
        "browserbase_latency_local", local_results,
        resource_summary=sampler.summary(),
        extras={
            "step_timings": aggregate_step_timings(local_timings_all),
            "mode": "local_chromium",
        },
    )
    local_metrics.print_summary()

    # ── Browserbase runs ──
    bb_metrics = None
    bb_durations = []

    if not local_only:
        print(f"\n── Mode B: Browserbase CDP ({n} iterations) ──")
        bb_results, bb_timings_all = [], []

        async with async_playwright() as pw:
            async with ResourceSampler() as sampler_bb:
                for i in range(n):
                    r, timings = await run_browserbase(pw, headless)
                    r.worker_id = i
                    bb_results.append(r)
                    if timings:
                        bb_timings_all.append(timings)
                    status = "✓" if r.success else "✗"
                    print(f"  [{i+1:02d}/{n}] {status} {r.duration_s:.2f}s")

        bb_durations = [r.duration_s for r in bb_results]
        bb_metrics = ExperimentMetrics.from_results(
            "browserbase_latency_cdp", bb_results,
            resource_summary=sampler_bb.summary(),
            extras={
                "step_timings": aggregate_step_timings(bb_timings_all),
                "mode": "browserbase_cdp",
            },
        )
        bb_metrics.print_summary()

    # ── Comparison ──
    print(f"\n{'='*62}")
    print("  Latency Comparison: Local vs Browserbase")
    print(f"{'='*62}")

    la, lp95 = local_metrics.avg_runtime_sec, local_metrics.p95_runtime_sec
    print(f"  Local     — avg: {la:.2f}s   p95: {lp95:.2f}s   "
          f"success: {local_metrics.success_rate}%")

    if bb_metrics:
        ba, bp95 = bb_metrics.avg_runtime_sec, bb_metrics.p95_runtime_sec
        diff_avg = ((ba - la) / la) * 100
        diff_p95 = ((bp95 - lp95) / lp95) * 100
        print(f"  Browserbase — avg: {ba:.2f}s   p95: {bp95:.2f}s   "
              f"success: {bb_metrics.success_rate}%")
        print(f"\n  Avg overhead : {diff_avg:+.1f}%")
        print(f"  p95 overhead : {diff_p95:+.1f}%  "
              f"({'more consistent' if diff_p95 < diff_avg else 'less consistent'} tail)")

        # Per-step breakdown
        if local_timings_all and bb_timings_all:
            lt = aggregate_step_timings(local_timings_all)
            bt = aggregate_step_timings(bb_timings_all)
            steps = sorted(set(lt) | set(bt))
            if steps:
                print(f"\n  Step-level breakdown:")
                print(f"  {'Step':22}  {'Local avg':>10}  {'BB avg':>10}  {'Diff':>8}")
                print(f"  {'-'*22}  {'-'*10}  {'-'*10}  {'-'*8}")
                for s in steps:
                    lv = lt.get(s, {}).get("avg_s", 0)
                    bv = bt.get(s, {}).get("avg_s", 0)
                    diff = ((bv - lv) / lv * 100) if lv > 0 else 0
                    print(f"  {s:22}  {lv:>8.3f}s  {bv:>8.3f}s  {diff:>+7.1f}%")
    else:
        print("  Browserbase — skipped (no API key)")

    print(f"{'='*62}\n")

    # Save metrics
    local_metrics.save("/tmp/exp_local_latency.json")
    if bb_metrics:
        bb_metrics.save("/tmp/exp_bb_latency.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--headless", default="true")
    parser.add_argument("--local-only", action="store_true")
    asyncio.run(main(parser.parse_args()))
