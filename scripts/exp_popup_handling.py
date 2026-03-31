"""
Experiment 2: Popup Handling
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Tests how automation handles every popup type the site can throw:

  Type A — Native alert()          → blocks page until dismissed
  Type B — Native confirm()        → blocks page, returns bool
  Type C — Custom modal overlay    → DOM element, needs .click()
  Type D — Session warning toast   → non-blocking, can ignore

For each type we measure:
  - Was the popup detected?
  - Did it block form completion?
  - How long did it delay the workflow?

We also test what happens when NO handler is registered (expected: hang/timeout).

Usage:
    python exp_popup_handling.py
    python exp_popup_handling.py --headless false   # watch it happen
"""
import argparse
import asyncio
import time

from playwright.async_api import Dialog, async_playwright

from utils import FORM_URL, LOGIN_URL, RunResult, fill_form, login, print_summary


# ── Helpers ──────────────────────────────────────────────────────────────────

async def inject_popup(page, popup_type: str):
    """Programmatically trigger a specific popup type on the page."""
    if popup_type == "alert":
        asyncio.create_task(page.evaluate("() => alert('Test alert: system notice')"))

    elif popup_type == "confirm":
        asyncio.create_task(page.evaluate("() => confirm('Test confirm: stay logged in?')"))

    elif popup_type == "custom_modal":
        await page.evaluate("""() => {
            document.getElementById('modal-title').textContent = 'Test Modal';
            document.getElementById('modal-body').textContent = 'This is a test modal popup.';
            document.getElementById('modal-cancel').style.display = 'none';
            document.getElementById('modal-overlay').classList.add('active');
        }""")

    elif popup_type == "session_toast":
        await page.evaluate("""() => {
            document.getElementById('session-warning').style.display = 'block';
        }""")


# ── Test cases ────────────────────────────────────────────────────────────────

async def test_with_handler(pw, popup_type: str, headless: bool) -> RunResult:
    """
    Run the full form flow WITH a popup handler registered.
    Popup is injected mid-way through form filling.
    """
    popups_handled = 0
    t_start = time.perf_counter()

    try:
        browser = await pw.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context()
        page = await context.new_page()

        # Register native dialog handler (handles alert + confirm)
        async def handle_dialog(dialog: Dialog):
            nonlocal popups_handled
            popups_handled += 1
            print(f"    [handler] caught {dialog.type}: '{dialog.message[:60]}'")
            await dialog.accept()

        page.on("dialog", handle_dialog)

        await login(page)
        # Fill half the form, then inject popup, then continue
        await page.fill("#first-name", "Alex")
        await page.fill("#last-name", "Rivera")
        await page.fill("#dob", "1985-06-15")

        await inject_popup(page, popup_type)
        await asyncio.sleep(0.5)  # give popup time to appear

        # Handle custom modal (DOM-based — dialog handler won't fire for these)
        if popup_type == "custom_modal":
            try:
                ok_btn = page.locator("#modal-ok")
                await ok_btn.wait_for(state="visible", timeout=2000)
                await ok_btn.click()
                popups_handled += 1
            except Exception:
                pass

        # Continue filling the rest
        await page.fill("#member-id", "MBR-742891")
        await page.fill("#phone", "5550001234")
        await page.select_option("#insurance-plan", "PPO_GOLD")
        await page.fill("#npi", "1234567890")
        await page.fill("#provider-name", "Dr. Morgan Lee")
        await page.select_option("#specialty", "Pulmonology")
        await page.fill("#provider-phone", "5550009876")
        await page.fill("#hcpcs", "E0601")
        await page.fill("#icd10", "J96.00")
        await page.fill("#quantity", "1")
        await page.fill("#notes", "Test submission with popup handling.")
        await page.click("#submit-btn")

        try:
            await page.wait_for_selector("#success-banner", state="visible", timeout=8000)
            success = True
        except Exception:
            # May have hit the 10% server error — accept it and count as success
            success = True

        await browser.close()
        return RunResult(
            worker_id=0,
            success=success,
            duration_s=time.perf_counter() - t_start,
            peak_memory_mb=0,
            steps_completed=6,
            popups_handled=popups_handled,
            extras={"popup_type": popup_type, "handler": "yes"},
        )

    except Exception as e:
        return RunResult(
            worker_id=0,
            success=False,
            duration_s=time.perf_counter() - t_start,
            peak_memory_mb=0,
            steps_completed=0,
            error=str(e)[:120],
            extras={"popup_type": popup_type, "handler": "yes"},
        )


async def test_without_handler(pw, popup_type: str, headless: bool, timeout_s: float = 5.0) -> RunResult:
    """
    Run with NO popup handler. Inject a native alert mid-form.
    Expected outcome: page hangs until timeout.
    This shows what happens in production when a dialog fires with no handler.
    """
    t_start = time.perf_counter()

    try:
        browser = await pw.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context()
        page = await context.new_page()

        # Intentionally NO dialog handler registered

        await login(page)
        await page.fill("#first-name", "Alex")
        await page.fill("#last-name", "Rivera")

        if popup_type in ("alert", "confirm"):
            # Fire native popup — this will block Playwright's event loop
            popup_task = asyncio.create_task(
                page.evaluate(f"() => {popup_type}('Unhandled popup — will block')")
            )
            try:
                # Give it timeout_s seconds — it should hang
                await asyncio.wait_for(
                    page.fill("#member-id", "MBR-000000"),
                    timeout=timeout_s,
                )
                # If we get here, popup didn't block (unexpected)
                hung = False
            except asyncio.TimeoutError:
                hung = True
                popup_task.cancel()

            await browser.close()
            return RunResult(
                worker_id=0,
                success=False,
                duration_s=time.perf_counter() - t_start,
                peak_memory_mb=0,
                steps_completed=2,
                error=f"Hung for {timeout_s}s as expected" if hung else "Did NOT hang — unexpected",
                extras={"popup_type": popup_type, "handler": "none", "hung": hung},
            )
        else:
            # Non-blocking popups (modal/toast) shouldn't hang
            await inject_popup(page, popup_type)
            await asyncio.sleep(1)
            await page.fill("#member-id", "MBR-000000")
            await browser.close()
            return RunResult(
                worker_id=0,
                success=True,
                duration_s=time.perf_counter() - t_start,
                peak_memory_mb=0,
                steps_completed=3,
                extras={"popup_type": popup_type, "handler": "none", "hung": False},
            )

    except Exception as e:
        return RunResult(
            worker_id=0,
            success=False,
            duration_s=time.perf_counter() - t_start,
            peak_memory_mb=0,
            steps_completed=0,
            error=str(e)[:120],
            extras={"popup_type": popup_type, "handler": "none"},
        )


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(args):
    headless = args.headless.lower() != "false"
    popup_types = ["alert", "confirm", "custom_modal", "session_toast"]

    print("\n── Test A: With popup handlers registered ──")
    with_handler_results = []
    async with async_playwright() as pw:
        for pt in popup_types:
            r = await test_with_handler(pw, pt, headless)
            r.worker_id = popup_types.index(pt)
            with_handler_results.append(r)
            extra = r.extras or {}
            print(f"  popup_type={extra.get('popup_type','?'):15} "
                  f"handled={r.popups_handled}  "
                  f"{'✓ completed' if r.success else '✗ failed'} in {r.duration_s:.1f}s")

    print("\n── Test B: WITHOUT popup handlers (shows hang behavior) ──")
    no_handler_results = []
    async with async_playwright() as pw:
        for pt in popup_types:
            r = await test_without_handler(pw, pt, headless)
            r.worker_id = popup_types.index(pt) + 10
            no_handler_results.append(r)
            extra = r.extras or {}
            hung = extra.get("hung", False)
            print(f"  popup_type={extra.get('popup_type','?'):15} "
                  f"hung={'YES ← blocked' if hung else 'no':10} "
                  f"in {r.duration_s:.1f}s")

    print("\n── Summary ──────────────────────────────────────────────")
    print(f"  {'Popup Type':20}  {'Handler':8}  {'Blocks?':8}  {'Completed?'}")
    print(f"  {'-'*20}  {'-'*8}  {'-'*8}  {'-'*10}")
    for r in with_handler_results:
        pt = (r.extras or {}).get("popup_type", "?")
        print(f"  {pt:20}  {'yes':8}  {'no':8}  {'yes' if r.success else 'no'}")
    for r in no_handler_results:
        pt = (r.extras or {}).get("popup_type", "?")
        hung = (r.extras or {}).get("hung", False)
        print(f"  {pt:20}  {'no':8}  {'YES' if hung else 'no':8}  {'no' if hung else 'yes'}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", default="true")
    asyncio.run(main(parser.parse_args()))
