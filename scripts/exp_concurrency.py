"""
Experiment 1: Concurrency Limits
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Launches N Playwright browsers in parallel, each completing the full
login → form fill → submit flow. Measures:

  - Peak RSS memory per worker
  - Time to complete per worker
  - First failure point (where browsers start crashing or timing out)

Run with increasing concurrency levels to find the ceiling.

Usage:
    python exp_concurrency.py            # runs 1,2,4,8,16 workers
    python exp_concurrency.py --n 8      # run exactly 8 workers once
    python exp_concurrency.py --n 4 --headless false  # watch browsers
"""
import argparse
import asyncio
import time

from playwright.async_api import async_playwright

from utils import RunResult, fill_form, get_process_memory_mb, login, print_summary


async def run_single_worker(
    worker_id: int,
    playwright,
    headless: bool = True,
) -> RunResult:
    """Spin up one browser, login, fill the form, return metrics."""
    mem_before = get_process_memory_mb()
    t_start = time.perf_counter()
    peak_mem = mem_before
    steps = 0

    try:
        browser = await playwright.chromium.launch(
            headless=headless,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-gpu",
                "--disable-accelerated-2d-canvas",
            ],
        )
        context = await browser.new_context()
        page = await context.new_page()

        # Dismiss any native dialogs automatically (alert/confirm)
        page.on("dialog", lambda d: asyncio.create_task(d.accept()))

        ok = await login(page)
        steps = 1 if ok else 0

        if ok:
            form_steps, _ = await fill_form(page)
            steps = 1 + form_steps  # login step + form steps

        peak_mem = get_process_memory_mb() - mem_before
        await browser.close()

        return RunResult(
            worker_id=worker_id,
            success=steps >= 6,
            duration_s=time.perf_counter() - t_start,
            peak_memory_mb=max(peak_mem, 0),
            steps_completed=steps,
        )

    except Exception as e:
        return RunResult(
            worker_id=worker_id,
            success=False,
            duration_s=time.perf_counter() - t_start,
            peak_memory_mb=max(get_process_memory_mb() - mem_before, 0),
            steps_completed=steps,
            error=str(e)[:120],
        )


async def run_concurrency_level(n: int, headless: bool) -> list[RunResult]:
    """Launch exactly N browsers in parallel and collect results."""
    print(f"\n▶ Running {n} concurrent worker(s)...")
    async with async_playwright() as pw:
        tasks = [run_single_worker(i, pw, headless) for i in range(n)]
        return await asyncio.gather(*tasks)


async def main(args):
    if args.n:
        levels = [args.n]
    else:
        levels = [1, 2, 4, 8, 16]

    headless = args.headless.lower() != "false"
    all_data: dict[int, list[RunResult]] = {}

    for n in levels:
        results = await run_concurrency_level(n, headless)
        all_data[n] = results
        print_summary(results, f"Concurrency = {n}")

        # Stop escalating if more than 50% of workers failed
        fail_rate = sum(1 for r in results if not r.success) / len(results)
        if fail_rate > 0.5:
            print(f"  ⚠ >50% failure rate at concurrency={n}. Stopping escalation.")
            break

    # Final comparison table
    if len(all_data) > 1:
        print("\nConcurrency Scaling Summary:")
        print(f"  {'N':>4}  {'success':>8}  {'avg_time':>10}  {'avg_mem':>10}  {'fail_rate':>10}")
        for n, results in all_data.items():
            ok = sum(1 for r in results if r.success)
            avg_t = sum(r.duration_s for r in results) / len(results)
            avg_m = sum(r.peak_memory_mb for r in results) / len(results)
            fail = (len(results) - ok) / len(results) * 100
            print(f"  {n:>4}  {ok:>4}/{len(results):<3}  {avg_t:>8.1f}s  {avg_m:>8.0f}MB  {fail:>8.0f}%")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=None, help="Fixed concurrency level (default: escalates 1,2,4,8,16)")
    parser.add_argument("--headless", default="true", help="Run headless? (default: true)")
    asyncio.run(main(parser.parse_args()))
