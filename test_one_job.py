"""
End-to-end test: apply to a single job URL without running the full pipeline.
Skips discovery and resume tailoring — uses base_resume.docx directly.

Run from PowerShell (load secrets first):
    Import-Module CredentialManager
    $env:CRED_KEY            = (Get-StoredCredential -Target JobApp_CRED_KEY).GetNetworkCredential().Password
    $env:BROWSER_PROFILE_DIR = "C:\\Users\\Ekadyu\\JobApp\\browser_profile"
    $env:HEADLESS            = "false"
    $env:PYTHONIOENCODING    = "utf-8"
    python test_one_job.py <job_url> [company_name] [job_title] [--ats-user u] [--ats-pass p]

Example (Workday):
    python test_one_job.py "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Pune/VP-Product_JR-1" "Acme Corp" "VP Product" --ats-user you@email.com --ats-pass YourPass
"""

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


class _Tee:
    """Mirror stdout/stderr to a log file so the full run can be shared as one file."""
    def __init__(self, stream, fh):
        self._stream = stream
        self._fh = fh

    def write(self, data):
        try:
            self._stream.write(data)
        except Exception:
            pass
        try:
            self._fh.write(data)
            self._fh.flush()
        except Exception:
            pass

    def flush(self):
        for t in (self._stream, self._fh):
            try:
                t.flush()
            except Exception:
                pass


def _start_logging() -> Path:
    Path("logs").mkdir(exist_ok=True)
    log_path = Path("logs") / "citi_run.log"
    fh = open(log_path, "w", encoding="utf-8")
    fh.write(f"=== test_one_job run {datetime.now().isoformat()} ===\n")
    sys.stdout = _Tee(sys.stdout, fh)
    sys.stderr = _Tee(sys.stderr, fh)
    return log_path

import yaml
from playwright.sync_api import sync_playwright

from agents.agent1_job_discovery import JobPosting
from agents.agent3_application import (
    ApplicationLog,
    HEADLESS,
    BROWSER_PROFILE_DIR,
    _BROWSER_UA,
    _ATS_HANDLER_MAP,
    _NO_LOGIN_ATS,
    _load_applicant_profile,
    detect_ats,
    detect_captcha,
    ApplicationAgent,
)
from agents.agent4_credentials import CredentialManager


def _open_browser(playwright):
    args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
            "--ignore-certificate-errors", "--disable-blink-features=AutomationControlled"]
    if BROWSER_PROFILE_DIR and Path(BROWSER_PROFILE_DIR).exists():
        ctx = playwright.chromium.launch_persistent_context(
            user_data_dir=BROWSER_PROFILE_DIR,
            headless=HEADLESS,
            args=args,
            user_agent=_BROWSER_UA,
            viewport={"width": 1366, "height": 768},
            locale="en-IN",
            ignore_https_errors=True,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        print(f"  [Test] Persistent profile: {BROWSER_PROFILE_DIR}  headless={HEADLESS}")
        return ctx, page, ctx.close

    browser = playwright.chromium.launch(headless=HEADLESS, args=args)
    ctx = browser.new_context(
        user_agent=_BROWSER_UA,
        viewport={"width": 1366, "height": 768},
        locale="en-IN",
        ignore_https_errors=True,
    )
    page = ctx.new_page()
    print("  [Test] Ephemeral browser (no saved profile found).")
    return ctx, page, browser.close


def main():
    parser = argparse.ArgumentParser(description="Apply to a single job URL end-to-end.")
    parser.add_argument("url",     help="Full ATS job URL")
    parser.add_argument("company", nargs="?", default="Test Company", help="Company name")
    parser.add_argument("title",   nargs="?", default="VP Product Owner", help="Job title")
    parser.add_argument("--ats-user", default="", help="Username/email for ATS login")
    parser.add_argument("--ats-pass", default="", help="Password for ATS login")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fill forms but do NOT click the final Submit button")
    args = parser.parse_args()

    log_path = _start_logging()
    print(f"[Test] Full log being written to: {log_path.resolve()}")
    print(f"[Test] git HEAD:")
    try:
        import subprocess
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
        subj = subprocess.run(["git", "log", "-1", "--format=%s"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
        print(f"[Test]   {head}  {subj}")
    except Exception as _e:
        print(f"[Test]   (could not read git HEAD: {_e})")

    resume = Path("resume/base_resume.docx")
    if not resume.exists():
        print(f"[Test] ERROR: {resume} not found. Copy your real CV there first.")
        sys.exit(1)

    with open("config/config.yaml") as f:
        config = yaml.safe_load(f)

    profile = _load_applicant_profile()
    print(f"\n[Test] Applicant : {profile.get('name')} / {profile.get('email')} / {profile.get('phone')}")
    print(f"[Test] Resume    : {resume.resolve()}")
    print(f"[Test] Target    : {args.company} | {args.title}")
    print(f"[Test] URL       : {args.url}")

    ats_label = detect_ats(args.url) or "generic"
    print(f"[Test] ATS type  : {ats_label}")
    if args.dry_run:
        print("[Test] DRY-RUN   : forms will be filled but final Submit will NOT be clicked.")
    print()

    if ats_label not in _ATS_HANDLER_MAP:
        print(f"[Test] WARNING: no dedicated handler for '{ats_label}' — attempting GenericAtsHandler.")

    job = JobPosting(
        platform=ats_label,
        title=args.title,
        company=args.company,
        location="Pune",
        url=args.url,
    )

    # Try to get ATS credentials from credentials.enc if not passed on CLI
    ats_user = args.ats_user
    ats_pass = args.ats_pass
    if not ats_user and ats_label not in _NO_LOGIN_ATS:
        cred_key_str = os.environ.get("CRED_KEY", "")
        if cred_key_str:
            try:
                mgr = CredentialManager("config/credentials.enc")
                site_creds = mgr.get_site_credentials(ats_label, cred_key_str.encode())
                ats_user = site_creds.get("username", "")
                ats_pass = site_creds.get("password", "")
                print(f"  [Test] Loaded {ats_label} credentials from credentials.enc.")
            except Exception:
                pass
        if not ats_user:
            print(f"  [Test] No credentials for {ats_label}. "
                  f"Re-run with --ats-user <email> --ats-pass <password>")
            if ats_label not in _NO_LOGIN_ATS:
                print("  [Test] This ATS likely needs login. Proceeding anyway...")

    with sync_playwright() as pw:
        ctx, page, closer = _open_browser(pw)
        try:
            handler_cls = _ATS_HANDLER_MAP.get(ats_label)
            if not handler_cls:
                print(f"  [Test] No handler for {ats_label} in _ATS_HANDLER_MAP.")
                print(f"  [Test] Opening URL in browser for manual inspection...")
                page.goto(args.url, timeout=40000, wait_until="domcontentloaded")
                print(f"  [Test] Page loaded: {page.url}")
                print(f"  [Test] Pausing 30s for inspection. Press Ctrl+C to exit.")
                time.sleep(30)
                return

            handler = handler_cls(page)

            # Navigate to job page first
            print(f"  [Test] Navigating to job page...")
            page.goto(args.url, timeout=40000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            print(f"  [Test] Current URL: {page.url}")

            if detect_captcha(page):
                print("  [Test] CAPTCHA detected. Solve it in the browser window.")
                print("  [Test] Press Enter here once solved...")
                input()

            # Login if needed
            if ats_user and ats_label not in _NO_LOGIN_ATS:
                print(f"  [Test] Logging into {ats_label} as {ats_user}...")
                logged_in = handler.login(ats_user, ats_pass)
                print(f"  [Test] Login: {'OK' if logged_in else 'FAILED (continuing anyway)'}")
                page.wait_for_timeout(2000)
            else:
                handler.login("", "")  # Greenhouse/Lever always return True from login()

            if args.dry_run:
                print("  [Test] DRY-RUN: would call handler.apply() here. Pausing 15s...")
                time.sleep(15)
                print("  [Test] DRY-RUN complete.")
                return

            print(f"  [Test] Applying...")
            success = handler.apply(job, resume)
            print(f"\n[Test] Result: {'*** SUBMITTED ***' if success else 'FAILED'}")
            if not success:
                try:
                    shot = Path("logs") / "last_failure.png"
                    shot.parent.mkdir(exist_ok=True)
                    page.screenshot(path=str(shot), full_page=True)
                    print(f"[Test] Saved screenshot of failure state -> {shot.resolve()}")
                    print("[Test] Send me that PNG and the lines above to debug.")
                except Exception as _e:
                    print(f"[Test] (could not save screenshot: {_e})")

        except KeyboardInterrupt:
            print("\n[Test] Interrupted by user.")
        except Exception as exc:
            import traceback
            print(f"\n[Test] ERROR: {exc}")
            traceback.print_exc()
        finally:
            print("\n" + "=" * 60)
            print("[Test] SEND ME THIS FILE:  logs\\citi_run.log")
            print("[Test] (and logs\\last_failure.png if it failed)")
            print("=" * 60)
            print("[Test] Browser closes in 5s (Ctrl+C to keep it open)...")
            try:
                time.sleep(5)
            except KeyboardInterrupt:
                pass
            closer()


if __name__ == "__main__":
    main()
