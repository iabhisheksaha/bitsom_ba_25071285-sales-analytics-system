"""
Agent 3: Application Execution and Notification
Navigates to job pages, logs in, fills forms, uploads the tailored resume,
handles CAPTCHAs via Telegram, and sends a daily summary.
"""

import json
import os
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import requests
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from agents.agent1_job_discovery import JobPosting
from agents.agent4_credentials import CredentialManager


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
        """
        Poll the Telegram getUpdates endpoint to detect when the user replies
        `/captcha_done`, confirming the CAPTCHA has been solved.
        Returns True if confirmed within timeout, False otherwise.
        """
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


def detect_captcha(driver: webdriver.Chrome) -> bool:
    for selector in _CAPTCHA_SELECTORS:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            if elements:
                return True
        except WebDriverException:
            pass
    return False


# ---------------------------------------------------------------------------
# Platform-specific application handlers
# ---------------------------------------------------------------------------

class BaseApplicationHandler:
    def __init__(self, driver: webdriver.Chrome, wait: WebDriverWait):
        self.driver = driver
        self.wait = wait

    def login(self, username: str, password: str) -> bool:
        raise NotImplementedError

    def apply(self, job: JobPosting, resume_path: Path) -> bool:
        raise NotImplementedError


class NaukriHandler(BaseApplicationHandler):

    LOGIN_URL = "https://www.naukri.com/nlogin/login"

    def login(self, username: str, password: str) -> bool:
        try:
            self.driver.get(self.LOGIN_URL)
            self.wait.until(EC.presence_of_element_located((By.ID, "usernameField")))
            self.driver.find_element(By.ID, "usernameField").send_keys(username)
            self.driver.find_element(By.ID, "passwordField").send_keys(password)
            self.driver.find_element(By.XPATH, "//button[@type='submit']").click()
            time.sleep(3)
            return "naukri.com" in self.driver.current_url
        except (NoSuchElementException, TimeoutException) as exc:
            print(f"    [Naukri] Login failed: {exc}")
            return False

    def apply(self, job: JobPosting, resume_path: Path) -> bool:
        try:
            self.driver.get(job.url)
            self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "[class*='apply']")))
            apply_btn = self.driver.find_element(By.CSS_SELECTOR, "button[class*='apply'], a[class*='apply']")
            apply_btn.click()
            time.sleep(2)
            # Upload resume if prompted
            try:
                upload = self.driver.find_element(By.CSS_SELECTOR, "input[type='file']")
                upload.send_keys(str(resume_path.absolute()))
                time.sleep(1)
            except NoSuchElementException:
                pass
            # Submit
            submit = self.driver.find_element(By.CSS_SELECTOR, "button[type='submit'], button[class*='submit']")
            submit.click()
            time.sleep(2)
            return True
        except (NoSuchElementException, TimeoutException) as exc:
            print(f"    [Naukri] Apply failed: {exc}")
            return False


class IndeedHandler(BaseApplicationHandler):

    LOGIN_URL = "https://secure.indeed.com/auth"

    def login(self, username: str, password: str) -> bool:
        try:
            self.driver.get(self.LOGIN_URL)
            self.wait.until(EC.presence_of_element_located((By.ID, "login-email-input")))
            self.driver.find_element(By.ID, "login-email-input").send_keys(username)
            self.driver.find_element(By.XPATH, "//button[@type='submit']").click()
            self.wait.until(EC.presence_of_element_located((By.ID, "login-password-input")))
            self.driver.find_element(By.ID, "login-password-input").send_keys(password)
            self.driver.find_element(By.XPATH, "//button[@type='submit']").click()
            time.sleep(3)
            return True
        except (NoSuchElementException, TimeoutException) as exc:
            print(f"    [Indeed] Login failed: {exc}")
            return False

    def apply(self, job: JobPosting, resume_path: Path) -> bool:
        try:
            self.driver.get(job.url)
            apply_btn = self.wait.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "button[class*='ia-continueButton'], #indeedApplyButton")
            ))
            apply_btn.click()
            time.sleep(2)
            try:
                upload = self.driver.find_element(By.CSS_SELECTOR, "input[type='file']")
                upload.send_keys(str(resume_path.absolute()))
                time.sleep(1)
            except NoSuchElementException:
                pass
            submit = self.driver.find_element(By.XPATH, "//button[contains(text(), 'Submit')]")
            submit.click()
            time.sleep(2)
            return True
        except (NoSuchElementException, TimeoutException) as exc:
            print(f"    [Indeed] Apply failed: {exc}")
            return False


class LinkedInHandler(BaseApplicationHandler):

    LOGIN_URL = "https://www.linkedin.com/login"

    def login(self, username: str, password: str) -> bool:
        try:
            self.driver.get(self.LOGIN_URL)
            self.wait.until(EC.presence_of_element_located((By.ID, "username")))
            self.driver.find_element(By.ID, "username").send_keys(username)
            self.driver.find_element(By.ID, "password").send_keys(password)
            self.driver.find_element(By.XPATH, "//button[@type='submit']").click()
            time.sleep(3)
            return "linkedin.com/feed" in self.driver.current_url
        except (NoSuchElementException, TimeoutException) as exc:
            print(f"    [LinkedIn] Login failed: {exc}")
            return False

    def apply(self, job: JobPosting, resume_path: Path) -> bool:
        try:
            self.driver.get(job.url)
            easy_apply = self.wait.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "button[class*='jobs-apply-button']")
            ))
            easy_apply.click()
            time.sleep(2)
            # Handle multi-step Easy Apply modal
            for _ in range(5):
                try:
                    upload = self.driver.find_element(By.CSS_SELECTOR, "input[type='file']")
                    upload.send_keys(str(resume_path.absolute()))
                    time.sleep(1)
                except NoSuchElementException:
                    pass
                try:
                    next_btn = self.driver.find_element(
                        By.XPATH, "//button[contains(@aria-label, 'Submit') or contains(@aria-label, 'Next')]"
                    )
                    next_btn.click()
                    time.sleep(1)
                    if "aria-label" in next_btn.get_attribute("outerHTML") and "Submit" in next_btn.get_attribute("aria-label"):
                        break
                except NoSuchElementException:
                    break
            return True
        except (NoSuchElementException, TimeoutException) as exc:
            print(f"    [LinkedIn] Apply failed: {exc}")
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
        if not self.telegram:
            print("[Agent3] Telegram not configured — CAPTCHA alerts and summaries disabled.")

    # ------------------------------------------------------------------
    # Browser lifecycle
    # ------------------------------------------------------------------

    def _build_driver(self) -> webdriver.Chrome:
        opts = Options()
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1280,800")
        # Reduce fingerprinting
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        return webdriver.Chrome(options=opts)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, jobs: list[JobPosting], tailored_resumes: dict[str, Path]) -> None:
        """
        Process the job list. tailored_resumes maps f"{platform}::{company}::{title}" → Path.
        """
        print(f"[Agent3] Starting application run for {len(jobs)} jobs")

        # Group by platform so we only log in once per site
        by_platform: dict[str, list[JobPosting]] = {}
        for job in jobs:
            by_platform.setdefault(job.platform, []).append(job)

        for platform, platform_jobs in by_platform.items():
            self._process_platform(platform, platform_jobs, tailored_resumes)

        # Daily summary
        submitted, failed = self.log.todays_summary()
        print(f"\n[Agent3] Daily totals — submitted: {len(submitted)}, failed: {len(failed)}")
        if self.telegram:
            self.telegram.send_daily_summary(submitted, failed)

    def _process_platform(
        self,
        platform: str,
        jobs: list[JobPosting],
        tailored_resumes: dict[str, Path],
    ) -> None:
        if platform not in _HANDLER_MAP:
            print(f"  [Agent3] No handler for platform '{platform}' — skipping.")
            return

        try:
            creds = self.cred_manager.get_site_credentials(platform, self.cred_key)
        except KeyError:
            print(f"  [Agent3] No credentials for '{platform}' — skipping.")
            return

        driver = self._build_driver()
        wait = WebDriverWait(driver, timeout=20)
        handler: BaseApplicationHandler = _HANDLER_MAP[platform](driver, wait)

        try:
            logged_in = handler.login(creds.get("username", ""), creds.get("password", ""))
            if not logged_in:
                print(f"  [Agent3] Login failed for {platform}")
                return

            for job in jobs:
                self._apply_to_job(handler, driver, job, tailored_resumes)
                time.sleep(self.delay)
        finally:
            driver.quit()

    def _apply_to_job(
        self,
        handler: BaseApplicationHandler,
        driver: webdriver.Chrome,
        job: JobPosting,
        tailored_resumes: dict[str, Path],
    ) -> None:
        key = f"{job.platform}::{job.company}::{job.title}"

        if self.log.already_applied(job):
            print(f"  [Agent3] Already applied to {job.company} ({job.title}) — skipping.")
            return

        resume_path = tailored_resumes.get(key)
        if not resume_path or not resume_path.exists():
            print(f"  [Agent3] No tailored resume for {key} — skipping.")
            self.log.record(job, "skipped", "no resume")
            return

        print(f"  [Agent3] Applying → {job.company} | {job.title} ({job.platform})")

        # Check for CAPTCHA before attempting
        if detect_captcha(driver):
            resolved = self._handle_captcha(job, driver.current_url)
            if not resolved:
                self.log.record(job, "failed", "CAPTCHA timeout")
                return

        success = handler.apply(job, resume_path)

        # Post-apply CAPTCHA check
        if success and detect_captcha(driver):
            resolved = self._handle_captcha(job, driver.current_url)
            success = resolved

        status = "submitted" if success else "failed"
        self.log.record(job, status, "" if success else "apply error")
        print(f"  [Agent3] {status.upper()} — {job.company}")

    def _handle_captcha(self, job: JobPosting, current_url: str) -> bool:
        print(f"  [Agent3] CAPTCHA detected for {job.company}")
        if self.telegram:
            self.telegram.send_captcha_alert(job, current_url, self.captcha_timeout)
            return self.telegram.poll_captcha_resolved(self.captcha_timeout)
        else:
            print(
                f"  [Agent3] CAPTCHA on {current_url} — Telegram not configured. "
                "Cannot request manual intervention. Skipping."
            )
            return False
