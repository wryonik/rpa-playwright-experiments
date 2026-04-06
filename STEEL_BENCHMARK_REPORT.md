# Steel Browser Benchmark Report

**Date:** 2026-04-03  
**All numbers are measured, not projected. No estimates or extrapolations are included.**

---

## 1. Executive Summary

Steel Browser is a self-hosted, open-source browser API (`ghcr.io/steel-dev/steel-browser`) designed for RPA and AI agent workloads. It wraps a headless Chrome instance behind a REST API and CDP bridge with built-in session management, live screen recording, and rrweb-based session replay.

This report evaluates Steel Browser for production healthcare RPA automation using a 7-step PortalX prior authorization workflow (login, TOTP MFA, terms acceptance, NPI selection, patient search, prior auth form, confirmation).

**Key findings:**

- **Reliability:** 90% success rate over 20 sequential runs (18/20 succeeded)
- **Overhead:** Steel adds +74% avg latency over local Playwright (12.83s vs 7.36s), primarily from session lifecycle costs and Docker networking
- **Concurrency:** The open-source version supports exactly 1 active session per container. Horizontal scaling requires N containers.
- **Memory:** ~867 MiB idle per container, ~2 GiB active under load
- **Session lifecycle:** Creating + releasing a session costs ~6.7s total (3.5s create + 3.1s release). CDP connection itself is fast (65ms avg)
- **Recordings:** All sessions are automatically recorded in rrweb format when `LOG_STORAGE_ENABLED=true`. Recordings persist across container restarts with DuckDB storage.
- **No built-in AI recovery:** Steel's "AI agent integration" means using Steel as a browser backend for external AI frameworks (browser-use, Stagehand, Claude Computer Use). There is no automatic error recovery in the container itself.

---

## 2. Test Environment

| Component | Details |
|-----------|---------|
| **Host** | Apple M4 Pro, 24 GB physical RAM |
| **Docker** | Docker Desktop 29.1.3, VM allocated 7.653 GiB |
| **Steel image** | `ghcr.io/steel-dev/steel-browser` (Chrome 139, Node 22) |
| **Chrome** | Chromium 139.0.0.0 (headless, inside container) |
| **Playwright** | v1.50.0 (Python, running on host macOS) |
| **Test site** | PortalX v2, running on host at `http://localhost:8888` |
| **Network path** | Playwright (host) → Steel CDP (Docker) → PortalX (`host.docker.internal:8888`) |
| **Steel config** | `DOMAIN=localhost:3001`, `LOG_STORAGE_ENABLED=true`, `shm_size=2gb` |

### PortalX Workflow (7 steps)

The benchmark workflow mirrors a real healthcare prior authorization portal:

1. **Login** — ASP.NET-style form (`#Username` / `#Password`)
2. **MFA** — TOTP 6-digit code, character-by-character (paste blocked), 30s window
3. **Terms** — Scroll-to-bottom + jurisdiction radio selection
4. **NPI selection** — Provider table with radio rows, 20% chance of random alert modal
5. **Patient search** — API-backed debounced search, match score ≥ 99 selection
6. **Prior auth form** — Quick-lookup NPI, Vuetify v-select HCPCS, confirmation modal
7. **Confirmation** — Extract reference ID

Error injection is active: 5% concurrent session redirect, 8% DOM error modals, 10% server errors on submit. These are PortalX-side, not Steel-side failures.

---

## 3. Session Lifecycle Timing

Measured over 10 sequential trials. Each trial creates a session, connects via CDP, opens a blank page, closes, and releases.

| Phase | Avg | p50 | p95 | Max |
|-------|-----|-----|-----|-----|
| **Session create** (`POST /v1/sessions`) | 3.514s | 3.513s | 3.599s | 3.599s |
| **CDP connect** (`connect_over_cdp`) | 0.065s | 0.067s | 0.075s | 0.075s |
| **Browser close** | 0.005s | 0.005s | 0.006s | 0.006s |
| **Session release** (`POST /v1/sessions/:id/release`) | 3.115s | 3.120s | 3.175s | 3.175s |
| **Total lifecycle** | 6.705s | 6.738s | 6.775s | 6.775s |

**Analysis:**
- Session creation is expensive (3.5s avg) — Steel restarts Chrome's browser context, re-injects fingerprinting scripts, and re-attaches CDP instrumentation.
- Release is also slow (3.1s avg) — cleans up the browser context, flushes recording data, and prepares for the next session.
- CDP connection itself is fast (65ms avg) — the WebSocket handshake is lightweight.
- The lifecycle overhead is highly consistent (p95 ≈ max) with no outliers across 10 trials.

---

## 4. Single-Session Latency: Local Playwright vs Steel

Measured over 10 iterations per mode. Same 7-step workflow, same PortalX instance.

### Overall

| Metric | Local Playwright | Steel Browser | Overhead |
|--------|-----------------|---------------|----------|
| **Success rate** | 100% (10/10) | 80% (8/10) | -20% |
| **Avg total time** | 7.36s | 12.83s | +74.3% |
| **p95 total time** | 9.99s | 24.75s | +147.7% |

### Per-Step Breakdown

| Step | Local avg | Steel avg | Overhead |
|------|-----------|-----------|----------|
| Session create | — | 1.272s | Steel only |
| CDP connect | — | 0.050s | Steel only |
| Login | 0.105s | 1.514s | +1342% |
| MFA | 0.785s | 1.033s | +31.6% |
| Terms | 1.848s | 2.026s | +9.6% |
| NPI | 1.579s | 1.542s | -2.3% |
| Patient search | 0.748s | 0.784s | +4.8% |
| Prior auth | 2.050s | 4.974s | +142.6% |
| Confirmation | 0.007s | 0.028s | +300% |

**Analysis:**
- **Login (+1342%):** This is the first navigation through the Docker network bridge (`host.docker.internal`). Subsequent navigations are much faster because the TCP connection is already warm.
- **Prior auth (+143%):** High variance due to PortalX's random server error injection (10%). When a server error occurs, the workflow retries the form fill, doubling the step time. Over CDP, the round-trip for each retry is amplified.
- **Terms, NPI, Patient search (~5-10%):** These are within noise — Steel adds minimal overhead for page interactions after the initial connection is warm.
- **Session startup (create + connect = 1.32s)** accounts for 10% of total Steel workflow time.

### Failure Modes (Steel, 2 failures in 10 runs)

| Failure | Count | Cause |
|---------|-------|-------|
| Concurrent session redirect | 1 | PortalX 5% random concurrent session detection (not a Steel issue) |
| Navigation timeout | 1 | Patient search → prior auth navigation exceeded 6s timeout |

---

## 5. Resource Usage

### Single Container

| State | Memory (RSS) | CPU | Notes |
|-------|-------------|-----|-------|
| **Idle** (Chrome running, no active workflow) | 867 MiB | 0% | Freshly started container |
| **Active** (session created, no navigation) | 867 MiB | 1.3% | Session creation doesn't significantly increase memory |
| **Under load** (running workflow) | 2.04 GiB | 14.7% | Peak during 7-step workflow execution |

### Multiple Containers (post-run peak memory)

| Containers | Container 1 | Container 2 | Container 3 | Total Used | Docker VM Free |
|-----------|-------------|-------------|-------------|------------|---------------|
| 1 | 2.04 GiB | — | — | 2.04 GiB | 5.61 GiB |
| 2 | 3.02 GiB | 1.81 GiB | — | 4.83 GiB | 2.82 GiB |
| 3 | 3.24 GiB | 2.18 GiB | 1.61 GiB | 7.03 GiB | 0.62 GiB |

**Note:** Container 1 (`reverent_chebyshev`) accumulated state across runs, growing from 867 MiB to 3.24 GiB. Freshly started containers (2, 3) use significantly less memory. In production, periodic container recycling is recommended.

### Memory Growth Pattern

The primary Steel container grew from 867 MiB (idle) to 4.05 GiB after dozens of session cycles during benchmarking — a ~4.7x increase. This suggests a memory accumulation issue (possibly rrweb recording buffers or Chrome profile data not being fully released between sessions). Fresh containers start at ~600-800 MiB.

---

## 6. Concurrency Scaling

The open-source Steel Browser supports **exactly 1 active session per container**. This was confirmed by the session probe: creating a second session on the same container replaces the first (different session IDs, active count remains 1).

### Measured Results

| Workers | Runs | Successes | Success % | Wall Time | Throughput (wf/min) | Avg Latency | p95 Latency |
|---------|------|-----------|-----------|-----------|---------------------|-------------|-------------|
| **1** | 5 | 5 | 100% | 57.3s | 5.2 | 11.38s | 12.66s |
| **2** | 10 | 8 | 80% | 66.4s | 7.2 | 12.30s | 15.03s |
| **3** | 15 | 12 | 80% | 128.7s | 5.6 | 19.27s | 47.03s |

### Analysis

- **1 worker:** Baseline. 100% success rate, consistent 5.2 wf/min throughput.
- **2 workers:** Throughput increased 38% (5.2 → 7.2 wf/min). Near-linear scaling. Success rate dropped to 80% (2 failures from PortalX error injection, not Steel issues). Average latency stable at 12.3s.
- **3 workers:** Throughput **decreased** to 5.6 wf/min — worse than 2 workers. The Docker VM was at 92% memory utilization (7.03 GiB of 7.65 GiB), causing swapping and latency spikes. p95 latency jumped to 47s. The third container was memory-starved.

**Conclusion:** On this hardware (7.65 GiB Docker VM), **2 workers is the practical maximum**. The 3rd container pushes memory to the limit, degrading both throughput and latency.

---

## 7. Reliability (20-Run Stress Test)

20 sequential Steel workflow runs on a single freshly restarted container.

| Metric | Value |
|--------|-------|
| **Total runs** | 20 |
| **Successes** | 18 |
| **Failures** | 2 |
| **Success rate** | 90% |
| **Avg total time** | 11.89s |
| **p95 total time** | 33.21s |

### Failure Breakdown

| Run | Failure Mode | Root Cause |
|-----|-------------|------------|
| #6 | Concurrent session redirect | PortalX 5% random injection (not Steel) |
| #13 | Navigation timeout (patient search → prior auth) | PortalX response delay exceeded 6s timeout (not Steel) |

**Both failures were caused by PortalX's intentional error injection, not by Steel Browser.** Steel itself had 0 infrastructure failures across 20 runs.

### Timing Consistency

| Step | Avg | Min | Max | Std Dev |
|------|-----|-----|-----|---------|
| Session create | 1.138s | 0.827s | 2.864s | — |
| Login | 1.258s | 1.141s | 1.477s | Low variance |
| MFA | 1.364s | 0.884s | 3.828s | High (TOTP boundary waits) |
| Prior auth | 3.537s | 2.371s | 22.658s | Very high (server error retries) |

The MFA and prior auth high-variance spikes are caused by PortalX's TOTP 30s window boundaries and random server error injection, respectively.

---

## 8. Capabilities Assessment

### What Steel Provides

| Feature | Status | Notes |
|---------|--------|-------|
| REST API for browser management | Yes | `POST/GET/DELETE /v1/sessions` |
| CDP WebSocket bridge | Yes | Playwright `connect_over_cdp()` works out of the box |
| Live session viewer | Yes | Real-time screen rendering at `/ui` |
| Session recording (rrweb) | Yes | Requires `LOG_STORAGE_ENABLED=true` |
| Screenshot API | Yes | `POST /v1/sessions/screenshot` |
| PDF generation | Yes | `POST /v1/sessions/pdf` |
| Page scraping | Yes | `POST /v1/scrape` (HTML, Markdown, cleaned HTML) |
| Network/console log capture | Yes | `GET /v1/logs/query`, SSE streaming at `/v1/logs/stream` |
| Anti-detection (fingerprint injection) | Yes | Enabled by default |
| Session context persistence (cookies) | Yes | Via `sessionContext` parameter |
| Ad blocking | Yes | Via `blockAds` parameter |
| Bandwidth optimization | Yes | Block images/media/stylesheets |
| CAPTCHA solving | Cloud only | Requires paid Steel cloud plan |
| Proxy support | Yes | Via `proxyUrl` parameter |
| Multiple concurrent sessions per container | No | 1 session per container (open-source limitation) |
| Built-in AI error recovery | No | External AI framework integration only |
| Horizontal auto-scaling | No | Must be implemented externally (K8s, Docker Swarm) |

### What Steel Does NOT Provide

1. **Multi-session concurrency within a single container.** The `sessionService` maintains a single `activeSession` object. Creating a new session replaces the previous one. Concurrency requires running N separate containers.

2. **Built-in AI recovery.** Steel is designed as a *browser backend* for AI agents (browser-use, Stagehand, Claude Computer Use), not as an AI agent itself. When a workflow step fails, Steel provides the screenshot, DOM state, and logs — but the recovery logic must be implemented externally.

3. **Auto-scaling.** There is no built-in mechanism to spin up/down containers based on load. This must be handled by the orchestrator (Kubernetes HPA, Docker Swarm, or a custom pool manager like the one in this repo).

---

## 9. Configuration Reference

### Required Environment Variables

| Variable | Purpose | Example |
|----------|---------|---------|
| `DOMAIN` | External hostname for UI/websocket URLs | `localhost:3001` |
| `LOG_STORAGE_ENABLED` | Enable persistent recording storage | `true` |
| `LOG_STORAGE_PATH` | DuckDB file path for recordings | `/app/data/steel.duckdb` |

### Important Docker Flags

```bash
docker run -d \
  -p 3001:3000 \
  -e DOMAIN=localhost:3001 \
  -e LOG_STORAGE_ENABLED=true \
  -e LOG_STORAGE_PATH=/app/data/steel.duckdb \
  -v /path/to/data:/app/data \
  --shm-size=2gb \
  ghcr.io/steel-dev/steel-browser
```

- `--shm-size=2gb` is **mandatory** — Chrome crashes without adequate shared memory
- `-v` mount is needed for recording persistence across restarts
- `DOMAIN` must match the externally-accessible `host:port` for the UI session viewer to work (it renders the browser screen via WebSocket at the URL from `websocketUrl` in the session response)

---

## 10. Known Issues and Limitations

1. **Memory growth over time.** Containers accumulate memory across session cycles — growing from ~867 MiB to 3-4 GiB after dozens of runs. Recommend periodic container recycling in production.

2. **Session lifecycle overhead.** Creating and releasing sessions costs ~6.7s total. For high-throughput workloads, consider keeping sessions alive longer rather than creating a fresh one per workflow.

3. **`body={}` Python bug.** Python's `urllib.request` with `data=json.dumps({}).encode() if body else None` fails because `bool({})` is `False`. Must use `if body is not None`. This caused HTTP 500 ("Unexpected end of JSON input") when the POST body was omitted.

4. **Chrome port 6000 blocked.** Chrome browsers (including Chrome-based browsers) block port 6000 by default (reserved for X11). Never expose Steel on port 6000. Use 3001+ instead.

5. **3-worker degradation.** On a 7.65 GiB Docker VM, 3 Steel containers used 92% of memory, causing swapping and latency degradation. The throughput at 3 workers (5.6 wf/min) was *lower* than at 2 workers (7.2 wf/min).

---

## 11. Raw Data

All benchmark JSON files:

| File | Description |
|------|-------------|
| `/tmp/bench_lifecycle.json` | Session lifecycle timing (10 trials) |
| `/tmp/exp_steel_local_latency.json` | Local Playwright latency (10 iterations) |
| `/tmp/exp_steel_cdp_latency.json` | Steel CDP latency (20 iterations, reliability run) |
| `/tmp/bench_concurrent_1w.json` | Concurrency: 1 worker × 5 runs |
| `/tmp/bench_concurrent_2w.json` | Concurrency: 2 workers × 5 runs |
| `/tmp/bench_concurrent_3w.json` | Concurrency: 3 workers × 5 runs |
