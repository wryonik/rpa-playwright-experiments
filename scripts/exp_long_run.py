"""
Experiment 7: Long-Run Stability
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Runs a fixed concurrency level for an extended period (default 60 minutes).
Looks for:
  - Memory growth over time (leak detection)
  - Browser crash rate
  - Failure rate drift (does it get worse over time?)
  - Stuck workflows (jobs that never complete)

This is the most important experiment for production readiness.

Usage:
    python exp_long_run.py                     # 60 min, 4 workers
    python exp_long_run.py --duration 30 --workers 6
    python exp_long_run.py --duration 10 --workers 4   # quick smoke test
"""
import argparse
import asyncio
import statistics
import time
from collections import deque
from dataclasses import dataclass, field

import psutil
from playwright.async_api import async_playwright

from utils import CHROME_ARGS, ExperimentMetrics, fill_form, login


WORKFLOW_TIMEOUT_S = 60  # Kill a workflow if it runs over this


@dataclass
class WorkflowResult:
    run_number: int
    worker_id: int
    success: bool
    duration_s: float
    timestamp: float = field(default_factory=time.time)
    error: str | None = None


async def run_workflow_with_timeout(worker_id: int, run_number: int, pw, headless: bool) -> WorkflowResult:
    """Run one workflow with a hard timeout."""
    t = time.perf_counter()
    try:
        async def _run():
            browser = await pw.chromium.launch(headless=headless, args=CHROME_ARGS)
            context = await browser.new_context()
            page = await context.new_page()
            page.on("dialog", lambda d: asyncio.create_task(d.accept()))
            ok = await login(page)
            if ok:
                await fill_form(page)
            await browser.close()
            return ok

        ok = await asyncio.wait_for(_run(), timeout=WORKFLOW_TIMEOUT_S)
        return WorkflowResult(
            run_number=run_number, worker_id=worker_id,
            success=bool(ok), duration_s=time.perf_counter() - t,
        )
    except asyncio.TimeoutError:
        return WorkflowResult(
            run_number=run_number, worker_id=worker_id,
            success=False, duration_s=time.perf_counter() - t,
            error=f"timeout>{WORKFLOW_TIMEOUT_S}s",
        )
    except Exception as e:
        return WorkflowResult(
            run_number=run_number, worker_id=worker_id,
            success=False, duration_s=time.perf_counter() - t,
            error=str(e)[:80],
        )


async def worker_loop(
    worker_id: int,
    run_counter: list,  # shared mutable counter
    results: list,
    stop_event: asyncio.Event,
    pw,
    headless: bool,
):
    """Continuously process workflows until stop_event is set."""
    while not stop_event.is_set():
        run_number = len(run_counter)
        run_counter.append(1)
        result = await run_workflow_with_timeout(worker_id, run_number, pw, headless)
        results.append(result)


async def memory_monitor(
    snapshots: list,
    stop_event: asyncio.Event,
    interval_s: float = 15.0,
):
    """Record memory snapshots every interval_s seconds."""
    proc = psutil.Process()
    t_start = time.time()
    while not stop_event.is_set():
        mem = proc.memory_info().rss / 1024 / 1024
        cpu = proc.cpu_percent(interval=None)
        snapshots.append({
            "elapsed_s": round(time.time() - t_start),
            "memory_mb": round(mem, 1),
            "cpu_pct": round(cpu, 1),
        })
        await asyncio.sleep(interval_s)


def failure_rate_over_time(results: list[WorkflowResult], window: int = 20) -> list[dict]:
    """Compute rolling failure rate in windows of N workflows."""
    out = []
    for i in range(0, len(results), window):
        chunk = results[i:i + window]
        if not chunk:
            break
        fail_rate = sum(1 for r in chunk if not r.success) / len(chunk) * 100
        avg_time = statistics.mean(r.duration_s for r in chunk)
        out.append({
            "window_start": i,
            "window_end": i + len(chunk),
            "failure_rate_pct": round(fail_rate, 1),
            "avg_duration_s": round(avg_time, 2),
        })
    return out


async def main(args):
    headless = args.headless.lower() != "false"
    duration_min = args.duration
    n_workers = args.workers

    print(f"\nLong-Run Stability Test")
    print(f"Duration : {duration_min} minutes")
    print(f"Workers  : {n_workers}")
    print(f"Timeout  : {WORKFLOW_TIMEOUT_S}s per workflow\n")

    results: list[WorkflowResult] = []
    run_counter: list = []
    mem_snapshots: list = []
    stop_event = asyncio.Event()

    t_start = time.perf_counter()

    async with async_playwright() as pw:
        # Start memory monitor
        monitor_task = asyncio.create_task(
            memory_monitor(mem_snapshots, stop_event, interval_s=30.0)
        )

        # Start workers
        worker_tasks = [
            asyncio.create_task(
                worker_loop(wid, run_counter, results, stop_event, pw, headless)
            )
            for wid in range(n_workers)
        ]

        # Run for duration_min
        print_interval = 60  # print progress every 60s
        next_print = print_interval

        while time.perf_counter() - t_start < duration_min * 60:
            await asyncio.sleep(5)
            elapsed = time.perf_counter() - t_start
            if elapsed >= next_print:
                done = len(results)
                ok = sum(1 for r in results if r.success)
                fail_rate = (done - ok) / done * 100 if done else 0
                mem = mem_snapshots[-1]["memory_mb"] if mem_snapshots else 0
                print(f"  [{elapsed/60:.1f}min] workflows={done}  "
                      f"failure_rate={fail_rate:.1f}%  mem={mem:.0f}MB")
                next_print += print_interval

        # Stop workers gracefully
        stop_event.set()
        for t in worker_tasks:
            t.cancel()
        monitor_task.cancel()
        try:
            await asyncio.gather(*worker_tasks, return_exceptions=True)
        except Exception:
            pass

    elapsed_total = time.perf_counter() - t_start

    # ── Analysis ──
    ok_results = [r for r in results if r.success]
    fail_results = [r for r in results if not r.success]
    durations = [r.duration_s for r in results]
    windows = failure_rate_over_time(results, window=max(1, len(results) // 10))

    # Memory growth
    mem_values = [s["memory_mb"] for s in mem_snapshots]
    mem_start = mem_values[0] if mem_values else 0
    mem_end = mem_values[-1] if mem_values else 0
    mem_growth = mem_end - mem_start

    # Crash detection (workflows that timed out)
    timeouts = [r for r in fail_results if r.error and "timeout" in r.error]
    crashes = [r for r in fail_results if r.error and "timeout" not in r.error]

    print(f"\n{'='*62}")
    print("  Long-Run Stability Results")
    print(f"{'='*62}")
    print(f"  Duration        : {elapsed_total/60:.1f} min")
    print(f"  Total workflows : {len(results)}")
    print(f"  Successful      : {len(ok_results)} ({len(ok_results)/len(results)*100:.1f}%)")
    print(f"  Failed          : {len(fail_results)}")
    print(f"    Timeouts      : {len(timeouts)}")
    print(f"    Crashes/errors: {len(crashes)}")
    print(f"  Avg workflow    : {statistics.mean(durations):.2f}s" if durations else "")
    print(f"  p95 workflow    : {sorted(durations)[int(len(durations)*0.95)]:.2f}s" if durations else "")
    print(f"\n  Memory (MB):")
    print(f"    Start         : {mem_start:.0f}")
    print(f"    End           : {mem_end:.0f}")
    print(f"    Growth        : +{mem_growth:.0f} MB over {elapsed_total/60:.0f} min")
    print(f"    Growth/hr     : +{mem_growth/(elapsed_total/3600):.0f} MB/hr")
    if mem_values:
        print(f"    Peak          : {max(mem_values):.0f}")

    print(f"\n  Failure rate over time (each window = ~{len(results)//max(len(windows),1)} workflows):")
    for w in windows:
        bar = "█" * int(w["failure_rate_pct"] / 5)
        print(f"    jobs {w['window_start']:>4}–{w['window_end']:<4}  "
              f"{w['failure_rate_pct']:>5.1f}%  {bar}")

    drift = "STABLE" if len(windows) < 2 else (
        "⚠ DRIFTING UP" if windows[-1]["failure_rate_pct"] > windows[0]["failure_rate_pct"] + 5
        else "✓ STABLE"
    )
    print(f"\n  Failure drift: {drift}")
    print(f"{'='*62}\n")

    metrics = ExperimentMetrics(
        experiment="long_run_stability",
        concurrency=n_workers,
        total_runs=len(results),
        successful_runs=len(ok_results),
        failed_runs=len(fail_results),
        success_rate=round(len(ok_results)/len(results)*100, 1) if results else 0,
        avg_runtime_sec=round(statistics.mean(durations), 2) if durations else 0,
        p95_runtime_sec=round(sorted(durations)[int(len(durations)*0.95)], 2) if durations else 0,
        memory_avg_mb=round(statistics.mean(mem_values), 1) if mem_values else 0,
        memory_peak_mb=round(max(mem_values), 1) if mem_values else 0,
        extras={
            "duration_min": round(elapsed_total / 60, 1),
            "memory_growth_mb": round(mem_growth, 1),
            "memory_growth_mb_per_hr": round(mem_growth / (elapsed_total / 3600), 1) if elapsed_total > 0 else 0,
            "timeouts": len(timeouts),
            "crashes": len(crashes),
            "failure_drift": drift,
            "failure_windows": windows,
            "memory_snapshots": mem_snapshots,
        },
    )
    metrics.save("/tmp/exp_long_run.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=60, help="Test duration in minutes (default: 60)")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent workers (default: 4)")
    parser.add_argument("--headless", default="true")
    asyncio.run(main(parser.parse_args()))
