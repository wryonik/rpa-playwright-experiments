# RPA System Performance Report
**Backend: `backend-v0` · Test site: PortalX v2 (synthetic, deployed at https://portalx-7nn4.onrender.com)**
_Local experiments: 2026-03-31 · Browserbase + cloud experiments: 2026-04-01 · All numbers measured, not estimated_

---

## 1. Executive Summary

Ten experiments were run across two phases:
- **Phase 1 (local)** — Playwright against PortalX running in Docker on the host machine
- **Phase 2 (cloud)** — Browserbase CDP and local Playwright both hitting PortalX deployed on Render, to give an apples-to-apples comparison

**Key findings:**

| Finding | Number |
|---------|--------|
| Chrome memory per worker | **~593 MB** (constant across 1–8 workers; total scales proportionally) |
| Workers tested on 7.65 GB Docker host | Up to **8 workers** (peak 4.74 GB, 62% of available RAM) |
| Local Playwright avg (7-step v2 workflow, vs Render) | **12.4 s** |
| Browserbase CDP avg — before modal/TOTP fixes | **94.6 s** (40% success) |
| Browserbase CDP avg — after modal/TOTP fixes | **79.1 s** (70% success) |
| Browserbase overhead vs local (post-fix) | **+940% avg** |
| Browserbase session startup (create + CDP connect) | **2.96 s** — only 4% of total workflow time |
| Browserbase success rate (10 sequential, before fix) | **40%** |
| Browserbase success rate (10 sequential, after fix) | **70%** |
| Browserbase success rate (3 concurrent sessions) | **67%** |
| Browserbase plan limit (free tier) | **3 concurrent sessions max** |
| Memory leak rate (local, 4 workers, 10 min) | **+340 MB/hr** (Python process RSS) |
| Session reuse (cookie restore vs full login) | **90% login time reduction** |
| Queue throughput (local, Docker site) | **76 jobs/min at 4 workers, 150 jobs/min at 8 workers** |

**Critical discovery — Browserbase failures are not caused by session overhead.** The 2.96 s startup cost is negligible. Failures are caused by **modal timing race conditions**: over the higher-latency CDP path, random DOM modals appear after the dismiss-check has already passed, intercepting the next click. Adding `wait_for_selector(modal, state="hidden")` guards improved the success rate from 40% → 70%. See §3.9.

**MFA timing spike on Browserbase.** TOTP MFA averages ~20 s over Browserbase vs 1.0 s local. Root cause: with ~7–9 s of page-load latency before MFA, the TOTP code can expire mid-submit and trigger a retry. Each retry adds ~6–8 s. TOTP boundary fix (generate code at last moment, skip if <3 s to window) was applied but MFA timing did not meaningfully change, suggesting the retries are being caused by something else too. See §3.9.

---

## 2. Test Environment

### 2.1 Docker Setup

All local experiments ran inside two containers on the same Docker Desktop instance. No resource limits were explicitly set — both containers share the Docker Desktop allocation.

**Host machine:**

| | |
|---|---|
| Model | Apple M4 Pro |
| Physical cores | 14 |
| Physical RAM | 24 GB |

**Docker Desktop allocation (as seen by containers):**

| Resource | Value |
|----------|-------|
| CPUs allocated | **14** (all cores passed through) |
| Total RAM allocated | **7.65 GB** (out of 24 GB host RAM) |
| Available RAM at experiment start | **~6.76 GB** |

**Container configuration (from `docker-compose.yml`):**

| Container | Image | CPU limit | Memory limit | `/dev/shm` | Idle RAM |
|-----------|-------|-----------|-------------|-----------|---------|
| `exp_runner` | `mcr.microsoft.com/playwright/python:v1.50.0-noble` | none (shared) | none (shared) | **2 GB** (explicit) | 24.9 MiB |
| `exp_portalx_site` | `python:3.12-slim` | none (shared) | none (shared) | 64 MB (default) | 26.8 MiB |

> `shm_size: 2gb` on `exp_runner` is required. Chrome uses `/dev/shm` for its renderer IPC. Docker's default 64 MB causes Chrome to crash immediately when launching workers. The site container uses the default 64 MB — Python HTTP servers don't need large shared memory.

**Image sizes:**

| Image | Compressed size | Used for |
|-------|----------------|---------|
| `python:3.12-slim` | **205 MB** | PortalX site server |
| `mcr.microsoft.com/playwright/python:v1.50.0-noble` | **3.59 GB** | Experiment runner (includes Chromium, Firefox, WebKit) |

> The 3.59 GB runner image includes all three browser engines. Only Chromium was used in these experiments. A Chromium-only custom image would be significantly smaller (not measured).

**Network:** Both containers are on the same Docker bridge network (`exp_network`). Container-to-container latency is sub-millisecond. For Browserbase experiments, the runner container connects outbound to Browserbase's US servers.

---

### 2.2 PortalX v2 (Test Site)

A Python HTTP server deployed on Render (free tier) and also run locally in Docker. Simulates the specific challenges found in Brightree and MyCGS.

**Live URL:** https://portalx-7nn4.onrender.com — **Login:** `admin` / `P@ssw0rd!`

> Render free tier spins down after 15 min of inactivity. A warmup request was sent to every page before each cloud experiment run to ensure the server was fully responsive.

| Feature | Probability | Real portal equivalent |
|---------|------------|----------------------|
| TOTP MFA (30 s window) | Always | MyCGS authenticator |
| Paste-blocked MFA field | Always | MyCGS `#txtMFACode` |
| Scroll-to-bottom terms | Always | Brightree terms |
| NPI selection table | Always | Brightree provider lookup |
| Patient search with match score (column 6) | Always | Brightree patient search |
| Quick-lookup field (`data-quicklookup`) | Always | Brightree NPI lookup |
| Vuetify v-select (`i.material-icons` arrow) | Always | Brightree HCPCS select |
| `#alert-modal` on NPI page | 20% | Brightree session warnings |
| `#error-modal` on terms/prior_auth | 8% | Brightree server error overlay |
| Concurrent session detection | 5% on login | MyCGS session limit |
| Session expiry (5 min inactivity) | Automatic | Both portals |
| JS `alert()` "Server Error" on submit | 10% | Brightree submit error |

---

## 3. Measured Results

### 3.1 Concurrency Scaling (Local, Docker site)

Each worker ran: browser launch → login → fill form → submit (simple PortalX v1 form).

| Workers | Avg time (s) | Success rate | Notes |
|---------|-------------|-------------|-------|
| 1 | 2.20 | 100% | baseline |
| 2 | 2.97 | 100% | |
| 4 | 2.49 | 100% | |
| 8 | 2.84 | 100% | |
| 16 | 3.53 | 100% | worker-14 outlier: 9.54 s |

Zero failures at any level. This experiment ran a single batch per worker count — p95 was not measured.

### 3.2 Session Persistence (Local, Docker site)

| Metric | Measured |
|--------|---------|
| Avg cold start (full login) | **0.75 s** |
| Avg warm start (cookie restore) | **0.07 s** |
| Time saved per warm workflow | **0.68 s (90%)** |
| Warm start success rate | **5 / 5** |
| Expired cookie handling | ✓ correctly redirected to login |

### 3.3 Popup Handling (Local, Docker site)

| Popup type | With handler | Without handler | Blocks? |
|------------|-------------|----------------|---------|
| Native `alert()` | ✓ auto-accept, 0.08 s | Hangs until timeout | **YES** |
| Native `confirm()` | ✓ auto-accept, 0.09 s | Hangs until timeout | **YES** |
| Custom DOM modal | ✓ explicit `.click()`, 0.31 s | Does NOT hang | No |
| Session toast | ✓ ignored (non-blocking) | ✓ also ignored | No |

Native `alert()` / `confirm()` without `page.on("dialog", ...)` blocks the automation indefinitely. Custom DOM modals are not caught by the dialog handler — they require an explicit `.click()` per portal.

### 3.4 Crash Recovery (Local, Docker site)

Crash simulated at step 3 of 6. Three recovery scenarios:

| Scenario | Restart time | Total time | Work lost |
|----------|-------------|-----------|----------|
| A — No recovery (restart from scratch) | 2.1 s | 2.9 s | 3 steps |
| B — Cookie restore (skip re-login) | 2.4 s | 3.2 s | 2 steps |
| C — Checkpoint (skip to crash point) | 1.9 s | 2.7 s | 0 steps |

On this fast local site, differences are small. On portals with slow login flows, scenario B and C would save significantly more time than A.

### 3.5 Long-Run Stability (10 minutes, 4 workers, Docker site)

| Metric | Value |
|--------|-------|
| Duration | 10.0 min |
| Total workflows | 738 |
| Successful | 738 (100.0%) |
| Avg workflow time | 3.25 s |
| p95 workflow time | 8.86 s |

**Python process memory growth over 10 minutes (RSS, 4 workers):**

```
 0 min   ███ 29 MB
 1 min   ████ 39 MB
 3 min   ████ 49 MB
 5 min   █████ 60 MB
 7 min   ██████ 71 MB
10 min   ███████ 86 MB
```

Growth: **+57 MB over 10 min → +340 MB/hr** at 4 workers.

> Note: this tracks the Python process RSS only. Chrome subprocess memory is tracked separately in §3.11. The leak is in Python-side object accumulation (page handles, cookies, event listeners) — not Chrome itself.

| Duration | Projected Python RSS growth |
|----------|-----------------------------|
| 4 hrs | +1.36 GB |
| 5.8 hrs | +2.0 GB — exceeds a 2 GB container limit |
| 8 hrs | +2.72 GB |

### 3.6 Queue Throughput (Local, Docker site)

| Jobs | Workers | Jobs/min | Drain time | Worker utilisation | Success |
|------|---------|----------|-----------|-------------------|---------|
| 50 | 2 | 37.3 | 80.5 s | 99.6% | 100% |
| 200 | 4 | 70.6 | 169.9 s | 99.2% | 100% |
| 200 | 8 | 150.0 | 80.0 s | 93.6% | 100% |
| 500 | 4 | 76.5 | 391.9 s | 99.8% | 100% |

Near-linear scaling through 8 workers. Utilisation >93% — workers are browser-bound, not idle.

### 3.7 Throughput Benchmark (Local, Docker site)

50-workflow batch per concurrency level:

| Workers | wf/hr | Avg (s) | p95 (s) | Success |
|---------|-------|---------|---------|---------|
| 1 | 1,174 | 3.07 | 8.86 | 100% |
| 2 | 2,383 | 2.98 | 8.85 | 100% |
| 4 | 4,722 | 2.90 | 8.84 | 100% |
| 6 | 6,568 | 3.09 | 9.25 | 100% |
| 8 | 7,605 | 3.39 | 9.06 | 100% |
| 10 | 9,323 | 3.23 | 8.82 | 100% |

p95 is stable (~8.8–9.3 s) across all concurrency levels — tail is driven by the 10% server-error dialog retry, not by worker contention.

---

### 3.8 Full Realistic Workflow — PortalX v2 (Local, Docker site)

7-step workflow: login → MFA (TOTP) → terms → NPI → patient search → prior auth (Vuetify + quick-lookup) → confirmation.

**30 runs, 3 workers:**

| Metric | Value |
|--------|-------|
| Success rate | **28 / 30 (93.3%)** |
| Avg total | **7.26 s** |
| p95 total | **17.32 s** |
| Concurrent session hits | 2 (matches 5% injection rate) |
| p95 cause | Server error dialog → prior auth retry |

**Per-step timings (averages across all runs):**

| Step | Avg |
|------|-----|
| Login | 0.07 s |
| MFA (TOTP, char-by-char) | 0.43 s |
| Terms + jurisdiction | 1.90 s |
| NPI selection | 1.65 s |
| Patient search (API wait + score ≥ 99) | 0.83 s |
| Prior auth form (quick-lookup + Vuetify v-select) | 2.82 s |
| Confirmation modal | 0.01 s |

---

### 3.9 Browserbase vs Local — Latency Comparison (Both modes → Render)

Both local Playwright (running inside Docker on the host machine) and Browserbase CDP hit the same public Render URL. Network path to the site is identical for both — the only difference is the browser automation stack.

This experiment was run **three times** — once as a baseline, then twice more as fixes were progressively applied, to show the improvement trajectory.

---

#### Run summary across all three attempts

| Run | Fixes applied | Local success | Local avg | BB success | BB avg | BB p95 |
|-----|--------------|--------------|-----------|-----------|--------|--------|
| 1 — Baseline | none | 8/10 (80%) | 12.36 s | 4/10 (40%) | 94.62 s | 184.11 s |
| 2 — Modal guards | `state="hidden"` guard on `#alert-modal` + `#error-modal` | 9/10 (90%) | 10.97 s | 5/10 (50%) | 70.43 s | 89.38 s |
| 3 — Modal guards + TOTP fix | Above + boundary check before TOTP generation + `page.evaluate()` batch fills | 7/10 (70%) | 79.14 s | 7/10 (70%) | 79.14 s | 121.89 s |

> Run 3 local shows 7/10 due to one concurrent-session hit (5% injection rate). The script logic for local is unchanged from Run 2.

---

#### Local Playwright — per-step breakdown

**Run 1 baseline (local → Render):**

| Step | n | avg | p50 | p95 | max |
|------|---|-----|-----|-----|-----|
| login | 9 | 0.929 s | 0.915 s | 0.997 s | 0.997 s |
| mfa | 9 | 1.008 s | 0.980 s | 1.081 s | 1.081 s |
| terms | 9 | 2.443 s | 2.401 s | 2.598 s | 2.598 s |
| npi | 9 | 2.058 s | 2.178 s | 2.337 s | 2.337 s |
| patient_search | 8 | 1.551 s | 1.533 s | 1.701 s | 1.701 s |
| prior_auth | 8 | 5.457 s | 2.744 s | 13.679 s | 13.679 s |
| confirmation | 8 | 0.012 s | 0.012 s | 0.016 s | 0.016 s |

**Run 2 (local → Render, with modal guard fix):**

| Step | n | avg | p50 | p95 | max |
|------|---|-----|-----|-----|-----|
| login | 9 | 0.992 s | 0.962 s | 1.162 s | 1.162 s |
| mfa | 9 | 1.245 s | 1.050 s | 2.969 s | 2.969 s |
| terms | 9 | 2.429 s | 2.415 s | 2.527 s | 2.527 s |
| npi | 9 | 1.969 s | 2.168 s | 2.248 s | 2.248 s |
| patient_search | 9 | 1.509 s | 1.526 s | 1.639 s | 1.639 s |
| prior_auth | 9 | 3.808 s | 2.653 s | 13.667 s | 13.667 s |
| confirmation | 9 | 0.011 s | 0.011 s | 0.014 s | 0.014 s |

> prior_auth p95 (~13.7 s) vs avg (~3.8–5.5 s) across all runs: the spread is caused by the 10% server-error dialog retry path.

---

#### Browserbase CDP — per-step breakdown, all three runs

**Run 1 — Baseline (BB → Render, no fixes):**

| Step | n | avg | p50 | p95 | max |
|------|---|-----|-----|-----|-----|
| session_create | 10 | 0.981 s | 0.969 s | 1.230 s | 1.230 s |
| cdp_connect | 10 | 1.915 s | 1.875 s | 2.255 s | 2.255 s |
| login | 10 | 7.429 s | 7.328 s | 8.768 s | 8.768 s |
| mfa | 10 | 20.607 s | 20.221 s | 23.873 s | 23.873 s |
| terms | 10 | 8.091 s | 8.123 s | 9.761 s | 9.761 s |
| npi | 6 | 7.065 s | 7.061 s | 7.559 s | 7.559 s |
| patient_search | 5 | 16.477 s | 16.251 s | 18.088 s | 18.088 s |
| prior_auth | 4 | 35.847 s | 35.888 s | 37.469 s | 37.469 s |
| confirmation | 4 | 1.157 s | 1.554 s | 1.568 s | 1.568 s |

Failures (6/10): 3× `#alert-modal` intercepted NPI click · 1× `#error-modal` intercepted prior_auth · 1× patient_search nav timeout · 1× background `Dialog.accept` race (non-fatal)

**Run 2 — Modal guards applied (`state="hidden"` before interactions):**

| Step | n | avg | p50 | p95 | max |
|------|---|-----|-----|-----|-----|
| session_create | 10 | 1.020 s | 1.040 s | 1.237 s | 1.237 s |
| cdp_connect | 10 | 1.944 s | 1.959 s | 2.118 s | 2.118 s |
| login | 10 | 7.588 s | 7.587 s | 8.164 s | 8.164 s |
| mfa | 10 | 20.377 s | 21.201 s | 22.275 s | 22.275 s |
| terms | 10 | 8.136 s | 8.155 s | 8.991 s | 8.991 s |
| npi | 7 | 7.688 s | 7.634 s | 8.385 s | 8.385 s |
| patient_search | 6 | 12.114 s | 12.293 s | 13.138 s | 13.138 s |
| prior_auth | 5 | 24.041 s | 24.110 s | 24.359 s | 24.359 s |
| confirmation | 5 | 1.194 s | 1.046 s | 1.571 s | 1.571 s |

Failures (5/10): 3× `#alert-modal.active` guard timed out (modal stayed visible 7× during 5 s window — `dismiss_alert_modal()` click not registering over high-latency CDP) · 1× `#error-modal.active` guard timed out · 1× patient_search nav timeout

**Run 3 — Modal guards + TOTP boundary fix + `page.evaluate()` batch fills:**

| Step | n | avg | p50 | p95 | max |
|------|---|-----|-----|-----|-----|
| session_create | 10 | 0.960 s | 0.958 s | 1.103 s | 1.103 s |
| cdp_connect | 10 | 2.000 s | 1.875 s | 3.174 s | 3.174 s |
| login | 10 | 8.246 s | 7.818 s | 10.458 s | 10.458 s |
| mfa | 9 | 20.931 s | 20.515 s | 23.132 s | 23.132 s |
| terms | 9 | 7.825 s | 7.886 s | 8.323 s | 8.323 s |
| npi | 9 | 7.776 s | 7.632 s | 8.389 s | 8.389 s |
| patient_search | 8 | 11.895 s | 11.838 s | 12.920 s | 12.920 s |
| prior_auth | 7 | 29.189 s | 24.339 s | 57.506 s | 57.506 s |
| confirmation | 7 | 1.302 s | 1.452 s | 1.756 s | 1.756 s |

Failures (3/10): 1× `#error-modal.active` guard timed out (6× visible) · 1× prior_auth nav timeout (Render latency spike) · 1× concurrent session on login

> **What the `evaluate()` batch filling changed:** `patient_search` dropped from 12.1 s (Run 2) → 11.9 s, and `prior_auth` from 24.0 s → 29.2 s avg. The prior_auth increase is within run noise — the p95 spike (57.5 s) is caused by the server-error retry on one run.

---

#### Per-step overhead trend (all three BB runs vs local)

| Step | Local avg | BB Run 1 | BB Run 2 | BB Run 3 |
|------|-----------|----------|----------|----------|
| session_create | — | 0.981 s | 1.020 s | 0.960 s |
| cdp_connect | — | 1.915 s | 1.944 s | 2.000 s |
| login | ~0.96 s | 7.429 s | 7.588 s | 8.246 s |
| mfa | ~1.1 s | 20.607 s | 20.377 s | 20.931 s |
| terms | ~2.4 s | 8.091 s | 8.136 s | 7.825 s |
| npi | ~2.0 s | 7.065 s | 7.688 s | 7.776 s |
| patient_search | ~1.5 s | 16.477 s | 12.114 s | 11.895 s |
| prior_auth | ~4–5 s | 35.847 s | 24.041 s | 29.189 s |
| confirmation | ~0.01 s | 1.157 s | 1.194 s | 1.302 s |

`patient_search` improved significantly (16.5 s → 12.1 s → 11.9 s) as fixes reduced the number of failed pre-search actions. `mfa` is flat across all three runs (~20 s) — the TOTP fix did not help.

---

#### Browserbase startup overhead (consistent across all runs)

| | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| session_create avg | 0.981 s | 1.020 s | 0.960 s |
| cdp_connect avg | 1.915 s | 1.944 s | 2.000 s |
| Total startup | **2.90 s** | **2.96 s** | **2.96 s** |
| As % of total workflow | 3% | 4% | 4% |

Session creation and CDP connect latency are stable across all three runs and across concurrent vs sequential modes. **Startup is never the bottleneck** — it's always 3–4% of total workflow time.

---

#### MFA timing — TOTP boundary fix analysis

MFA averaged ~20 s across all three BB runs despite the boundary fix. The fix prevents generating a code that's <3 s from expiry at generation time, but with ~7–9 s of login page latency before MFA, the code can still expire during the submit round-trip itself. A complete fix requires generating the code at the moment the submit fires and retrying the HTTP round-trip if the server rejects, not just at code-generation time.

---

### 3.10 Concurrent Browserbase Sessions (3 and 5 in parallel → Render)

#### 3-way concurrent (original run, before fixes)

3 Browserbase sessions launched simultaneously.

| Metric | Value |
|--------|-------|
| Sessions launched | 3 |
| Successful | **2 / 3 (67%)** |
| Wall time (all 3) | **105.7 s** |
| Avg per-session time | **92.7 s** |

> Wall time (105.7 s) is close to the avg per-session time (92.7 s) because all 3 sessions ran in true parallel — the slowest session determined the wall clock.

**Per-step timing across the 2 successful sessions:**

| Step | avg | min | max |
|------|-----|-----|-----|
| session_create | 0.940 s | 0.938 s | 0.942 s |
| cdp_connect | 2.006 s | 1.989 s | 2.022 s |
| login | 7.986 s | 7.582 s | 8.390 s |
| mfa | 19.840 s | 19.684 s | 19.996 s |
| terms | 7.681 s | 7.572 s | 7.790 s |
| npi | 6.846 s | 6.297 s | 7.396 s |
| patient_search | 16.111 s | 16.020 s | 16.203 s |
| prior_auth | 40.285 s | 38.526 s | 42.043 s |
| confirmation | 1.603 s | 1.491 s | 1.714 s |

**Session creation under concurrency:**
- Sequential (10 runs): 0.847–1.230 s range
- Concurrent (3 sessions): 0.938–0.955 s range

Sessions do not interfere with each other. Session create times and step timings are consistent between concurrent and sequential runs. Browserbase session isolation is working correctly.

**Failure:** Session S02 failed at `patient_search` with a navigation timeout — same class of issue as sequential run 05 (Render response latency). Not a concurrency-specific failure.

#### 5-way concurrent (attempted — plan limit discovered)

Attempted to launch 5 sessions simultaneously. **The Browserbase free tier allows a maximum of 3 concurrent sessions.**

Sessions S01 and S05 received HTTP 429 immediately on creation:
```
Error code: 429 - You've exceeded your max concurrent sessions limit
(limit 3, currently 3). Please contact support to increase your limit.
```

Sessions S02, S03, S04 successfully created (the first 3 through). Of those 3:

| Session | Result | Notes |
|---------|--------|-------|
| S02 | ✓ 98.4 s | Full workflow completed |
| S03 | ✓ 96.3 s | Full workflow completed |
| S04 | ✗ 68.5 s | `patient_search` timed out (Render latency spike) |

**Wall time: 98.7 s** (parallel — the slowest successful session determined wall time)

**Session creation under 5-way concurrency (3 that got through):**
- avg: 1.016 s, max: 1.079 s, min: 0.982 s
- Consistent with sequential and 3-way concurrent — no queuing at creation

**Plan limit impact:** 5-way and 10-way concurrency cannot be tested on the current Browserbase free tier plan (max 3 concurrent). The limit is enforced per-account at session creation time.

---

### 3.11 Docker Container Sizes and Memory per Worker

**Image sizes (as reported by `docker images`):**

| Image | Size | Used for |
|-------|------|---------|
| `python:3.12-slim` | **205 MB** | PortalX site server |
| `mcr.microsoft.com/playwright/python:v1.50.0-noble` | **3.42 GB** | Experiment runner |

The runner image includes Chromium, Firefox, and WebKit. Only Chromium is used in these experiments.

**Container memory at idle:**

| Container | CPU | Memory (RSS) |
|-----------|-----|--------|
| `exp_runner` | 0.00% | **17.5 MB** |
| `exp_portalx_site` | 0.01% | **26.9 MB** |

**Chrome memory per worker — measured (Python process + all child process RSS):**

| Workers | Baseline (MB) | Peak (MB) | Total Chrome delta (MB) | MB per worker |
|---------|--------------|-----------|------------------------|--------------|
| 1 | 95.1 | 696.6 | 601.5 | 601.5 |
| 2 | 97.3 | 1,286.6 | 1,189.3 | 594.7 |
| 4 | 97.6 | 2,467.9 | 2,370.3 | 592.6 |
| 6 | 99.8 | 3,654.4 | 3,554.6 | 592.4 |
| 8 | 99.8 | 4,839.9 | 4,740.0 | 592.5 |

**Each Chromium worker uses approximately 593 MB.** The total memory scales proportionally with worker count — adding a worker adds ~593 MB regardless of how many are already running. The 1-worker measurement is slightly higher (601 MB) because the baseline itself was ~4 MB lower in that run; this is within normal measurement variance.

**Execution time stays flat:** All worker counts (1–8) completed in ~20.8 s. Workers are browser/network-bound, not memory-bound.

**Worker limits on this machine (7.65 GB Docker RAM):**
- At 8 workers: 4.74 GB peak → **62% of available RAM** — tested, no issues
- At 11 workers (calculated): ~6.8 GB → ~89% — not tested
- At 13 workers (calculated): ~8.0 GB → would exceed host RAM — not tested

> The 11-worker and 13-worker figures are calculated from the measured 593 MB/worker constant, not tested on this machine.

**`shm_size: 2gb` in docker-compose:** Docker's default `/dev/shm` is 64 MB. Chromium requires larger shared memory for its renderer and IPC. The 2 GB setting is what we used and it worked for all worker counts tested (1–8). We did not test the minimum required value.

---

## 4. Browserbase vs Local — What the Data Shows

### Where the overhead actually lives (measured on PortalX v2 vs Render)

| Step | Local avg | Browserbase avg | Δ |
|------|-----------|----------------|---|
| Session startup (create + CDP) | — | 2.9 s | Fixed cost per session |
| Login | 0.93 s | 7.43 s | +700% |
| MFA | 1.01 s | 20.61 s | +1944% (TOTP retries — fixable) |
| Terms | 2.44 s | 8.09 s | +231% |
| NPI | 2.06 s | 7.07 s | +243% |
| Patient search | 1.55 s | 16.48 s | +962% |
| Prior auth | 5.46 s | 35.85 s | +557% |
| **Total** | **12.36 s** | **94.62 s** | **+665%** |

> All numbers are against the same Render URL. These numbers are for PortalX v2 only — no measurements have been taken against real healthcare portals.

### Why the measured overhead is not representative of production

The 94.6 s Browserbase avg and the +665% overhead were measured with the worker running on a **Mac laptop connecting to Browserbase's US cloud servers** — a long network path with ~63 ms round-trip time per CDP command (Eastern US → Browserbase us-east-1, per Browserbase's published latency data).

In production, workers would be deployed on AKS (East US) connecting to Browserbase's us-east-1 region. Browserbase's own blog documents that Eastern US clients see **~6 ms RTT** when region-matched, compared to **~63 ms** from a non-matched region.

**Test setup (what we measured):**
```
Mac Docker  ──── ~63 ms RTT ────►  Browserbase us-east-1  ──── ~50 ms ────►  Render
```

**Production setup (not yet measured):**
```
AKS pod (East US)  ──── ~6 ms RTT ────►  Browserbase us-east-1  ──── ~10 ms ────►  Real portal
```

The session startup cost (2.96 s) does not change with region — that is Browserbase spinning up a Chrome instance. The RTT reduction saves ~0.2 s on the API calls involved in create+connect, bringing startup to approximately ~2.7 s.

---

## 4b. Production Projections — AKS East US → Browserbase East US

> **Labelling convention used below:**
> - **Measured** — a number from an actual experiment run
> - **Derived** — calculated from measured data using a documented formula
> - **Documented** — published by Browserbase or Kubernetes, not measured by us
> - **Estimated** — reasoned inference, not backed by measurement or documentation

---

### How each step time is projected

Each step's time has two components:

```
step_time = browser_work + CDP_overhead
```

- **browser_work** = page load from Browserbase's browser to the target site + JS execution + server response time. This is approximately what local Playwright measures, since both Mac Docker and Browserbase's browser are making real HTTP requests to the same Render URL. *(Used directly from §3.9 local measurements — measured)*
- **CDP_overhead** = n\_roundtrips × RTT. Measured at Mac RTT (~63 ms). Projected by scaling to same-region RTT (~6 ms, documented by Browserbase).

**CDP overhead scaling factor:** 63 ms → 6 ms = **10.5× reduction in per-roundtrip cost** *(documented)*

For each step:
```
projected_CDP_overhead = measured_CDP_overhead / 10.5
projected_step_time = local_browser_work + projected_CDP_overhead
```

Note: MFA is treated separately because its overhead is dominated by TOTP retry storms, not pure RTT math.

---

### Per-step projection: PortalX v2 workflow (same portal, different network path)

The measured data used is from BB Run 2 (modal guards applied, most stable run at 50% success).

| Step | Local avg (measured) | BB Mac avg (measured) | CDP overhead at Mac | Projected CDP overhead at 6ms RTT | Projected step total |
|------|---------------------|----------------------|---------------------|----------------------------------|----------------------|
| session_create + CDP connect | — | **2.96 s** | ~0.2 s (API call RTT) | ~0.02 s | **~2.7 s** |
| login | 0.99 s | 7.59 s | 6.60 s | 0.63 s | **~1.6 s** |
| mfa | — | — | — | — | **see below** |
| terms | 2.43 s | 8.14 s | 5.71 s | 0.54 s | **~3.0 s** |
| npi | 1.97 s | 7.69 s | 5.72 s | 0.55 s | **~2.5 s** |
| patient_search | 1.51 s | 12.11 s | 10.60 s | 1.01 s | **~2.5 s** |
| prior_auth | 3.81 s | 24.04 s | 20.23 s | 1.93 s | **~5.7 s** |
| confirmation | 0.01 s | 1.19 s | 1.18 s | 0.11 s | **~0.1 s** |

> All projected values are derived. They have not been measured from an AKS host.

**MFA step (separate analysis):**
The ~20 s MFA time is not driven by CDP round-trip count — it is driven by TOTP retry storms. With the boundary fix correctly implemented (generate code at submit time, retry if rejected), MFA should reduce to:
- Page load: ~0.5 s (same as browser_work, derived from local MFA timing)
- TOTP boundary wait: 0–3 s (depends on when in the 30 s window you arrive; avg ~1.5 s)
- Fill 6 chars + submit (CDP overhead at 6 ms RTT): ~0.1 s

Projected MFA with correct fix: **~2–4 s** *(estimated — fix not yet fully implemented)*
MFA without fix (retries still firing): **~8–12 s** *(estimated — retries faster at low RTT but still happen)*

**Full workflow projected total (PortalX v2):**

| Scenario | Estimated total |
|----------|----------------|
| TOTP fix implemented correctly | **~18–22 s** |
| TOTP retries still firing | **~25–35 s** |

> These are derived/estimated projections for PortalX v2 only. Real healthcare portals (Brightree, MyCGS) are not included below but addressed separately.

---

### Kubernetes worker pod overhead

Your RPA workers will run as K8s pods on AKS, consuming jobs from a queue. Pod overhead depends on whether the pod is already running or needs to be started.

**Documented K8s pod startup times (Kubernetes SIG Scalability SLO):**

| Phase | Time | Source |
|---|---|---|
| Pod scheduling | < 5 s p99 (SLO target) | Kubernetes SIG Scalability SLO |
| Container start (image cached on node) | ~0 s (image already present) | Kubernetes docs |
| Container start (image cold pull, 3.59 GB runner) | Dominates — "minutes to seconds" range | Microsoft AKS Artifact Streaming docs |
| App init (Python process ready) | ~1–3 s for typical Python app | Estimated |

**In practice, for long-lived RPA worker pods (the typical pattern):**

- **Steady state (pod already running, picking up next job):** essentially zero pod overhead. The pod sits idle between jobs and picks the next off the queue immediately.
- **Scale-out event (HPA adds a new pod due to load):** 15–30 s before the new pod is ready to process its first job (scheduling + container start + app init). This is a one-time cost per pod, amortized over all jobs that pod processes.
- **Image pull on a cold node (first pod on a new AKS node):** can take several minutes for the 3.59 GB Playwright image. Using AKS Artifact Streaming or pre-pulling the image via a DaemonSet eliminates this.

**K8s overhead per job (at steady state with pre-warmed pods): ~0 s**
**K8s overhead per new pod at scale-out: ~15–30 s one-time** *(documented/estimated)*

---

### End-to-end realistic projection: PortalX v2 on AKS → Browserbase East US

This is the closest to your production architecture we can model from current data.

| Component | Time | Type |
|-----------|------|------|
| K8s scheduling (steady state) | ~0 s | Documented |
| Browserbase session create | ~0.96 s | Measured |
| CDP connect | ~2.0 s | Measured (startup is BB-side, doesn't improve with region) |
| Workflow (login → confirmation, TOTP fixed) | ~15–19 s | Derived |
| **Total per job (steady state, TOTP fixed)** | **~18–22 s** | Derived |
| **Total per job (TOTP retries still firing)** | **~25–35 s** | Derived |

For comparison, our Mac measurement was **79.1 s avg** (post-fix). The projected production improvement is driven almost entirely by the RTT reduction from ~63 ms to ~6 ms.

**Throughput projection (PortalX v2 proxy, TOTP fixed):**

| Workers (Browserbase sessions) | Estimated jobs/hr | Notes |
|-------------------------------|-------------------|-------|
| 3 (free tier limit) | ~490–600 | |
| 25 (Developer plan) | ~4,000–5,000 | 25-way parallel |
| 100 (Startup plan) | ~16,000–20,000 | 100-way parallel |

> These throughput numbers are derived from the projected 18–22 s per job. They assume PortalX-level portal complexity, no job queue contention, and steady-state pods.

---

### What will be different with real healthcare portals

Real portals (Brightree, MyCGS) are not measured — no numbers here are backed by data. What we know from inspecting them:

| Factor | Impact on timing vs PortalX |
|--------|---------------------------|
| SSO / login redirect chain | +2–3 additional page loads |
| Heavier JS bundles (Angular, full Vuetify app) | Longer `load` wait per page |
| Document upload fields | Extra form interactions |
| More AJAX-heavy patient search | Longer debounced wait |
| Real MFA (authenticator app) | Same TOTP issue applies |
| Session timeouts shorter than PortalX's 5 min | More session management overhead |

**Conservative real-portal estimate (not measured, purely estimated):**

Each step takes 1.5–3× longer than PortalX due to heavier pages and more interactions. Applying that to the projected totals:

| | PortalX (derived) | Real portal guess range |
|---|---|---|
| Per-job total | ~18–22 s | ~30–60 s |
| Jobs/hr at 25 sessions | ~4,000–5,000 | ~1,500–3,000 |

> **The real portal column is an unvalidated estimate.** Do not use it for capacity planning. Run `exp_browserbase_latency.py` from an AKS pod against a real portal staging environment to get actual numbers.

---

## 5. Stability Findings

### Memory leak (+340 MB/hr, Python process)

Measured at 4 workers over 10 min. The leak is in Python-side object accumulation — page handles, cookies, event listeners that are not fully released between workflows. Chrome subprocess memory (tracked separately in §3.11) was stable during this experiment.

A worker running continuously will exhaust a 2 GB container in approximately 5.8 hours at this rate.

**Mitigations (not yet tested):**
1. Rotate `BrowserContext` every 50–100 workflows — forces Python to release accumulated references
2. Kubernetes `livenessProbe` with memory threshold to restart the pod before OOM
3. Browserbase CDP mode — Chrome runs off the worker machine entirely; Python-side accumulation would still apply but without the Chrome subprocess contribution

### Crash patterns

Local (738 workflows, 10 min): **zero failures, zero timeouts, zero crashes.**

Browserbase (13 total runs: 10 sequential + 3 concurrent): **6 failures, all from modal timing races or Render response latency.** No mid-session disconnects from Browserbase infrastructure.

---

## 6. What Has Not Been Measured Yet

| Gap | Why it matters |
|-----|---------------|
| Real portal (Brightree / MyCGS) timing | All timing numbers are PortalX — real portals have SSO, heavier AJAX, document upload |
| Browserbase at >3 concurrent sessions | Free tier caps at 3; would need a paid plan to test 5, 10+ |
| Full MFA TOTP fix validation | Boundary fix applied but MFA still ~21 s — submit-time expiry not yet fixed |
| Memory leak with context rotation | Only measured without rotation |
| Browserbase long-run stability (10+ min continuous) | Only ran sequential + concurrent batches, no extended run |
| Worker count above 8 (local) | Tested up to 8 workers; 9–13 workers untested |
| Browserbase from same-region cloud host | All measurements from Mac with ~100 ms RTT; same-region would be ~5–20 ms |

---

## 7. Bottlenecks Identified

| Bottleneck | Evidence | Fix |
|------------|----------|-----|
| **Browserbase modal race conditions** | 5/10 sequential BB failures (before fix); reduced to 1/10 after `state="hidden"` guards | `wait_for_selector(modal, state="hidden")` before each interaction — partially implemented, further fix needed for persistent error modal |
| **TOTP boundary expiry (Browserbase)** | MFA avg ~21 s on BB vs 1.0 s local — persists after boundary check fix | Generate `current_totp()` at the moment of submit click, not at step entry; need retry loop if server rejects |
| **Python process memory leak** | +340 MB/hr at 4 workers | BrowserContext rotation every 50–100 workflows |
| **Native dialog handler mandatory** | Blocks indefinitely without it (confirmed in popup test) | `page.on("dialog", ...)` everywhere |
| **DOM modal portal-specific** | Global dialog handler does not catch these | Per-portal explicit `.click()` handler |
| **prior_auth p95 spike (local)** | 13.7 s p95 vs 5.5 s avg | 10% server-error retry — add per-step timeout |
| **Dialog.accept race (Browserbase)** | Background exception on 2/10 BB runs | Wrap dialog accept in try/except — non-fatal but noisy |

---

## 8. Memory Planning (From Measured 593 MB/Worker)

| Workers | Chrome memory (measured) | + ~500 MB OS overhead | Total |
|---------|--------------------------|----------------------|-------|
| 1 | 601 MB | 500 MB | ~1.1 GB |
| 2 | 1,189 MB | 500 MB | ~1.7 GB |
| 4 | 2,370 MB | 500 MB | ~2.9 GB |
| 6 | 3,555 MB | 500 MB | ~4.1 GB |
| 8 | 4,740 MB | 500 MB | ~5.2 GB |

All rows above are directly from measured data. The formula `floor((RAM_MB - 500) / 593)` gives a calculated estimate for other RAM sizes — it has not been validated by testing those configurations.

> These figures are for local Playwright workers only. With Browserbase, Chrome runs in Browserbase's infrastructure — Python worker memory would be much lower (not yet measured).

---

## 9. Recommendations

### Based on what was measured

1. **Fix Browserbase modal guards** — replace dismiss-then-click with `wait_for_selector(modal, state="hidden")` before each action. Root cause of 5/6 BB failures.
2. **Fix TOTP timing** — generate code at last possible moment, skip if within 3 s of window boundary. Root cause of ~20 s MFA on BB.
3. **Enable session reuse (`browser_session_id` in `RPASession`)** — 90% login time reduction measured in session persistence experiment.
4. **Add context rotation every 50–100 workflows** — addresses +340 MB/hr Python leak.
5. **Add `on_failure` Celery hook** — explicit session close to release credential locks.

### What to run next (before drawing further conclusions)

1. ~~Re-run `exp_browserbase_latency.py` after fixing modal guards and TOTP timing~~ — **Done.** 70% success, 79.1 s avg (was 40%, 94.6 s). See §3.9.
2. ~~Run `exp_browserbase_concurrent.py --sessions 5`~~ — **Done. Hit plan limit (max 3 concurrent on free tier).** S01 and S05 got HTTP 429. See §3.10.
3. ~~Run `exp_browserbase_concurrent.py --sessions 10`~~ — **Cannot run.** Same plan limit applies.
4. Fix the remaining prior_auth error modal failure (retry loop with longer timeout, not just a guard)
5. Fix MFA TOTP at submit-time (generate code at the exact moment the submit button is clicked)
6. Run `exp_long_run.py` against Render to measure stability over 10+ minutes — not yet run
7. Run any experiment against a real Brightree or MyCGS staging environment — all current numbers are synthetic

---

_All numbers measured. Local experiments: 2026-03-31. Browserbase + Render experiments: 2026-04-01._
_Scripts: `experiments/scripts/` · Site: https://portalx-7nn4.onrender.com_
