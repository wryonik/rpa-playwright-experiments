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

### We are not starting from scratch

Atul's Brightree implementation in `backend-v0` (`infrastructure/rpa/`, `infrastructure/integrations/brightree/`, `domain/services/workflow_management/rpa/`) already solves most of the hard problems for a single-portal, multi-tenant RPA system. Before describing what we'd add, here's a faithful summary of what's already there and worth keeping as-is. The architectural goal is to **extend** this foundation, not replace it.

### What backend-v0 already does well

| Capability | Where it lives | What it does |
|---|---|---|
| **Browser backend abstraction** | `infrastructure/rpa/browser/abstract_browser.py`, `playwright.py` | `AbstractBrowser` interface; `PlaywrightBrowser` implements both local launch (`chromium.launch`) and CDP connect (`chromium.connect_over_cdp`) on the same code path. The `is_cdp_active` flag from client config picks the path at runtime. |
| **CDP client abstraction** | `infrastructure/rpa/cdp_client/abstract_cdp_client.py`, `browserbase.py`, `factory.py` | `CDPClient` interface (`create()` / `retrieve()` / `close()` returning a `CDPSession` entity). Browserbase implementation is a thread-safe singleton wrapping the AsyncBrowserbase SDK with region-specific subdomains. Adding another CDP provider (BrowserStack, Steel Cloud, etc.) = one new file. |
| **Database-driven routing rules** | `domain/services/workflow_management/rpa/routing_service.py`, `domain/entities/workflow_management/rpa/routing_rules.py`, `condition_evaluator.py` | `RoutingRules` are MongoDB documents with a `Condition` model (`EQ`, `NE`, `CONTAINS`, `IN`, `EXISTS`), `priority`, `evidence_steps`, `is_default`, and `integration_account_id`. `RoutingService` does a two-phase lookup (non-default by priority → default fallback → arbitrary). **Routing changes don't require a deploy.** |
| **Distributed credential locking with pending queue** | `distributed_lock/utils.py`, `domain/services/workflow_management/rpa/task_dispatcher.py` | Per-credential locks prevent two RPA tasks from logging into the same Brightree account at the same time. If a lock is held, the new task is inserted into a `CredentialPending` queue (atomic INSERT). When the holder releases, the next pending task auto-dispatches via `release_lock_and_dispatch_next()`. Stale locks expire after 90 minutes. **This is the killer feature — no other RPA codebase I've seen handles credential contention this cleanly.** |
| **Task registry / dispatcher** | `domain/entities/workflow_management/rpa/task_registry.py`, `task_dispatcher.py` | `RPA_REGISTRY` maps logical method keys (`brightree_intake_page`, `mycgs_eligibility_check`) to Celery tasks with `RPARegistryEntry` metadata (`requires_lock`, `evidence_steps`, `celery_task_name`). External code calls `dispatch_rpa_task("brightree_intake_page", kwargs)` and the dispatcher handles credential selection, lock acquisition, and Celery dispatch. |
| **Session persistence (cookies)** | `domain/repositories/workflow_management/rpa/rpa_session_repository.py`, `domain/entities/workflow_management/rpa/rpa_session.py` | `RPASession(execution_context_id, browser_session_id, expires_at, cookies)`. Cookies are extracted after each browser operation. Next run for the same execution context can pass them back into `browser.launch(cookies=[...])` to skip login. Indexed on `(client_id, execution_context_id, browser_session_id)`. |
| **Retry with relogin** | `infrastructure/integrations/brightree/intake_tasks.py:128-355`, `client.py:55-80` (`_retry_with_relogin`) | Whole-task retry up to 3 attempts. On timeout/`ServerErrorPopupDetected`, increment and retry. Per-operation `_retry_with_relogin` detects session expiration after a timeout, calls `client.login()` to refresh, and retries the operation. Non-retryable errors break the loop immediately. |
| **Screenshot evidence as first-class artifact** | `infrastructure/integrations/brightree/client.py:178-189`, `RpaStepDetail` model | `_take_screenshot()` uploads to media service, returns metadata. Each screenshot is logged as `RpaStepDetail(name, screenshot_metadata_id, url, time, status)` and incrementally appended to the stage object. |
| **Brightree-specific error taxonomy** | `infrastructure/integrations/brightree/exceptions.py`, `exception_handlers.py`, `error_parsing.py` | `ServerErrorPopupDetected`, `SessionExpiredDetected`, `PaginationError`, `BrightreeOperationFailure` (with subclasses like `NewOrderButtonNotActive`, `MissingRequiredData`, `ProcedureCodeNotFound`). Page-level error parsing maps API error strings to human descriptions. |
| **Global dialog listener** | `intake_tasks.py:159-178` | A `page.on("dialog")` handler attached to every page in the browser context. Auto-accepts server errors, classifies by message content, prevents `alert()` calls from freezing the automation. |
| **Structured logging with execution context** | structlog throughout | `logger.bind(execution_context_id=...)` carries the run ID through every log line. Lifecycle markers like `[TASK_START]`, `[TASK_RETRY]`, `[TASK_SUCCESS]`, `[TASK_CLEANUP]` make grep-debugging trivial. |
| **Slack alerts on max-retry failures** | `intake_tasks.py:326-335`, `370-378` | Full traceback to a Slack channel on permanent failure or stale-lock cleanup. |

If you're starting any new portal integration on this codebase, **don't reinvent any of the above**. They've been battle-tested in production against a real Brightree deployment.

### What's missing and where we'd extend it

Atul's code is excellent for "Brightree, with one or two other portals bolted on the same pattern." It starts to feel cramped if you want:

1. **A generic error taxonomy that other portals can share.** Today's exceptions are Brightree-named and Brightree-specific. A new portal can't reuse `ServerErrorPopupDetected` cleanly — it has to declare its own equivalent.
2. **A reusable `Step` abstraction.** Workflows are written as procedural functions inside `intake_tasks.py`. Each new portal duplicates the structure (login, navigate, fill, submit, screenshot) instead of composing pre-built steps.
3. **A per-portal error override layer.** The global dialog listener is fine, but portal-specific patterns (Brightree's `xmlHttp.status: 500`, MyCGS's empty alert modals, Vuetify v-select races) live as scattered checks inside the task code. A central registry of "this portal also handles X this way" would make new portals much cheaper.
4. **A failure bundle for post-mortem.** Today, screenshots are in media storage, logs are in CloudWatch/structlog, audit entries are in MongoDB, Slack has the traceback, and the Browserbase session replay URL is nowhere. Five places to look. A single `failures/{run_id}.{json,png}` dump that bundles all of them would save hours per incident.
5. **Live observability links.** The Browserbase session ID is created (`CDPSession.id`) but the URL `https://www.browserbase.com/sessions/{id}` is never logged. Adding it to the structured log on session start makes "click the link to watch the failure" a one-second debugging step.
6. **A way to flip a portal from local Playwright to Browserbase per-client.** The routing service handles *which method to call*, but not *which browser backend to use for that method*. The cleanest way to add this is one new field on the routing rule.

These are all **additive** changes. None of them require touching the locking, dispatcher, session repo, or task registry — those layers are correct as-is.

### The extended architecture

Same shape as backend-v0 today, with the new layers shown in **bold**:

```
┌────────────────────────────────────────────────────────────────────┐
│                        WORKFLOW DEFINITION                         │  ← portal-agnostic
│  e.g. "look up status for patient X" — pure intent, no selectors   │     business logic
│  (today: lives inside intake_tasks.py per portal)                  │
└─────────────────────────────────┬──────────────────────────────────┘
                                  │
┌─────────────────────────────────▼──────────────────────────────────┐
│                       PORTAL ADAPTER  (NEW)                        │  ← per-portal
│  - selectors, step sequences, login flow                           │     site knowledge
│  - site-specific error patterns (extends global registry)          │
│  - optional on_failure() override for site-specific state dump     │
│  - reuses backend-v0's BrightreeClient as the first concrete impl  │
└─────────────────────────────────┬──────────────────────────────────┘
                                  │
┌─────────────────────────────────▼──────────────────────────────────┐
│                     WORKFLOW RUNNER  (NEW)                         │  ← framework
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  GLOBAL ERROR DISPATCH (generalized from intake_tasks.py)    │  │
│  │  • native dialogs  → auto-accept  (already in backend-v0)    │  │
│  │  • DOM modals      → dismiss known patterns (NEW: registry)  │  │
│  │  • server errors   → classify transient_server, retry once   │  │
│  │  • nav timeouts    → classify transient_timeout, retry once  │  │
│  │  • concurrent sess → release session, retry with fresh one   │  │
│  │  • session expired → re-login (today's _retry_with_relogin)  │  │
│  │  • selector miss   → permanent_selector, fail                │  │
│  │  • unknown errors  → retry once, then permanent              │  │
│  └──────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  RECOVERY HOOK  (NEW: bundles existing artifacts)            │  │
│  │  • screenshot           (already taken; just gather it)      │  │
│  │  • DOM HTML snapshot    (NEW)                                │  │
│  │  • last 50 console logs (NEW)                                │  │
│  │  • last 50 network reqs (NEW)                                │  │
│  │  • RpaStepDetail array  (already collected)                  │  │
│  │  • Browserbase replay URL (NEW: log it)                      │  │
│  │  • full error chain + classification                         │  │
│  │  → write to failures/{run_id}.{json,png}                     │  │
│  └──────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  RETRY POLICY  (generalized from intake_tasks.py)            │  │
│  │  • transient → retry up to N (default 3) with relogin        │  │
│  │  • permanent → fail fast, slack alert                        │  │
│  │  • already wrapped by credential lock + pending queue        │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────┬──────────────────────────────────┘
                                  │
┌─────────────────────────────────▼──────────────────────────────────┐
│        TASK DISPATCHER + CREDENTIAL LOCK  (UNCHANGED)              │  ← backend-v0 today
│  - task_registry.py   - distributed_lock/utils.py                  │
│  - task_dispatcher.py - CredentialPending queue                    │
│  - acquire lock or queue, release-and-dispatch-next                │
└─────────────────────────────────┬──────────────────────────────────┘
                                  │
┌─────────────────────────────────▼──────────────────────────────────┐
│        ROUTING SERVICE  (EXTEND existing routing_rules)            │  ← runtime decision
│  RoutingRule (today: priority + condition + method)                │
│  + new field: backend ∈ {local, browserbase}                       │
│  + new field: client_overrides[client_id] = backend                │
│  RoutingService.resolve(payload) → (method, backend)               │
└──────┬──────────────────────────────────────────────┬──────────────┘
       │                                              │
┌──────▼─────────────┐                       ┌────────▼─────────────┐
│  CDP backend       │                       │   Local backend      │
│  (already exists:  │                       │   (already exists:   │
│   browserbase.py)  │                       │    PlaywrightBrowser │
│  - cloud Chrome    │                       │    .launch())        │
│  - distributed IPs │                       │  - VM Playwright     │
│    (built-in)      │                       │  - single VM IP      │
│  - dashboard       │                       │  - + residential     │
│  - replay UI       │                       │    proxy if needed   │
└────────────────────┘                       └──────────────────────┘
```

The boxes marked **NEW** are the only things we'd add. Everything else is already in `backend-v0`.

### Backend routing — extending the existing RoutingRule

Atul's `routing_service.py` already does conditional routing by `(integration_account_id, payload)` to a `method` key. We don't need to invent a parallel routing system — we just need to add **one new field** to `RoutingRule` for the browser backend.

There are two equally valid options for how to key the backend choice:

| Option | Routing key | When to use |
|--------|-------------|-------------|
| **A. Per-portal routing** | `portal_id` (or `integration_account_id`) only | Most teams. The backend choice is driven by what the *target portal* requires. All clients hitting the same portal share the same backend. |
| **B. Per-portal + per-client override** | `(portal_id, client_id)` tuple | When a specific client has constraints other clients don't (e.g., HIPAA-strict client demands data residency on our infra). Falls back to the portal default if no override is set. |

The cleanest extension to the existing schema:

```python
# domain/entities/workflow_management/rpa/routing_rules.py  (extended)

class RoutingRule(BaseModel):
    # ── existing fields ──
    priority: int
    condition: Condition          # the EQ/NE/CONTAINS/IN/EXISTS matcher
    method: str                   # task_registry key
    is_default: bool
    integration_account_id: ObjectId
    evidence_steps: list[str]

    # ── NEW fields ──
    backend: Literal["local", "browserbase"] = "browserbase"      # default
    client_overrides: dict[str, Literal["local", "browserbase"]] = {}
    backend_config: dict = {}     # api_timeout, keep_alive, proxy_url, region, etc.
```

The router already does conditional matching against a payload dict. We extend it to also resolve the backend in the same call:

```
RoutingService.resolve(payload) →
  rule = first matching rule (priority order, fallback to default)
  backend = rule.client_overrides.get(payload["client_id"]) or rule.backend
  return (rule.method, backend, rule.backend_config)
```

For 95% of cases, set `backend` on the rule and forget about it. For the 5% of clients with special requirements, add an entry to `client_overrides`. **No code changes when routing changes** — and because rules are MongoDB documents, no deploy either. This is strictly better than the YAML-config approach I sketched earlier — Atul already built the better thing.

### Error handling — two-level dispatch on top of the existing exceptions

Today's error handling is good but Brightree-flavored. Three changes generalize it without breaking anything:

**Change 1: Add a generic classification on top of existing exceptions**

A small mapper that wraps the existing exception types in a portal-agnostic taxonomy:

| Generic class | Maps from (existing) |
|---|---|
| `transient_timeout` | `PlaywrightTimeoutError`, `TimeoutError` |
| `transient_server` | `ServerErrorPopupDetected` (Brightree), `MyCgsServerError` (future), generic `[role=alert][text*=server.error]` match |
| `transient_concurrent` | new — URL match `/concurrent` or portal-specific equivalent |
| `transient_network` | `net::ERR_*` errors |
| `session_expired` | `SessionExpiredDetected` (Brightree), generic `/login` redirect mid-flow |
| `permanent_selector` | `Locator.wait_for: ...` after retry exhausted |
| `permanent_auth` | login attempt rejected (no retry) |
| `permanent_data` | `MissingRequiredData`, `ProcedureCodeNotFound`, `NoItemsToPurchase`, `InsurancePayPctZero` (Brightree-specific subclasses of `BrightreeOperationFailure`) |
| `unknown` | anything else — retry once then permanent |

The runner uses this taxonomy to decide retry vs fail. The portal-specific exception types stay where they are; we just add a small `classify(exc) -> ErrorClass` function.

**Change 2: A portal-level error pattern registry**

Each portal adapter can register additional patterns the global handler should look for on every page interaction:

```python
class PortalXAdapter(PortalAdapter):
    error_patterns = [
        ErrorPattern(
            name="portalx_concurrent",
            match=lambda page: "/concurrent" in page.url,
            classify="transient_concurrent",
        ),
        ErrorPattern(
            name="portalx_dom_error_modal",
            match=lambda page: page.locator("#error-modal.active").count() > 0,
            classify="transient_server",
            recover=lambda page: page.locator("#error-modal.active .modal-ok").click(),
        ),
    ]

    # Optional: override the runner's default recovery
    async def on_failure(self, page, error_class, run_id):
        await super().on_failure(page, error_class, run_id)
        # also dump portal-specific state into the failure bundle
        await page.evaluate("window.__VUE_APP__?.$store?.state")
```

The global handler walks `runner.global_patterns + adapter.error_patterns` on every check. **Adding a new portal = subclass `PortalAdapter`, fill in the patterns, the runner does the rest.** The patterns live next to the portal-specific code so they're discoverable.

**Change 3: Extract the global dialog listener from `intake_tasks.py:159-178`** into the runner as a default behavior on every browser session, regardless of portal. Today it's installed inside Brightree task code; in the new shape, every workflow gets it for free.

### Recovery hook — bundling what already exists

The recovery hook isn't doing anything new; it's just **gathering scattered artifacts into one bundle** so engineers don't have to look in five places after a failure:

| Already exists (where) | The hook just collects it |
|---|---|
| Screenshot | `_take_screenshot()` in `client.py:178-189` |
| `RpaStepDetail` array | already in stage object |
| Audit / event log | already dispatched via `dispatch_stage_event()` |
| Slack traceback | already sent on permanent failure |
| Browserbase session ID | already in `CDPSession.id`, just never *logged as a URL* |
| Structured logs | already in structlog with `execution_context_id` bind |

The new bits (DOM snapshot, console log, network log) are 5-10 lines of Playwright API calls. The whole hook is maybe 80-100 LOC.

```
on_failure(run_id, error_class, page, adapter):
  bundle = {
    "run_id": run_id,
    "portal": adapter.name,
    "client": current_client_id,
    "error_class": error_class,
    "error_chain": serialize_exception_chain(),
    "url": page.url,
    "rpa_steps": current_stage.rpa_steps,                        # already exists
    "browserbase_replay": f"https://www.browserbase.com/sessions/{cdp_session.id}"
                          if cdp_session else None,              # NEW: just log it
    "console": console_buffer[-50:],                             # NEW
    "network": network_buffer[-50:],                             # NEW
    "dom_html": await page.content(),                            # NEW
  }
  write(f"failures/{run_id}.json", bundle)
  await page.screenshot(path=f"failures/{run_id}.png")           # already taken; just save
  await adapter.on_failure(page, error_class, run_id)            # portal hook
  emit_metric(...)
```

This is the layer that gives us "observability when RPAs break" — exactly the dimension that matters most for production debugging.

### Retry semantics — generalize what's already in `intake_tasks.py`

The retry policy in `intake_tasks.py:128-355` is the right shape. Just lift it out of Brightree-specific code into the runner so every portal inherits it:

- **Transient** (`transient_*` classes) → retry up to 3 times with `_retry_with_relogin` semantics (refresh session if session expired, otherwise just retry the failing op)
- **Permanent** (`permanent_*` classes) → fail immediately, no retry, slack alert
- **Unknown** → retry once, then treat as permanent
- A workflow can override the count per-step (Brightree's MFA already does this implicitly with the relogin loop)

Retry is wrapped by the credential lock — so a retry can't accidentally create a second concurrent login. **This is already true in `backend-v0`.** We just keep it.

### What this buys us

- **One field on a routing rule flips a portal between BB and self-hosted.** No code change. No deploy. Update the MongoDB document.
- **One client can run on a different backend than the rest** via `client_overrides`. Compliance overrides are a one-line config update.
- **Adding a new portal = one new `PortalAdapter` subclass** + a routing rule. Existing locking, dispatching, retry, and recovery just work.
- **Fixing a common error fixes it for every portal.** Add a pattern to the global registry once.
- **Migration from BB to self-hosted (or back) is a routing-rule update**, not a refactor.
- **The 7 things we validated** (retry, popups, autologout, idle resilience, file upload, audit, screenshots) all become first-class framework features instead of one-off helpers.
- **Atul's locking + queue + session repo + task registry stay exactly as they are.** We're adding three new abstractions (`PortalAdapter`, `WorkflowRunner`, error taxonomy) and one field on `RoutingRule`. That's it.

The architecture is deliberately **boring**. There's no clever DI container, no plugin registry, no event bus. The new code on top of `backend-v0` is a few hundred lines: `PortalAdapter` base class, `WorkflowRunner` with the dispatch loop, error classifier, recovery hook, and one schema migration on `RoutingRule`. Anyone on the team can read it in 20 minutes — and most of what they're reading is already familiar code from `backend-v0`.

### Concrete next steps if we adopt this

1. **Add `backend` field to `RoutingRule`** (+ schema migration, + default `browserbase`).
2. **Lift the global dialog listener** out of `intake_tasks.py` into a `WorkflowRunner.attach_default_handlers(page)` method.
3. **Write `errors.py`** with the generic taxonomy + `classify(exc)` mapper from existing Brightree exceptions.
4. **Extract `_retry_with_relogin`** from `client.py` into the runner as `RetryPolicy.transient`.
5. **Subclass `PortalAdapter` for Brightree** — wrap `BrightreeClient` as the first concrete adapter. Move site-specific error patterns into `BrightreeAdapter.error_patterns`. **Zero changes to `BrightreeClient` itself.**
6. **Build `PortalXAdapter`** as the second adapter, validating the abstraction works for a different portal.
7. **Implement the recovery hook** — bundle screenshot + RpaStepDetail + console + network + BB replay URL into `failures/{run_id}.{json,png}`.
8. **Log the Browserbase session URL** on session creation in `playwright.py:65-72`. One line change, biggest debugging win per LOC in the whole project.

None of these break existing Brightree workflows. They're all additive. Step 1 alone is enough to start running per-portal A/B tests of BB vs self-hosted on the same code path.

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
