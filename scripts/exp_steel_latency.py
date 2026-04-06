"""
Experiment: Steel Browser vs Local Playwright Latency
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Runs the full 7-step PortalX v2 workflow in both modes and compares per-step
latency to isolate WHERE the overhead lives (session creation, CDP connect,
navigation, form interaction, etc.).

Mode A — Local Playwright  : chromium.launch() inside the Docker container
Mode B — Steel Browser CDP : chromium.connect_over_cdp() to a Steel session

Steel runs a self-hosted Chrome instance exposed via a REST API + CDP bridge.
Each run creates a fresh session (POST /v1/sessions), connects over CDP, runs
the workflow, then releases the session (DELETE /v1/sessions/:id).

Recordings:
    All sessions are recorded (rrweb) and persisted to DuckDB when
    LOG_STORAGE_ENABLED=true is set on the Steel container. View past
    recordings in the Steel UI → Logs tab, or query via:
        GET http://localhost:3001/v1/logs/query?eventTypes=Recording

AI Recovery (optional):
    Set ANTHROPIC_API_KEY to enable Claude-based recovery when a step fails.
    Steel takes a screenshot of the failure state; Claude analyzes it and
    suggests the recovery action; the script retries with that guidance.

Environment variables:
    SITE_URL            Docker-internal URL for local mode. Default: http://portalx-site:8888
    PUBLIC_SITE_URL     Publicly reachable URL for Steel mode when Steel is not
                        on the same Docker network as the test site. If unset,
                        SITE_URL is used (correct when both share a Docker network).
    STEEL_API_URL       Steel REST API base URL. Default: http://localhost:3001
    ANTHROPIC_API_KEY   Enable Claude AI recovery on step failures (optional)

Usage:
    python exp_steel_latency.py
    python exp_steel_latency.py --iterations 20
    python exp_steel_latency.py --local-only
    python exp_steel_latency.py --steel-only --ai-recovery

Docker (all on same network — no PUBLIC_SITE_URL needed):
    docker exec \\
      -e STEEL_API_URL=http://steel-browser:3000 \\
      exp_runner python3 exp_steel_latency.py --iterations 10
"""
import argparse
import asyncio
import base64
import json
import os
import statistics
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse, urlunparse

from playwright.async_api import async_playwright

import exp_full_workflow as wf
from utils import CHROME_ARGS, ExperimentMetrics, ResourceSampler, RunResult

LOCAL_SITE = os.getenv("SITE_URL", "http://portalx-site:8888")
PUBLIC_SITE = os.getenv("PUBLIC_SITE_URL", "")   # only needed if Steel can't reach LOCAL_SITE
STEEL_API_URL = os.getenv("STEEL_API_URL", "http://localhost:3001").rstrip("/")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

STEPS = [
    "session_create",   # Steel only
    "cdp_connect",      # Steel only
    "login",
    "mfa",
    "terms",
    "npi",
    "patient_search",
    "prior_auth",
    "confirmation",
]


# ── Steel REST helpers ────────────────────────────────────────────────────────

def _http(method: str, url: str, body: dict | None = None) -> dict:
    """Minimal synchronous JSON HTTP call (no extra dependencies)."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


async def steel_create_session() -> dict:
    """POST /v1/sessions → session object."""
    return await asyncio.to_thread(_http, "POST", f"{STEEL_API_URL}/v1/sessions", {})


async def steel_release_session(session_id: str) -> None:
    """DELETE /v1/sessions/:id — best-effort, swallow errors."""
    try:
        await asyncio.to_thread(
            _http, "DELETE", f"{STEEL_API_URL}/v1/sessions/{session_id}", None
        )
    except Exception:
        pass


def _remap_ws_url(ws_url: str) -> str:
    """
    Steel returns websocketUrl with its internal bind address (e.g. ws://0.0.0.0:3000/).
    Replace the host:port with whatever STEEL_API_URL points to so the caller
    (inside or outside Docker) can actually reach it.
    """
    parsed_api = urlparse(STEEL_API_URL)
    parsed_ws = urlparse(ws_url)
    ws_scheme = "wss" if parsed_api.scheme == "https" else "ws"
    return urlunparse(parsed_ws._replace(scheme=ws_scheme, netloc=parsed_api.netloc))


# ── Recordings ───────────────────────────────────────────────────────────────

def recording_url(session_id: str) -> str:
    """
    URL to replay this session's recording in the Steel UI.
    Recordings are persisted when LOG_STORAGE_ENABLED=true on the Steel container.
    """
    return f"{STEEL_API_URL}/ui?sessionId={session_id}"


# ── AI recovery ───────────────────────────────────────────────────────────────

async def ai_recovery_hint(page, step_name: str, error: str) -> str | None:
    """
    On step failure: take a screenshot, send it to Claude with context about
    what step failed, and return Claude's suggested recovery action.
    Requires ANTHROPIC_API_KEY to be set; returns None otherwise.
    """
    if not ANTHROPIC_API_KEY:
        return None

    try:
        screenshot_bytes = await page.screenshot(type="png", full_page=False)
        screenshot_b64 = base64.standard_b64encode(screenshot_bytes).decode()

        body = json.dumps({
            "model": "claude-opus-4-6",
            "max_tokens": 256,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": screenshot_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            f"This is a screenshot of an RPA automation that failed at step '{step_name}'.\n"
                            f"Error: {error}\n\n"
                            "Look at the screenshot and describe in 1-2 sentences:\n"
                            "1. What is currently visible on the page?\n"
                            "2. What is the most likely cause of the failure?\n"
                            "3. What single recovery action should the bot try next?\n"
                            "Be specific about selectors or UI elements if visible."
                        ),
                    },
                ],
            }],
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read())
            return result["content"][0]["text"]

    except Exception as e:
        return f"[AI recovery unavailable: {e}]"


# ── Single run ────────────────────────────────────────────────────────────────

async def run_workflow(
    page, label: str, run_idx: int, total: int, ai_recovery: bool = False
) -> tuple[bool, dict]:
    """Execute all 7 steps. Returns (success, step_timings)."""
    timings = {}
    prefix = f"  [{run_idx:02d}/{total}] {label}"

    page.on("dialog", lambda d: asyncio.create_task(d.accept()))

    steps = [
        ("login",         "login_s",        wf.step_login),
        ("mfa",           "mfa_s",          wf.step_mfa),
        ("terms",         "terms_s",        wf.step_terms),
        ("npi",           "npi_s",          wf.step_npi),
        ("patient_search","patient_search_s",wf.step_patient_search),
        ("prior_auth",    "prior_auth_s",   wf.step_prior_auth_form),
    ]

    try:
        for step_name, key, fn in steps:
            elapsed = await fn(page)
            timings[key] = round(elapsed, 3)
            short = step_name[:10].ljust(10)
            print(f"{prefix}  {short}  {elapsed:6.3f}s")

            if step_name == "login" and "/concurrent" in page.url:
                print(f"{prefix}  ✗ concurrent session — skipping run")
                return False, timings

        elapsed, ref_id = await wf.step_confirmation(page)
        timings["confirmation_s"] = round(elapsed, 3)
        print(f"{prefix}  confirm     {elapsed:6.3f}s   ref={ref_id}")
        return True, timings

    except Exception as e:
        failed_step = next((s for s, k, _ in steps if k not in timings), "unknown")
        print(f"{prefix}  ✗ ERROR at {failed_step}: {e}")

        if ai_recovery and ANTHROPIC_API_KEY:
            print(f"{prefix}  🤖 asking Claude for recovery hint...")
            hint = await ai_recovery_hint(page, failed_step, str(e))
            if hint:
                print(f"{prefix}  💡 Claude says: {hint}")
                timings["ai_recovery_hint"] = hint

        return False, timings


async def run_local(pw, idx: int, total: int) -> tuple[RunResult, dict]:
    """One full workflow run using local Chromium."""
    wf.SITE = LOCAL_SITE
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


async def run_steel(pw, idx: int, total: int, ai_recovery: bool = False) -> tuple[RunResult, dict]:
    """One full workflow run using Steel Browser via CDP."""
    # Steel can reach the site at SITE_URL if on the same Docker network.
    # Set PUBLIC_SITE_URL only when Steel is running outside that network.
    steel_site = PUBLIC_SITE if PUBLIC_SITE else LOCAL_SITE
    wf.SITE = steel_site

    t_start = time.perf_counter()
    timings: dict = {}
    session_id: str | None = None

    try:
        # ── Session creation ─────────────────────────────────────────────────
        t_sess = time.perf_counter()
        session = await steel_create_session()
        session_id = session["id"]
        sess_s = round(time.perf_counter() - t_sess, 3)
        timings["session_create_s"] = sess_s
        print(f"  [{idx:02d}/{total}] STEEL  session_create  {sess_s:6.3f}s  id={session_id[:8]}…")
        print(f"  [{idx:02d}/{total}] STEEL  recording → {recording_url(session_id)}")

        # ── CDP connect ──────────────────────────────────────────────────────
        ws_url = _remap_ws_url(session["websocketUrl"])
        t_cdp = time.perf_counter()
        browser = await pw.chromium.connect_over_cdp(ws_url)
        cdp_s = round(time.perf_counter() - t_cdp, 3)
        timings["cdp_connect_s"] = cdp_s
        print(f"  [{idx:02d}/{total}] STEEL  cdp_connect     {cdp_s:6.3f}s")

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()

        ok, step_timings = await run_workflow(page, "STEEL ", idx, total, ai_recovery=ai_recovery)
        timings.update(step_timings)

        await browser.close()
        await steel_release_session(session_id)

        total_s = time.perf_counter() - t_start
        status = "✓" if ok else "✗"
        print(f"  [{idx:02d}/{total}] STEEL  ── total {total_s:.3f}s  {status}  replay → {recording_url(session_id)}\n")

        return RunResult(
            worker_id=idx, success=ok,
            duration_s=total_s, peak_memory_mb=0,
            steps_completed=len(step_timings),
        ), timings

    except Exception as e:
        if session_id:
            await steel_release_session(session_id)
        total_s = time.perf_counter() - t_start
        print(f"  [{idx:02d}/{total}] STEEL  ✗ FATAL: {e}\n")
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
        "n":   len(vals),
        "avg": round(statistics.mean(vals), 3),
        "min": round(min(vals), 3),
        "p50": round(pct(vals, 50), 3),
        "p95": round(pct(vals, 95), 3),
        "max": round(max(vals), 3),
    }


def print_step_table(label: str, all_timings: list[dict]):
    step_keys = [f"{k}_s" for k in STEPS]
    present = [k for k in step_keys if any(k in t for t in all_timings)]

    print(f"\n  {label} — per-step breakdown (seconds):")
    print(f"  {'Step':22}  {'n':>3}  {'avg':>7}  {'p50':>7}  {'p95':>7}  {'max':>7}")
    print(f"  {'-'*22}  {'-'*3}  {'-'*7}  {'-'*7}  {'-'*7}  {'-'*7}")
    for k in present:
        a = agg(all_timings, k)
        if not a:
            continue
        name = k.replace("_s", "")
        print(f"  {name:22}  {a['n']:>3}  {a['avg']:>7.3f}  {a['p50']:>7.3f}  {a['p95']:>7.3f}  {a['max']:>7.3f}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(args):
    n = args.iterations
    run_local_mode = not args.steel_only
    run_steel_mode = not args.local_only

    local_metrics = None
    steel_metrics = None
    local_timings_all: list[dict] = []
    steel_timings_all: list[dict] = []

    # ── Local runs ────────────────────────────────────────────────────────────
    if run_local_mode:
        print(f"\n{'='*62}")
        print(f"  Mode A: Local Playwright  ({n} iterations)")
        print(f"  Site: {LOCAL_SITE}")
        print(f"{'='*62}\n")

        local_results: list[RunResult] = []

        async with async_playwright() as pw:
            async with ResourceSampler() as sampler_local:
                for i in range(1, n + 1):
                    r, t = await run_local(pw, i, n)
                    local_results.append(r)
                    if t:
                        local_timings_all.append(t)

        local_metrics = ExperimentMetrics.from_results(
            "steel_latency_local", local_results,
            resource_summary=sampler_local.summary(),
            extras={"mode": "local_chromium", "site": LOCAL_SITE},
        )

        print(f"\n  Local summary:")
        print(f"    success rate : {local_metrics.success_rate}%")
        print(f"    avg total    : {local_metrics.avg_runtime_sec:.3f}s")
        print(f"    p95 total    : {local_metrics.p95_runtime_sec:.3f}s")
        print_step_table("Local", local_timings_all)
        local_metrics.save("/tmp/exp_steel_local_latency.json")

    # ── Steel runs ────────────────────────────────────────────────────────────
    if run_steel_mode:
        steel_site = PUBLIC_SITE if PUBLIC_SITE else LOCAL_SITE
        print(f"\n{'='*62}")
        print(f"  Mode B: Steel Browser CDP  ({n} iterations)")
        print(f"  Steel API: {STEEL_API_URL}")
        print(f"  Site: {steel_site}")
        print(f"{'='*62}\n")

        # Verify Steel is reachable before starting runs
        try:
            sessions = await asyncio.to_thread(_http, "GET", f"{STEEL_API_URL}/v1/sessions", None)
            print(f"  Steel is up — {len(sessions.get('sessions', []))} existing session(s)")
        except Exception as e:
            print(f"  ✗ Cannot reach Steel API at {STEEL_API_URL}: {e}")
            print(f"    Start Steel with: docker run -d -p 3001:3000 -e DOMAIN=localhost:3001 ghcr.io/steel-dev/steel-browser")
            print(f"    Or set STEEL_API_URL to the correct address.\n")
            return

        ai_recovery = getattr(args, "ai_recovery", False)
        if ai_recovery and not ANTHROPIC_API_KEY:
            print(f"  ⚠  --ai-recovery set but ANTHROPIC_API_KEY is not set — recovery disabled\n")
            ai_recovery = False
        elif ai_recovery:
            print(f"  🤖 AI recovery enabled (Claude will analyze failures)\n")
        else:
            print()

        steel_results: list[RunResult] = []

        async with async_playwright() as pw:
            async with ResourceSampler() as sampler_steel:
                for i in range(1, n + 1):
                    r, t = await run_steel(pw, i, n, ai_recovery=ai_recovery)
                    steel_results.append(r)
                    if t:
                        steel_timings_all.append(t)

        steel_metrics = ExperimentMetrics.from_results(
            "steel_latency_cdp", steel_results,
            resource_summary=sampler_steel.summary(),
            extras={"mode": "steel_cdp", "steel_api": STEEL_API_URL, "site": steel_site},
        )

        print(f"\n  Steel summary:")
        print(f"    success rate : {steel_metrics.success_rate}%")
        print(f"    avg total    : {steel_metrics.avg_runtime_sec:.3f}s")
        print(f"    p95 total    : {steel_metrics.p95_runtime_sec:.3f}s")
        print_step_table("Steel", steel_timings_all)
        steel_metrics.save("/tmp/exp_steel_cdp_latency.json")

    # ── Comparison ────────────────────────────────────────────────────────────
    if local_metrics and steel_metrics and local_timings_all and steel_timings_all:
        la, lp95 = local_metrics.avg_runtime_sec, local_metrics.p95_runtime_sec
        sa, sp95 = steel_metrics.avg_runtime_sec, steel_metrics.p95_runtime_sec

        print(f"\n{'='*62}")
        print(f"  Local vs Steel — comparison")
        print(f"{'='*62}")
        print(f"  {'':22}  {'Local':>10}  {'Steel':>12}  {'Δ overhead':>12}")
        print(f"  {'-'*22}  {'-'*10}  {'-'*12}  {'-'*12}")
        print(f"  {'Total avg':22}  {la:>9.3f}s  {sa:>11.3f}s  {((sa-la)/la*100):>+11.1f}%")
        print(f"  {'Total p95':22}  {lp95:>9.3f}s  {sp95:>11.3f}s  {((sp95-lp95)/lp95*100):>+11.1f}%")

        all_step_keys = sorted(set(k for t in local_timings_all + steel_timings_all for k in t))
        print(f"\n  Per-step overhead:")
        print(f"  {'Step':22}  {'Local avg':>10}  {'Steel avg':>10}  {'Δ':>10}")
        print(f"  {'-'*22}  {'-'*10}  {'-'*10}  {'-'*10}")
        for k in all_step_keys:
            la_step = agg(local_timings_all, k)
            sa_step = agg(steel_timings_all, k)
            lv = la_step.get("avg", 0)
            sv = sa_step.get("avg", 0)
            if sv == 0 and lv == 0:
                continue
            if lv > 0:
                diff = f"{(sv - lv) / lv * 100:+.1f}%"
            else:
                diff = "Steel only"
            name = k.replace("_s", "")
            print(f"  {name:22}  {lv:>9.3f}s  {sv:>9.3f}s  {diff:>10}")

        if steel_timings_all:
            sess_vals = [t["session_create_s"] for t in steel_timings_all if "session_create_s" in t]
            cdp_vals  = [t["cdp_connect_s"]    for t in steel_timings_all if "cdp_connect_s"    in t]
            if sess_vals:
                overhead = statistics.mean(sess_vals) + (statistics.mean(cdp_vals) if cdp_vals else 0)
                print(f"\n  Steel session startup overhead:")
                print(f"    session create : {statistics.mean(sess_vals):.3f}s avg  p95={pct(sess_vals, 95):.3f}s")
                if cdp_vals:
                    print(f"    cdp connect    : {statistics.mean(cdp_vals):.3f}s avg  p95={pct(cdp_vals, 95):.3f}s")
                print(f"    total startup  : {overhead:.3f}s avg")
                if sa > 0:
                    print(f"    (startup is {overhead/sa*100:.0f}% of total Steel workflow time)")

        print(f"\n  Results saved to /tmp/exp_steel_local_latency.json and /tmp/exp_steel_cdp_latency.json")

    print(f"{'='*62}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Steel Browser vs Local Playwright latency benchmark")
    parser.add_argument("--iterations", type=int, default=10, help="Number of workflow runs per mode")
    parser.add_argument("--local-only", action="store_true", help="Skip Steel runs")
    parser.add_argument("--steel-only", action="store_true", help="Skip local Playwright runs")
    parser.add_argument("--ai-recovery", action="store_true",
                        help="Enable Claude AI recovery analysis on step failures (requires ANTHROPIC_API_KEY)")
    asyncio.run(main(parser.parse_args()))
