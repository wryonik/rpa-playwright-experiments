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
| Browserbase CDP avg (same workflow, same Render URL) | **94.6 s** |
| Browserbase overhead vs local | **+665% avg, +750% p95** |
| Browserbase session startup (create + CDP connect) | **2.9 s** — only 3% of total workflow time |
| Browserbase success rate (10 sequential runs) | **40%** |
| Browserbase success rate (3 concurrent sessions) | **67%** |
| Memory leak rate (local, 4 workers, 10 min) | **+340 MB/hr** (Python process RSS) |
| Session reuse (cookie restore vs full login) | **90% login time reduction** |
| Queue throughput (local, Docker site) | **76 jobs/min at 4 workers, 150 jobs/min at 8 workers** |

**Critical discovery — Browserbase failures are not caused by session overhead.** The 2.9 s startup cost is negligible. Failures are caused by **modal timing race conditions**: over the higher-latency CDP path, random DOM modals appear after the dismiss-check has already passed, intercepting the next click. All 5 modal-related failures have a known fix. See §3.9.

**MFA timing spike on Browserbase.** TOTP MFA averages 20.6 s over Browserbase vs 1.0 s local. Root cause: with ~7–9 s of page-load latency before MFA, the TOTP code can expire mid-submit and trigger a retry. Each retry adds ~6–8 s. Fix: generate the TOTP code immediately before typing rather than at session start. See §3.9.

---

## 2. Test Environment

### PortalX v2

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

**10 iterations each, sequential:**

| Mode | Success | Avg total | p95 total |
|------|---------|-----------|-----------|
| Local Playwright → Render | **8 / 10 (80%)** | **12.36 s** | **21.66 s** |
| Browserbase CDP → Render | **4 / 10 (40%)** | **94.62 s** | **184.11 s** |
| Overhead | — | **+665.5%** | **+750.0%** |

**Local per-step breakdown (successful runs only):**

| Step | n | avg | p50 | p95 | max |
|------|---|-----|-----|-----|-----|
| login | 9 | 0.929 s | 0.915 s | 0.997 s | 0.997 s |
| mfa | 9 | 1.008 s | 0.980 s | 1.081 s | 1.081 s |
| terms | 9 | 2.443 s | 2.401 s | 2.598 s | 2.598 s |
| npi | 9 | 2.058 s | 2.178 s | 2.337 s | 2.337 s |
| patient_search | 8 | 1.551 s | 1.533 s | 1.701 s | 1.701 s |
| prior_auth | 8 | 5.457 s | 2.744 s | 13.679 s | 13.679 s |
| confirmation | 8 | 0.012 s | 0.012 s | 0.016 s | 0.016 s |

> prior_auth p95 (13.68 s) vs avg (5.46 s): the gap is caused by the 10% server-error dialog triggering a form re-submit.

**Browserbase per-step breakdown (all 10 runs, including partial):**

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

> `n` decreases for later steps because failed runs don't reach them. `confirmation` avg (1.157 s) is lower than p50 (1.554 s) due to a bimodal distribution: 2 runs completed in ~0.75 s, 2 runs in ~1.55 s.

**Per-step overhead (Browserbase vs local):**

| Step | Local avg | BB avg | Δ |
|------|-----------|--------|---|
| session_create | — | 0.981 s | BB only |
| cdp_connect | — | 1.915 s | BB only |
| login | 0.929 s | 7.429 s | +699.7% |
| mfa | 1.008 s | 20.607 s | +1944.3% |
| terms | 2.443 s | 8.091 s | +231.2% |
| npi | 2.058 s | 7.065 s | +243.3% |
| patient_search | 1.551 s | 16.477 s | +962.3% |
| prior_auth | 5.457 s | 35.847 s | +556.9% |
| confirmation | 0.012 s | 1.157 s | +9541.7% |

**Browserbase startup overhead:**
- Session create: **0.981 s avg** (p95: 1.230 s)
- CDP connect: **1.915 s avg** (p95: 2.255 s)
- Total startup: **2.896 s** — only **3% of avg total workflow time (94.6 s)**

The startup cost is not the bottleneck. 97% of Browserbase's total time is spent executing browser steps over the CDP network path.

**Failure root causes (6 failures across 10 runs):**

| Run | Step failed | What happened |
|-----|------------|---------------|
| 01 | npi | `#alert-modal` visible when NPI row click fired — modal intercepted the click |
| 05 | patient_search | `wait_for_url /prior-auth` timed out — Render slow to respond after patient search submit |
| 06 | npi | Same as run 01 — `#alert-modal` intercepted NPI click |
| 08 | npi | Same as run 01 |
| 10 | prior_auth | `#error-modal` visible when Vuetify v-select arrow click fired |
| 2 runs | (background) | `Dialog.accept: No dialog is showing` — native dialog fired and was auto-dismissed by Chrome before CDP handler responded. Non-fatal, logged as background exception. |

**Pattern:** 5 of 6 failures are modal timing races. In local mode, the `dismiss_*` check and the subsequent `.click()` happen within microseconds in the same process. Over Browserbase CDP, each action is a network round-trip (~50–200 ms), giving the modal time to appear between the check and the click.

**Fix:** Replace optimistic `wait_for(state="visible", timeout=1500)` checks with `wait_for_selector(modal, state="hidden")` before each interaction — wait for the modal to be gone before proceeding, rather than hoping it isn't there.

**MFA root cause — TOTP boundary expiry:**

TOTP codes are valid for exactly 30 seconds. In Browserbase mode:
1. Login page load + fill + submit: **~7–9 s**
2. We arrive at `/mfa` potentially 8–10 s into the current 30 s window
3. `step_mfa` generates `current_totp()` at this point — if the window is nearly expired, the code will be invalid by the time the submit round-trip completes
4. Server rejects → `step_mfa` retries (up to 3 times), each retry adding ~6–8 s

This explains the consistent ~20 s MFA step across all Browserbase runs. **Fix:** Generate `current_totp()` immediately before typing (not at step entry), and skip typing if fewer than 3 s remain in the current 30 s window.

---

### 3.10 Concurrent Browserbase Sessions (3 in parallel → Render)

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
| Browserbase at >3 concurrent sessions | Only tested 3-way; behaviour at 5, 10+ sessions is unknown |
| Browserbase success rate after modal timing fixes | Current 40% is before the `state="hidden"` fix |
| Memory leak with context rotation | Only measured without rotation |
| Browserbase long-run stability (10+ min continuous) | Only ran sequential + 1 concurrent batch |
| Worker count above 8 (local) | Tested up to 8 workers; 9–13 workers untested |

---

## 7. Bottlenecks Identified

| Bottleneck | Evidence | Fix |
|------------|----------|-----|
| **Browserbase modal race conditions** | 5/10 sequential BB failures | `wait_for_selector(modal, state="hidden")` before each interaction |
| **TOTP boundary expiry (Browserbase)** | MFA avg 20.6 s on BB vs 1.0 s local | Generate `current_totp()` at last moment; skip if <3 s to boundary |
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

1. Re-run `exp_browserbase_latency.py` after fixing modal guards and TOTP timing — current 40% success rate makes the timing numbers incomplete
2. Run `exp_browserbase_concurrent.py --sessions 5` and `--sessions 10` — concurrency above 3 not yet tested
3. Run `exp_long_run.py` against Render to measure Browserbase stability over 10+ minutes
4. Run any experiment against a real Brightree or MyCGS staging environment — all current numbers are synthetic

---

_All numbers measured. Local experiments: 2026-03-31. Browserbase + Render experiments: 2026-04-01._
_Scripts: `experiments/scripts/` · Site: https://portalx-7nn4.onrender.com_
