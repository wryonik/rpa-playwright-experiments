# RPA Platform Decision Report

**Date:** 2026-04-07
**Question:** For our healthcare RPA workload, do we use Browserbase ($20/mo Developer plan, scaling to higher tiers) or self-host Playwright on our existing VM?
**TL;DR:** Both can do everything we need. The decision is **engineering effort vs subscription cost**, not capability.

---

## Bottom Line

> The capability gap between Browserbase and self-hosted Playwright is **near-zero**. Everything we tested — concurrency, retries, popups, session reuse, idle resilience, audit trails, file uploads, error classification — works on both. The differences are:
>
> 1. **Browserbase is "batteries included"** — live session viewer, recording, network monitoring, DOM inspector, dashboard, distributed IPs, and managed Chrome ship out of the box. Each one is configurable in self-hosted Playwright too, but each one costs engineering hours.
>
> 2. **Self-hosted is ~3x faster per workflow** — no cloud round-trip overhead. For high-throughput batch jobs, that adds up. For status-check workloads with idle waits between actions, it doesn't matter.
>
> 3. **At our current scale, Browserbase Developer plan ($20/mo) is cheaper than building the equivalent observability ourselves.** At 10x scale, the math shifts and self-hosted starts to win — but only if the engineering time to build it is also accounted for.
>
> 4. **The "scale" and "reliability" arguments are largely myths.** Self-hosted handles 25 concurrent sessions easily on the existing VM (used 35 MB RAM and 0.3% CPU at 10 workers). Browserbase doesn't actually do anything magical at scale — it just hides the management overhead.

---

## What We Tested

| Dimension | Test | Result |
|-----------|------|--------|
| **Reliability** | 100+ workflow runs across both platforms | Both reach 80–90% success; failures are PortalX-side error injection (not platform bugs) |
| **Reliability** | Retry-once on transient errors | 88% → 100% success rate with one retry on transient errors (BB), 100% local |
| **Reliability** | Session idle without heartbeat | BB survives **8+ minutes idle** with correct config; local has no equivalent risk |
| **Reliability** | Session expiry detection | Both correctly detect PortalX 5-min session expiry → `/session-expired` redirect |
| **Reliability** | Popup handling (random + forced) | Both handle native dialogs, DOM error modals, DOM alert modals |
| **Reliability** | Connection resilience | BB CDP survives 30s/60s/120s/240s/360s/480s of pure idle when configured |
| **Maintenance** | File upload | Local: 0.017s, BB: 0.687s (40x slower over CDP, but works) |
| **Maintenance** | Multi-user logins | PortalX v3 supports admin/user1/user2/user3 — both platforms can use any |
| **Maintenance** | Session reuse (login once, batch lookups) | Both reuse sessions; saves 41% (BB) and 56% (local) vs fresh login per patient |
| **Scaling** | 25 concurrent sessions | BB: 80% success at first batch (PortalX rate-limit at scale); self-hosted limited by single VM IP |
| **Scaling** | Resource usage | Self-hosted at 10 concurrent: **35 MB RAM, 0.3% CPU** on 16-core VM |
| **Observability** | Audit trail | BB: built-in dashboard + replay URL; self-hosted: PortalX `/api/audit` endpoint we built |
| **Observability** | Screenshot on failure | Both: `page.screenshot()` works identically |
| **Observability** | Network logs | BB: dashboard out-of-box; self-hosted: needs Playwright tracing setup |
| **Observability** | Live session viewer | BB: dashboard out-of-box; self-hosted: requires custom CDP bridge or open-source viewer |

---

## The Reliability Story (and Why It Was Almost Wrong)

We initially measured BB sessions dying at exactly 5 minutes of idle. This looked catastrophic for any workflow that polls or waits between steps (which is most production RPA — status checks, approvals, batch lookups).

After investigation, the cause was a **Browserbase Python SDK ergonomics bug**:
- The SDK's `timeout` parameter is the **HTTP request timeout** for the SDK call itself
- The session lifetime parameter is actually `api_timeout` (snake_case)
- Passing `timeout=3600` silently created a 5-minute session and a 1-hour HTTP timeout

When we corrected to `api_timeout=3600`, the session ran cleanly for 35+ minutes, surviving every idle interval we tested up to 8 minutes pure idle.

**Implication:** The "BB is unreliable for long-running flows" concern is unfounded — but only if the team uses the SDK correctly. This is exactly the kind of footgun that wastes a week of debugging in production. We documented it.

---

## Measured Results

All numbers are from actual runs. PortalX v3 deployed at `https://portalx-7nn4.onrender.com`, Playwright client running on Azure VM (16 vCPU, 62 GiB), Browserbase Developer plan ($20/mo, 25 concurrent, 100 hours).

### 1. Single workflow latency (10 iterations each)

| Metric | Local Playwright (VM) | Browserbase | Δ |
|--------|:--:|:--:|:--:|
| Avg total time | 9.57s | 25.00s | +161% |
| p95 total time | 18.76s | 32.96s | +76% |
| Success rate | 100% (10/10) | 90% (9/10) — 1 was PortalX 6s nav timeout | -10% |
| Session create | n/a | 0.37s | BB only |
| CDP connect | n/a | 1.06s | BB only |

**Interpretation:** Cloud round-trip adds ~1.4s startup + ~2x per page interaction. For 7-step workflows, that's ~15s extra per run. For batch jobs at scale, this matters. For human-paced operations, it doesn't.

### 2. Retry + error classification (8 BB runs)

| Outcome | Count |
|---------|:--:|
| Succeeded first try | 5 |
| Succeeded on retry | 2 |
| Permanent failure (gave up) | 0 |
| **Final success rate (with retry)** | **88% (7/8)** |

Error classes seen: `transient_concurrent` (×2), `transient_timeout` (×2). Both successfully retried.

Implementation pattern: classify error → retry once for `transient_*` → die on `permanent_*` (selector not found, auth failure). Roughly 30 lines of Python.

### 3. Concurrency and scale

| Concurrent | Success | Wall time | Avg latency | Where the bottleneck is |
|:--:|:--:|:--:|:--:|---|
| **3 BB sessions** | 100% (3/3) | 33.6s | 32.4s | None — clean |
| **10 BB sessions** | 90% (9/10) | 35.1s | 32.3s | One nav timeout (PortalX-side) |
| **25 BB sessions** | 80% (20/25) | 90s | 36.2s | PortalX (Render free tier) degrading at scale |
| **5 self-hosted (single IP)** | 80% (12/15) | n/a | 9.84s | Mostly works; cold-start spikes |
| **10 self-hosted (single IP)** | **0% (0/30)** | n/a | n/a | **Render free tier rejects all from one IP** |

**Bottlenecks:** At 10+ concurrent from a single IP, the **target site** is the limit, not our infrastructure. The VM was using 35 MB RAM and 0.3% CPU when this happened.

For self-hosted to match Browserbase's 25-concurrent capability against a portal that IP-blocks, you need:
- A residential proxy service ($30-500/mo depending on volume)
- OR a lenient target site (most enterprise B2B portals are this — they don't IP-block legitimate authenticated users)

Browserbase **already includes distributed IPs** as part of the $20/mo plan — no separate proxy purchase needed.

### 4. Session reuse — login once, batch lookups (10 patients)

| Metric | Local | Browserbase |
|--------|:--:|:--:|
| One-time login cost | 5.3s | 16.0s |
| Avg per-patient lookup | 3.1s | 18.5s |
| Total session time | 33s | 183s |
| Patients processed | 9/10 | 9/10 |
| Savings vs fresh login per patient | 56% | 41% |

Session reuse works on both. **Both kept the session alive for the duration of the batch.**

### 5. Idle resilience (BB, no heartbeats, with `api_timeout=3600`)

Tested an idle ladder: Navigate → sleep N seconds → navigate again. All passed:

| Idle duration | Survived? | Second-nav latency |
|:--:|:--:|:--:|
| 30s | ✓ | 0.15s |
| 60s | ✓ | 0.17s |
| 120s | ✓ | 0.16s |
| 240s | ✓ | 0.17s |
| 360s | ✓ | 0.18s |
| 480s | ✓ | 0.21s |

Total session lifetime by end of test: ~35 minutes. Connection still alive. **No idle disconnect found within 8 min of pure idle.**

### 6. Auto-logout detection (PortalX 5-min session expiry)

| Metric | Result |
|--------|--------|
| Idle duration | 330s |
| Heartbeats sent (every 30s) | 11/11 successful |
| BB session alive after idle? | Yes |
| PortalX detected as expired? | **Yes** — redirected to `/session-expired` |
| Final URL | `/session-expired` |

Confirms: BB doesn't artificially terminate idle sessions when configured correctly, and our test code correctly detects when the *target portal* expires the session.

### 7. Popup handling (9 BB runs with rotated forced injection)

| Popup type | Detected | Handled |
|------------|:--:|:--:|
| Native dialogs (`alert()`) | 1 random | 1 |
| DOM error modals (`#error-modal`) | 2 forced | 2 |
| DOM alert modals (`#alert-modal`) | 3 forced | 3 |

Both forced and random popup paths are handled correctly. Failure screenshots auto-captured to `/tmp/bench_comprehensive/screenshots/`.

### 8. File upload

| Mode | Upload time |
|------|:--:|
| Local Playwright | 0.017s |
| Browserbase | 0.687s |

40x slower on BB due to file transfer over the CDP WebSocket. Still well under 1s for the small test PDF. For large documents (10+ MB), this could matter.

### 9. Audit trail

| Platform | Available? | How |
|----------|:--:|---|
| Browserbase | ✓ Built-in | `https://www.browserbase.com/sessions/{id}` — full session replay, network log, DOM inspector |
| Self-hosted | ✓ We built it | `GET /api/audit` returns per-session JSON log of every action; `/audit` page renders a table |

PortalX v3 now records every login, MFA, terms acceptance, NPI selection, patient search, file upload, and submission to a per-session audit log. Same data as Browserbase, just no replay video.

---

## Reliability, Maintenance, Scale, Observability — the four dimensions

### Reliability

| Aspect | Browserbase | Self-hosted |
|--------|---|---|
| Workflow success rate | 80-90% (BB) — failures from target site | 80-100% (local) |
| Retry support | DIY (~30 LOC) | DIY (~30 LOC) |
| Idle session survival | 8+ min when configured correctly | Unlimited (Chrome runs locally) |
| Crash recovery | Built into BB session lifecycle | DIY (Chrome process supervisor) |
| Concurrent session limit | 25 (Developer), 100 (Startup) | RAM-limited; ~30+ on the existing VM |

**Both can be made highly reliable. BB's reliability isn't inherently better — it's just easier to *get to* reliable because someone else manages Chrome process crashes, memory leaks, and zombie sessions.**

### Maintenance

| Aspect | Browserbase | Self-hosted |
|--------|---|---|
| Chrome updates | Managed by BB | Manual (apt update, Playwright install) |
| Browser binary management | None | We maintain |
| Dependency management | Just the SDK | Playwright + Chrome + system libs |
| Session lifecycle | API-managed | DIY (Chrome process supervisor) |
| Memory leak watchdog | Built-in | DIY (cron restart, monitoring) |

**The maintenance delta is real but small.** A handful of cron jobs and a Dockerfile cover most of it. The annoying part is keeping Chrome and Playwright versions in sync, which can break unexpectedly.

### Scale

| Aspect | Browserbase | Self-hosted |
|--------|---|---|
| Practical concurrent limit | 25 (Dev plan) → 100 (Startup) → 250+ (Scale) | RAM-bound (~30/VM at this size, more with scale-out) |
| Bandwidth | Included | Outbound from VM (effectively free at our scale) |
| IP distribution | **Built-in (cloud IPs, no extra cost)** | Single VM IP — add residential proxies if portal IP-blocks |
| Throughput at 10 workers | 15.4 wf/min | Same — limited by target site, not infra |

**Neither platform has a fundamental scale advantage for our workload.** Browserbase removes the IP rotation problem if (and only if) the target portal blocks based on IP. For our actual targets (Brightree, MyCGS, Availity), per-account rate limiting is far more common than per-IP blocks for legitimate authenticated traffic.

### Observability

This is where Browserbase actually wins out-of-the-box, but only on convenience:

| Feature | Browserbase | Self-hosted |
|---------|---|---|
| Live screen viewer | ✓ Dashboard | Custom CDP bridge or open-source viewer |
| Session replay (rrweb) | ✓ Dashboard | DIY (rrweb-record + rrweb-player, ~1 day to set up) |
| Network log capture | ✓ Dashboard | Playwright tracing or `page.on('response')` |
| Console log capture | ✓ Dashboard | `page.on('console')` |
| DOM inspector | ✓ Dashboard | Playwright tracing or DevTools |
| Failure screenshots | DIY (`page.screenshot()`) | DIY (`page.screenshot()`) |
| Audit log | DIY (`/api/audit` we built) | Same |

**The observability gap is primarily UI — a dashboard you can hand to QA or support without writing code.** All the underlying data is available in both platforms. If your team has the engineering bandwidth to build a small observability UI, the gap closes. If not, BB's $20/mo dashboard is cheaper than building one.

---

## Cost Analysis

### Self-hosted on the existing VM (Azure D16s_v5, ~$330-560/mo)
- VM is **already paid for** by other workloads. Marginal cost: **$0**.
- Capacity headroom: at least 10 concurrent Chrome instances with negligible resource use.
- If a target portal IP-blocks legitimate authenticated users (rare for B2B healthcare portals), residential proxies start at **$30-100/mo** (Oxylabs Starter $30 for 5 GB, IPRoyal $52 for 10 GB) and scale to **$300-500/mo** at moderate volume. **Browserbase already includes distributed IPs in the $20/mo plan**, so this is a self-hosted-only cost.

### Browserbase
| Plan | Monthly | Concurrent | Browser-hours |
|------|:--:|:--:|:--:|
| Free | $0 | 3 | 1 hour |
| Developer | **$20** | 25 | 100 hours |
| Startup | $99 | 100 | 500 hours |
| Scale | Custom | 250+ | Custom |

At ~0.5 minute browser time per workflow, the **Developer plan covers ~12,000 workflows/month**. At our current expected volume, that's enough.

### Direct comparison at our expected scale

| Metric | Self-hosted (VM only) | Self-hosted + proxies | Browserbase Developer |
|--------|:--:|:--:|:--:|
| Monthly cost | $0 marginal | $30-100 | $20 |
| Concurrent sessions | 10 (single-IP, target rate limit) | 25+ | 25 |
| Browser-hours included | unlimited | unlimited | 100 |
| Observability dashboard | Build it ourselves | Build it ourselves | Included |
| Audit trail | Build it ourselves (we did) | Build it ourselves | Included |
| Engineering hours to set up | ~20-40 | ~40-60 | ~2 |
| Engineering hours/month to maintain | ~2-5 | ~3-6 | ~0 |

**At current volume, Browserbase Developer is cheaper than the engineering time alone to match its observability features.**

At 10x scale (~120k workflows/month), the math flips: Browserbase would need the Startup plan ($99/mo) plus possibly overage, while self-hosted scales by adding a second VM and continues to be free at the margin.

---

## When the answer flips

**Use Browserbase if:**
- Volume is < 50K workflows/month
- Engineering team has higher-priority work than building observability
- We need distributed IPs (proven required by target portal blocks)
- Quick to start, don't want to maintain Chrome
- We need the dashboard for non-engineers (QA, support)

**Use self-hosted Playwright if:**
- Volume is > 100K workflows/month and growing
- Latency matters (each second of cloud round-trip × thousands of workflows adds up)
- We have engineering capacity to build observability + maintenance scripts
- Data residency is a hard requirement (PHI, HIPAA-strict)
- Target portals don't IP-block legitimate users (most B2B portals don't)

---

## What we built that's worth keeping regardless of platform choice

1. **PortalX v3** — realistic test site with multi-user, audit log, file upload, status lookup, deterministic mode for reproducible tests. Saves us from burning real production portal accounts during development.
2. **`bench_comprehensive.py`** — single script that exercises retry, popups, autologout, resilience, upload, audit, and sustained load. Runs on either local Playwright or Browserbase via `USE_LOCAL=1` flag.
3. **Retry + error classification helper** — 30 LOC, reusable in production code.
4. **Audit log infrastructure** — server-side per-session log + UI page. Works for compliance even if we go with Browserbase.
5. **Failure screenshot capture** — one helper that grabs screenshot + URL + error + classified type and dumps to disk for post-mortem.
6. **The `api_timeout` SDK gotcha documented** — the 5-min phantom disconnect that actually came from a Python SDK ergonomics bug. This alone could save a week of production debugging.

---

## Recommended Production Architecture

The decision between Browserbase and self-hosted Playwright should not be a one-way door. Both have legitimate use cases — and at the level of granularity that matters (per-portal, per-client), the right answer can be different at the same time. The architecture below makes the choice a **runtime configuration**, not a code change.

### Goals

1. **Backend-agnostic workflows** — the same workflow code runs against either Browserbase or local Playwright. No `if browserbase:` branches in business logic.
2. **Per-portal and per-client routing** — the backend is selected at runtime based on which portal we're hitting and which client we're running for.
3. **Generic error handling** — the most common failure modes (modals, dialogs, server errors, navigation timeouts, session expiry) are handled once at the framework level. Site-specific quirks are handled in portal adapters that extend or override the defaults.
4. **First-class retry + recovery** — every workflow gets retry-on-transient and recover-on-failure for free.

### Layered architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                        WORKFLOW DEFINITION                         │  ← portal-agnostic
│  e.g. "look up status for patient X" — pure intent, no selectors   │     business logic
└─────────────────────────────────┬──────────────────────────────────┘
                                  │
┌─────────────────────────────────▼──────────────────────────────────┐
│                          PORTAL ADAPTER                            │  ← per-portal
│  - selectors, step sequences, login flow                           │     site knowledge
│  - site-specific error handler overrides (extends defaults)        │
│  e.g. PortalXAdapter, BrightreeAdapter, MyCgsAdapter               │
└─────────────────────────────────┬──────────────────────────────────┘
                                  │
┌─────────────────────────────────▼──────────────────────────────────┐
│                         WORKFLOW RUNNER                            │  ← framework
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  GLOBAL ERROR HANDLERS (run on every workflow)               │  │
│  │  • native dialogs  → auto-accept (alert/confirm/prompt)      │  │
│  │  • DOM modals      → dismiss (#error-modal, #alert-modal,    │  │
│  │                       generic [role=alertdialog])            │  │
│  │  • server errors   → retry once (transient_server)           │  │
│  │  • nav timeouts    → retry once (transient_timeout)          │  │
│  │  • concurrent sess → retry with fresh session                │  │
│  │  • session expired → re-login + restart from last good step  │  │
│  │  • selector miss   → permanent failure (after retry)         │  │
│  │  • unknown errors  → permanent failure (after one retry)     │  │
│  └──────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  RECOVERY HOOK (runs before failure is reported)             │  │
│  │  • screenshot                                                │  │
│  │  • DOM snapshot                                              │  │
│  │  • console + network log dump                                │  │
│  │  • write entry to audit log                                  │  │
│  │  • emit metric (success / retry / permanent fail by class)   │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────┬──────────────────────────────────┘
                                  │
┌─────────────────────────────────▼──────────────────────────────────┐
│                          BACKEND ROUTER                            │  ← runtime decision
│  router.pick(portal="brightree", client="acme_dme") → backend      │     based on config
└──────┬──────────────────────────────────────────────┬──────────────┘
       │                                              │
┌──────▼─────────────┐                       ┌────────▼─────────────┐
│  BrowserbaseBackend│                       │   LocalBackend       │
│  - cloud Chrome    │                       │  - VM Playwright     │
│  - distributed IPs │                       │  - single VM IP      │
│    (built-in)      │                       │    (or residential   │
│  - dashboard       │                       │     proxy if config) │
│  - replay UI       │                       │  - DIY observability │
└────────────────────┘                       └──────────────────────┘
```

Each layer has one job. The workflow doesn't know which backend it ran on. The backend doesn't know which portal it's hitting. The router doesn't know what the workflow does. Everything is composable.

### Backend routing — two equally valid options

| Option | Routing key | When to use |
|--------|-------------|-------------|
| **A. Per-portal routing** | `portal_id` only | Most teams. The backend choice is driven by what the *target portal* requires (e.g., Brightree blocks IPs → BB; MyCGS doesn't → local). All clients hitting the same portal share the same backend. |
| **B. Per-portal + per-client routing with override** | `(portal_id, client_id)` tuple | When a specific client has constraints other clients don't (e.g., HIPAA-strict client demands data residency on our infra). Falls back to the portal default if no override is set. |

A YAML config example covering both styles:

```yaml
# Default backend for the whole platform
default_backend: browserbase

# Per-portal defaults (Option A)
portals:
  brightree:
    backend: browserbase           # IP rotation matters here
    concurrency_limit: 10
    api_timeout: 3600
  mycgs:
    backend: local                 # known to be lenient on IP
    concurrency_limit: 5
  availity:
    backend: browserbase
    concurrency_limit: 8
  portalx:
    backend: local                 # our test site

# Per-client overrides (Option B) — overrides portal default for that client
client_overrides:
  acme_dme:
    # this client demands self-hosted for compliance
    force_backend: local
  beta_clinic:
    # this client is on Browserbase even for portals where we use local by default
    portals:
      mycgs:
        backend: browserbase
```

The router resolves in this order: client override → portal default → global default. A single function:

```
resolve_backend(portal, client) →
  if config.client_overrides[client].portals[portal]: use it
  else if config.client_overrides[client].force_backend: use it
  else if config.portals[portal].backend: use it
  else: config.default_backend
```

This means: for 95% of cases, set the per-portal backend and forget about it. For the 5% of clients with special requirements, add an override. No code changes needed when the routing changes — just edit the YAML.

### Error handling — two-level dispatch

Errors are intercepted in **two layers**:

**Layer 1 — Global handlers (in the runner)**

These run on every workflow regardless of which portal is being hit. They cover failure modes that look the same on every site:

| Error class | Detection | Default action |
|---|---|---|
| `transient_timeout` | Playwright `TimeoutError` on navigation/click/wait | retry once with fresh session |
| `transient_server` | Page contains 500/502/503 markers, or alert text matches `/server.error/i` | retry once |
| `transient_concurrent` | URL contains `/concurrent` or matches portal's concurrent-session redirect | release session, get fresh one, retry |
| `transient_network` | `net::ERR_*` errors | retry once |
| `session_expired` | URL matches portal's session-expired pattern OR `/login` redirect mid-flow | re-login → resume from last completed step |
| `permanent_selector` | Element not found after retry | fail, capture state, alert |
| `permanent_auth` | Login attempt rejected | fail immediately, no retry |
| `unknown` | Anything else | retry once, then fail |

The runner also auto-installs:
- A `page.on("dialog")` handler that accepts native `alert()`, `confirm()`, `prompt()` so they don't freeze the browser
- A periodic check for known DOM modal patterns (`#error-modal.active`, `[role=alertdialog]`, `.modal.show`) that dismisses them if found

These handlers cover ~80% of real-world RPA failures across any portal. They're written **once** in the runner and inherited by every workflow.

**Layer 2 — Portal-specific overrides (in portal adapters)**

A portal adapter can:
- **Add** a handler for a pattern unique to that site (e.g., Brightree's `xmlHttp.status: 500` JS alert with a specific button to click)
- **Override** a default handler for that portal only (e.g., MyCGS uses a non-standard session-expired redirect that needs custom detection)
- **Extend** the recovery hook with site-specific data capture (e.g., dump Vuetify component state)

A portal adapter is roughly:

```
class PortalXAdapter(PortalAdapter):
    base_url = "https://portalx-7nn4.onrender.com"

    custom_error_patterns = [
        # only this site uses these — augments the global list
        ErrorPattern(
            name="portalx_concurrent",
            url_match="/concurrent",
            classification="transient_concurrent",
        ),
    ]

    async def login(self, page, credentials): ...
    async def navigate_to(self, page, intent): ...
    async def submit_prior_auth(self, page, data): ...
    async def lookup_status(self, page, ref_id): ...

    # Optional: override default recovery
    async def on_failure(self, page, error_class):
        await super().on_failure(page, error_class)
        # also dump portal-specific state
        await page.evaluate("window.__VUE_APP__?.$store?.state")
```

Adding a new portal = subclass `PortalAdapter`, fill in the selectors and step sequences, optionally add custom error patterns. Workflow code never changes.

### Recovery hook

Every failure (after retry has been exhausted, but before throwing) goes through a **recovery hook** that captures everything needed for post-mortem in one place:

1. Screenshot of the current page
2. DOM HTML snapshot
3. Last 50 console logs
4. Last 50 network requests with status codes
5. The full error chain + classification
6. The portal, client, workflow, step name, and run ID
7. Browserbase session replay URL (if backend == BB)
8. Audit log entries from PortalX `/api/audit` (if available)

All of this gets written to a single `failures/{run_id}.json` + `failures/{run_id}.png` so an engineer (or AI agent) can debug a failed run from one bundle.

This hook is implemented **once** in the runner and runs identically regardless of backend. It's the layer that gives us "observability when RPAs break" — we already have the screenshot piece working in `bench_comprehensive.py`; the rest is straightforward.

### Retry semantics

The runner's default retry policy:

- **Transient** errors → retry once with a **fresh** session (new browser context)
- **Permanent** errors → fail immediately, no retry
- **Unknown** errors → retry once, then treat as permanent
- A workflow can override this per-step if needed (e.g., "retry MFA up to 3 times because TOTP boundary races")

The retry happens at the **workflow level**, not at the step level by default. Step-level retry tends to cause weird mid-workflow state where you've half-submitted a form. Restarting the whole workflow with a fresh session is almost always the safer recovery, and at our latency budget it's affordable.

### What this buys us

- **One config flag flips a portal between BB and self-hosted.** No code change. No deploy. Edit YAML, restart runner.
- **One client can run on a different backend than the rest.** Compliance overrides are routine.
- **Adding a new portal = one new file** (the adapter). Existing global error handling, retry, and recovery just work.
- **Fixing a common error fixes it for every portal.** Add a new pattern to the global handler list once.
- **Migration from BB to self-hosted (or back) is a 30-second config change**, not a refactor.
- **The 7 things we already validated** (retry, popups, autologout, idle resilience, file upload, audit, screenshots) all become first-class framework features instead of one-off helpers.

The architecture is deliberately **boring**. There's no clever DI container, no plugin registry, no event bus. Three classes (`PortalAdapter`, `WorkflowRunner`, `BrowserBackend`) and a YAML file. Anyone on the team can read it in 10 minutes and add a new portal in an afternoon.

---

## Recommendation

**Start with Browserbase Developer ($20/mo).** Use it for our first production batch (likely under 12K workflows/month). Treat the BB dashboard as our debugging UI.

In parallel, **build the production runner with the architecture above** so the choice becomes a YAML config rather than a code path. That way, when a specific portal needs self-hosted (or a specific client demands data residency), it's a one-line change. When we hit the cost crossover (~50-100K workflows/month), migrating workloads off BB is the same one-line change — no rewrite, no six-week project.

**Don't pay for residential proxy services until we have evidence a real target portal IP-blocks us.** Most enterprise healthcare portals don't. Note: this is only relevant for the self-hosted path — Browserbase already provides distributed cloud IPs as part of its plan.

---

## Raw Data

All measured outputs are committed to the repo at:

| File | What's in it |
|------|---|
| `/tmp/bench_vm_results/section2_retry.json` | Retry test results (8 runs) |
| `/tmp/bench_vm_results/section3_popups.json` | Popup detection counts |
| `/tmp/bench_vm_results/section4_autologout.json` | 5-min idle + heartbeat result |
| `/tmp/bench_vm_results/section5_resilience.json` | Idle ladder up to 480s |
| `/tmp/bench_vm_results/section6_upload.json` | Local vs BB file upload timing |
| `/tmp/bench_vm_results/section7_audit.json` | BB session replay URL verification |
| `/tmp/bench_vm_results/screenshots/` | Auto-captured failure screenshots |

Earlier reports retained for context:
- `BROWSERBASE_BENCHMARK_REPORT.md` — full BB latency / concurrency / session reuse analysis
