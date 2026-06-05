"""
Agent 3: Application Execution and Notification
Navigates to job pages, logs in, fills forms, uploads the tailored resume,
handles CAPTCHAs via Telegram, and sends a daily summary.
Uses Playwright for browser automation.
"""

import json
import os
import re
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


_ATS_DOMAINS_TUPLE = (
    "workday.com", "myworkdayjobs.com", "greenhouse.io", "lever.co",
    "icims.com", "taleo.net", "smartrecruiters.com", "successfactors",
    "bamboohr.com", "jobvite.com", "ashbyhq.com",
)

_ATS_URL_RE = re.compile(
    r'https?://[^\s"\'<>]*(?:'
    r'workday\.com|myworkdayjobs\.com|greenhouse\.io|'
    r'lever\.co|icims\.com|taleo\.net|smartrecruiters\.com|'
    r'successfactors|bamboohr\.com|jobvite\.com|ashbyhq\.com'
    r')[^\s"\'<>]*',
    re.IGNORECASE,
)

_NAUKRI_LOGIN_URL = "https://www.naukri.com/central-login-services/v2/login"


def _naukri_login(username: str, password: str) -> "Optional[requests.Session]":
    """POST to Naukri login API; return an authenticated session or None."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept":       "application/json",
        "Content-Type": "application/json",
        "Referer":      "https://www.naukri.com/",
        "appId":        "105",
        "systemId":     "105",
    })
    try:
        resp = session.post(
            _NAUKRI_LOGIN_URL,
            json={"username": username, "password": password},
            timeout=30,
        )
        if resp.ok:
            print(f"  [Agent3/Naukri] Login OK (HTTP {resp.status_code})")
            return session
        print(f"  [Agent3/Naukri] Login failed: HTTP {resp.status_code} — {resp.text[:200]}")
        return None
    except requests.RequestException as exc:
        print(f"  [Agent3/Naukri] Login request error: {exc}")
        return None


def _naukri_find_apply_link(obj, depth: int = 0) -> "str | None":
    """Recursively search a JSON object for an external ATS apply URL."""
    if depth > 12:
        return None
    if isinstance(obj, dict):
        for key in (
            "applyLink", "externalApplyLink", "companyCareerLink",
            "redirectLink", "applyUrl", "externalApplyUrl",
            "applyRedirectUrl", "redirectUrl", "extApplyUrl",
            "companyUrl", "externalUrl", "jobApplyUrl",
            "careerPageUrl", "externalJobUrl", "atsUrl",
        ):
            val = obj.get(key)
            if isinstance(val, str) and val.startswith("http"):
                if any(d in val for d in _ATS_DOMAINS_TUPLE):
                    return val
        for v in obj.values():
            if isinstance(v, str) and v.startswith("http"):
                if any(d in v for d in _ATS_DOMAINS_TUPLE):
                    return v
            elif isinstance(v, (dict, list)):
                found = _naukri_find_apply_link(v, depth + 1)
                if found:
                    return found
    elif isinstance(obj, list):
        for v in obj:
            found = _naukri_find_apply_link(v, depth + 1)
            if found:
                return found
    return None

CHROMIUM_BIN = os.environ.get(
    "CHROMIUM_BIN",
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
)
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")


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

    def _poll_updates(self, timeout_seconds: int, trigger_fn, poll_interval: int = 10):
        """Generic Telegram update poller. Calls trigger_fn(text) on each message.
        Returns whatever trigger_fn returns (non-None = stop polling)."""
        deadline = time.time() + timeout_seconds
        last_update_id = 0
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
                        text = (
                            update.get("message", {}).get("text", "") or
                            update.get("channel_post", {}).get("text", "")
                        ).strip()
                        result = trigger_fn(text)
                        if result is not None:
                            return result
            except requests.RequestException:
                pass
            time.sleep(poll_interval)
        return None

    def poll_captcha_resolved(self, timeout_seconds: int, poll_interval: int = 10) -> bool:
        print(f"  [Agent3] Waiting up to {timeout_seconds}s for CAPTCHA resolution…")
        def check(text):
            if "/captcha_done" in text.lower():
                print("  [Agent3] CAPTCHA resolved by user.")
                return True
            return None
        return self._poll_updates(timeout_seconds, check, poll_interval) or False

    def request_credentials(self, ats: str, site_url: str, job: "JobPosting",
                            timeout_seconds: int) -> "dict | None":
        """Ask user for ATS credentials via Telegram, return {username, password} or None."""
        msg = (
            f"🔐 *Credentials needed — {job.company}*\n\n"
            f"*Role:* {job.title}\n"
            f"*ATS:* {ats.title()}\n"
            f"*URL:* {site_url}\n\n"
            f"1\\. If you already have an account, reply:\n"
            f"`/creds your@email.com:yourpassword`\n\n"
            f"2\\. If you need to register first, visit the URL above, "
            f"create an account, then reply with `/creds`\\.\n\n"
            f"Reply `/skip` to skip this job\\.\n"
            f"_\\(Waiting {timeout_seconds // 60} minutes\\)_"
        )
        self.send(msg)
        print(f"  [Agent3] Waiting up to {timeout_seconds}s for {ats} credentials…")

        def check(text):
            if text.lower().startswith("/creds "):
                payload = text[7:].strip()
                colon = payload.find(":")
                if colon > 0:
                    return {"username": payload[:colon], "password": payload[colon + 1:]}
            if text.lower() in ("/skip", "/skip_job"):
                print("  [Agent3] User skipped the job.")
                return "SKIP"
            return None

        result = self._poll_updates(timeout_seconds, check)
        if result == "SKIP" or result is None:
            return None
        return result


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
# ATS detection
# ---------------------------------------------------------------------------

_ATS_PATTERNS: dict[str, list[str]] = {
    "workday":        ["workday.com", "myworkdayjobs.com"],
    "greenhouse":     ["greenhouse.io", "boards.greenhouse.io"],
    "lever":          ["jobs.lever.co", "lever.co"],
    "icims":          ["icims.com"],
    "taleo":          ["taleo.net"],
    "smartrecruiters":["smartrecruiters.com"],
    "successfactors": ["successfactors.com", "successfactors.eu", "sapsf.com"],
    "bamboohr":       ["bamboohr.com"],
    "jobvite":        ["jobvite.com"],
    "ashby":          ["ashbyhq.com"],
}


def detect_ats(url: str) -> "str | None":
    url_lower = url.lower()
    for ats, domains in _ATS_PATTERNS.items():
        if any(d in url_lower for d in domains):
            return ats
    return None


# ---------------------------------------------------------------------------
# Platform-specific handlers
# ---------------------------------------------------------------------------

class SkipApplication(Exception):
    """Raised when a job cannot be applied to automatically (e.g. no Easy Apply)."""


class ExternalApplicationRequired(Exception):
    """Raised when a job requires applying on an external ATS website."""
    def __init__(self, url: str):
        super().__init__(url)
        self.url = url


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


_LINKEDIN_EASY_APPLY_SELECTORS = [
    "button[aria-label*='Easy Apply']",
    "button.jobs-apply-button",
    "button[class*='jobs-apply-button']",
    "button:has-text('Easy Apply')",
]


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

    def _find_easy_apply_btn(self):
        for sel in _LINKEDIN_EASY_APPLY_SELECTORS:
            try:
                btn = self.page.query_selector(sel)
                if btn and btn.is_visible():
                    return btn
            except Exception:
                pass
        return None

    def apply(self, job: JobPosting, resume_path: Path) -> bool:
        try:
            self.page.goto(job.url, timeout=30000)
            self.page.wait_for_load_state("domcontentloaded")
            self.page.wait_for_timeout(3000)

            easy_apply = self._find_easy_apply_btn()
            if easy_apply:
                # ── Easy Apply inline flow ──────────────────────────────
                easy_apply.click()
                self.page.wait_for_timeout(2000)
                for _ in range(6):
                    upload = self.page.query_selector("input[type='file']")
                    if upload:
                        upload.set_input_files(str(resume_path.absolute()))
                        self.page.wait_for_timeout(1000)
                    submit_btn = self.page.query_selector("button[aria-label*='Submit']")
                    next_btn   = self.page.query_selector("button[aria-label*='Next']")
                    review_btn = self.page.query_selector("button[aria-label*='Review']")
                    if submit_btn:
                        submit_btn.click()
                        self.page.wait_for_timeout(2000)
                        break
                    elif review_btn:
                        review_btn.click()
                        self.page.wait_for_timeout(1000)
                    elif next_btn:
                        next_btn.click()
                        self.page.wait_for_timeout(1000)
                    else:
                        break
                return True

            # ── No Easy Apply — look for external Apply button ─────────
            ext_btn = self.page.query_selector(
                "button:has-text('Apply'), a.jobs-apply-button"
            )
            if ext_btn:
                try:
                    with self.page.context.expect_page(timeout=8000) as popup_info:
                        ext_btn.click()
                    popup = popup_info.value
                    popup.wait_for_load_state("domcontentloaded", timeout=15000)
                    external_url = popup.url
                    popup.close()
                    raise ExternalApplicationRequired(external_url)
                except PWTimeout:
                    # Button didn't open a new tab — navigation happened in same page
                    self.page.wait_for_timeout(3000)
                    if self.page.url != job.url:
                        raise ExternalApplicationRequired(self.page.url)

            raise SkipApplication("No apply button found on LinkedIn page")

        except (SkipApplication, ExternalApplicationRequired):
            raise
        except PWTimeout as exc:
            print(f"    [LinkedIn] Apply timeout: {exc}")
            return False


_HANDLER_MAP: dict[str, type] = {
    "naukri": NaukriHandler,
    "indeed": IndeedHandler,
    "linkedin": LinkedInHandler,
}


# ---------------------------------------------------------------------------
# External ATS handlers
# ---------------------------------------------------------------------------

class WorkdayHandler(BaseApplicationHandler):
    """Handles *.myworkdayjobs.com / *.workday.com — requires login."""

    def login(self, username: str, password: str) -> bool:
        try:
            self.page.wait_for_load_state("domcontentloaded")
            self.page.wait_for_timeout(2000)
            sign_in = self.page.query_selector(
                "[data-automation-id='signInButton'], a:has-text('Sign In'), button:has-text('Sign In')"
            )
            if sign_in:
                sign_in.click()
                self.page.wait_for_timeout(2000)
            email = self.page.query_selector("[data-automation-id='email'], input[type='email']")
            if email:
                email.fill(username)
                nxt = self.page.query_selector("[data-automation-id='nextButton'], button:has-text('Next')")
                if nxt:
                    nxt.click()
                    self.page.wait_for_timeout(1500)
            pw = self.page.query_selector("[data-automation-id='password'], input[type='password']")
            if pw:
                pw.fill(password)
                ok = self.page.query_selector(
                    "[data-automation-id='signInSubmitButton'], [data-automation-id='signInButton']"
                )
                if ok:
                    ok.click()
                    self.page.wait_for_timeout(3000)
            return True
        except PWTimeout as exc:
            print(f"    [Workday] Login timeout: {exc}")
            return False

    def apply(self, job: JobPosting, resume_path: Path) -> bool:
        try:
            self.page.wait_for_load_state("domcontentloaded")
            self.page.wait_for_timeout(2000)
            apply_btn = self.page.query_selector(
                "[data-automation-id='applyButton'], button:has-text('Apply')"
            )
            if apply_btn:
                apply_btn.click()
                self.page.wait_for_timeout(2000)
            for _ in range(10):
                upload = self.page.query_selector("input[type='file']")
                if upload:
                    upload.set_input_files(str(resume_path.absolute()))
                    self.page.wait_for_timeout(1000)
                done = self.page.query_selector("[data-automation-id='bottom-navigation-done-btn']")
                nxt  = self.page.query_selector("[data-automation-id='bottom-navigation-next-btn']")
                if done:
                    done.click()
                    self.page.wait_for_timeout(2000)
                    break
                elif nxt:
                    nxt.click()
                    self.page.wait_for_timeout(1500)
                else:
                    break
            return True
        except PWTimeout as exc:
            print(f"    [Workday] Apply timeout: {exc}")
            return False


class GreenhouseHandler(BaseApplicationHandler):
    """Handles boards.greenhouse.io — no login, just form filling."""

    def login(self, username: str, password: str) -> bool:
        return True  # Greenhouse has no login wall

    def apply(self, job: JobPosting, resume_path: Path) -> bool:
        try:
            self.page.wait_for_load_state("domcontentloaded")
            self.page.wait_for_timeout(2000)
            profile = _load_applicant_profile()
            for sel, val in [
                ("#first_name, [name='job_application[first_name]']",  profile.get("first_name", "")),
                ("#last_name,  [name='job_application[last_name]']",   profile.get("last_name", "")),
                ("#email,      [name='job_application[email]']",        profile.get("email", "")),
                ("#phone,      [name='job_application[phone]']",        profile.get("phone", "")),
            ]:
                el = self.page.query_selector(sel)
                if el and val:
                    el.fill(val)
            upload = self.page.query_selector("#resume, input[type='file']")
            if upload:
                upload.set_input_files(str(resume_path.absolute()))
                self.page.wait_for_timeout(1000)
            submit = self.page.query_selector(
                "#submit_app, [data-provides='submit-btn'], button:has-text('Submit Application')"
            )
            if submit:
                submit.click()
                self.page.wait_for_timeout(2000)
            return True
        except PWTimeout as exc:
            print(f"    [Greenhouse] Apply timeout: {exc}")
            return False


class LeverHandler(BaseApplicationHandler):
    """Handles jobs.lever.co — no login, just form filling."""

    def login(self, username: str, password: str) -> bool:
        return True

    def apply(self, job: JobPosting, resume_path: Path) -> bool:
        try:
            self.page.wait_for_load_state("domcontentloaded")
            self.page.wait_for_timeout(2000)
            apply_btn = self.page.query_selector("a:has-text('Apply'), button:has-text('Apply')")
            if apply_btn:
                apply_btn.click()
                self.page.wait_for_timeout(2000)
            profile = _load_applicant_profile()
            for sel, val in [
                ("[name='name']",    profile.get("name", "")),
                ("[name='email']",   profile.get("email", "")),
                ("[name='phone']",   profile.get("phone", "")),
                ("[name='org']",     ""),  # current company — leave blank
                ("[name='urls[LinkedIn]']", profile.get("linkedin_url", "")),
            ]:
                el = self.page.query_selector(sel)
                if el and val:
                    el.fill(val)
            upload = self.page.query_selector("input[type='file']")
            if upload:
                upload.set_input_files(str(resume_path.absolute()))
                self.page.wait_for_timeout(1000)
            submit = self.page.query_selector(
                "button[type='submit'], button:has-text('Submit application')"
            )
            if submit:
                submit.click()
                self.page.wait_for_timeout(2000)
            return True
        except PWTimeout as exc:
            print(f"    [Lever] Apply timeout: {exc}")
            return False


_NO_LOGIN_ATS = {"greenhouse", "lever", "ashby"}

_ATS_HANDLER_MAP: dict[str, type] = {
    "workday":    WorkdayHandler,
    "greenhouse": GreenhouseHandler,
    "lever":      LeverHandler,
}

_applicant_profile_cache: "dict | None" = None

def _load_applicant_profile() -> dict:
    global _applicant_profile_cache
    if _applicant_profile_cache is not None:
        return _applicant_profile_cache
    try:
        import yaml
        cfg = yaml.safe_load(open("config/config.yaml"))
        raw = cfg.get("applicant", {})
        name_parts = raw.get("name", "").split(" ", 1)
        _applicant_profile_cache = {
            "name":         raw.get("name", ""),
            "first_name":   name_parts[0] if name_parts else "",
            "last_name":    name_parts[1] if len(name_parts) > 1 else "",
            "email":        raw.get("email", ""),
            "phone":        raw.get("phone", ""),
            "linkedin_url": raw.get("linkedin_url", ""),
        }
    except Exception:
        _applicant_profile_cache = {}
    return _applicant_profile_cache


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
        self.creds_request_timeout = self.app_config.get("creds_request_timeout", 600)
        self._naukri_auth_session: Optional[requests.Session] = None
        self._naukri_session_tried: bool = False

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
        # No proxy: ATS sites (LinkedIn, Workday, Greenhouse, Lever) don't need
        # one, and routing through ScraperAPI burns credits on every page load.
        return playwright.chromium.launch(**launch_kwargs)

    def _new_stealth_page(self, browser: Browser) -> tuple:
        """Return (context, page) with stealth patches applied."""
        ctx = browser.new_context(
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768},
            locale="en-IN",
        )
        page = ctx.new_page()
        try:
            from playwright_stealth import stealth_sync
            stealth_sync(page)
        except ImportError:
            pass
        return ctx, page

    def _get_naukri_session(self) -> "Optional[requests.Session]":
        """Lazily authenticate with Naukri; cache session for the run."""
        if self._naukri_session_tried:
            return self._naukri_auth_session
        self._naukri_session_tried = True
        try:
            creds = self.cred_manager.get_site_credentials("naukri", self.cred_key)
            self._naukri_auth_session = _naukri_login(
                creds.get("username", ""), creds.get("password", "")
            )
        except (KeyError, FileNotFoundError) as exc:
            print(f"  [Agent3/Naukri] Could not load Naukri credentials: {exc}")
        return self._naukri_auth_session

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

        # Platforms where we apply via job URL → ATS redirect rather than through
        # the platform's own login + apply flow.
        # - indeed:  no platform login available in credentials.enc
        # - naukri:  login page is served through Akamai CDN which blocks headless
        #            browsers; most Naukri listings redirect to company ATS anyway
        _DIRECT_URL_PLATFORMS = {"indeed", "naukri"}

        if platform in _DIRECT_URL_PLATFORMS:
            print(f"  [Agent3] {platform}: using direct-URL apply (follow job link → company ATS).")
            browser: Browser = self._launch_browser(playwright)
            ctx, page = self._new_stealth_page(browser)
            # Use a shorter delay for direct-URL applies — each job triggers a
            # ScraperAPI render call rather than an interactive browser session,
            # so the heavy 30s rate-limit delay is unnecessary here.
            direct_delay = max(5, self.delay // 6)
            try:
                for job in jobs:
                    self._apply_via_job_url(page, job, tailored_resumes)
                    time.sleep(direct_delay)
            finally:
                ctx.close()
                browser.close()
            return

        try:
            creds = self.cred_manager.get_site_credentials(platform, self.cred_key)
        except KeyError:
            print(f"  [Agent3] No credentials for '{platform}' — skipping.")
            return

        browser: Browser = self._launch_browser(playwright)
        ctx, page = self._new_stealth_page(browser)
        handler: BaseApplicationHandler = _HANDLER_MAP[platform](page)

        try:
            logged_in = handler.login(creds.get("username", ""), creds.get("password", ""))
            if not logged_in:
                print(f"  [Agent3] Login failed for {platform} — falling back to direct-URL apply.")
                for job in jobs:
                    self._apply_via_job_url(page, job, tailored_resumes)
                    time.sleep(max(5, self.delay // 6))
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

        try:
            success = handler.apply(job, resume_path)
        except SkipApplication as exc:
            self.log.record(job, "skipped", str(exc))
            print(f"  [Agent3] SKIPPED — {job.company} ({exc})")
            return
        except ExternalApplicationRequired as exc:
            self._handle_external_apply(page, job, resume_path, exc.url)
            return

        if success and detect_captcha(page):
            resolved = self._handle_captcha(job, page.url)
            success = resolved

        status = "submitted" if success else "failed"
        self.log.record(job, status, "" if success else "apply error")
        print(f"  [Agent3] {status.upper()} — {job.company}")

    def _apply_via_job_url(self, page: Page, job: "JobPosting", tailored_resumes: dict) -> None:
        """
        Apply to a job by following its URL to the company's external ATS.

        For Naukri: proxy-mode Playwright gets empty shells (Akamai CDN).
          → Use ScraperAPI render API to fetch the rendered job page, extract
            the external apply link from HTML/JSON, then navigate Playwright
            directly to the ATS URL (Workday/Greenhouse/Lever etc. are not
            Akamai-protected, so Playwright works fine there).

        For Indeed / other platforms: use Playwright directly (the job URL
          redirects to the ATS with a normal navigation flow).
        """
        key = f"{job.platform}::{job.company}::{job.title}"

        if self.log.already_applied(job):
            print(f"  [Agent3] Already applied to {job.company} — skipping.")
            return

        resume_path = tailored_resumes.get(key)
        if not resume_path or not resume_path.exists():
            print(f"  [Agent3] No tailored resume for {key} — skipping.")
            self.log.record(job, "skipped", "no resume")
            return

        if not job.url:
            self.log.record(job, "skipped", "no job URL")
            return

        print(f"  [Agent3] Direct-apply → {job.company} | {job.title}")

        # ── Naukri: render-API approach ────────────────────────────────────
        if job.platform == "naukri":
            self._apply_naukri_via_render(page, job, resume_path)
            return

        # ── All other platforms: Playwright navigation approach ────────────
        try:
            page.goto(job.url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            current_url = page.url

            ats = detect_ats(current_url)
            if ats:
                self._handle_external_apply(page, job, resume_path, current_url)
                return

            apply_link = (
                page.query_selector("a[data-testid='viewJobButtonLinkComponent']") or
                page.query_selector("a[href*='applyRedirect']") or
                page.query_selector("a:has-text('Apply on company site')") or
                page.query_selector("a:has-text('Apply now')") or
                page.query_selector("button:has-text('Apply on company site')")
            )

            if not apply_link:
                print(f"  [Agent3] No external apply link on {current_url[:70]} — skipping.")
                self.log.record(job, "skipped", "no external apply link")
                return

            try:
                with page.context.expect_page(timeout=8000) as popup_info:
                    apply_link.click()
                popup = popup_info.value
                popup.wait_for_load_state("domcontentloaded", timeout=15000)
                ext_url = popup.url
                ats = detect_ats(ext_url)
                if ats:
                    self._handle_external_apply(popup, job, resume_path, ext_url)
                else:
                    self.log.record(job, "skipped", f"unsupported ATS: {ext_url[:60]}")
            except Exception:
                page.wait_for_timeout(2000)
                ext_url = page.url
                ats = detect_ats(ext_url)
                if ats:
                    self._handle_external_apply(page, job, resume_path, ext_url)
                else:
                    self.log.record(job, "skipped", f"unsupported ATS at {ext_url[:60]}")

        except Exception as exc:
            print(f"  [Agent3] Direct-apply error for {job.company}: {exc}")
            self.log.record(job, "failed", str(exc)[:100])

    def _apply_naukri_via_render(self, page: Page, job: "JobPosting", resume_path: Path) -> None:
        """
        Get the external ATS URL for a Naukri job, then navigate Playwright there.

        Priority order (cheapest first - zero ScraperAPI credits):
          1. job.apply_url pre-fetched by Agent1 during discovery.
          2. Naukri job detail JSON API (/jobapi/v3/job?jobId=...) - free.
          3. ScraperAPI render + regex/HTML scan - costs credits; only used if
             SCRAPER_API_KEY is set and both free methods fail.
        """
        import json as _json
        from bs4 import BeautifulSoup as _BS

        ext_url: str = ""

        # ── 1. Pre-fetched apply_url from Agent1 ──────────────────────────
        if getattr(job, "apply_url", ""):
            ext_url = job.apply_url
            print(f"  [Agent3/Naukri] Using pre-fetched apply_url: {ext_url[:80]}")

        # ── 2. Naukri job detail API (unauthenticated, free) ─────────────
        if not ext_url and job.job_id:
            ext_url = self._naukri_api_apply_url(job.job_id)
            if ext_url:
                print(f"  [Agent3/Naukri] Apply URL from job API (jobId={job.job_id}): {ext_url[:80]}")
            else:
                print(f"  [Agent3/Naukri] Job API (unauth): no ATS URL for jobId={job.job_id}")

        # ── 2.5. Authenticated Naukri API (unlocks applyRedirectUrl) ─────
        if not ext_url and job.job_id:
            auth_session = self._get_naukri_session()
            if auth_session:
                ext_url = self._naukri_api_apply_url(job.job_id, session=auth_session)
                if ext_url:
                    print(f"  [Agent3/Naukri] Apply URL from auth API: {ext_url[:80]}")
                else:
                    print(f"  [Agent3/Naukri] Job API (auth): no ATS URL for jobId={job.job_id}")

        # ── 3. ScraperAPI render scan (optional, credit-consuming) ────────
        if not ext_url:
            from agents.agent1_job_discovery import scraperapi_fetch, SCRAPER_API_KEY as _KEY
            if not _KEY:
                print(f"  [Agent3/Naukri] No SCRAPER_API_KEY — skipping {job.company}.")
                self.log.record(job, "skipped", "naukri: no apply URL (no ScraperAPI key)")
                return
            print(f"  [Agent3/Naukri] Falling back to ScraperAPI render for {job.company}…")
            resp = scraperapi_fetch(job.url, render=True, country="in")
            if resp is None or resp.status_code != 200:
                code = resp.status_code if resp else "none"
                print(f"  [Agent3/Naukri] Render-fetch HTTP {code} — skipping.")
                self.log.record(job, "skipped", "naukri render-fetch failed")
                return
            html = resp.text
            # Regex scan: catches ATS URLs in JSON strings / data-attrs anywhere
            matches = _ATS_URL_RE.findall(html)
            if matches:
                ext_url = matches[0].rstrip(".,;)")
                print(f"  [Agent3/Naukri] ATS URL via regex: {ext_url[:80]}")
            if not ext_url:
                soup = _BS(html, "html.parser")
                script = soup.find("script", id="__NEXT_DATA__")
                if script and script.string:
                    try:
                        ext_url = _naukri_find_apply_link(_json.loads(script.string)) or ""
                        if ext_url:
                            print(f"  [Agent3/Naukri] ATS URL from __NEXT_DATA__: {ext_url[:80]}")
                    except Exception as exc:
                        print(f"  [Agent3/Naukri] __NEXT_DATA__ parse error: {exc}")
            if not ext_url:
                soup = _BS(html, "html.parser")
                for a in soup.find_all("a", href=True):
                    if detect_ats(a["href"]):
                        ext_url = a["href"]
                        print(f"  [Agent3/Naukri] ATS href in <a>: {ext_url[:80]}")
                        break
            if not ext_url:
                debug_dir = Path("logs/debug")
                debug_dir.mkdir(parents=True, exist_ok=True)
                slug = re.sub(r"[^a-z0-9]+", "_", job.company.lower())[:30]
                out  = debug_dir / f"naukri_job_{slug}.html"
                try:
                    out.write_text(html, encoding="utf-8")
                    ext_links = [
                        (a.get_text(strip=True)[:30], a["href"][:80])
                        for a in _BS(html, "html.parser").find_all("a", href=True)
                        if a["href"].startswith("http") and "naukri.com" not in a["href"]
                    ][:10]
                    print(f"  [Agent3/Naukri] No ATS URL — dump → {out}. External hrefs: {ext_links}")
                except Exception:
                    pass
                self.log.record(job, "skipped", "naukri: no ext apply link")
                return

        # ── 4. Navigate Playwright to ATS URL ────────────────────────────
        ats = detect_ats(ext_url)
        if not ats:
            print(f"  [Agent3/Naukri] URL not a known ATS: {ext_url[:80]}")
            self.log.record(job, "skipped", f"naukri ext link unsupported ATS: {ext_url[:60]}")
            return

        try:
            page.goto(ext_url, timeout=30000, wait_until="domcontentloaded")
            self._handle_external_apply(page, job, resume_path, ext_url)
        except Exception as exc:
            print(f"  [Agent3/Naukri] ATS navigate error for {job.company}: {exc}")
            self.log.record(job, "failed", str(exc)[:100])

    def _naukri_api_apply_url(self, job_id: str,
                              session: "Optional[requests.Session]" = None) -> str:
        """Call Naukri's job detail JSON API to get the external apply URL (free)."""
        _HEADERS = {
            "system-id":  "109",
            "Appid":      "109",
            "clientId":   "d3skt0p",
            "gid":        "LOCATION,INDUSTRY,EDUCATION,FAREA_ROLE",
            "Accept":     "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer":    "https://www.naukri.com/",
        }
        requester = session or requests.Session()
        try:
            resp = requester.get(
                "https://www.naukri.com/jobapi/v3/job",
                params={"jobId": job_id},
                headers=_HEADERS,
                timeout=20,
            )
            if not resp.ok:
                return ""
            data = resp.json()
            # Recursive search handles deeply nested ATS URLs
            found = _naukri_find_apply_link(data)
            if found:
                return found
        except Exception as exc:
            print(f"  [Agent3/Naukri] Job API error (jobId={job_id}): {exc}")
        return ""

    def _handle_external_apply(self, page: Page, job: "JobPosting",
                                resume_path: Path, external_url: str) -> None:
        ats = detect_ats(external_url)
        if not ats or ats not in _ATS_HANDLER_MAP:
            ats_label = ats or "unknown ATS"
            print(f"  [Agent3] No handler for {ats_label} — skipping {job.company}.")
            self.log.record(job, "skipped", f"unsupported ATS: {ats_label}")
            return

        print(f"  [Agent3] External apply via {ats.title()} → {job.company}")

        # Credentials: not needed for Greenhouse/Lever, required for Workday etc.
        creds = {"username": "", "password": ""}
        if ats not in _NO_LOGIN_ATS:
            cred_key_name = f"{ats}_{job.company.lower().replace(' ', '_')[:24]}"
            try:
                creds = self.cred_manager.get_site_credentials(cred_key_name, self.cred_key)
            except (KeyError, FileNotFoundError):
                if not self.telegram:
                    print(f"  [Agent3] No {ats.title()} credentials and Telegram not set — skipping.")
                    self.log.record(job, "skipped", f"no {ats} credentials")
                    return
                creds = self.telegram.request_credentials(
                    ats, external_url, job, self.creds_request_timeout
                )
                if not creds:
                    self.log.record(job, "skipped", f"user skipped {ats} credentials")
                    return
                # Save for future runs
                self.cred_manager.upsert_credential(cred_key_name, "username", creds["username"], self.cred_key)
                self.cred_manager.upsert_credential(cred_key_name, "password", creds["password"], self.cred_key)
                print(f"  [Agent3] {ats.title()} credentials saved for {job.company}")

        page.goto(external_url, timeout=30000)
        ext_handler: BaseApplicationHandler = _ATS_HANDLER_MAP[ats](page)

        if ats not in _NO_LOGIN_ATS:
            logged_in = ext_handler.login(creds["username"], creds["password"])
            if not logged_in:
                print(f"  [Agent3] {ats.title()} login failed for {job.company}.")
                self.log.record(job, "failed", f"{ats} login failed")
                return

        success = ext_handler.apply(job, resume_path)
        status = "submitted" if success else "failed"
        self.log.record(job, status, "" if success else f"{ats} apply error")
        print(f"  [Agent3] {status.upper()} ({ats.title()}) — {job.company}")

    def _handle_captcha(self, job, current_url):
        print(f"  [Agent3] CAPTCHA detected for {job.company}")
        if self.telegram:
            self.telegram.send_captcha_alert(job, current_url, self.captcha_timeout)
            return self.telegram.poll_captcha_resolved(self.captcha_timeout)
        print(f"  [Agent3] Telegram not configured — skipping CAPTCHA job.")
        return False
