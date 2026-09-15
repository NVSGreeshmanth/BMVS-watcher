#!/usr/bin/env python3
"""
BMVS Slot Watcher
==================

Watches the Bupa Medical Visa Service (BMVS) appointment booking site for
open slots at specific assessment centres, and emails you the moment one
appears.

Target page: https://bmvs.onlineappointmentscheduling.net.au/oasis/Default.aspx
             (this leads to Location.aspx after you enter a postcode - the
             Location.aspx URL itself requires a live session, so this bot
             drives a real (headless) browser through the flow rather than
             just downloading the page.)

------------------------------------------------------------------------
BEFORE YOU RUN THIS
------------------------------------------------------------------------
1. Install dependencies:
       pip install playwright
       playwright install chromium

2. Set these environment variables (never hard-code your password):
       BMVS_POSTCODE          e.g. "2174"
       BMVS_TARGET_CENTRES    comma-separated names to watch, e.g.
                              "Charlestown,The Junction"
       BMVS_CHECK_MINUTES     how often to check, e.g. "30"
       SMTP_SERVER            e.g. "smtp.gmail.com"
       SMTP_PORT              e.g. "587"
       EMAIL_ADDRESS          the Gmail (or other) account sending alerts
       EMAIL_APP_PASSWORD     a Gmail "App Password" (NOT your normal
                               password - generate one at
                               https://myaccount.google.com/apppasswords)
       ALERT_TO_EMAIL         where the alert should be sent (can be the
                               same as EMAIL_ADDRESS)

   Any variable left unset (or set to an empty string, which is what
   GitHub Actions does for secrets you haven't created) falls back to the
   defaults below.

3. First run: use --debug once so you can WATCH the browser (headed mode)
   and confirm it actually reaches the results table. Booking sites like
   this change their HTML periodically, so the selectors below are
   written defensively (matching by visible text/role rather than brittle
   CSS ids), but you may still need to tweak SELECTOR NOTES marked below
   if the site has changed since this was written.

       python bmvs_slot_watcher.py --debug

4. Normal run (headless, loops forever, emails on change):
       python bmvs_slot_watcher.py

   Single check and exit (used by GitHub Actions):
       python bmvs_slot_watcher.py --once

------------------------------------------------------------------------
IMPORTANT - PLEASE READ
------------------------------------------------------------------------
- Many booking sites (especially government / visa-related ones) rate-limit
  or block automated traffic, and some prohibit scraping in their Terms of
  Use. You are responsible for checking BMVS's terms and using this
  reasonably. If you get CAPTCHA'd or blocked, stop and increase the
  interval, or check manually.
- State (what was seen last time) is saved to bmvs_state.json next to this
  script so it won't spam you with repeat emails for the same open slot,
  and so it remembers state across restarts.
"""

import asyncio
import json
import logging
import os
import random
import re
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout


def env(name: str, default: str | None = None) -> str | None:
    """Like os.environ.get, but treats empty strings as unset."""
    value = os.environ.get(name, "").strip()
    return value if value else default


# ------------------------------------------------------------------ config
START_URL = "https://bmvs.onlineappointmentscheduling.net.au/oasis/Default.aspx"

POSTCODE = env("BMVS_POSTCODE", "2174")
DEFAULT_CENTRES = (
    "Parramatta,Sydney,Canberra,Ingleburn,Bankstown,"
    "BMC - Blacktown,Blacktown,Charlestown,The Junction,"
    "Baulkham Hills,Corrimal,Dapto"
)
TARGET_CENTRES = [
    c.strip().lower()
    for c in env("BMVS_TARGET_CENTRES", DEFAULT_CENTRES).split(",")
    if c.strip()
]
CHECK_MINUTES = float(env("BMVS_CHECK_MINUTES", "30"))

# Centres that already show a far-out date rather than "No available slot" -
# for these we alert on any EARLIER date appearing, not just any date.
ALREADY_BOOKABLE_CENTRES = {"parramatta", "sydney", "canberra"}

SMTP_SERVER = env("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(env("SMTP_PORT", "587"))
EMAIL_ADDRESS = env("EMAIL_ADDRESS")
EMAIL_APP_PASSWORD = env("EMAIL_APP_PASSWORD")
ALERT_TO_EMAIL = env("ALERT_TO_EMAIL", EMAIL_ADDRESS)

STATE_FILE = Path(__file__).with_name("bmvs_state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bmvs_watcher")


# --------------------------------------------------------------- state io
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("State file corrupt, starting fresh.")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


# ---------------------------------------------------------------- email
def send_email(subject: str, body: str) -> None:
    if not (EMAIL_ADDRESS and EMAIL_APP_PASSWORD and ALERT_TO_EMAIL):
        log.error(
            "Email is not configured (EMAIL_ADDRESS / EMAIL_APP_PASSWORD / "
            "ALERT_TO_EMAIL missing) - printing alert instead:\n%s\n%s",
            subject,
            body,
        )
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = ALERT_TO_EMAIL

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
            server.sendmail(EMAIL_ADDRESS, [ALERT_TO_EMAIL], msg.as_string())
        log.info("Alert email sent to %s", ALERT_TO_EMAIL)
    except Exception as e:
        log.error("Failed to send email: %s", e)


# ------------------------------------------------------------- scraping
async def fetch_slots(debug: bool = False) -> list[dict]:
    """
    Drives the booking flow and returns a list of rows:
        [{"label": <first line>, "row_text": <full lowercased row text>,
          "availability": <text>}, ...]

    We keep the full row text (not just the first column) because on this
    site the first column is sometimes the nearby town/suburb (e.g.
    "Newcastle") rather than the actual centre name (e.g. "Charlestown
    Medical & Dental Centre") - matching against the whole row is more
    reliable than matching against just the label.
    """
    results: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not debug)
        page = await browser.new_page()

        try:
            log.info("Opening start page...")
            await page.goto(START_URL, wait_until="domcontentloaded", timeout=30000)

            # SELECTOR NOTE: the start page usually has a "Make a booking" /
            # "Book Now" style link/button. Try a few common labels.
            for label in ["make a booking", "book now", "book an appointment", "start"]:
                try:
                    btn = page.get_by_role("link", name=re.compile(label, re.I))
                    if await btn.count():
                        await btn.first.click()
                        break
                    btn = page.get_by_role("button", name=re.compile(label, re.I))
                    if await btn.count():
                        await btn.first.click()
                        break
                except Exception:
                    continue

            await page.wait_for_load_state("domcontentloaded")

            # SELECTOR NOTE: postcode/suburb input field - matched by
            # placeholder or nearby label text rather than a fixed id.
            postcode_input = page.get_by_placeholder(re.compile("postcode|suburb|town", re.I))
            if not await postcode_input.count():
                # fallback: first visible text input on the page
                postcode_input = page.locator("input[type='text']").first
            await postcode_input.fill(POSTCODE)

            if debug:
                await page.screenshot(path="debug_after_postcode.png")

            # SELECTOR NOTE: submit / search button
            submitted = False
            for label in ["search", "next", "find", "go"]:
                btn = page.get_by_role("button", name=re.compile(label, re.I))
                if await btn.count():
                    await btn.first.click()
                    submitted = True
                    break
            if not submitted:
                await postcode_input.press("Enter")

            # Wait for the results table to show up
            await page.wait_for_selector("table", timeout=20000)
            await page.wait_for_timeout(1500)  # let JS finish rendering rows

            if debug:
                await page.screenshot(path="debug_results.png", full_page=True)

            rows = page.locator("table tr")
            row_count = await rows.count()
            for i in range(row_count):
                row_text = (await rows.nth(i).inner_text()).strip()
                if not row_text:
                    continue
                # Availability is either "No available slot" or a date like
                # "Thursday 08/10/2026 08:45 AM"
                m = re.search(
                    r"(No available slot|[A-Za-z]+day \d{2}/\d{2}/\d{4}[^\n]*)",
                    row_text,
                )
                if not m:
                    continue
                availability = m.group(1).strip()
                label = row_text.splitlines()[0].strip()
                results.append(
                    {
                        "label": label,
                        "row_text": row_text.lower(),
                        "availability": availability,
                    }
                )

        except PWTimeout:
            log.error("Timed out waiting for a page element - site layout may have changed.")
            if debug:
                await page.screenshot(path="debug_timeout.png", full_page=True)
        finally:
            await browser.close()

    return results


# ------------------------------------------------------------------ main
def _parse_date(text: str):
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", text)
    if not m:
        return None
    day, month, year = (int(x) for x in m.groups())
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


async def run_once(debug: bool = False) -> None:
    log.info("Checking BMVS availability for: %s", ", ".join(TARGET_CENTRES))
    rows = await fetch_slots(debug=debug)

    if not rows:
        log.warning("No rows parsed - check debug screenshots / selectors.")
        return

    state = load_state()
    changed = []
    matched_row_ids = set()

    for wanted in TARGET_CENTRES:
        # pick the first row containing this name that hasn't already been
        # claimed by an earlier (more specific) target in this same run
        candidates = [
            (i, r) for i, r in enumerate(rows)
            if wanted in r["row_text"] and i not in matched_row_ids
        ]
        if not candidates:
            log.warning("Could not find a row matching '%s' on the page.", wanted)
            continue

        idx, row = candidates[0]
        matched_row_ids.add(idx)
        current = row["availability"]
        state_key = wanted  # keyed by the target name we searched for
        previous = state.get(state_key)
        log.info("%-25s (%-30s) %s", wanted, row["label"], current)

        is_regional = wanted not in ALREADY_BOOKABLE_CENTRES

        if is_regional:
            is_free_now = "no available slot" not in current.lower()
            was_free_before = previous is not None and "no available slot" not in previous.lower()
            if is_free_now and (previous is None or not was_free_before or previous != current):
                changed.append((row["label"], current))
        else:
            # Bupa centre: already shows a date - alert only if it got earlier
            new_date = _parse_date(current)
            old_date = _parse_date(previous) if previous else None
            if new_date and (old_date is None or new_date < old_date):
                changed.append((row["label"], current))

        state[state_key] = current

    save_state(state)

    if changed:
        lines = [f"- {name}: {avail}" for name, avail in changed]
        body = (
            "A slot has opened up at one or more watched BMVS centres:\n\n"
            + "\n".join(lines)
            + f"\n\nChecked at {datetime.now().isoformat(timespec='seconds')}\n"
            + "Book here: https://bmvs.onlineappointmentscheduling.net.au/oasis/Default.aspx"
        )
        send_email("BMVS slot available!", body)
    else:
        log.info("No new openings at watched centres.")


async def main_loop(debug: bool = False) -> None:
    while True:
        try:
            await run_once(debug=debug)
        except Exception as e:
            log.error("Unexpected error during check: %s", e, exc_info=debug)

        if debug:
            log.info("Debug run complete, exiting (loop skipped in --debug).")
            return

        # jitter the wait so requests don't land at exactly the same
        # second every time
        jitter = random.uniform(-2, 2)
        wait_minutes = max(1, CHECK_MINUTES + jitter)
        log.info("Sleeping for %.1f minutes...", wait_minutes)
        await asyncio.sleep(wait_minutes * 60)


if __name__ == "__main__":
    debug_mode = "--debug" in sys.argv
    once_mode = "--once" in sys.argv or debug_mode

    if once_mode:
        # Single check-and-exit - used for --debug, and for CI/GitHub
        # Actions runs (where a scheduled workflow does the "looping").
        asyncio.run(run_once(debug=debug_mode))
    else:
        try:
            asyncio.run(main_loop(debug=False))
        except KeyboardInterrupt:
            log.info("Stopped by user.")
