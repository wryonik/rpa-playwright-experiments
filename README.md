# RPA Playwright Experiments

Self-contained experiments measuring Playwright and Browserbase limits for healthcare portal automation.

Uses **PortalX** — a synthetic healthcare prior authorization portal that mirrors the real complexity of Brightree/MyCGS: TOTP MFA, Vuetify v-select dropdowns, quick-lookup fields, patient search with match scoring, concurrent session detection, and server error dialogs.

All numbers in `PERFORMANCE_REPORT.md` are measured from actual runs, not estimates.

---

## Repository Structure

```
.
├── docker-compose.yml          # Isolated experiment stack (site + runner)
├── PERFORMANCE_REPORT.md       # Full benchmark results with measured numbers
├── scripts/                    # Experiment scripts
│   ├── exp_full_workflow.py    # 7-step realistic healthcare workflow (main)
│   ├── exp_concurrency.py      # Parallel browser scaling (1→16 workers)
│   ├── exp_session_persistence.py  # Cookie reuse vs cold login
│   ├── exp_crash_recovery.py   # Mid-task crash + recovery scenarios
│   ├── exp_popup_handling.py   # Native alert/confirm/DOM modal handling
│   ├── exp_long_run.py         # Memory leak measurement (10-min stability)
│   ├── exp_queue_throughput.py # Job queue drain at varying concurrency
│   ├── exp_throughput.py       # Workflows/hr benchmark
│   ├── exp_browserbase_latency.py  # Browserbase CDP vs local Playwright
│   ├── utils.py                # Shared helpers, ResourceSampler, ExperimentMetrics
│   └── requirements.txt
└── site/                       # PortalX v2 synthetic test site
    ├── server.py               # Python HTTP server (session state, TOTP, error injection)
    └── pages/
        ├── login.html          # ASP.NET-style login (#Username, #Password)
        ├── mfa.html            # TOTP MFA entry (#txtMFACode, paste-blocked)
        ├── terms.html          # Scroll-to-bottom terms + jurisdiction radio
        ├── npi_select.html     # Provider NPI table with radio rows
        ├── patient_search.html # Patient search (LastName/FirstName/DOB, match score col 6)
        ├── prior_auth.html     # Full PA form: quick-lookup + Vuetify v-select
        ├── confirmation.html   # Confirmation modal before submit
        ├── session_expired.html
        └── concurrent.html     # Concurrent session error page
```

---

## Quickstart

### Prerequisites

- Docker Desktop (or Docker Engine + Compose v2)
- No Python or Node needed locally — everything runs inside containers

### 1. Start the stack

```bash
docker compose up -d
```

This starts:
- `exp_portalx_site` — PortalX server on `http://localhost:8888`
- `exp_runner` — Playwright container (stays up, ready for `exec`)

Wait for the health check to pass (~10s), then verify:

```bash
docker compose ps
# Both containers should show "healthy" / "running"
```

### 2. Run the full workflow experiment

```bash
docker exec exp_runner python3 exp_full_workflow.py --workers 3 --runs 30
```

This runs 30 workflows across 3 parallel browsers. Each workflow:
1. Login (`admin` / `P@ssw0rd!`)
2. TOTP MFA (server-side validation, 30s window)
3. Terms + jurisdiction selection
4. NPI provider selection from table
5. Patient search with match score ≥ 99
6. Prior auth form (quick-lookup + Vuetify v-select)
7. Confirmation modal + submit

### 3. Run other experiments

```bash
# Concurrency scaling
docker exec exp_runner python3 exp_concurrency.py

# Memory leak measurement (10 minutes)
docker exec exp_runner python3 exp_long_run.py --duration 600 --workers 4

# Session cookie reuse
docker exec exp_runner python3 exp_session_persistence.py --runs 5

# Queue throughput
docker exec exp_runner python3 exp_queue_throughput.py

# Interactive shell
docker exec -it exp_runner bash
```

### 4. Tear down

```bash
docker compose down
```

---

## PortalX v2 — Test Site Credentials

| Field | Value |
|-------|-------|
| URL | `http://localhost:8888/login` |
| Username | `admin` |
| Password | `P@ssw0rd!` |
| MFA | TOTP — see below |

**Getting the current TOTP code:**

```bash
python3 -c "import time; print(str(int(time.time() // 30) % 1_000_000).zfill(6))"
```

Or from inside the runner container:

```bash
docker exec exp_runner python3 -c "import time; print(str(int(time.time() // 30) % 1_000_000).zfill(6))"
```

---

## PortalX v2 — What It Simulates

| Feature | Implementation | Real Portal |
|---------|---------------|-------------|
| TOTP MFA | Server-side `time.time() // 30 % 1_000_000`, 30s window | MyCGS authenticator app |
| Paste-blocked MFA field | JS intercepts `paste` event | MyCGS `#txtMFACode` |
| Scroll-to-bottom terms | `scrollTop >= scrollHeight - clientHeight - 5` | Brightree terms page |
| NPI selection table | Radio rows, 20% random alert on load | Brightree provider lookup |
| Patient search | Debounced API call, result table with column 6 match score | Brightree patient search |
| Quick-lookup field | `data-quicklookup` attribute, `div.v-menu__content.menuable__content__active` dropdown | Brightree NPI lookup |
| Vuetify v-select | `i.material-icons` arrow, `div[role='listbox']`, ArrowDown pagination | Brightree equipment select |
| Server error dialog | 8–10% random injection, `#error-modal` DOM overlay | Brightree `xmlHttp.status: 500` |
| Concurrent session | 5% on login → redirect to `/concurrent` | MyCGS session limit |
| Session expiry | 300s inactivity timeout → redirect to `/session_expired` | Both portals |
| Confirmation modal | `#confirm-modal` before final submit | Brightree submit flow |

---

## Browserbase Support

`exp_browserbase_latency.py` connects to Browserbase via CDP and measures:
- Cold session start latency vs local Playwright
- Per-step timing breakdown (login, navigation, form fill, submit)
- Memory per worker (moves Chrome off-worker → leak becomes irrelevant)
- p95 latency comparison

To run it, set environment variables before `docker exec`:

```bash
docker exec \
  -e BROWSERBASE_API_KEY=your_key \
  -e BROWSERBASE_PROJECT_ID=your_project_id \
  exp_runner python3 exp_browserbase_latency.py --iterations 10
```

Results will be added to `PERFORMANCE_REPORT.md` in the Browserbase section.

---

## Key Findings (Summary)

See `PERFORMANCE_REPORT.md` for full measured data. Headlines:

- **Full 7-step workflow:** 28/30 success (93.3%), avg 7.26s, p95 17.3s at 3 workers
- **Memory leak:** +340 MB/hr at 4 workers — workers must be recycled before ~6 hours
- **Session reuse:** 90% login time reduction (0.07s warm vs 0.75s cold) — biggest single ROI improvement
- **Native dialog handlers mandatory:** `alert()` without `page.on("dialog")` hangs indefinitely
- **Near-linear queue scaling:** 4 workers → 76.5 jobs/min, 8 workers → 150 jobs/min

---

## Notes

- `shm_size: 2gb` in `docker-compose.yml` is required — Chrome crashes in Docker without it
- The runner image is `mcr.microsoft.com/playwright/python:v1.50.0-noble` (~1.8 GB, includes Chromium/Firefox/WebKit)
- All test data is synthetic — no real patient or provider data anywhere in this repo
- Scripts connect to `http://portalx-site:8888` inside Docker (container hostname), not `localhost`
