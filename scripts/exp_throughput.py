"""
Experiment 8: End-to-End Throughput Benchmark
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Runs a fixed batch of workflows at different concurrency levels
to find the optimal throughput vs. stability curve.

Measures:
  - workflows/hour at each concurrency level
  - system saturation point
  - quality degradation threshold

Usage:
    python exp_throughput.py                      # sweep 2,4,6,8,10 workers
    python exp_throughput.py --workers 4 --batch 100
"""
import argparse
import asyncio
import statistics
import time

from playwright.async_api import async_playwright

from utils import CHROME_ARGS, ExperimentMetrics, ResourceSampler, fill_form, login


async def run_batch(n_workflows: int, n_workers: int, pw, headless: bool) -> dict:
    """
    Process exactly n_workflows total using n_workers concurrent browsers.
    """
    queue: asyncio.Queue = asyncio.Queue()
    for i in range(n_workflows):
        await queue.put(i)

    results = []
    lock = asyncio.Lock()

    async def worker():
        while True:
            try:
                job_id = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            t = time.perf_counter()
            success = False
            try:
                browser = await pw.chromium.launch(headless=headless, args=CHROME_ARGS)
                context = await browser.new_context()
                page = await context.new_page()
                page.on("dialog", lambda d: asyncio.create_task(d.accept()))
                ok = await login(page)
                if ok:
                    await fill_form(page)
                await browser.close()
                success = bool(ok)
            except Exception:
                pass
            finally:
                duration = time.perf_counter() - t
                async with lock:
                    results.append({"success": success, "duration_s": duration})
                queue.task_done()

    t_start = time.perf_counter()
    async with ResourceSampler(interval_s=2.0) as sampler:
        await asyncio.gather(*[worker() for _ in range(n_workers)])
    elapsed = time.perf_counter() - t_start

    durations = [r["duration_s"] for r in results]
    ok_count = sum(1 for r in results if r["success"])
    wf_per_hour = len(results) / elapsed * 3600

    return {
        "n_workflows": n_workflows,
        "n_workers": n_workers,
        "elapsed_s": round(elapsed, 1),
        "completed": len(results),
        "success_rate": round(ok_count / len(results) * 100, 1) if results else 0,
        "wf_per_hour": round(wf_per_hour, 0),
        "avg_s": round(statistics.mean(durations), 2) if durations else 0,
        "p95_s": round(sorted(durations)[int(len(durations) * 0.95)], 2) if durations else 0,
        "resource": sampler.summary(),
    }


async def main(args):
    headless = args.headless.lower() != "false"

    if args.workers and args.batch:
        configs = [(args.batch, args.workers)]
    else:
        # Sweep: fixed batch of 50 workflows at each concurrency level
        batch = args.batch or 50
        configs = [(batch, w) for w in [1, 2, 4, 6, 8, 10]]

    all_results = []
    print(f"\nThroughput Benchmark")
    print(f"{'─'*50}")

    async with async_playwright() as pw:
        for n_wf, n_w in configs:
            print(f"\n▶ {n_wf} workflows × {n_w} workers...")
            r = await run_batch(n_wf, n_w, pw, headless)
            all_results.append(r)
            print(f"  ✓ {r['wf_per_hour']:.0f} wf/hr  |  "
                  f"avg={r['avg_s']}s  p95={r['p95_s']}s  |  "
                  f"success={r['success_rate']}%  |  "
                  f"mem={r['resource'].get('memory_peak_mb',0):.0f}MB")

    # Find saturation point (first concurrency where wf/hr stops growing >5%)
    saturation_workers = None
    for i in range(1, len(all_results)):
        prev = all_results[i - 1]["wf_per_hour"]
        curr = all_results[i]["wf_per_hour"]
        growth = (curr - prev) / prev * 100 if prev > 0 else 100
        if growth < 5:
            saturation_workers = all_results[i - 1]["n_workers"]
            break

    print(f"\n{'='*62}")
    print("  Throughput vs Concurrency")
    print(f"{'='*62}")
    print(f"  {'Workers':>8}  {'wf/hr':>8}  {'Avg(s)':>7}  {'p95(s)':>7}  {'Success':>8}  {'MemPeak':>8}")
    print(f"  {'-'*8}  {'-'*8}  {'-'*7}  {'-'*7}  {'-'*8}  {'-'*8}")
    prev_wfhr = 0
    for r in all_results:
        wfhr = r["wf_per_hour"]
        gain = f" (+{wfhr-prev_wfhr:.0f})" if prev_wfhr else ""
        sat_marker = " ← SAT" if r["n_workers"] == saturation_workers else ""
        print(f"  {r['n_workers']:>8}  {wfhr:>6.0f}{gain[:5]:5}  "
              f"{r['avg_s']:>7}  {r['p95_s']:>7}  "
              f"{r['success_rate']:>7}%  "
              f"{r['resource'].get('memory_peak_mb',0):>6.0f}MB"
              f"{sat_marker}")
        prev_wfhr = wfhr

    if saturation_workers:
        print(f"\n  Saturation point: {saturation_workers} workers "
              f"(throughput gains <5% beyond this)")
    print(f"{'='*62}\n")

    # Save all metrics
    for r in all_results:
        m = ExperimentMetrics(
            experiment="throughput_benchmark",
            concurrency=r["n_workers"],
            total_runs=r["n_workflows"],
            successful_runs=int(r["n_workflows"] * r["success_rate"] / 100),
            failed_runs=int(r["n_workflows"] * (1 - r["success_rate"] / 100)),
            success_rate=r["success_rate"],
            avg_runtime_sec=r["avg_s"],
            p95_runtime_sec=r["p95_s"],
            memory_peak_mb=r["resource"].get("memory_peak_mb", 0),
            memory_avg_mb=r["resource"].get("memory_avg_mb", 0),
            cpu_avg=r["resource"].get("cpu_avg", 0),
            cpu_peak=r["resource"].get("cpu_peak", 0),
            extras={
                "wf_per_hour": r["wf_per_hour"],
                "elapsed_s": r["elapsed_s"],
                "saturation_point_workers": saturation_workers,
            },
        )
        m.save(f"/tmp/exp_throughput_{r['n_workers']}w.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--batch", type=int, default=None)
    parser.add_argument("--headless", default="true")
    asyncio.run(main(parser.parse_args()))
