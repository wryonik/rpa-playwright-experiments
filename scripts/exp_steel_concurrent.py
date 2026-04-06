"""
Experiment: Steel Browser Pool — Autoscaling Concurrent Sessions + Resource Usage
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The open-source Steel Browser supports ONE active session per container.
Concurrency = number of running containers.

This script manages the pool dynamically:
  • Discovers already-running Steel containers via Docker
  • Spawns new ones to reach the requested worker count
  • Tears down containers it started (leaves pre-existing ones running)
  • Reports per-instance resource usage and a production scale projection

Resource cost per instance (expected, matches local Playwright Chrome):
  Chrome : ~593 MB RSS
  Node   : ~100 MB RSS
  Total  : ~700 MB per slot

Scale formula:
  max_workers = floor((host_RAM_MB - 1000) / 700)
  8 GB  → 10 workers
  16 GB → 21 workers
  32 GB → 44 workers

Environment variables:
    SITE_URL        PortalX URL. Default: http://portalx-site:8888
    PUBLIC_SITE_URL Override for Steel containers on a different network
    STEEL_POOL      Comma-separated URLs of pre-existing instances to use
                    (skips Docker management if set)

Usage:
    # Auto-manage pool — spawns exactly as many containers as needed
    python exp_steel_concurrent.py --workers 5 --runs 3

    # Use pre-existing instances (no Docker management)
    python exp_steel_concurrent.py --workers 3 \\
        --pool http://localhost:3001,http://localhost:3002,http://localhost:3003

    # Just probe resource usage at a given scale, no workflow runs
    python exp_steel_concurrent.py --workers 4 --runs 0
"""
import argparse
import asyncio
import json
import os
import subprocess
import time
import urllib.request
from urllib.parse import urlparse, urlunparse

import psutil
from playwright.async_api import async_playwright

import exp_full_workflow as wf
from utils import ExperimentMetrics, ResourceSampler, RunResult

LOCAL_SITE  = os.getenv("SITE_URL", "http://portalx-site:8888")
PUBLIC_SITE = os.getenv("PUBLIC_SITE_URL", "")
STEEL_IMAGE = "ghcr.io/steel-dev/steel-browser"
BASE_PORT   = 3100   # dynamic containers start here


# ── Dynamic pool manager ──────────────────────────────────────────────────────

class SteelPool:
    """
    Manages a pool of Steel Browser containers.

    - discover()      finds already-running Steel containers
    - scale(n)        ensures exactly n instances are running
    - teardown()      stops only containers we started (leaves others alone)
    """

    def __init__(self):
        self._managed: list[dict] = []   # containers we started
        self._external: list[str] = []   # pre-existing URLs we did not start

    # ── Discovery ─────────────────────────────────────────────────────────────

    def discover(self) -> list[str]:
        """Return deduplicated API URLs of all running Steel containers via Docker."""
        try:
            out = subprocess.check_output(
                ["docker", "ps", "--format",
                 "{{.Names}}\t{{.Ports}}\t{{.Image}}"],
                text=True, timeout=10,
            )
        except Exception:
            return []

        seen_ports: set[str] = set()
        urls: list[str] = []
        for line in out.strip().splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            if STEEL_IMAGE not in parts[2]:
                continue
            # Ports string may have duplicate IPv4+IPv6 entries — dedupe by port number
            for segment in parts[1].split(","):
                segment = segment.strip()
                if "->3000/tcp" not in segment:
                    continue
                host_port = segment.split("->")[0].split(":")[-1]
                if host_port not in seen_ports:
                    seen_ports.add(host_port)
                    urls.append(f"http://localhost:{host_port}")
        return urls

    def _next_free_port(self, used_ports: set[int]) -> int:
        port = BASE_PORT
        while port in used_ports:
            port += 1
        return port

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _start_one(self, port: int) -> dict:
        """Spin up one Steel container on `port` and return its info."""
        name = f"steel_exp_{port}"
        # Use a host-side tmpdir per container so LOG_STORAGE_PATH exists at mount time.
        # Recordings from dynamically spawned containers are ephemeral (in /tmp).
        data_dir = f"/tmp/steel-data-{port}"
        os.makedirs(data_dir, exist_ok=True)
        subprocess.check_call([
            "docker", "run", "-d",
            "--name", name,
            "-p", f"{port}:3000",
            "-e", f"DOMAIN=localhost:{port}",
            "-e", "LOG_STORAGE_ENABLED=true",
            "-e", "LOG_STORAGE_PATH=/app/data/steel.duckdb",
            "-v", f"{data_dir}:/app/data",
            "--shm-size=2gb",
            STEEL_IMAGE,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"name": name, "url": f"http://localhost:{port}", "port": port}

    def _stop_one(self, name: str) -> None:
        subprocess.call(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    async def scale(self, target: int, external: list[str] | None = None) -> list[str]:
        """
        Ensure `target` Steel instances are available.
        If `external` URLs are given, use those as the pool directly.
        Otherwise auto-discover existing containers and spawn more as needed.
        Returns the list of API URLs to use.
        """
        if external:
            self._external = external[:target]
            return self._external

        existing = self.discover()
        print(f"  Discovered {len(existing)} running Steel instance(s)")

        pool = list(existing)
        used_ports = {urlparse(u).port for u in pool}

        # Start new containers to fill the gap
        to_start = max(0, target - len(pool))
        if to_start:
            print(f"  Starting {to_start} new container(s)…")
            for _ in range(to_start):
                port = self._next_free_port(used_ports)
                used_ports.add(port)
                info = self._start_one(port)
                self._managed.append(info)
                pool.append(info["url"])
                print(f"    → steel_exp_{port}  http://localhost:{port}/ui")

            # Wait for new containers to be ready
            await self._wait_healthy([c["url"] for c in self._managed])

        return pool[:target]

    async def _wait_healthy(self, urls: list[str], timeout_s: float = 30.0) -> None:
        deadline = time.perf_counter() + timeout_s
        pending  = list(urls)
        while pending and time.perf_counter() < deadline:
            await asyncio.sleep(2)
            still_pending = []
            for url in pending:
                try:
                    await asyncio.to_thread(
                        _http, "GET", f"{url}/v1/sessions", None
                    )
                except Exception:
                    still_pending.append(url)
            pending = still_pending
        if pending:
            print(f"  ⚠  {len(pending)} container(s) did not become healthy in time")

    def teardown(self) -> None:
        """Stop only the containers this pool manager started."""
        if not self._managed:
            return
        print(f"\n  Stopping {len(self._managed)} managed container(s)…")
        for info in self._managed:
            self._stop_one(info["name"])
            print(f"    stopped {info['name']}")
        self._managed.clear()


# ── Steel REST helpers ────────────────────────────────────────────────────────

def _http(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


async def steel_create(api: str) -> dict:
    return await asyncio.to_thread(_http, "POST", f"{api}/v1/sessions", {})


async def steel_release(api: str, session_id: str) -> None:
    try:
        await asyncio.to_thread(_http, "DELETE", f"{api}/v1/sessions/{session_id}", None)
    except Exception:
        pass


def remap_ws(ws_url: str, api_url: str) -> str:
    pa = urlparse(api_url)
    pw_ = urlparse(ws_url)
    scheme = "wss" if pa.scheme == "https" else "ws"
    return urlunparse(pw_._replace(scheme=scheme, netloc=pa.netloc))


# ── Concurrency limit probe ───────────────────────────────────────────────────

async def probe_single_session_limit(api: str) -> dict:
    s1 = await steel_create(api)
    s2 = await steel_create(api)
    resp = await asyncio.to_thread(_http, "GET", f"{api}/v1/sessions", None)
    active = sum(1 for s in resp.get("sessions", []) if s.get("status") in ("live", "idle"))
    await steel_release(api, s2["id"])
    return {
        "session_1_id": s1["id"][:8],
        "session_2_id": s2["id"][:8],
        "different_ids": s1["id"] != s2["id"],
        "active_reported": active,
        "verdict": "single-session per container confirmed" if active <= 1 else "WARNING: multiple sessions active",
    }


# ── Docker resource snapshot ──────────────────────────────────────────────────

def docker_stats() -> list[dict]:
    try:
        out = subprocess.check_output(
            ["docker", "stats", "--no-stream", "--format",
             "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}"],
            text=True, timeout=15,
        )
        rows = []
        for line in out.strip().splitlines():
            p = line.split("\t")
            if len(p) == 4:
                rows.append({"name": p[0], "cpu": p[1], "mem": p[2], "mem_pct": p[3]})
        return rows
    except Exception as e:
        return [{"error": str(e)}]


def print_docker_stats(label: str, pool_urls: list[str]) -> None:
    # Map port → container name so we can match by port even if name doesn't contain "steel"
    try:
        out = subprocess.check_output(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}\t{{.Image}}"],
            text=True, timeout=10,
        )
        steel_names = set()
        for line in out.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and STEEL_IMAGE in parts[2]:
                steel_names.add(parts[0])
    except Exception:
        steel_names = set()

    rows = docker_stats()
    relevant = [r for r in rows if "error" not in r and r.get("name", "") in steel_names]
    if not relevant:
        relevant = rows  # fallback: print all
    print(f"\n  {label}:")
    print(f"  {'Container':30}  {'CPU':8}  {'Memory':22}  {'Mem%':6}")
    print(f"  {'-'*30}  {'-'*8}  {'-'*22}  {'-'*6}")
    for r in relevant:
        if "error" in r:
            print(f"  docker stats error: {r['error']}")
        else:
            print(f"  {r['name']:30}  {r['cpu']:8}  {r['mem']:22}  {r['mem_pct']:6}")


# ── Pool worker ───────────────────────────────────────────────────────────────

async def pool_worker(
    worker_id: int,
    api_url: str,
    n_runs: int,
    site: str,
    pw,
    results: list,
) -> None:
    wf.SITE = site
    port = urlparse(api_url).port

    for run_i in range(n_runs):
        t_start = time.perf_counter()
        session_id = None

        try:
            session = await steel_create(api_url)
            session_id = session["id"]
            browser = await pw.chromium.connect_over_cdp(remap_ws(session["websocketUrl"], api_url))
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await context.new_page()
            page.on("dialog", lambda d: asyncio.create_task(d.accept()))

            steps_done = 0
            try:
                await wf.step_login(page);           steps_done += 1
                if "/concurrent" not in page.url:
                    await wf.step_mfa(page);         steps_done += 1
                    await wf.step_terms(page);       steps_done += 1
                    await wf.step_npi(page);         steps_done += 1
                    await wf.step_patient_search(page); steps_done += 1
                    await wf.step_prior_auth_form(page); steps_done += 1
                    _, ref = await wf.step_confirmation(page); steps_done += 1
                ok = steps_done == 7
            except Exception:
                ok = False

            await browser.close()
            await steel_release(api_url, session_id)

            duration = time.perf_counter() - t_start
            print(f"  [w{worker_id:02d} r{run_i+1}] {'✓' if ok else '✗'} "
                  f"{duration:.2f}s  :{port}  steps={steps_done}/7")
            results.append(RunResult(
                worker_id=worker_id, success=ok, duration_s=duration,
                peak_memory_mb=0, steps_completed=steps_done, total_steps=7,
            ))

        except Exception as e:
            if session_id:
                await steel_release(api_url, session_id)
            duration = time.perf_counter() - t_start
            print(f"  [w{worker_id:02d} r{run_i+1}] ✗ FATAL {e}")
            results.append(RunResult(
                worker_id=worker_id, success=False, duration_s=duration,
                peak_memory_mb=0, steps_completed=0, error=str(e)[:100],
            ))


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(args):
    n_workers = args.workers
    n_runs    = args.runs
    site      = PUBLIC_SITE if PUBLIC_SITE else LOCAL_SITE
    external  = [u.strip() for u in args.pool.split(",")] if args.pool else None

    pool_mgr = SteelPool()

    print(f"\n{'='*64}")
    print(f"  Steel Browser Pool — Autoscaling Benchmark")
    print(f"{'='*64}")
    print(f"  Target workers : {n_workers}")
    print(f"  Runs/worker    : {n_runs}  (total: {n_workers * n_runs})")
    print(f"  Site           : {site}")
    if external:
        print(f"  Mode           : pre-existing pool (no Docker management)")
    else:
        print(f"  Mode           : auto-manage (discover + spawn as needed)")

    pool = []
    try:
        # ── Scale pool ────────────────────────────────────────────────────────
        print(f"\n  {'─'*60}")
        print(f"  Scaling pool to {n_workers} instance(s)")
        print(f"  {'─'*60}")
        pool = await pool_mgr.scale(n_workers, external=external)
        print(f"  Pool ready: {len(pool)} instance(s)")
        for i, url in enumerate(pool):
            print(f"    [{i+1}] {url}  (UI: {url}/ui)")

        if not pool:
            print("  No instances available. Aborting.")
            return

        actual_workers = len(pool)

        # ── Probe single-session limit ────────────────────────────────────────
        print(f"\n  {'─'*60}")
        print(f"  Single-session limit probe (on first instance)")
        print(f"  {'─'*60}")
        probe = await probe_single_session_limit(pool[0])
        print(f"  Session 1 id : {probe['session_1_id']}…")
        print(f"  Session 2 id : {probe['session_2_id']}… (different: {probe['different_ids']})")
        print(f"  Active count : {probe['active_reported']}")
        print(f"  → {probe['verdict']}")

        # ── Idle resource baseline ────────────────────────────────────────────
        print_docker_stats("Idle resource usage (before runs)", pool)

        if n_runs == 0:
            print(f"\n  --runs 0: skipping workflow execution.\n")
            return

        # ── Concurrent run ────────────────────────────────────────────────────
        print(f"\n  {'─'*60}")
        print(f"  Running {actual_workers} parallel workers × {n_runs} runs")
        print(f"  {'─'*60}\n")

        results: list[RunResult] = []
        t_wall = time.perf_counter()

        async with async_playwright() as pw:
            async with ResourceSampler() as sampler:
                await asyncio.gather(*[
                    pool_worker(wid, pool[wid], n_runs, site, pw, results)
                    for wid in range(actual_workers)
                ])

        wall_s = time.perf_counter() - t_wall

        # ── Peak resource usage ───────────────────────────────────────────────
        print_docker_stats("Peak resource usage (after runs)", pool)

        # ── Results ───────────────────────────────────────────────────────────
        ok    = [r for r in results if r.success]
        tput  = len(ok) / wall_s * 60  # workflows/min

        metrics = ExperimentMetrics.from_results(
            "steel_concurrent", results,
            concurrency=actual_workers,
            resource_summary=sampler.summary(),
            extras={
                "pool_size":            actual_workers,
                "wall_time_s":          round(wall_s, 2),
                "throughput_wf_min":    round(tput, 1),
                "single_session_limit": probe["verdict"],
            },
        )

        print(f"\n{'='*64}")
        print(f"  Benchmark results")
        print(f"{'='*64}")
        print(f"  Workers      : {actual_workers}  (1 session per container)")
        print(f"  Total runs   : {len(results)}  ✓{len(ok)}  ✗{len(results)-len(ok)}")
        print(f"  Success rate : {metrics.success_rate}%")
        print(f"  Wall time    : {wall_s:.1f}s")
        print(f"  Throughput   : {tput:.1f} workflows/min")
        print(f"  Avg latency  : {metrics.avg_runtime_sec:.2f}s")
        print(f"  p95 latency  : {metrics.p95_runtime_sec:.2f}s")

        # Scale projection based on measured throughput
        tput_per_worker = tput / actual_workers if actual_workers else 0
        print(f"\n  Production scale projection  (~700 MB RAM per Steel instance):")
        print(f"  {'RAM':>6}  {'Instances':>10}  {'Workflows/hr':>14}  {'Workflows/day':>14}")
        print(f"  {'-'*6}  {'-'*10}  {'-'*14}  {'-'*14}")
        for ram_gb in [8, 16, 32, 64, 128]:
            max_w = max(1, (ram_gb * 1024 - 1000) // 700)
            wf_hr  = max_w * tput_per_worker * 60
            wf_day = wf_hr * 24
            print(f"  {ram_gb:>4}GB  {max_w:>10}  {wf_hr:>14,.0f}  {wf_day:>14,.0f}")

        metrics.save("/tmp/exp_steel_concurrent.json")
        print(f"{'='*64}\n")

    finally:
        pool_mgr.teardown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Steel Browser autoscaling pool benchmark"
    )
    parser.add_argument("--workers", type=int, default=3,
                        help="Number of concurrent Steel instances to use")
    parser.add_argument("--runs", type=int, default=3,
                        help="Workflows per worker (0 = resource probe only)")
    parser.add_argument("--pool", type=str, default=None,
                        help="Comma-separated Steel API URLs — skips Docker management")
    asyncio.run(main(parser.parse_args()))
