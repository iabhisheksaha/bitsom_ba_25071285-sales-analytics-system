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
from utils.ai_form_filler import (
    fill_page_fields,
    fill_radio_groups,
    load_applicant_profile,
    extract_resume_text,
)


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

_LINKEDIN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _linkedin_offsite_apply_url(job_url: str) -> str:
    """
    Resolve a LinkedIn job-view URL to the external company apply URL.

    LinkedIn's guest job-posting page embeds the off-site apply URL in a hidden
    <code id="applyUrl"> element (wrapped in an HTML comment) for jobs that
    "apply on company website". This is fetchable with a plain HTTP request —
    no login, no proxy, no credits. Returns "" for Easy-Apply (on-site) jobs.
    """
    import re as _re
    import json as _json
    from bs4 import BeautifulSoup as _BS, Comment as _Comment

    m = _re.search(r"(\d{6,})", job_url)
    if not m:
        return ""
    job_id = m.group(1)
    api = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
    try:
        resp = requests.get(
            api,
            headers={"User-Agent": _LINKEDIN_UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=20,
        )
        if not resp.ok:
            print(f"  [Agent3/LinkedIn] Guest API HTTP {resp.status_code} for job {job_id}")
            return ""
    except requests.RequestException as exc:
        print(f"  [Agent3/LinkedIn] Guest API error for job {job_id}: {exc}")
        return ""

    html = resp.text
    soup = _BS(html, "html.parser")
    code = soup.find("code", id="applyUrl")
    if code:
        comment = next((c for c in code.children if isinstance(c, _Comment)), None)
        raw = (str(comment) if comment else code.get_text()).strip()
        try:
            url = _json.loads(raw)
        except Exception:
            url = raw.strip().strip('"')
        if isinstance(url, str) and url.startswith("http"):
            return url
    return ""


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

# ── Browser mode (local vs CI) ─────────────────────────────────────────────
# HEADLESS=false runs a visible browser (needed for local runs so login
# checkpoints/CAPTCHAs can be seen and solved once).
# BROWSER_PROFILE_DIR points at a persistent Chrome profile so LinkedIn/Naukri
# stay logged in across daily runs — log in once via login_setup.py, and every
# subsequent run reuses the saved cookies (no repeated login challenges).
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
BROWSER_PROFILE_DIR = os.environ.get("BROWSER_PROFILE_DIR", "")
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
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
                # ── Easy Apply inline multi-step flow ──────────────────
                easy_apply.click()
                self.page.wait_for_timeout(2000)

                profile = _load_applicant_profile()
                resume_text = extract_resume_text(resume_path)

                for step in range(12):
                    # AI-fill all visible form fields on this step
                    fill_page_fields(self.page, profile, resume_text)
                    fill_radio_groups(self.page, profile, resume_text)
                    self.page.wait_for_timeout(500)

                    # Upload resume if file input present and empty
                    upload = self.page.query_selector(
                        "input[type='file']:not([style*='display: none'])"
                    )
                    if upload and upload.is_visible():
                        upload.set_input_files(str(resume_path.absolute()))
                        self.page.wait_for_timeout(1000)

                    # Uncheck optional "follow company" to avoid noise
                    follow_chk = self.page.query_selector(
                        "input[type='checkbox'][id*='follow']"
                    )
                    if follow_chk and follow_chk.is_checked():
                        follow_chk.click()

                    submit_btn = self.page.query_selector(
                        "button[aria-label*='Submit'], button:has-text('Submit application')"
                    )
                    next_btn   = self.page.query_selector(
                        "button[aria-label*='Next'], button:has-text('Next')"
                    )
                    review_btn = self.page.query_selector(
                        "button[aria-label*='Review'], button:has-text('Review')"
                    )
                    done_btn   = self.page.query_selector(
                        "button[aria-label*='Done'], button:has-text('Done')"
                    )

                    if submit_btn and submit_btn.is_visible():
                        submit_btn.click()
                        self.page.wait_for_timeout(3000)
                        content = self.page.content()
                        if any(kw in content for kw in
                               ["Application submitted", "applied successfully",
                                "You applied", "application was sent"]):
                            print(f"    [LinkedIn] Easy Apply confirmed ✓")
                        if done_btn:
                            done_btn.click()
                        return True
                    elif review_btn and review_btn.is_visible():
                        review_btn.click()
                        self.page.wait_for_timeout(1000)
                    elif next_btn and next_btn.is_visible():
                        next_btn.click()
                        self.page.wait_for_timeout(1500)
                    else:
                        if step > 0:
                            return True
                        break
                return True

            # ── No Easy Apply — search for external apply button ───────
            _EXT_SELECTORS = [
                "button:has-text('Apply'):not(:has-text('Easy'))",
                "a.jobs-apply-button",
                ".jobs-apply-button--top-card a[href]",
                "a[aria-label*='Apply']:not([aria-label*='Easy'])",
                "button[aria-label*='Apply']:not([aria-label*='Easy'])",
                "a[target='_blank'][rel*='noopener'][href*='://']",
            ]
            ext_btn = None
            for _sel in _EXT_SELECTORS:
                try:
                    btn = self.page.query_selector(_sel)
                    if btn and btn.is_visible():
                        if (btn.text_content() or "").strip().lower() == "applied":
                            continue
                        ext_btn = btn
                        break
                except Exception:
                    pass

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
                    # Button didn't open a new tab — navigation in same page
                    self.page.wait_for_timeout(3000)
                    if self.page.url != job.url:
                        raise ExternalApplicationRequired(self.page.url)

            # ── Last resort: scan page HTML for embedded ATS URLs ──────
            try:
                _hits = _ATS_URL_RE.findall(self.page.content())
                if _hits:
                    ext_url = _hits[0].replace('\\/', '/')
                    print(f"    [LinkedIn] Found ATS URL in page source: {ext_url[:70]}")
                    raise ExternalApplicationRequired(ext_url)
            except ExternalApplicationRequired:
                raise
            except Exception:
                pass

            applied_badge = self.page.query_selector(
                "button[aria-label*='Applied'], button:has-text('Applied')"
            )
            reason = (
                "LinkedIn 'Applied' badge shown — no external ATS URL found in page"
                if applied_badge else
                "No apply button found on LinkedIn page"
            )
            raise SkipApplication(reason)

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
        profile = _load_applicant_profile()
        resume_text = extract_resume_text(resume_path)

        try:
            self.page.wait_for_load_state("domcontentloaded")
            self.page.wait_for_timeout(2000)
            apply_btn = self.page.query_selector(
                "[data-automation-id='applyButton'], button:has-text('Apply')"
            )
            if apply_btn:
                apply_btn.click()
                self.page.wait_for_timeout(2000)

            for step in range(12):
                # Upload resume / cover letter file input if visible
                upload = self.page.query_selector(
                    "input[type='file']:not([style*='display: none'])"
                )
                if upload and upload.is_visible():
                    upload.set_input_files(str(resume_path.absolute()))
                    self.page.wait_for_timeout(1000)

                # AI-fill all visible form fields on this wizard step
                fill_page_fields(self.page, profile, resume_text)
                fill_radio_groups(self.page, profile, resume_text)
                self.page.wait_for_timeout(500)

                done   = self.page.query_selector("[data-automation-id='bottom-navigation-done-btn']")
                nxt    = self.page.query_selector("[data-automation-id='bottom-navigation-next-btn']")
                submit = self.page.query_selector(
                    "[data-automation-id='submitButton'], button:has-text('Submit')"
                )

                if done and done.is_visible():
                    done.click()
                    self.page.wait_for_timeout(2000)
                    return True
                elif submit and submit.is_visible():
                    submit.click()
                    self.page.wait_for_timeout(2000)
                    return True
                elif nxt and nxt.is_visible():
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

            # Greenhouse embeds its form inside an iframe on some pages.
            # Navigate directly to the iframe src for reliable access.
            iframe = self.page.query_selector("iframe#grnhse_app, iframe[src*='greenhouse']")
            if iframe:
                src = iframe.get_attribute("src") or ""
                if src.startswith("http"):
                    self.page.goto(src, timeout=30000, wait_until="domcontentloaded")
                    self.page.wait_for_timeout(2000)

            profile = _load_applicant_profile()
            resume_text = extract_resume_text(resume_path)

            # Explicit contact fields (reliable selectors)
            for sel, val in [
                ("#first_name, [name='job_application[first_name]']",  profile.get("first_name", "")),
                ("#last_name, [name='job_application[last_name]']",    profile.get("last_name", "")),
                ("#email, [name='job_application[email]']",            profile.get("email", "")),
                ("#phone, [name='job_application[phone]']",            profile.get("phone", "")),
            ]:
                el = self.page.query_selector(sel)
                if el and val:
                    el.fill(val)

            upload = self.page.query_selector("#resume, input[type='file']")
            if upload:
                upload.set_input_files(str(resume_path.absolute()))
                self.page.wait_for_timeout(1000)

            # AI-fill remaining custom questions, dropdowns, and select fields
            fill_page_fields(self.page, profile, resume_text)
            fill_radio_groups(self.page, profile, resume_text)

            submit = self.page.query_selector(
                "#submit_app, [data-provides='submit-btn'], "
                "button:has-text('Submit Application'), button[type='submit']"
            )
            if submit:
                submit.click()
                self.page.wait_for_timeout(3000)
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

            # Lever trick (proficiently-claude-skills): append /apply to the job URL
            # to navigate directly to the application form instead of clicking Apply.
            current_url = self.page.url.rstrip("/")
            if not current_url.endswith("/apply"):
                apply_url = current_url + "/apply"
                self.page.goto(apply_url, timeout=30000, wait_until="domcontentloaded")
                self.page.wait_for_timeout(2000)
            else:
                apply_btn = self.page.query_selector("a:has-text('Apply'), button:has-text('Apply')")
                if apply_btn:
                    apply_btn.click()
                    self.page.wait_for_timeout(2000)

            profile = _load_applicant_profile()
            resume_text = extract_resume_text(resume_path)

            for sel, val in [
                ("[name='name']",            profile.get("name", "")),
                ("[name='email']",           profile.get("email", "")),
                ("[name='phone']",           profile.get("phone", "")),
                ("[name='org']",             ""),  # current company — leave blank
                ("[name='urls[LinkedIn]']",  profile.get("linkedin_url", "")),
                ("[name='urls[Portfolio]']", profile.get("portfolio_url", "")),
            ]:
                el = self.page.query_selector(sel)
                if el and val:
                    el.fill(val)

            upload = self.page.query_selector("input[type='file']")
            if upload:
                upload.set_input_files(str(resume_path.absolute()))
                self.page.wait_for_timeout(1000)

            # AI-fill custom screening questions and dropdowns
            fill_page_fields(self.page, profile, resume_text)
            fill_radio_groups(self.page, profile, resume_text)

            submit = self.page.query_selector(
                "button[type='submit'], button:has-text('Submit application')"
            )
            if submit:
                submit.click()
                self.page.wait_for_timeout(3000)
            return True
        except PWTimeout as exc:
            print(f"    [Lever] Apply timeout: {exc}")
            return False


class GenericAtsHandler(BaseApplicationHandler):
    """
    Fallback handler for ATS platforms without a dedicated implementation.
    Detects login wall, fills form via AI form filler, uploads resume, submits.
    """

    def login(self, username: str, password: str) -> bool:
        try:
            self.page.wait_for_load_state("domcontentloaded")
            self.page.wait_for_timeout(2000)
            pw_field = self.page.query_selector("input[type='password']")
            if not pw_field:
                return True  # No login wall visible
            email_field = self.page.query_selector(
                "input[type='email'], input[name*='email'], input[name*='user']"
            )
            if email_field:
                email_field.fill(username)
            pw_field.fill(password)
            submit = self.page.query_selector(
                "button[type='submit'], button:has-text('Sign In'), "
                "button:has-text('Log In'), button:has-text('Login')"
            )
            if submit:
                submit.click()
                self.page.wait_for_timeout(3000)
            return True
        except PWTimeout as exc:
            print(f"    [GenericATS] Login timeout: {exc}")
            return False

    def apply(self, job: JobPosting, resume_path: Path) -> bool:
        try:
            self.page.wait_for_load_state("domcontentloaded")
            self.page.wait_for_timeout(2000)

            # Click an Apply button if present (e.g. iCIMS / Taleo landing page)
            apply_btn = self.page.query_selector(
                "a:has-text('Apply Now'), button:has-text('Apply Now'), "
                "a:has-text('Apply'), button:has-text('Apply')"
            )
            if apply_btn and apply_btn.is_visible():
                apply_btn.click()
                self.page.wait_for_timeout(2000)

            profile = _load_applicant_profile()
            resume_text = extract_resume_text(resume_path)
            fill_page_fields(self.page, profile, resume_text)
            fill_radio_groups(self.page, profile, resume_text)

            upload = self.page.query_selector("input[type='file']")
            if upload:
                upload.set_input_files(str(resume_path.absolute()))
                self.page.wait_for_timeout(1000)

            submit = self.page.query_selector(
                "button[type='submit'], button:has-text('Submit'), "
                "button:has-text('Submit Application'), "
                "button:has-text('Submit my application')"
            )
            if submit:
                submit.click()
                self.page.wait_for_timeout(2000)
            return True
        except PWTimeout as exc:
            print(f"    [GenericATS] Apply timeout: {exc}")
            return False


_NO_LOGIN_ATS = {"greenhouse", "lever", "ashby"}

_ATS_HANDLER_MAP: dict[str, type] = {
    "workday":         WorkdayHandler,
    "greenhouse":      GreenhouseHandler,
    "lever":           LeverHandler,
    # Use generic handler for remaining known ATS platforms
    "icims":           GenericAtsHandler,
    "taleo":           GenericAtsHandler,
    "smartrecruiters": GenericAtsHandler,
    "successfactors":  GenericAtsHandler,
    "bamboohr":        GenericAtsHandler,
    "jobvite":         GenericAtsHandler,
    "ashby":           GenericAtsHandler,
}

_applicant_profile_cache: "dict | None" = None

def _load_applicant_profile() -> dict:
    global _applicant_profile_cache
    if _applicant_profile_cache is None:
        _applicant_profile_cache = load_applicant_profile(
            "config/config.yaml", "config/applicant_answers.yaml"
        )
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
        entry = self._data.get(key)
        return entry is not None and entry.get("status") == "submitted"

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

    def _open_session(self, playwright) -> tuple:
        """
        Open a browser session and return (context, page, closer).

        Two modes:
        - Persistent profile (BROWSER_PROFILE_DIR set): launch_persistent_context
          so cookies/logins survive across runs. This is the local-run path —
          you log into LinkedIn/Naukri once via login_setup.py and every daily
          run reuses that session. `closer()` closes the context.
        - Ephemeral (CI / no profile): plain launch + fresh context, as before.
        """
        chromium_bin = CHROMIUM_BIN if Path(CHROMIUM_BIN).exists() else None
        args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                "--ignore-certificate-errors", "--disable-blink-features=AutomationControlled"]

        if BROWSER_PROFILE_DIR:
            Path(BROWSER_PROFILE_DIR).mkdir(parents=True, exist_ok=True)
            kwargs = dict(
                user_data_dir=BROWSER_PROFILE_DIR,
                headless=HEADLESS,
                args=args,
                ignore_https_errors=True,
                user_agent=_BROWSER_UA,
                viewport={"width": 1366, "height": 768},
                locale="en-IN",
            )
            if chromium_bin:
                kwargs["executable_path"] = chromium_bin
            ctx = playwright.chromium.launch_persistent_context(**kwargs)
            print(f"  [Agent3] Persistent browser profile: {BROWSER_PROFILE_DIR} "
                  f"(headless={HEADLESS})")
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            self._apply_stealth(page)
            return ctx, page, ctx.close

        launch_kwargs = dict(headless=HEADLESS, args=args)
        if chromium_bin:
            launch_kwargs["executable_path"] = chromium_bin
        browser = playwright.chromium.launch(**launch_kwargs)
        ctx = browser.new_context(
            ignore_https_errors=True,
            user_agent=_BROWSER_UA,
            viewport={"width": 1366, "height": 768},
            locale="en-IN",
        )
        page = ctx.new_page()
        self._apply_stealth(page)
        return ctx, page, browser.close

    @staticmethod
    def _apply_stealth(page) -> None:
        try:
            from playwright_stealth import stealth_sync
            stealth_sync(page)
        except ImportError:
            pass

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
        _DIRECT_URL_PLATFORMS = {"indeed", "naukri"}

        ctx, page, close = self._open_session(playwright)
        try:
            if platform in _DIRECT_URL_PLATFORMS:
                print(f"  [Agent3] {platform}: using direct-URL apply (follow job link → company ATS).")
                direct_delay = max(5, self.delay // 6)
                for job in jobs:
                    self._apply_via_job_url(page, job, tailored_resumes)
                    time.sleep(direct_delay)
                return

            handler: BaseApplicationHandler = _HANDLER_MAP[platform](page)
            if not self._ensure_logged_in(platform, handler, page):
                print(f"  [Agent3] Not logged in to {platform} — falling back to direct-URL apply.")
                for job in jobs:
                    self._apply_via_job_url(page, job, tailored_resumes)
                    time.sleep(max(5, self.delay // 6))
                return

            for job in jobs:
                self._apply_to_job(handler, page, job, tailored_resumes)
                time.sleep(self.delay)
        finally:
            close()

    def _ensure_logged_in(self, platform: str, handler, page) -> bool:
        """
        Ensure we're logged in to the platform. With a persistent profile the
        session cookie usually survives from a previous login_setup.py run, so
        we check that first and skip the credential login entirely.
        """
        if BROWSER_PROFILE_DIR and platform == "linkedin":
            try:
                page.goto("https://www.linkedin.com/feed/", timeout=30000,
                          wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                if "/feed" in page.url and "login" not in page.url and "authwall" not in page.url:
                    print("  [Agent3] LinkedIn already logged in (persistent profile).")
                    return True
                print("  [Agent3] LinkedIn persistent session not active — trying credential login.")
            except Exception as exc:
                print(f"  [Agent3] LinkedIn session check failed: {exc}")

        try:
            creds = self.cred_manager.get_site_credentials(platform, self.cred_key)
        except (KeyError, FileNotFoundError):
            print(f"  [Agent3] No stored credentials for '{platform}'.")
            return False
        return handler.login(creds.get("username", ""), creds.get("password", ""))

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

        # ── LinkedIn: resolve off-site apply URL via guest API ─────────────
        if job.platform == "linkedin":
            # 1. Cheap HTTP guest API
            ext_url = _linkedin_offsite_apply_url(job.url)
            # 2. Fall back to loading the job-view page in the real browser
            if not ext_url:
                ext_url = self._linkedin_apply_url_via_browser(page, job.url)
            if not ext_url:
                print(f"  [Agent3/LinkedIn] No off-site apply URL (Easy-Apply only) — skipping {job.company}.")
                self.log.record(job, "skipped", "linkedin: easy-apply only (no off-site URL)")
                return
            ats = detect_ats(ext_url)
            print(f"  [Agent3/LinkedIn] Off-site apply URL ({ats or 'unknown'}): {ext_url[:70]}")
            try:
                # _handle_external_apply navigates itself; don't double-navigate
                self._handle_external_apply(page, job, resume_path, ext_url)
            except Exception as exc:
                print(f"  [Agent3/LinkedIn] ATS apply error for {job.company}: {exc}")
                self.log.record(job, "failed", str(exc)[:100])
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

            _ext_selectors = [
                # Indeed-specific attributes
                "a[data-testid='viewJobButtonLinkComponent']",
                "a[href*='applyRedirect']",
                "a[href*='apply_redirect']",
                "a[href*='applyredirect']",
                "#applyButtonLinkContainer a",
                # Text-based (cover common capitalisation variants)
                "a:has-text('Apply on company site')",
                "button:has-text('Apply on company site')",
                "a:has-text('Apply on Company Site')",
                "button:has-text('Apply on Company Site')",
                "a:has-text('Apply now')",
                "a:has-text('Apply Now')",
                "button:has-text('Apply now')",
                "button:has-text('Apply Now')",
                # Class / data heuristics
                "a[class*='external-apply']",
                "a[class*='companyApply']",
                "a[class*='applyButton']",
                "[data-testid='apply-button']",
                # External-link heuristic (target=_blank, non-same-platform)
                "a[target='_blank'][rel*='noopener'][href*='://']",
            ]
            apply_link = None
            for _sel in _ext_selectors:
                try:
                    el = page.query_selector(_sel)
                    if el and el.is_visible():
                        href = el.get_attribute("href") or ""
                        if any(s in href for s in [
                            "indeed.com", "naukri.com", "linkedin.com",
                            "javascript:", "#",
                        ]):
                            continue
                        apply_link = el
                        break
                except Exception:
                    pass

            if not apply_link:
                # Scan page HTML for embedded ATS domain links
                _hits = _ATS_URL_RE.findall(page.content())
                if _hits:
                    _html_ats_url = _hits[0].replace('\\/', '/')
                    print(f"  [Agent3] Found ATS URL in page source: {_html_ats_url[:70]}")
                    self._handle_external_apply(page, job, resume_path, _html_ats_url)
                    return
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

    def _linkedin_apply_url_via_browser(self, page: Page, job_url: str) -> str:
        """
        Load a LinkedIn job-view page in the real browser and extract the
        off-site apply URL from the hidden <code id="applyUrl"> block.
        Used as a fallback when the guest HTTP API is blocked (403).
        """
        import re as _re
        import json as _json
        try:
            page.goto(job_url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            html = page.content()
        except Exception as exc:
            print(f"  [Agent3/LinkedIn] Browser load failed for {job_url[:60]}: {exc}")
            return ""
        m = _re.search(r'id="applyUrl"[^>]*>\s*<!--\s*(.*?)\s*-->', html, _re.DOTALL)
        if m:
            raw = m.group(1).strip()
            try:
                url = _json.loads(raw)
            except Exception:
                url = raw.strip().strip('"')
            if isinstance(url, str) and url.startswith("http"):
                return url
        return ""

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
        # _handle_external_apply navigates itself; GenericAtsHandler covers
        # platforms not yet in _ATS_HANDLER_MAP.
        print(f"  [Agent3/Naukri] Apply URL ({detect_ats(ext_url) or 'generic'}): {ext_url[:80]}")
        try:
            self._handle_external_apply(page, job, resume_path, ext_url)
        except Exception as exc:
            print(f"  [Agent3/Naukri] ATS apply error for {job.company}: {exc}")
            self.log.record(job, "failed", str(exc)[:100])

    def _naukri_api_apply_url(self, job_id: str,
                              session: "Optional[requests.Session]" = None) -> str:
        """Call Naukri's job detail JSON API to get the external apply URL (free)."""
        _HEADERS = {
            "appid":      "109",
            "systemid":   "109",
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
        ats_label = ats.title() if ats else "ATS"

        # Use specific handler if available, otherwise fall back to generic
        handler_class = _ATS_HANDLER_MAP.get(ats, GenericAtsHandler)
        if ats and ats not in _ATS_HANDLER_MAP:
            print(f"  [Agent3] Unknown ATS ({ats_label}) — using generic handler for {job.company}")

        print(f"  [Agent3] External apply via {ats_label} → {job.company}")

        # Navigate to the ATS page so we can inspect for a login wall
        try:
            page.goto(external_url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
        except PWTimeout:
            print(f"  [Agent3] Timeout navigating to {external_url[:70]}")
            self.log.record(job, "failed", "ATS navigation timeout")
            return

        # Decide whether credentials are actually needed by checking the live page
        in_no_login = ats in _NO_LOGIN_ATS
        has_login_wall = bool(page.query_selector("input[type='password']"))
        needs_login = (not in_no_login) and has_login_wall

        creds = {"username": "", "password": ""}
        if not in_no_login:
            cred_key_name = f"{(ats or 'generic')}_{job.company.lower().replace(' ', '_')[:24]}"
            try:
                creds = self.cred_manager.get_site_credentials(cred_key_name, self.cred_key)
                needs_login = True  # stored creds exist — always try login
            except (KeyError, FileNotFoundError):
                if needs_login:
                    # Login wall visible but no stored creds — ask user via Telegram
                    if not self.telegram:
                        print(f"  [Agent3] Login wall but Telegram not set — skipping.")
                        self.log.record(job, "skipped", f"no {ats_label} credentials")
                        return
                    if os.environ.get("CI") == "true":
                        print(f"  [Agent3] {ats_label} needs login; no stored creds (CI) — skipping.")
                        self.log.record(job, "skipped", f"{ats_label} needs account (no stored creds)")
                        return
                    creds_result = self.telegram.request_credentials(
                        ats_label, external_url, job, self.creds_request_timeout
                    )
                    if not creds_result:
                        self.log.record(job, "skipped", f"user skipped {ats_label} credentials")
                        return
                    creds = creds_result
                    self.cred_manager.upsert_credential(
                        cred_key_name, "username", creds["username"], self.cred_key
                    )
                    self.cred_manager.upsert_credential(
                        cred_key_name, "password", creds["password"], self.cred_key
                    )
                    print(f"  [Agent3] {ats_label} credentials saved for {job.company}")

        ext_handler: BaseApplicationHandler = handler_class(page)

        if needs_login and (creds.get("username") or creds.get("password")):
            logged_in = ext_handler.login(creds["username"], creds["password"])
            if not logged_in:
                print(f"  [Agent3] {ats_label} login failed for {job.company}.")
                self.log.record(job, "failed", f"{ats_label} login failed")
                return

        success = ext_handler.apply(job, resume_path)
        status = "submitted" if success else "failed"
        self.log.record(job, status, "" if success else f"{ats_label} apply error")
        print(f"  [Agent3] {status.upper()} ({ats_label}) — {job.company}")

    def _handle_captcha(self, job, current_url):
        print(f"  [Agent3] CAPTCHA detected for {job.company}")
        if self.telegram:
            self.telegram.send_captcha_alert(job, current_url, self.captcha_timeout)
            return self.telegram.poll_captcha_resolved(self.captcha_timeout)
        print(f"  [Agent3] Telegram not configured — skipping CAPTCHA job.")
        return False
