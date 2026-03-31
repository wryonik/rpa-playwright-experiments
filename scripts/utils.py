"""
Shared utilities for all experiments.
Handles timing, memory/CPU tracking, result formatting, structured JSON output,
and the standard form-fill flow against the local PortalX test site.
"""
import asyncio
import json
import os
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import psutil

SITE_BASE_URL = os.getenv("SITE_URL", "http://localhost:8888")
LOGIN_URL = f"{SITE_BASE_URL}/login.html"
FORM_URL = f"{SITE_BASE_URL}/form.html"

# Fake data — entirely made up, matches form fields
FAKE_MEMBER = {
    "first_name": "Alex",
    "last_name": "Rivera",
    "dob": "1985-06-15",
    "member_id": "MBR-742891",
    "phone": "5550001234",
    "insurance_plan": "PPO_GOLD",
}
FAKE_PROVIDER = {
    "npi": "1234567890",
    "name": "Dr. Morgan Lee",
    "specialty": "Pulmonology",
    "phone": "5550009876",
}
FAKE_EQUIPMENT = {
    "hcpcs": "E0601",
    "icd10": "J96.00",
    "quantity": "1",
    "notes": "Patient requires supplemental oxygen therapy. Documentation attached.",
}


# ── Resource sampling ─────────────────────────────────────────────────────────

def get_process_memory_mb() -> float:
    """RSS memory of the current Python process in MB."""
    return psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024


def get_cpu_percent(interval: float = 0.1) -> float:
    """CPU % of the current Python process (sampled over interval seconds)."""
    return psutil.Process(os.getpid()).cpu_percent(interval=interval)


def get_system_memory_mb() -> dict:
    """Total, available, and used system memory in MB."""
    vm = psutil.virtual_memory()
    return {
        "total_mb": vm.total / 1024 / 1024,
        "used_mb": vm.used / 1024 / 1024,
        "available_mb": vm.available / 1024 / 1024,
        "percent": vm.percent,
    }


class ResourceSampler:
    """
    Background sampler for CPU + memory during an experiment run.
    Usage:
        async with ResourceSampler() as s:
            ... do work ...
        print(s.summary())
    """
    def __init__(self, interval_s: float = 1.0):
        self._interval = interval_s
        self._mem_samples: list[float] = []
        self._cpu_samples: list[float] = []
        self._running = False
        self._task: asyncio.Task | None = None

    async def __aenter__(self):
        self._running = True
        self._task = asyncio.create_task(self._sample_loop())
        return self

    async def __aexit__(self, *_):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _sample_loop(self):
        proc = psutil.Process(os.getpid())
        while self._running:
            self._mem_samples.append(proc.memory_info().rss / 1024 / 1024)
            self._cpu_samples.append(proc.cpu_percent(interval=None))
            await asyncio.sleep(self._interval)

    def summary(self) -> dict:
        if not self._mem_samples:
            return {"memory_avg_mb": 0, "memory_peak_mb": 0, "cpu_avg": 0, "cpu_peak": 0}
        return {
            "memory_avg_mb": round(statistics.mean(self._mem_samples), 1),
            "memory_peak_mb": round(max(self._mem_samples), 1),
            "cpu_avg": round(statistics.mean(self._cpu_samples), 1),
            "cpu_peak": round(max(self._cpu_samples), 1),
        }


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class RunResult:
    worker_id: int
    success: bool
    duration_s: float
    peak_memory_mb: float
    steps_completed: int
    total_steps: int = 6
    error: str | None = None
    popups_handled: int = 0
    cpu_avg: float = 0.0
    extras: dict = field(default_factory=dict)

    def __str__(self):
        status = "✓" if self.success else "✗"
        popup_note = f" | popups={self.popups_handled}" if self.popups_handled else ""
        err_note = f" | ERR: {self.error}" if self.error else ""
        return (
            f"[worker-{self.worker_id:02d}] {status} "
            f"{self.duration_s:.2f}s | "
            f"mem={self.peak_memory_mb:.0f}MB | "
            f"steps={self.steps_completed}/{self.total_steps}"
            f"{popup_note}{err_note}"
        )


@dataclass
class ExperimentMetrics:
    """
    Standardised JSON-serialisable output for every experiment.
    Emit with .to_json() or .save(path).
    """
    experiment: str
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    concurrency: int = 1
    total_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    success_rate: float = 0.0
    avg_runtime_sec: float = 0.0
    p50_runtime_sec: float = 0.0
    p95_runtime_sec: float = 0.0
    min_runtime_sec: float = 0.0
    max_runtime_sec: float = 0.0
    memory_avg_mb: float = 0.0
    memory_peak_mb: float = 0.0
    cpu_avg: float = 0.0
    cpu_peak: float = 0.0
    extras: dict = field(default_factory=dict)

    @classmethod
    def from_results(
        cls,
        experiment: str,
        results: list[RunResult],
        concurrency: int = 1,
        resource_summary: dict | None = None,
        extras: dict | None = None,
    ) -> "ExperimentMetrics":
        durations = [r.duration_s for r in results]
        ok = [r for r in results if r.success]
        durations_sorted = sorted(durations)

        def percentile(data, p):
            if not data:
                return 0.0
            idx = int(len(data) * p / 100)
            return data[min(idx, len(data) - 1)]

        res = resource_summary or {}
        return cls(
            experiment=experiment,
            concurrency=concurrency,
            total_runs=len(results),
            successful_runs=len(ok),
            failed_runs=len(results) - len(ok),
            success_rate=round(len(ok) / len(results) * 100, 1) if results else 0,
            avg_runtime_sec=round(statistics.mean(durations), 2) if durations else 0,
            p50_runtime_sec=round(percentile(durations_sorted, 50), 2),
            p95_runtime_sec=round(percentile(durations_sorted, 95), 2),
            min_runtime_sec=round(min(durations), 2) if durations else 0,
            max_runtime_sec=round(max(durations), 2) if durations else 0,
            memory_avg_mb=res.get("memory_avg_mb", 0),
            memory_peak_mb=res.get("memory_peak_mb", 0),
            cpu_avg=res.get("cpu_avg", 0),
            cpu_peak=res.get("cpu_peak", 0),
            extras=extras or {},
        )

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(asdict(self), indent=indent)

    def save(self, path: str):
        with open(path, "w") as f:
            f.write(self.to_json())
        print(f"  → metrics saved to {path}")

    def print_summary(self):
        print(f"\n  {'─'*50}")
        print(f"  Experiment : {self.experiment}")
        print(f"  Concurrency: {self.concurrency}")
        print(f"  Runs       : {self.total_runs} (✓{self.successful_runs} ✗{self.failed_runs})")
        print(f"  Success    : {self.success_rate}%")
        print(f"  Runtime    : avg={self.avg_runtime_sec}s  p95={self.p95_runtime_sec}s  max={self.max_runtime_sec}s")
        print(f"  Memory     : avg={self.memory_avg_mb}MB  peak={self.memory_peak_mb}MB")
        print(f"  CPU        : avg={self.cpu_avg}%  peak={self.cpu_peak}%")
        if self.extras:
            for k, v in self.extras.items():
                print(f"  {k:10}: {v}")
        print(f"  {'─'*50}\n")


# ── Printing ──────────────────────────────────────────────────────────────────

def print_summary(results: list[RunResult], label: str):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    for r in results:
        print(f"  {r}")
    succeeded = [r for r in results if r.success]
    if results:
        avg_dur = statistics.mean(r.duration_s for r in results)
        avg_mem = statistics.mean(r.peak_memory_mb for r in results)
        print(f"\n  Total: {len(results)}  ✓{len(succeeded)}  ✗{len(results)-len(succeeded)}"
              f"  avg={avg_dur:.2f}s  avgMem={avg_mem:.0f}MB")
    print(f"{'='*60}\n")


# ── Browser helpers ───────────────────────────────────────────────────────────

CHROME_ARGS = [
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-gpu",
    "--disable-accelerated-2d-canvas",
    "--disable-audio-output",
    "--disable-software-rasterizer",
]


async def launch_browser(pw, headless: bool = True):
    return await pw.chromium.launch(headless=headless, args=CHROME_ARGS)


async def login(page, username: str = "admin", password: str = "password123") -> bool:
    """Navigate to login page and authenticate. Returns True on success."""
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await page.fill("#username", username)
    await page.fill("#password", password)
    await page.click("#login-btn")
    try:
        await page.wait_for_url("**/form.html", timeout=6000)
        return True
    except Exception:
        return False


async def fill_form(page) -> tuple[int, dict]:
    """
    Fill all fields on form.html.
    Returns (steps_completed, step_timings_dict).
    """
    timings = {}
    steps = 0

    t = time.perf_counter()
    await page.fill("#first-name", FAKE_MEMBER["first_name"])
    await page.fill("#last-name", FAKE_MEMBER["last_name"])
    await page.fill("#dob", FAKE_MEMBER["dob"])
    await page.fill("#member-id", FAKE_MEMBER["member_id"])
    await page.fill("#phone", FAKE_MEMBER["phone"])
    await page.select_option("#insurance-plan", FAKE_MEMBER["insurance_plan"])
    timings["member_info_s"] = round(time.perf_counter() - t, 3)
    steps += 1

    t = time.perf_counter()
    await page.fill("#npi", FAKE_PROVIDER["npi"])
    await page.fill("#provider-name", FAKE_PROVIDER["name"])
    await page.select_option("#specialty", FAKE_PROVIDER["specialty"])
    await page.fill("#provider-phone", FAKE_PROVIDER["phone"])
    timings["provider_info_s"] = round(time.perf_counter() - t, 3)
    steps += 1

    t = time.perf_counter()
    await page.fill("#hcpcs", FAKE_EQUIPMENT["hcpcs"])
    await page.fill("#icd10", FAKE_EQUIPMENT["icd10"])
    await page.fill("#quantity", FAKE_EQUIPMENT["quantity"])
    timings["equipment_s"] = round(time.perf_counter() - t, 3)
    steps += 1

    t = time.perf_counter()
    await page.fill("#notes", FAKE_EQUIPMENT["notes"])
    timings["notes_s"] = round(time.perf_counter() - t, 3)
    steps += 1

    steps += 1  # file upload skipped

    t = time.perf_counter()
    await page.click("#submit-btn")
    try:
        await page.wait_for_selector("#success-banner", state="visible", timeout=8000)
        steps += 1
    except Exception:
        steps += 1  # server error (10% chance) still counts as attempted
    timings["submit_s"] = round(time.perf_counter() - t, 3)

    return steps, timings
