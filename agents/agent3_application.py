"""
Agent 3: Application Execution and Notification
Navigates to job pages, logs in, fills forms, uploads the tailored resume,
handles CAPTCHAs via Telegram, and sends a daily summary.
Uses Playwright for browser automation.
"""

import json
import os
import smtplib
import time
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import requests
from playwright.sync_api import sync_playwright, Page, Browser, TimeoutError as PWTimeout

from agents.agent1_job_discovery import JobPosting
from agents.agent4_credentials import CredentialManager

CHROMIUM_BIN = os.environ.get(
    "CHROMIUM_BIN",
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
)


# ---------------------------------------------------------------------------
# Telegram helper
# ---------------------------------------------------------------------------

class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._base = f"https://api.telegram.org/bot{bot_token}"

    def send(self, text: str) -> bool:
        try:
            resp = requests.post(
                f"{self._base}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10,
            )
            return resp.ok
        except requests.RequestException as exc:
            print(f"  [Agent3/Telegram] Send failed: {exc}")
            return False

    def send_captcha_alert(self, job: JobPosting, app_url: str, timeout_seconds: int) -> None:
        msg = (
            f"*CAPTCHA Required — Manual Action Needed*\n\n"
            f"*Company:* {job.company}\n"
            f"*Role:* {job.title}\n"
            f"*Platform:* {job.platform.title()}\n"
            f"*URL:* {app_url}\n\n"
            f"Please solve the CAPTCHA and reply `/captcha_done` within "
            f"{timeout_seconds // 60} minutes."
        )
        self.send(msg)

    def send_daily_summary(self, submitted: list[dict], failed: list[dict]) -> None:
        today = date.today().isoformat()
        lines = [f"*Daily Application Summary — {today}*\n"]
        lines.append(f"✅ Submitted: *{len(submitted)}*")
        for app in submitted:
            lines.append(f"  • {app['company']} — {app['title']} ({app['platform']})")
        if failed:
            lines.append(f"\n❌ Failed: *{len(failed)}*")
            for app in failed:
                lines.append(f"  • {app['company']}: {app.get('reason', 'unknown')}")
        self.send("\n".join(lines))

    def poll_captcha_resolved(self, timeout_seconds: int, poll_interval: int = 10) -> bool:
        deadline = time.time() + timeout_seconds
        last_update_id = 0
        print(f"  [Agent3] Waiting up to {timeout_seconds}s for CAPTCHA resolution…")
        while time.time() < deadline:
            try:
                resp = requests.get(
                    f"{self._base}/getUpdates",
                    params={"offset": last_update_id + 1, "timeout": poll_interval},
                    timeout=poll_interval + 5,
                )
                if resp.ok:
                    for update in resp.json().get("result", []):
                        last_update_id = update["update_id"]
                        msg_text = (
                            update.get("message", {}).get("text", "") or
                            update.get("channel_post", {}).get("text", "")
                        )
                        if "/captcha_done" in msg_text.lower():
                            print("  [Agent3] CAPTCHA resolved by user.")
                            return True
            except requests.RequestException:
                pass
            time.sleep(poll_interval)
        return False


# ---------------------------------------------------------------------------
# CAPTCHA detector
# ---------------------------------------------------------------------------

_CAPTCHA_SELECTORS = [
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "div.g-recaptcha",
    "div[class*='captcha']",
    "img[src*='captcha']",
    "#captcha",
    "[data-testid='captcha']",
]


# ---------------------------------------------------------------------------
# Email notifier
# ---------------------------------------------------------------------------

class EmailNotifier:
    """
    Sends daily summary and CAPTCHA alerts via email (Gmail / any SMTP).
    Required env vars:
        EMAIL_FROM      — sender address (e.g. abhisheksaha@gmail.com)
        EMAIL_PASSWORD  — app password (Gmail: generate at myaccount.google.com/apppasswords)
        EMAIL_TO        — recipient address
        EMAIL_SMTP_HOST — defaults to smtp.gmail.com
        EMAIL_SMTP_PORT — defaults to 587
    """

    def __init__(self):
        self.sender   = os.environ.get("EMAIL_FROM", "")
        self.password = os.environ.get("EMAIL_PASSWORD", "")
        self.recipient = os.environ.get("EMAIL_TO", self.sender)
        self.smtp_host = os.environ.get("EMAIL_SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(os.environ.get("EMAIL_SMTP_PORT", "587"))
        self.enabled = bool(self.sender and self.password)

    def _send(self, subject: str, body: str) -> bool:
        if not self.enabled:
            return False
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self.sender
            msg["To"] = self.recipient
            msg.attach(MIMEText(body, "plain"))
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.sender, self.password)
                server.sendmail(self.sender, self.recipient, msg.as_string())
            return True
        except Exception as exc:
            print(f"  [Agent3/Email] Send failed: {exc}")
            return False

    def send_daily_summary(self, submitted: list[dict], failed: list[dict]) -> None:
        today = date.today().strftime("%d %b %Y")
        lines = [f"Job Application Summary — {today}\n"]
        lines.append(f"Submitted: {len(submitted)}")
        for a in submitted:
            lines.append(f"  ✓ {a['company']} — {a['title']} ({a['platform']})")
        if failed:
            lines.append(f"\nFailed: {len(failed)}")
            for a in failed:
                lines.append(f"  ✗ {a['company']}: {a.get('reason','')}")
        self._send(
            subject=f"[JobApp] {len(submitted)} applications submitted — {today}",
            body="\n".join(lines),
        )

    def send_captcha_alert(self, job, app_url: str) -> None:
        body = (
            f"CAPTCHA detected — manual action needed\n\n"
            f"Company : {job.company}\n"
            f"Role    : {job.title}\n"
            f"Platform: {job.platform}\n"
            f"URL     : {app_url}\n\n"
            f"Please open the URL, solve the CAPTCHA, then reply to this email."
        )
        self._send(subject=f"[JobApp] CAPTCHA needed — {job.company}", body=body)


def detect_captcha(page: Page) -> bool:
    for selector in _CAPTCHA_SELECTORS:
        try:
            if page.query_selector(selector):
                return True
        except Exception:
            pass
    return False


# ---------------------------------------------------------------------------
# Platform-specific handlers
# ---------------------------------------------------------------------------

class BaseApplicationHandler:
    def __init__(self, page: Page):
        self.page = page

    def login(self, username: str, password: str) -> bool:
        raise NotImplementedError

    def apply(self, job: JobPosting, resume_path: Path) -> bool:
        raise NotImplementedError


class NaukriHandler(BaseApplicationHandler):
    LOGIN_URL = "https://www.naukri.com/nlogin/login"

    def login(self, username: str, password: str) -> bool:
        try:
            self.page.goto(self.LOGIN_URL, timeout=30000)
            self.page.wait_for_selector("#usernameField", timeout=15000)
            self.page.fill("#usernameField", username)
            self.page.fill("#passwordField", password)
            self.page.click("button[type='submit']")
            self.page.wait_for_timeout(3000)
            logged_in = "naukri.com" in self.page.url and "login" not in self.page.url
            print(f"    [Naukri] Login {'succeeded' if logged_in else 'failed'} — URL: {self.page.url[:60]}")
            return logged_in
        except PWTimeout as exc:
            print(f"    [Naukri] Login timeout: {exc}")
            return False

    def apply(self, job: JobPosting, resume_path: Path) -> bool:
        try:
            self.page.goto(job.url, timeout=30000)
            self.page.wait_for_selector("[class*='apply']", timeout=15000)
            apply_btn = self.page.query_selector("button[class*='apply'], a[class*='apply']")
            if apply_btn:
                apply_btn.click()
                self.page.wait_for_timeout(2000)
            upload = self.page.query_selector("input[type='file']")
            if upload:
                upload.set_input_files(str(resume_path.absolute()))
                self.page.wait_for_timeout(1000)
            submit = self.page.query_selector("button[type='submit'], button[class*='submit']")
            if submit:
                submit.click()
                self.page.wait_for_timeout(2000)
            return True
        except PWTimeout as exc:
            print(f"    [Naukri] Apply timeout: {exc}")
            return False


class IndeedHandler(BaseApplicationHandler):
    LOGIN_URL = "https://secure.indeed.com/auth"

    def login(self, username: str, password: str) -> bool:
        try:
            self.page.goto(self.LOGIN_URL, timeout=30000)
            self.page.wait_for_selector("#login-email-input", timeout=15000)
            self.page.fill("#login-email-input", username)
            self.page.click("button[type='submit']")
            self.page.wait_for_selector("#login-password-input", timeout=10000)
            self.page.fill("#login-password-input", password)
            self.page.click("button[type='submit']")
            self.page.wait_for_timeout(3000)
            return True
        except PWTimeout as exc:
            print(f"    [Indeed] Login timeout: {exc}")
            return False

    def apply(self, job: JobPosting, resume_path: Path) -> bool:
        try:
            self.page.goto(job.url, timeout=30000)
            apply_btn = self.page.wait_for_selector(
                "button[class*='ia-continueButton'], #indeedApplyButton", timeout=15000
            )
            apply_btn.click()
            self.page.wait_for_timeout(2000)
            upload = self.page.query_selector("input[type='file']")
            if upload:
                upload.set_input_files(str(resume_path.absolute()))
                self.page.wait_for_timeout(1000)
            submit = self.page.query_selector("button:has-text('Submit')")
            if submit:
                submit.click()
                self.page.wait_for_timeout(2000)
            return True
        except PWTimeout as exc:
            print(f"    [Indeed] Apply timeout: {exc}")
            return False


class LinkedInHandler(BaseApplicationHandler):
    LOGIN_URL = "https://www.linkedin.com/login"

    def login(self, username: str, password: str) -> bool:
        try:
            self.page.goto(self.LOGIN_URL, timeout=30000)
            self.page.wait_for_selector("#username", timeout=15000)
            self.page.fill("#username", username)
            self.page.fill("#password", password)
            self.page.click("button[type='submit']")
            self.page.wait_for_timeout(4000)
            logged_in = "linkedin.com/feed" in self.page.url or "linkedin.com/in/" in self.page.url
            print(f"    [LinkedIn] Login {'succeeded' if logged_in else 'may need verification'} — URL: {self.page.url[:60]}")
            return logged_in
        except PWTimeout as exc:
            print(f"    [LinkedIn] Login timeout: {exc}")
            return False

    def apply(self, job: JobPosting, resume_path: Path) -> bool:
        try:
            self.page.goto(job.url, timeout=30000)
            easy_apply = self.page.wait_for_selector(
                "button[class*='jobs-apply-button']", timeout=15000
            )
            easy_apply.click()
            self.page.wait_for_timeout(2000)
            for _ in range(5):
                upload = self.page.query_selector("input[type='file']")
                if upload:
                    upload.set_input_files(str(resume_path.absolute()))
                    self.page.wait_for_timeout(1000)
                submit_btn = self.page.query_selector("button[aria-label*='Submit']")
                next_btn = self.page.query_selector("button[aria-label*='Next']")
                if submit_btn:
                    submit_btn.click()
                    break
                elif next_btn:
                    next_btn.click()
                    self.page.wait_for_timeout(1000)
                else:
                    break
            return True
        except PWTimeout as exc:
            print(f"    [LinkedIn] Apply timeout: {exc}")
            return False


_HANDLER_MAP: dict[str, type] = {
    "naukri": NaukriHandler,
    "indeed": IndeedHandler,
    "linkedin": LinkedInHandler,
}


# ---------------------------------------------------------------------------
# Application log
# ---------------------------------------------------------------------------

class ApplicationLog:
    def __init__(self, log_path: str):
        self.path = Path(log_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except json.JSONDecodeError:
                pass
        return {}

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._data, indent=2))

    def already_applied(self, job: JobPosting) -> bool:
        key = f"{job.platform}::{job.company}::{job.title}"
        return key in self._data

    def record(self, job: JobPosting, status: str, reason: str = "") -> None:
        key = f"{job.platform}::{job.company}::{job.title}"
        self._data[key] = {
            "company": job.company,
            "title": job.title,
            "platform": job.platform,
            "url": job.url,
            "status": status,
            "reason": reason,
            "timestamp": datetime.now().isoformat(),
        }
        self._save()

    def todays_summary(self) -> tuple[list[dict], list[dict]]:
        today = date.today().isoformat()
        submitted, failed = [], []
        for entry in self._data.values():
            if entry["timestamp"].startswith(today):
                (submitted if entry["status"] == "submitted" else failed).append(entry)
        return submitted, failed


# ---------------------------------------------------------------------------
# Main Application Agent
# ---------------------------------------------------------------------------

class ApplicationAgent:

    def __init__(self, config: dict, credential_manager: CredentialManager, cred_key: bytes):
        self.config = config
        self.cred_manager = credential_manager
        self.cred_key = cred_key
        self.app_config = config.get("application", {})
        self.log = ApplicationLog(self.app_config.get("log_file", "logs/applications.json"))
        self.delay = self.app_config.get("delay_between_apps", 30)
        self.captcha_timeout = self.app_config.get("captcha_timeout", 300)

        tg_cfg = config.get("telegram", {})
        bot_token = os.environ.get(tg_cfg.get("bot_token_env", "TELEGRAM_BOT_TOKEN"), "")
        chat_id = os.environ.get(tg_cfg.get("chat_id_env", "TELEGRAM_CHAT_ID"), "")
        self.telegram: Optional[TelegramNotifier] = (
            TelegramNotifier(bot_token, chat_id) if bot_token and chat_id else None
        )
        self.email = EmailNotifier()
        if not self.telegram and not self.email.enabled:
            print("[Agent3] No notifier configured — set Telegram or Email env vars for alerts.")

    def _launch_browser(self, playwright):
        chromium_bin = CHROMIUM_BIN if Path(CHROMIUM_BIN).exists() else None
        launch_kwargs = dict(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                  "--ignore-certificate-errors"],
        )
        if chromium_bin:
            launch_kwargs["executable_path"] = chromium_bin
        return playwright.chromium.launch(**launch_kwargs)

    def run(self, jobs: list[JobPosting], tailored_resumes: dict[str, Path]) -> None:
        print(f"[Agent3] Starting application run for {len(jobs)} jobs")

        by_platform: dict[str, list[JobPosting]] = {}
        for job in jobs:
            by_platform.setdefault(job.platform, []).append(job)

        with sync_playwright() as playwright:
            for platform, platform_jobs in by_platform.items():
                self._process_platform(playwright, platform, platform_jobs, tailored_resumes)

        submitted, failed = self.log.todays_summary()
        print(f"\n[Agent3] Daily totals — submitted: {len(submitted)}, failed: {len(failed)}")
        if self.telegram:
            self.telegram.send_daily_summary(submitted, failed)
        if self.email.enabled:
            self.email.send_daily_summary(submitted, failed)
            print("[Agent3] Daily summary emailed.")
        # Always write a file report
        from daily_report import format_report, save_report
        from datetime import date as _date
        report = format_report(_date.today(), self.log._data)
        path = save_report(report, _date.today())
        print(f"[Agent3] Report saved → {path}")

    def _process_platform(self, playwright, platform, jobs, tailored_resumes):
        if platform not in _HANDLER_MAP:
            print(f"  [Agent3] No handler for '{platform}' — skipping.")
            return

        try:
            creds = self.cred_manager.get_site_credentials(platform, self.cred_key)
        except KeyError:
            print(f"  [Agent3] No credentials for '{platform}' — skipping.")
            return

        browser: Browser = self._launch_browser(playwright)
        ctx = browser.new_context(ignore_https_errors=True)
        page = ctx.new_page()
        handler: BaseApplicationHandler = _HANDLER_MAP[platform](page)

        try:
            logged_in = handler.login(creds.get("username", ""), creds.get("password", ""))
            if not logged_in:
                print(f"  [Agent3] Login failed for {platform} — skipping platform.")
                return

            for job in jobs:
                self._apply_to_job(handler, page, job, tailored_resumes)
                time.sleep(self.delay)
        finally:
            browser.close()

    def _apply_to_job(self, handler, page, job, tailored_resumes):
        key = f"{job.platform}::{job.company}::{job.title}"

        if self.log.already_applied(job):
            print(f"  [Agent3] Already applied to {job.company} — skipping.")
            return

        resume_path = tailored_resumes.get(key)
        if not resume_path or not resume_path.exists():
            print(f"  [Agent3] No tailored resume for {key} — skipping.")
            self.log.record(job, "skipped", "no resume")
            return

        print(f"  [Agent3] Applying → {job.company} | {job.title} ({job.platform})")

        if detect_captcha(page):
            resolved = self._handle_captcha(job, page.url)
            if not resolved:
                self.log.record(job, "failed", "CAPTCHA timeout")
                return

        success = handler.apply(job, resume_path)

        if success and detect_captcha(page):
            resolved = self._handle_captcha(job, page.url)
            success = resolved

        status = "submitted" if success else "failed"
        self.log.record(job, status, "" if success else "apply error")
        print(f"  [Agent3] {status.upper()} — {job.company}")

    def _handle_captcha(self, job, current_url):
        print(f"  [Agent3] CAPTCHA detected for {job.company}")
        if self.telegram:
            self.telegram.send_captcha_alert(job, current_url, self.captcha_timeout)
            return self.telegram.poll_captcha_resolved(self.captcha_timeout)
        print(f"  [Agent3] Telegram not configured — skipping CAPTCHA job.")
        return False
