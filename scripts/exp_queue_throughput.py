"""
Experiment 6: Queue Throughput
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Simulates N jobs queued up and W concurrent workers draining the queue.
Mirrors the actual backend pattern: asyncio.Queue + worker coroutines,
analogous to Celery workers consuming from RabbitMQ.

Measures:
  - Jobs processed per minute (throughput)
  - Total time to drain queue
  - Per-worker utilisation
  - Failure rate under load

Usage:
    python exp_queue_throughput.py
    python exp_queue_throughput.py --jobs 500 --workers 4
    python exp_queue_throughput.py --jobs 200 --workers 8
"""
import argparse
import asyncio
import statistics
import time
from dataclasses import dataclass, field

from playwright.async_api import async_playwright

from utils import (
    CHROME_ARGS,
    ExperimentMetrics,
    ResourceSampler,
    fill_form,
    login,
)


@dataclass
class JobResult:
    job_id: int
    worker_id: int
    success: bool
    duration_s: float
    error: str | None = None


async def process_job(
    job_id: int,
    worker_id: int,
    pw,
    headless: bool,
) -> JobResult:
    """Process one job: spin up a browser, run the full workflow, close."""
    t = time.perf_counter()
    try:
        browser = await pw.chromium.launch(headless=headless, args=CHROME_ARGS)
        context = await browser.new_context()
        page = await context.new_page()
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))

        ok = await login(page)
        if ok:
            await fill_form(page)

        await browser.close()
        return JobResult(
            job_id=job_id, worker_id=worker_id,
            success=ok, duration_s=time.perf_counter() - t,
        )
    except Exception as e:
        return JobResult(
            job_id=job_id, worker_id=worker_id,
            success=False, duration_s=time.perf_counter() - t,
            error=str(e)[:100],
        )


async def worker(
    worker_id: int,
    queue: asyncio.Queue,
    results: list,
    pw,
    headless: bool,
    stats: dict,
):
    """Pull jobs from queue until empty."""
    jobs_done = 0
    busy_time = 0.0

    while True:
        try:
            job_id = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        t_job = time.perf_counter()
        result = await process_job(job_id, worker_id, pw, headless)
        busy_time += time.perf_counter() - t_job
        results.append(result)
        jobs_done += 1
        queue.task_done()

        if jobs_done % 10 == 0:
            print(f"  [w{worker_id:02d}] processed {jobs_done} jobs so far...")

    stats[worker_id] = {"jobs": jobs_done, "busy_s": busy_time}


async def run_queue_drain(n_jobs: int, n_workers: int, headless: bool) -> dict:
    """Enqueue n_jobs and drain with n_workers. Return throughput metrics."""
    queue: asyncio.Queue = asyncio.Queue()
    for i in range(n_jobs):
        await queue.put(i)

    results: list[JobResult] = []
    worker_stats: dict = {}

    t_start = time.perf_counter()

    async with async_playwright() as pw:
        async with ResourceSampler(interval_s=2.0) as sampler:
            worker_tasks = [
                asyncio.create_task(
                    worker(wid, queue, results, pw, headless, worker_stats)
                )
                for wid in range(n_workers)
            ]
            await asyncio.gather(*worker_tasks)

    elapsed = time.perf_counter() - t_start
    ok = [r for r in results if r.success]
    durations = [r.duration_s for r in results]

    throughput = len(results) / elapsed * 60  # jobs/minute

    # Worker utilisation
    utilisations = []
    for wid, s in worker_stats.items():
        util = (s["busy_s"] / elapsed) * 100 if elapsed > 0 else 0
        utilisations.append(util)

    return {
        "n_jobs": n_jobs,
        "n_workers": n_workers,
        "elapsed_s": round(elapsed, 1),
        "jobs_completed": len(results),
        "success_rate": round(len(ok) / len(results) * 100, 1) if results else 0,
        "throughput_jobs_per_min": round(throughput, 1),
        "avg_job_time_s": round(statistics.mean(durations), 2) if durations else 0,
        "p95_job_time_s": round(sorted(durations)[int(len(durations) * 0.95)], 2) if durations else 0,
        "worker_util_avg_pct": round(statistics.mean(utilisations), 1) if utilisations else 0,
        "worker_util_min_pct": round(min(utilisations), 1) if utilisations else 0,
        "resource": sampler.summary(),
    }


async def main(args):
    headless = args.headless.lower() != "false"

    # Run multiple configurations
    configs = []
    if args.jobs and args.workers:
        configs = [(args.jobs, args.workers)]
    else:
        configs = [
            (50, 2),
            (200, 4),
            (200, 8),
            (500, 4),
        ]

    all_metrics = []

    for n_jobs, n_workers in configs:
        print(f"\n▶ {n_jobs} jobs × {n_workers} workers...")
        result = await run_queue_drain(n_jobs, n_workers, headless)

        print(f"  Elapsed     : {result['elapsed_s']}s")
        print(f"  Throughput  : {result['throughput_jobs_per_min']} jobs/min")
        print(f"  Avg job time: {result['avg_job_time_s']}s")
        print(f"  Success rate: {result['success_rate']}%")
        print(f"  Worker util : {result['worker_util_avg_pct']}% avg")

        metrics = ExperimentMetrics(
            experiment="queue_throughput",
            concurrency=n_workers,
            total_runs=n_jobs,
            successful_runs=int(n_jobs * result["success_rate"] / 100),
            failed_runs=int(n_jobs * (1 - result["success_rate"] / 100)),
            success_rate=result["success_rate"],
            avg_runtime_sec=result["avg_job_time_s"],
            p95_runtime_sec=result["p95_job_time_s"],
            memory_avg_mb=result["resource"].get("memory_avg_mb", 0),
            memory_peak_mb=result["resource"].get("memory_peak_mb", 0),
            cpu_avg=result["resource"].get("cpu_avg", 0),
            cpu_peak=result["resource"].get("cpu_peak", 0),
            extras={
                "n_jobs": n_jobs,
                "elapsed_s": result["elapsed_s"],
                "throughput_jobs_per_min": result["throughput_jobs_per_min"],
                "worker_util_avg_pct": result["worker_util_avg_pct"],
            },
        )
        all_metrics.append(metrics)
        metrics.save(f"/tmp/exp_queue_{n_jobs}j_{n_workers}w.json")

    # Summary table
    print(f"\n{'='*70}")
    print("  Queue Throughput Summary")
    print(f"{'='*70}")
    print(f"  {'Jobs':>6}  {'Workers':>8}  {'Jobs/min':>10}  {'Elapsed':>9}  {'Util%':>6}  {'Success'}")
    print(f"  {'-'*6}  {'-'*8}  {'-'*10}  {'-'*9}  {'-'*6}  {'-'*7}")
    for m in all_metrics:
        e = m.extras
        print(f"  {e.get('n_jobs','?'):>6}  {m.concurrency:>8}  "
              f"{e.get('throughput_jobs_per_min','?'):>10}  "
              f"{e.get('elapsed_s','?'):>7}s  "
              f"{e.get('worker_util_avg_pct','?'):>5}%  "
              f"{m.success_rate}%")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=None)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--headless", default="true")
    asyncio.run(main(parser.parse_args()))
