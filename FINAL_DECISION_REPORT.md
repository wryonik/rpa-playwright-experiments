# RPA Platform Decision Report

**Date:** 2026-04-07
**Question:** For our healthcare RPA workload, do we use Browserbase ($20/mo Developer plan, scaling) or self-host Playwright on our existing VM — and how do we migrate the existing RPAs in `backend-v0` to a setup that lets us choose per-portal at runtime?

---

## TL;DR

1. **Both platforms can do everything we need.** The capability gap is near zero — we measured retries, popups, idle resilience, audit trails, file uploads, and concurrency on both. The differences are engineering effort vs subscription cost, not what's possible.
2. **Browserbase is "batteries included"** — live session viewer, recording, network/console capture, dashboard, distributed cloud IPs, managed Chrome. Each can be built on self-hosted, but each costs engineering hours.
3. **Self-hosted is ~3× faster per workflow** — no cloud round-trip. Matters for batch jobs, doesn't matter for status-check workloads with idle waits.
4. **At our current scale, BB Developer ($20/mo) is cheaper than building equivalent observability ourselves.** At 10× scale the math flips, but only if we're also paying for the engineering time.
5. **`backend-v0` already has the foundation for this**, but the two existing RPAs (Brightree and MyCGS) are written in two different styles. Migration is mostly about reconciling them to one shape, not greenfield work.
6. **The switch — local vs Browserbase — is one config field on `IntegrationAccount`**. Callers stop hardcoding `use_cdp=False`. Brightree gets there in a day. MyCGS is the harder lift because it bypasses `PlaywrightBrowser` entirely today.

---

## What we measured

All numbers are from actual runs against `https://portalx-7nn4.onrender.com` (a synthetic healthcare portal we built that mirrors the patterns in real DME prior-auth sites). Playwright client on Azure VM (16 vCPU, 62 GiB), Browserbase Developer plan (25 concurrent, 100 hours).

### Single workflow latency (10 iterations each)

| Metric | Local Playwright (VM) | Browserbase | Δ |
|---|:--:|:--:|:--:|
| Avg total time | 9.57s | 25.00s | +161% |
| p95 total time | 18.76s | 32.96s | +76% |
| Local success rate | 100% (10/10) | — | |
| BB success rate | — | 90% (9/10, one PortalX nav timeout) | |
| Session create | n/a | 0.37s | BB only |
| CDP connect | n/a | 1.06s | BB only |

Cloud round-trip adds ~1.4s startup + ~2× per page interaction. For our 7-step workflow that's ~15s extra per run. For batch jobs at scale that adds up; for human-paced status checks it doesn't.

### Concurrency

| Concurrent | Platform | Success | Wall time | Notes |
|:--:|---|:--:|:--:|---|
| 3 | Browserbase | 100% (3/3) | 33.6s | Clean |
| 10 | Browserbase | 90% (9/10) | 35.1s | One nav timeout (PortalX-side) |
| 25 | Browserbase | 80% (20/25) | 90s | PortalX (Render free tier) degrading at scale |
| 5 | Self-hosted (single VM IP) | 80% (12/15) | n/a | Mostly works, cold-start spikes |
| 10 | Self-hosted (single VM IP) | **0% (0/30)** | n/a | **Render free tier rejects all from one IP** |

The 10-worker collapse on self-hosted was the **target site** rejecting traffic from a single IP, not the VM choking. The VM was using 35 MB RAM and 0.3% CPU at the moment of total failure. We did **not** test self-hosted at 25 concurrent — at that level we'd need a residential proxy service, and we have no measurements for that scenario.

### Retry + error classification (8 BB runs, with one-retry-on-transient policy)

| Outcome | Count |
|---|:--:|
| Succeeded first try | 5 |
| Succeeded on retry | 2 |
| Permanent failure (gave up) | 0 |
| **Final success rate** | **7/8 = 88%** |

Error classes encountered: `transient_concurrent` (×2), `transient_timeout` (×2). Both fully recovered by one retry. Implementation pattern: classify on exception → retry once for `transient_*` → die on `permanent_*`. ~30 LOC.

### Session reuse (login once, batch 10 patient lookups in one session)

| Metric | Local | Browserbase |
|---|:--:|:--:|
| One-time login cost | 5.3s | 16.0s |
| Avg per-patient lookup | 3.1s | 18.5s |
| Total session time | 33s | 183s |
| Patients processed | 9/10 | 9/10 |
| Savings vs fresh login per patient | 56% | 41% |

Both platforms keep the session alive across the whole batch.

### Idle resilience (Browserbase, no heartbeats, with `api_timeout=3600`)

Tested an idle ladder: Navigate → sleep N seconds → navigate again. Every interval up to 8 minutes passed.

| Idle | Survived? | Second-nav latency |
|:--:|:--:|:--:|
| 30s | ✓ | 0.15s |
| 60s | ✓ | 0.17s |
| 120s | ✓ | 0.16s |
| 240s | ✓ | 0.17s |
| 360s | ✓ | 0.18s |
| 480s | ✓ | 0.21s |

Total session lifetime by end of test: ~35 minutes. **No idle disconnect found within 8 minutes of pure idle.**

### Auto-logout detection (PortalX has a 5-min server-side TTL)

| Metric | Result |
|---|---|
| Idle duration | 330s |
| CDP heartbeats sent every 30s | 11/11 successful |
| BB session alive after idle? | Yes |
| PortalX detected as expired? | **Yes** — redirected to `/session-expired` |

Confirms the BB session survived, and our test code correctly detected when the *target portal* expired the user session.

### Popup handling (9 runs with rotated forced-injection mode)

| Popup type | Detected | Handled |
|---|:--:|:--:|
| Native dialogs (`alert()`) | 1 random | 1 |
| DOM error modals (`#error-modal`) | 2 forced | 2 |
| DOM alert modals (`#alert-modal`) | 3 forced | 3 |

### File upload, audit trail

- **File upload:** local 0.017s, BB 0.687s (40× slower over CDP for the same small PDF — would matter for large clinical docs).
- **Audit trail:** BB has it built into the dashboard at `https://www.browserbase.com/sessions/{id}` (full replay, network log, DOM inspector). Self-hosted: we built `/api/audit` on PortalX as the equivalent. Same data, different UX.

---

## The `api_timeout` SDK gotcha (keep this for the team's bookmark file)

We initially measured BB sessions dying at exactly 5 minutes of idle. This looked catastrophic for any workflow that polls or waits between steps. Cause: a **Browserbase Python SDK ergonomics bug**:

- The SDK's `timeout` parameter is the **HTTP request timeout** for the SDK call itself.
- The **session lifetime** parameter is `api_timeout` (snake_case).
- Passing `timeout=3600` silently created a 5-minute session and a 1-hour HTTP timeout.

When we corrected to `api_timeout=3600`, sessions ran 35+ minutes including 8+ min pure idle. **This footgun would have wasted a week of production debugging if we'd hit it for the first time in prod.** Document it in the team wiki.

---

## Cost

### Self-hosted on the existing VM (Azure D16s_v5, ~$330–560/mo)

- VM is **already paid for** by other workloads. Marginal cost: **$0**.
- Capacity: at least 10 concurrent Chrome instances on the existing VM with negligible resource use (35 MB Python RAM, <3% CPU at 5 workers).
- If a target portal aggressively IP-blocks (we have no evidence Brightree, MyCGS, or Availity do this for legitimate authenticated users — but it's a possibility), residential proxies start at **$30–100/mo** (Oxylabs Starter $30/5 GB, IPRoyal $52/10 GB) and scale to **$300–500/mo** at moderate volume.

### Browserbase

| Plan | Monthly | Concurrent | Browser-hours |
|---|:--:|:--:|:--:|
| Free | $0 | 3 | 1 hr |
| **Developer** | **$20** | **25** | **100 hr** |
| Startup | $99 | 100 | 500 hr |
| Scale | Custom | 250+ | Custom |

At ~0.5 min browser time per workflow, the **Developer plan covers ~12,000 workflows/month**.

**A nuance to clear up:** Browserbase sessions run from BB's cloud data-center IP pool — multiple IPs, but **not residential**. Residential proxy rotation through BB is a separate `$12/GB` add-on. If a target portal blocks data-center IP ranges, BB's default IPs may also be blocked. So "BB has distributed IPs" is true (versus a single VM IP), but it is not the same as "BB has residential proxies." Don't conflate them.

### Comparison at our expected scale (~12K workflows/mo)

| Metric | Self-hosted (VM only) | Self-hosted + residential proxies | Browserbase Developer |
|---|:--:|:--:|:--:|
| Monthly cost | $0 marginal | $30–100 | $20 |
| Concurrent sessions | up to ~10 from a single IP | 25+ | 25 |
| Browser-hours included | unlimited | unlimited | 100 |
| Observability dashboard | DIY | DIY | Included |
| Audit trail UI | DIY | DIY | Included |
| Engineering hours to set up | 20–40 | 40–60 | ~2 |
| Engineering hours/month to maintain | 2–5 | 3–6 | ~0 |

At current volume, **Browserbase Developer is cheaper than the engineering time alone to match its observability features.** At 10× volume (~120K/mo), Browserbase needs the Startup plan ($99/mo) plus possibly overage; self-hosted scales by adding a second VM and stays effectively free. At that point the math flips, but only if you also count the engineering hours.

---

## How `backend-v0`'s existing RPAs work today

This is the part of the previous draft I had wrong. `backend-v0` does **not** have one unified RPA architecture. It has two — Brightree-style and MyCGS-style — and they share almost no infrastructure. Any migration plan has to handle both.

### Brightree style (the "infrastructure-first" pattern)

**Files:** `infrastructure/rpa/browser/`, `infrastructure/integrations/brightree/`, `domain/services/workflow_management/rpa/`, `domain/repositories/workflow_management/rpa/`

| Concept | Where | What it does |
|---|---|---|
| `AbstractBrowser` + `PlaywrightBrowser` | `infrastructure/rpa/browser/playwright.py:50` | Single `launch()` method that does either local Chromium or CDP-connect based on the `use_cdp` arg. |
| `CDPClient` interface + `BrowserbaseCDPClient` | `infrastructure/rpa/cdp_client/{abstract_cdp_client.py, browserbase.py, factory.py}` | Wraps the Browserbase SDK, returns a `CDPSession` entity. Selected by `client.config.cdp_client` (defaults to `"browserbase"`). |
| `RPASession` cookie persistence | `domain/repositories/workflow_management/rpa/rpa_session_repository.py` | Cookies are extracted after each browser run and stored. Next run can pass them back into `browser.launch(cookies=...)` to skip login. |
| Distributed credential lock + pending queue | `domain/services/distributed_lock/`, `task_dispatcher.py` | Per-credential lock prevents two RPA tasks from sharing one Brightree login. If held, the task goes into a `CredentialPending` queue and auto-dispatches when the lock releases. Stale locks expire after 90 min. |
| Task registry | `domain/entities/workflow_management/rpa/task_registry.py` | `RPA_REGISTRY` maps logical method keys (`brightree_intake_page`, `mycgs_prior_auth`, `mycgs_admc`, etc.) to Celery task names + `requires_lock` flag. |
| Routing rules (DB-backed) | `domain/services/workflow_management/rpa/routing_service.py`, `condition_evaluator.py` | `RoutingRule` is a MongoDB document with `name`, `priority`, `conditions: List[Condition]` (operators `EQ`/`NE`/`CONTAINS`/`IN`/`EXISTS`), `method`, `is_default`, `integration_account_id`. `RoutingService.match()` does a two-phase priority + default lookup against a payload dict. |
| Retry-with-relogin | `infrastructure/integrations/brightree/client.py:55` (`_retry_with_relogin`) and `intake_tasks.py:125-310` (whole-task retry loop, `max_retries=3`) | On `PlaywrightTimeoutError`, detect login expiry, refresh credentials, retry the operation. Whole-task retry wraps it. |
| Global dialog listener | `intake_tasks.py:177` | `page.on("dialog", handle_dialog)` attached per page. |
| Screenshot evidence | `client.py:1104` (`_take_screenshot`) → `RpaStepDetail` model → stage object | Screenshots are uploaded to media service and linked into `stage.rpa_steps`. |
| Brightree-specific exceptions | `exceptions.py` | `ServerErrorDialogDetected`, `ServerErrorPopupDetected`, `SessionExpiredDetected`, `PaginationError`, and `BrightreeOperationFailure` with subclasses (`NewOrderButtonNotActive`, `MissingRequiredData`, `ProcedureCodeNotFound`, `NoItemsToPurchase`, `InsurancePayPctZero`, `SalesOrderFlowNotImplemented`). |

**The catch:** every Brightree caller hardcodes `use_cdp=False`. There are at least 8 call sites (`intake_tasks.py:143`, `task_helpers.py:374, 758, 1224, 1497`, etc., plus `scripts/sync_dme_devices_rpa.py:201` and `parachute_rpa_task.py:122`). The CDP infrastructure exists but **nothing currently uses it.** Flipping Brightree to Browserbase is "stop hardcoding `False` and read from config" + cookie migration if needed.

### MyCGS style (the "fresh-implementation" pattern)

**Files:** `infrastructure/integrations/mycgs/{flows/, operations/, utils/}`, `application/tasks/integrations/mycgs/tasks.py`

MyCGS is a **completely separate codebase** from Brightree. It does not touch any of the infrastructure listed above.

| What MyCGS does | Where | Comment |
|---|---|---|
| Launches Playwright directly | `mycgs/operations/browser_setup.py:48` (`async_playwright().start()`) | Bypasses `PlaywrightBrowser`. **No CDP support at all today.** |
| Wipes all session state every run | `browser_setup.py:90-157` | Clears cookies, localStorage, sessionStorage, IndexedDB, caches. Fresh login every execution. **No `RPASessionRepository` use.** |
| Centralized retry utility | `mycgs/utils/retry_helpers.py:15` (`retry_operation()`) | `max_retries=3`, `delay=6.0 * attempt` (6s, 12s, 18s), `no_retry_exceptions` tuple. Used by login, MFA, dropdown waits, etc. **Cleaner than Brightree's inline retry.** |
| Domain-specific exceptions | `operations/login.py`, `mfa.py`, `navigation.py`, `program_selection.py` | `LoginBlockedError`, `MyCGSLoginError`, `MyCGSMFAError`, `MyCGSNavigationError`, `MyCGSSiteDownError`, `MyCGSCredentialNPIError`, `MyCGSDuplicatePriorAuthError`, `MyCGSQueryNotAvailableError`, `MyCGSSessionExpiredError`. |
| Credential rotation on lockout | `tasks.py:227-349` | On `LoginBlockedError`, retries with the next credential. Round-robin across `IntegrationAccount.credential_ids`, **MyCGS-specific** logic in `task_dispatcher.py:209-219`. |
| Site-down detection | `mycgs/operations/navigation.py:40-116` | Intercepts the `GetSupplierDetails` API response, raises `MyCGSSiteDownError` if `success: false`. |
| Modular code structure | `flows/` + `operations/` + `utils/` | Each operation is a separate file. **Closer to the "portal adapter" pattern we want than Brightree's monolithic client.** |

**The good:** MyCGS already has a centralized retry utility, domain-specific error taxonomy, credential rotation, and a site-down detector — none of which exist in the Brightree code.

**The bad:** None of this is reusable. It's hardcoded to MyCGS exceptions, MyCGS selectors, MyCGS endpoints. And it doesn't touch the CDP/PlaywrightBrowser infrastructure at all, so flipping it to Browserbase is a much bigger change than for Brightree — you'd have to replace `browser_setup.py` to go through `PlaywrightBrowser`.

### How dispatch actually flows today

There are **two paths** into RPA, and routing is only used by one:

1. **Workflow node path** (e.g. workflow engine triggers a node):
   `RPANodeRuntime.pre_process()` → `RoutingService().route(payload)` → returns a `method` key → `RPA_REGISTRY[method]` → `dispatch_rpa_task(method, kwargs)` → Celery task → browser launch.
2. **Direct dispatch path** (most everything else):
   `mycgs/tasks.py:109,160,336,349,365,378`, `brightree_sync_dispatcher.py`, `prior_auth_node_runtime.py`, GraphQL resolvers — all call `dispatch_rpa_task("hardcoded_method_key", kwargs)` directly. **Routing is never invoked.**

This matters for the migration: a "backend switch" that lives only on `RoutingRule` would only affect path 1. Path 2 needs the switch to live somewhere else — and the natural place is the `IntegrationAccount` itself (or a per-credential setting).

---

## MyCGS-specific pain points (the user's actual concerns)

You listed these. Here's what MyCGS does about them today and what's still missing:

| Concern | What MyCGS does today | What's missing / what BB would help with |
|---|---|---|
| **Dropdowns don't load** (program / equipment selectors) | Custom 15-second poll loop on the equipment dropdown enable (`program_selection.py:583-605`). 10s `wait_for_selector` on program category. | Hard-coded 15s ceiling. If the dropdown takes 16 seconds you fail. No adaptive timeout, no retry beyond that single poll. **Recovery via the unified retry helper would let us bump this to 30s for slow MyCGS days without touching every call site.** |
| **Website downtime — not detectable** | `navigation.py:40-116` intercepts the `GetSupplierDetails` API response, raises `MyCGSSiteDownError` if `success: false`. | Only checked after specific navigations. Other internal MyCGS APIs aren't monitored. No global health probe. **A network-event listener installed by the runner could catch any 5xx from any internal MyCGS endpoint and classify it consistently.** |
| **Website slow** | Hard-coded timeouts (10s for selectors, 30s for navigation) | No adaptive timeout, no per-portal SLO config. **With the runner's `RetryPolicy`, slow-portal mode becomes a config flag instead of code edits.** |
| **Backend API not working** | Only `GetSupplierDetails` is checked. Other API failures fail silently or as generic timeouts. | Same fix as "website downtime" — global network listener that classifies API failures per portal. |
| **Restricted concurrent sessions — can't reuse the same creds** | `LoginBlockedError` → credential rotation in `tasks.py:227-349`. Only works if you have multiple credentials. If a single creds set is locked, you're stuck waiting for it to free up. | **This is exactly where Browserbase's `keep_alive=true` helps.** If MyCGS just kicked you out because you tried to log in twice, but your previous session is still alive in Browserbase, you can `connect_over_cdp` to it again instead of attempting a fresh login that will be blocked. The session was never really lost — only the connection was. **But this only works if the previous run used Browserbase in the first place.** That's an argument for putting MyCGS specifically on Browserbase, not Brightree. |

The user's concern about MyCGS restricted sessions is the **strongest single argument we found in this whole exercise** for putting at least one specific portal on Browserbase. It's a property local Playwright structurally cannot offer (nobody to "reconnect" to after the worker dies).

---

## Why we want the switch

Different portals have different needs. What looks right for Brightree may not be right for MyCGS. The list below comes from what we measured and from the existing pain in `backend-v0`:

- **Per-portal latency tradeoff.** Brightree intake submits big forms that benefit from local Playwright's lower per-step latency. MyCGS prior auth waits on slow APIs and unreliable dropdowns where the BB recovery property matters more than raw speed.
- **Per-portal IP-block tolerance.** We have no evidence any of our targets currently IP-block. If one starts, BB cloud IPs are a quick mitigation; switching all portals together is overkill.
- **Per-portal observability needs.** A new portal we're still hardening (lots of failures, lots of debugging) benefits from BB's session viewer + replay. A stable portal that's been running for a year doesn't need that overhead.
- **Per-client compliance.** A specific client may demand data residency on our infrastructure (no cloud browser). They go on local; everyone else stays on whatever's default for the portal.
- **Cost ceilings.** At 12K workflows/mo BB Developer is cheaper than building observability ourselves. At 120K/mo it isn't. The migration path needs to support moving workloads off BB without a rewrite.
- **MyCGS keep-alive recovery.** As above — for one specific portal there's a structural reliability property only BB can give us.

---

## How we use them (the migration plan)

### The switch

The switch is one new field on `IntegrationAccount`:

```
IntegrationAccount.browser_backend: Literal["local", "browserbase"] = "local"
IntegrationAccount.browser_config: dict = {}
  # api_timeout, keep_alive, region, headless, etc.
```

Why on `IntegrationAccount`:

- It's the **one object both dispatch paths share**. Workflow-node path passes the account through `RoutingRule.integration_account_id`. Direct-dispatch path looks up the account by name. So both can read the field.
- It's already client-scoped (`BaseTenantDBModel`), so per-client overrides come for free — a client with a HIPAA constraint just gets a different `IntegrationAccount` row with `browser_backend="local"`.
- It's a MongoDB document, so changing the backend for a portal is one update, no deploy.

For the rare case where a single account needs different backends for different operations (e.g. Brightree intake on local but Brightree status check on BB), we add an optional override on the routing rule that wins over the account default:

```
RoutingRule.browser_backend_override: Optional[Literal["local", "browserbase"]] = None
```

That's it. No new tables. No YAML config. No DI container.

### Where the switch is read

There's one new utility, `BrowserBackendResolver`:

```
resolver.resolve(integration_account_id, routing_rule_id=None) -> BrowserBackendConfig
```

It returns `(backend, config)` after applying: routing-rule override → integration-account default → global default. Both call sites use it:

- **`PlaywrightBrowser` callers** (Brightree, parachute, sync scripts): instead of `await browser.launch(use_cdp=False)`, they call `await browser.launch(use_cdp=cfg.use_cdp, **cfg.launch_kwargs)` where `cfg = await resolver.resolve(...)`.
- **MyCGS `browser_setup.py`**: gets rewritten to go through `PlaywrightBrowser` instead of `async_playwright().start()` directly. Once it does, the same resolver call works.

### Step 1 — Get the switch in (no behavior change)

Do this first. It's purely additive and doesn't break anything.

1. Add `browser_backend` and `browser_config` fields to `IntegrationAccount` with default `"local"` / `{}`. Schema migration sets all existing accounts to `"local"` so behavior is unchanged.
2. Add `browser_backend_override` to `RoutingRule`, default `None`.
3. Write `BrowserBackendResolver` (~50 LOC) that does the lookup chain.
4. Replace the hardcoded `use_cdp=False` in **one** Brightree call site with `cfg = await resolver.resolve(account_id); use_cdp = cfg.use_cdp`. Verify the existing flow still works.
5. Roll out to the remaining Brightree call sites.

After step 5: Brightree can be flipped to Browserbase by updating one MongoDB document. No code change. We can A/B test on a single integration account.

### Step 2 — Generic error taxonomy

This is the layer we need for both portals to share retry/recovery logic.

1. Create `infrastructure/rpa/errors.py` with a small error taxonomy:
   - `transient_timeout`, `transient_server`, `transient_concurrent`, `transient_network`, `session_expired`
   - `permanent_selector`, `permanent_auth`, `permanent_data`
   - `unknown` (retry once then permanent)
2. Write `classify(exc) -> ErrorClass` that maps existing exceptions:
   - From Brightree: `ServerErrorDialogDetected`/`ServerErrorPopupDetected` → `transient_server`, `SessionExpiredDetected` → `session_expired`, `BrightreeOperationFailure` subclasses → `permanent_data`, `PlaywrightTimeoutError` → `transient_timeout`.
   - From MyCGS: `LoginBlockedError` → `transient_concurrent`, `MyCGSSiteDownError` → `transient_server`, `MyCGSSessionExpiredError` → `session_expired`, `MyCGSDuplicatePriorAuthError` → `permanent_data`, `MyCGSQueryNotAvailableError` → `transient_server`, `MyCGSCredentialNPIError` → `permanent_auth`.
3. Existing exception classes stay where they are. Nothing in Brightree or MyCGS code needs to change. The classifier is purely additive.

### Step 3 — Lift the runner abstractions out

Without breaking existing call sites, extract the cross-cutting concerns into a `WorkflowRunner` that wraps `PlaywrightBrowser`:

- **Default `page.on("dialog")` handler** (lifted from `intake_tasks.py:177`).
- **Default DOM modal sweeper** that handles the most common patterns (`#error-modal.active`, `#alert-modal.active`, `[role=alertdialog]`, `.modal.show`) before every action.
- **`RetryPolicy` class** that wraps `_retry_with_relogin` and the MyCGS `retry_operation` logic into one place. Reads error class from the classifier and decides retry vs fail.
- **Failure bundle hook** that, on permanent failure, gathers screenshot + DOM HTML + last 50 console logs + last 50 network requests + the BB session URL (if backend == BB) and writes them to `failures/{run_id}.{json,png}`. **One bundle per failure**, not five places to look.

Crucially: existing Brightree and MyCGS code keeps working. The runner is opt-in. New code uses it; old code migrates over time.

### Step 4 — The new RPA: one portal adapter as a worked example

Pick a new portal we need to add (or PortalX as a standin) and implement it from scratch using the new shape:

```
class PortalAdapter(ABC):
    name: str
    base_url: str
    error_patterns: list[ErrorPattern] = []   # additional patterns this portal cares about
    async def login(self, page, credentials): ...
    async def on_failure(self, page, error_class, run_id): ...   # optional override

class PortalXAdapter(PortalAdapter):
    name = "portalx"
    base_url = "https://portalx-7nn4.onrender.com"
    error_patterns = [
        ErrorPattern("portalx_concurrent",
                     match=lambda p: "/concurrent" in p.url,
                     classify="transient_concurrent"),
        ErrorPattern("portalx_dom_error",
                     match=lambda p: p.locator("#error-modal.active").count() > 0,
                     classify="transient_server",
                     recover=lambda p: p.locator("#error-modal.active .modal-ok").click()),
    ]
    async def login(self, page, creds): ...
    async def submit_prior_auth(self, page, data): ...
    async def lookup_status(self, page, ref_id): ...
```

The runner walks `runner.global_patterns + adapter.error_patterns` on every interaction. Workflow code lives in adapter methods. The runner provides retry, recovery, the failure bundle, and the backend choice. Adding a new portal = one new file plus a routing rule.

### Step 5 — Migrate Brightree (small)

Brightree already uses `PlaywrightBrowser`. The migration is mechanical:

1. Create `BrightreeAdapter` that wraps `BrightreeClient` — no logic change, just exposes the methods through the adapter interface and pushes Brightree's exception types into `error_patterns`.
2. Replace whole-task retry in `intake_tasks.py:125-310` with a `WorkflowRunner` call. The runner's `RetryPolicy` already does what `_retry_with_relogin` does.
3. Lift the global dialog listener from `intake_tasks.py:177` (the runner installs it).

After this, Brightree workflows benefit from the failure bundle and the BB switch. Zero change to `BrightreeClient` itself.

### Step 6 — Migrate MyCGS (bigger)

MyCGS doesn't use `PlaywrightBrowser` today, so this is the harder migration. In rough order:

1. Rewrite `mycgs/operations/browser_setup.py` to call `PlaywrightBrowser.launch()` instead of `async_playwright().start()` directly. Preserve the session-clearing behavior as an option (`launch(clear_session=True)` if needed).
2. Wrap `MyCGSPriorAuthFlow` and `MyCGSAdmcFlow` as `MyCGSAdapter` methods.
3. Move MyCGS exceptions into `error_patterns` so the unified runner can classify them.
4. Switch the centralized `retry_operation()` calls to the runner's `RetryPolicy`. The MyCGS retry behavior (6s/12s/18s backoff) becomes a `RetryPolicy.from_mycgs_defaults()` preset so we don't lose its existing tuning.
5. **Now MyCGS can also flip to Browserbase via the IntegrationAccount field.** Once it does, set `keep_alive=true` and the "restricted sessions" pain becomes a reconnect instead of a credential rotation.

### Step 7 — Log the BB session URL on creation

One-line addition to `playwright.py:65-72`:

```python
self._cdp_session = await cdp_client.create()
logger.info("[BROWSER_CDP_SESSION_CREATED]",
            session_id=self._cdp_session.id,
            replay_url=f"https://www.browserbase.com/sessions/{self._cdp_session.id}")
```

Smallest LOC, biggest debugging win in the whole project. Every BB-backed run has a one-click link to the replay UI in the structured log.

### What stays unchanged

- `domain/services/distributed_lock/` — the credential lock + pending queue is correct, doesn't need touching
- `RPA_REGISTRY` and `task_dispatcher.dispatch_rpa_task()` — the registry still maps method keys to Celery tasks
- `RPASession` cookie persistence (used by Brightree) — unchanged, just becomes opt-in for new portals
- All existing tests that exercise Brightree or MyCGS today should pass without modification. The migration is additive at every step.

---

## Recommendation

**Start with the switch.** Steps 1 and 7 above can ship in a few days and don't require any portal-side changes. Once they're in:

1. Flip Brightree to Browserbase on a single staging integration account, run a real workload, compare against the local baseline. Roll forward or back via the database.
2. Then do steps 2 and 3 (error taxonomy + runner) which unblock the bigger work.
3. Build the **new portal we need to add** as the first adapter using the new shape. This validates the architecture against an actual production need rather than a refactor.
4. Migrate Brightree to the adapter shape — small lift since it's already on `PlaywrightBrowser`.
5. Migrate MyCGS to the adapter shape — bigger lift, but the payoff is huge. Once MyCGS is on Browserbase with `keep_alive=true`, the restricted-sessions pain stops being a manual problem.

**Use Browserbase Developer ($20/mo)** for the staging A/B and for portals where the observability dashboard or `keep_alive` recovery matters. Use **local Playwright** for portals where latency dominates and the existing flow is already stable. **Don't pay for residential proxies** until we have evidence a real target portal IP-blocks us — and even then, evaluate it only on the self-hosted path.

The whole architecture is deliberately additive. Steps 1, 2, 7 ship behind feature flags with no risk. Steps 3–6 each migrate one portal at a time. At every point in the migration, every existing workflow keeps running on the same code path it's on today, and we can roll back any individual portal by flipping its `IntegrationAccount.browser_backend` field.

---

## Raw data

| File | Description |
|---|---|
| `BROWSERBASE_BENCHMARK_REPORT.md` | Per-step latency, concurrency ladder, session reuse |
| `bench_comprehensive.py` | All 8 sections (timeout, retry, popups, autologout, resilience, upload, audit, sustained) |
| `/tmp/bench_vm_results/section{2..7}.json` | Raw JSON from each measured benchmark |
| `/tmp/bench_vm_results/screenshots/` | Auto-captured failure screenshots |
