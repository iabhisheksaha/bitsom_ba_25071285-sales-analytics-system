"""
One-time login helper for local runs.

Opens a VISIBLE Chrome browser using the SAME persistent profile the daily
pipeline uses (BROWSER_PROFILE_DIR). Log into LinkedIn and Naukri manually
here — solve any CAPTCHA / OTP / device-check once. The cookies are saved in
the profile, so every later run of `python orchestrator.py` reuses the logged-in
session and never needs to log in (or face a CAPTCHA) again.

Run it once:
    # PowerShell
    $env:BROWSER_PROFILE_DIR = "$HOME\\JobApp\\browser_profile"
    python login_setup.py

When both sites show your logged-in homepage, press Enter in the terminal.
"""

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE_DIR = os.environ.get("BROWSER_PROFILE_DIR", "")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

SITES = [
    ("LinkedIn", "https://www.linkedin.com/login"),
    ("Naukri",   "https://www.naukri.com/nlogin/login"),
]


def main() -> None:
    if not PROFILE_DIR:
        print("ERROR: set BROWSER_PROFILE_DIR first, e.g.")
        print(r'  $env:BROWSER_PROFILE_DIR = "$HOME\JobApp\browser_profile"')
        sys.exit(1)

    Path(PROFILE_DIR).mkdir(parents=True, exist_ok=True)
    print(f"Using persistent profile: {PROFILE_DIR}\n")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            user_agent=UA,
            viewport={"width": 1366, "height": 768},
            locale="en-IN",
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for name, url in SITES:
            tab = page if name == SITES[0][0] else ctx.new_page()
            try:
                tab.goto(url, timeout=60000)
            except Exception as exc:
                print(f"  ({name}) navigation note: {exc}")
            print(f"  → {name}: log in in the opened tab.")

        print("\nLog into BOTH LinkedIn and Naukri in the browser window.")
        print("Solve any CAPTCHA / OTP. When both show your home feed, come back here.")
        input("\nPress Enter when done to save the session and close… ")
        ctx.close()
        print("Session saved. Daily runs will now reuse this login.")


if __name__ == "__main__":
    main()
