"""
One-time MANUAL login into any job site, saved into the persistent browser
profile so the automation reuses the session and never has to fight a popup /
CAPTCHA / OTP login again.

This is the robust way to handle sites like Citi Workday whose sign-in opens in
a popup or has bot-detection: you log in by hand ONCE here, the cookies are
stored in BROWSER_PROFILE_DIR, and test_one_job.py / the daily pipeline then
land on the apply page already authenticated.

Usage (PowerShell):
    $env:BROWSER_PROFILE_DIR = "C:\\Users\\Ekadyu\\JobApp\\browser_profile"
    python manual_login.py "https://citi.wd5.myworkdayjobs.com/2"

Then log in by hand in the window that opens, and press Enter here.
You only need to do this once per site (until the cookie expires).
"""

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE_DIR = os.environ.get("BROWSER_PROFILE_DIR", "")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
CHROMIUM_BIN = os.environ.get("CHROMIUM_BIN", "")


def main() -> None:
    if not PROFILE_DIR:
        print("ERROR: set BROWSER_PROFILE_DIR first, e.g.")
        print(r'  $env:BROWSER_PROFILE_DIR = "C:\Users\Ekadyu\JobApp\browser_profile"')
        sys.exit(1)

    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.naukri.com"
    Path(PROFILE_DIR).mkdir(parents=True, exist_ok=True)
    print(f"Persistent profile: {PROFILE_DIR}")
    print(f"Opening: {url}\n")

    launch_kwargs = dict(
        user_data_dir=PROFILE_DIR,
        headless=False,
        args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
        user_agent=UA,
        viewport=None,            # use full window
        locale="en-IN",
        ignore_https_errors=True,
    )
    if CHROMIUM_BIN and Path(CHROMIUM_BIN).exists():
        launch_kwargs["executable_path"] = CHROMIUM_BIN

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(**launch_kwargs)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(url, timeout=60000)
        except Exception as exc:
            print(f"  navigation note: {exc}")

        print("=" * 64)
        print(" Log in BY HAND in the browser window now.")
        print(" - Click 'Sign In', enter your email + password in the popup.")
        print(" - Solve any CAPTCHA / OTP / 'verify it's you' challenge.")
        print(" - Get to the point where you are clearly logged in.")
        print("=" * 64)
        input("\nPress Enter here once you are logged in (this saves the session)... ")
        ctx.close()
        print("\nSession saved into the profile.")
        print("Now run test_one_job.py - it will land already signed in and skip login.")


if __name__ == "__main__":
    main()
