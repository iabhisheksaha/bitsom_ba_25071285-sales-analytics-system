"""
Self-contained debug test harness for the job application pipeline.
Runs headless Chromium against a local mock HTTP server that simulates
every bug scenario reported by the user. Iterates until all pass.

Scenarios tested:
  1. Indeed  — "Apply on company site" visible link       → submitted
  2. Indeed  — No button; ATS URL buried in HTML          → submitted (HTML scan)
  3. Indeed  — Page navigates directly to ATS URL         → submitted
  4. LinkedIn — Easy Apply multi-step form                → submitted
  5. LinkedIn — "Applied" badge + ATS URL in page source  → submitted (HTML scan)
  6. LinkedIn — External apply button (popup)             → submitted
  7. Naukri  — scraperapi returns HTML with ATS URL        → submitted
  8. Workday  — Login wall + stored credentials            → submitted
  9. BambooHR — GenericAtsHandler, no login wall           → submitted
 10. already_applied(): skipped job IS retried             → PASS (no block)
 11. already_applied(): submitted job NOT retried          → PASS (blocked)
"""

import json
import os
import sys
import threading
import time
import traceback
import types
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from cryptography.fernet import Fernet

# ── Bootstrap env before any agent import ─────────────────────────────────────
os.environ.setdefault(
    "CHROMIUM_BIN",
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
)
os.environ["HEADLESS"] = "true"
os.environ.pop("BROWSER_PROFILE_DIR", None)   # ephemeral context for tests
os.environ.pop("SCRAPER_API_KEY", None)

# Create a throw-away credential store so imports succeed
_TEST_KEY = Fernet.generate_key()
os.environ["CRED_KEY"] = _TEST_KEY.decode()

from agents.agent1_job_discovery import JobPosting
from agents.agent2_resume_tailoring import ResumeTailoringAgent
from agents.agent4_credentials import CredentialManager, load_key_from_env
import agents.agent3_application as a3
from agents.agent3_application import (
    ApplicationAgent, ApplicationLog, detect_ats,
)

# ── Test credentials.enc ───────────────────────────────────────────────────────
_CRED_PATH = "logs/test_credentials.enc"
_mgr = CredentialManager(_CRED_PATH)
_mgr.init_store(_TEST_KEY, {
    "naukri":   {"username": "test@naukri.com",   "password": "naukri_pass"},
    "linkedin": {"username": "test@linkedin.com", "password": "li_pass"},
    # Workday creds stored with per-company key (matching _handle_external_apply logic)
    "workday_workday_company_ltd":      {"username": "wd@test.com", "password": "wdpass"},
})

# ── Mock HTTP server ───────────────────────────────────────────────────────────
PORT = 18090   # different from any other running server

def _ats_url(path: str) -> str:
    """Return a full mock URL whose path contains an ATS domain fragment
    so detect_ats() and _ATS_URL_RE both recognise it."""
    return f"http://127.0.0.1:{PORT}{path}"


PAGES = {
    # ── Scenario 1: Indeed with visible "Apply on company site" link ──────────
    "/indeed/job_button": f"""<html><body>
      <div class="job-details">VP Product Owner role at Acme Corp</div>
      <a href="{_ats_url('/boards.greenhouse.io/acme/apply/1')}"
         target="_blank" rel="noopener noreferrer">Apply on company site</a>
    </body></html>""",

    # ── Scenario 2: Indeed — ATS URL embedded in JS, no visible button ────────
    "/indeed/job_html_scan": f"""<html><body>
      <div class="job-details">SVP BA at FinServe</div>
      <script>
        var cfg = {{"applyUrl": "{_ats_url('/boards.greenhouse.io/finserve/apply/2')}"}};
      </script>
    </body></html>""",

    # ── Scenario 3: Indeed — page redirects straight to ATS ──────────────────
    # (simulated by serving ATS HTML at the job URL itself)
    "/indeed/job_ats_direct": f"""<html><body>
      <form id="s3.application-form">
        <input id="first_name" name="job_application[first_name]" />
        <input id="last_name"  name="job_application[last_name]" />
        <input id="email"      name="job_application[email]" />
        <input id="phone"      name="job_application[phone]" />
        <input id="resume" type="file" />
        <button id="submit_app">Submit Application</button>
      </form>
    </body></html>""",

    # ── Scenario 4: LinkedIn Easy Apply ───────────────────────────────────────
    "/linkedin/easy_apply": """<html><body>
      <button class="jobs-apply-button" aria-label="Easy Apply to VP PM">Easy Apply</button>
      <div class="jobs-easy-apply-content">
        <input type="text" aria-label="City" />
        <input type="file" />
        <button aria-label="Submit application">Submit application</button>
      </div>
    </body></html>""",

    # ── Scenario 5: LinkedIn "Applied" badge + ATS URL in page source ─────────
    "/linkedin/applied_badge": f"""<html><body>
      <button class="jobs-apply-button" aria-label="Applied">Applied</button>
      <script type="application/json" id="job-data">
        {{"applyUrl": "{_ats_url('/boards.greenhouse.io/techcorp/apply/5')}"}}
      </script>
    </body></html>""",

    # ── Scenario 6: LinkedIn external Apply button (opens popup) ─────────────
    "/linkedin/ext_button": f"""<html><body>
      <a class="jobs-apply-button"
         href="{_ats_url('/jobs.lever.co/startup/apply/6')}"
         target="_blank" rel="noopener noreferrer">Apply</a>
    </body></html>""",

    # ── Greenhouse form (Scenarios 1, 2, 5) ────────────────────────────────────
    # path prefix: /boards.greenhouse.io/
    "/boards.greenhouse.io/acme/apply/1": """<html><body>
      <input id="first_name" /><input id="last_name" />
      <input id="email" /><input id="phone" />
      <input id="resume" type="file" />
      <button id="submit_app">Submit Application</button>
    </body></html>""",

    "/boards.greenhouse.io/finserve/apply/2": """<html><body>
      <input id="first_name" /><input id="last_name" />
      <input id="email" /><input id="phone" />
      <input id="resume" type="file" />
      <button id="submit_app">Submit Application</button>
    </body></html>""",

    "/boards.greenhouse.io/techcorp/apply/5": """<html><body>
      <input id="first_name" /><input id="last_name" />
      <input id="email" /><input id="phone" />
      <input id="resume" type="file" />
      <button id="submit_app">Submit Application</button>
    </body></html>""",

    # ── Lever form (Scenario 6) ────────────────────────────────────────────────
    "/jobs.lever.co/startup/apply/6": """<html><body>
      <input name="name" placeholder="Full Name" />
      <input name="email" placeholder="Email" />
      <input name="phone" placeholder="Phone" />
      <input type="file" />
      <button type="submit">Submit application</button>
    </body></html>""",

    # ── Workday sign-in then form (Scenario 8) ─────────────────────────────────
    # Step 1: sign-in page
    "/myworkdayjobs.com/WorkdayCompanyLtd/apply/8": """<html><body>
      <button data-automation-id="signInButton">Sign In</button>
      <input type="email" data-automation-id="email" />
      <input type="password" data-automation-id="password" />
      <button data-automation-id="signInSubmitButton">Sign In</button>
      <button data-automation-id="applyButton">Apply</button>
      <button data-automation-id="bottom-navigation-next-btn">Next</button>
      <button data-automation-id="bottom-navigation-done-btn">Done</button>
    </body></html>""",

    # ── BambooHR form (Scenario 9) — no login wall ────────────────────────────
    "/app.bamboohr.com/careers/apply/9": """<html><body>
      <input type="text" name="full_name" placeholder="Full Name" />
      <input type="email" name="email" placeholder="Email" />
      <input type="tel" name="phone" placeholder="Phone" />
      <input type="file" />
      <button type="submit">Submit Application</button>
    </body></html>""",
}


class MockHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        html = PAGES.get(self.path, "<html><body><p>OK</p></body></html>")
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<html><body>submitted</body></html>")

    def log_message(self, *_):
        pass   # silence


def start_server() -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", PORT), MockHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    return srv


# ── Helpers ────────────────────────────────────────────────────────────────────

def _cfg():
    cfg = yaml.safe_load(open("config/config.yaml"))
    cfg["application"]["delay_between_apps"] = 0
    cfg["application"]["log_file"] = "logs/test_applications.json"
    return cfg


def _make_agent(cfg) -> ApplicationAgent:
    ag = ApplicationAgent(cfg, _mgr, _TEST_KEY)
    ag.telegram = None           # no Telegram in tests
    ag.delay    = 0
    return ag


def _clear_log(cfg):
    p = Path(cfg["application"]["log_file"])
    if p.exists():
        p.unlink()


def _log_data(cfg) -> dict:
    p = Path(cfg["application"]["log_file"])
    return json.loads(p.read_text()) if p.exists() else {}


def _resume_for(job: JobPosting, cfg) -> dict:
    """Tailor a resume for the job and return the tailored dict."""
    agent2 = ResumeTailoringAgent(cfg)
    path = agent2.tailor(job)
    key = f"{job.platform}::{job.company}::{job.title}"
    return {key: path} if path else {}


PASS = "✅ PASS"
FAIL = "❌ FAIL"
results = []

def run_scenario(n: int, desc: str, fn):
    print(f"\n{'─'*60}")
    print(f"  Scenario {n}: {desc}")
    print(f"{'─'*60}")
    try:
        outcome, detail = fn()
        status = PASS if outcome else FAIL
        print(f"  → {status}  {detail}")
        results.append((n, desc, outcome, detail))
    except Exception:
        tb = traceback.format_exc()
        print(f"  → {FAIL}  EXCEPTION:\n{tb}")
        results.append((n, desc, False, "EXCEPTION"))


# ── Patch _ensure_logged_in so mock server login always succeeds ───────────────
def _mock_ensure_logged_in(self, platform, handler, page):
    # Provide fake cookies so handlers don't try to nav to real LinkedIn/Naukri
    print(f"    [mock] _ensure_logged_in → True for {platform}")
    return True

ApplicationAgent._ensure_logged_in = _mock_ensure_logged_in


# ── Patch scraperapi_fetch for Naukri (Scenario 7) ────────────────────────────
def _mock_scraperapi_fetch(url, render=True, country="in", timeout=None):
    greenhouse_url = _ats_url("/boards.greenhouse.io/naukri_co/apply/7")
    html = f"""<html><body>
      <script>var d = {{"externalApplyLink": "{greenhouse_url}"}};</script>
    </body></html>"""
    resp = MagicMock()
    resp.status_code = 200
    resp.text = html
    resp.ok = True
    return resp

PAGES["/boards.greenhouse.io/naukri_co/apply/7"] = """<html><body>
  <input id="first_name" /><input id="last_name" />
  <input id="email" /><input id="phone" />
  <input id="resume" type="file" />
  <button id="submit_app">Submit Application</button>
</body></html>"""


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  JOB APPLICATION PIPELINE — MOCK DEBUG HARNESS")
    print(f"  {date.today()}  |  Chromium: {os.environ['CHROMIUM_BIN']}")
    print("=" * 60)

    srv = start_server()
    cfg = _cfg()

    # ── Scenario 1: Indeed — visible "Apply on company site" link ────────────
    def s1():
        _clear_log(cfg)
        job = JobPosting(
            platform="indeed", title="VP Product Owner", company="Acme Corp",
            location="Pune", url=_ats_url("/indeed/job_button"),
            jd_text="agile scrum product roadmap stakeholder management",
        )
        resumes = _resume_for(job, cfg)
        _make_agent(cfg).run([job], resumes)
        entry = _log_data(cfg).get("indeed::Acme Corp::VP Product Owner", {})
        ok = entry.get("status") == "submitted"
        return ok, f"status={entry.get('status')} reason={entry.get('reason','')!r}"

    run_scenario(1, "Indeed — visible 'Apply on company site' link", s1)

    # ── Scenario 2: Indeed — ATS URL in HTML, no visible button ──────────────
    def s2():
        _clear_log(cfg)
        job = JobPosting(
            platform="indeed", title="SVP Business Analyst", company="FinServe",
            location="Pune", url=_ats_url("/indeed/job_html_scan"),
            jd_text="BRD FRD gap analysis process improvement",
        )
        resumes = _resume_for(job, cfg)
        _make_agent(cfg).run([job], resumes)
        entry = _log_data(cfg).get("indeed::FinServe::SVP Business Analyst", {})
        ok = entry.get("status") == "submitted"
        return ok, f"status={entry.get('status')} reason={entry.get('reason','')!r}"

    run_scenario(2, "Indeed — ATS URL buried in HTML (HTML scan fallback)", s2)

    # ── Scenario 3: Indeed — page IS the ATS (direct redirect) ───────────────
    def s3():
        _clear_log(cfg)
        # URL looks like a greenhouse URL so detect_ats fires immediately
        ats_url = _ats_url("/boards.greenhouse.io/directco/apply/3")
        PAGES["/boards.greenhouse.io/directco/apply/3"] = """<html><body>
          <input id="first_name" /><input id="last_name" />
          <input id="email" /><input id="phone" />
          <input id="resume" type="file" />
          <button id="submit_app">Submit Application</button>
        </body></html>"""
        job = JobPosting(
            platform="indeed", title="Group VP BA", company="DirectCo",
            location="Pune", url=ats_url,
            jd_text="agile stakeholder budget governance",
        )
        resumes = _resume_for(job, cfg)
        _make_agent(cfg).run([job], resumes)
        entry = _log_data(cfg).get("indeed::DirectCo::Group VP BA", {})
        ok = entry.get("status") == "submitted"
        return ok, f"status={entry.get('status')} reason={entry.get('reason','')!r}"

    run_scenario(3, "Indeed — page IS the ATS (direct navigation to ATS URL)", s3)

    # ── Scenario 4: LinkedIn Easy Apply ──────────────────────────────────────
    def s4():
        _clear_log(cfg)
        job = JobPosting(
            platform="linkedin", title="VP Product Management", company="TechCorp",
            location="Pune", url=_ats_url("/linkedin/easy_apply"),
            jd_text="product roadmap agile okr kpi cross-functional",
        )
        resumes = _resume_for(job, cfg)
        _make_agent(cfg).run([job], resumes)
        entry = _log_data(cfg).get("linkedin::TechCorp::VP Product Management", {})
        ok = entry.get("status") == "submitted"
        return ok, f"status={entry.get('status')} reason={entry.get('reason','')!r}"

    run_scenario(4, "LinkedIn — Easy Apply multi-step form", s4)

    # ── Scenario 5: LinkedIn "Applied" badge — ATS URL in page source ─────────
    def s5():
        _clear_log(cfg)
        job = JobPosting(
            platform="linkedin", title="VP Engineering", company="AppliedCo",
            location="Pune", url=_ats_url("/linkedin/applied_badge"),
            jd_text="product roadmap engineering leadership stakeholder",
        )
        resumes = _resume_for(job, cfg)
        _make_agent(cfg).run([job], resumes)
        entry = _log_data(cfg).get("linkedin::AppliedCo::VP Engineering", {})
        ok = entry.get("status") == "submitted"
        return ok, f"status={entry.get('status')} reason={entry.get('reason','')!r}"

    run_scenario(5, "LinkedIn — 'Applied' badge + ATS URL in page source (HTML scan)", s5)

    # ── Scenario 6: LinkedIn external Apply button opens popup ────────────────
    def s6():
        _clear_log(cfg)
        job = JobPosting(
            platform="linkedin", title="VP Strategy", company="StartupX",
            location="Pune", url=_ats_url("/linkedin/ext_button"),
            jd_text="go-to-market strategy product lifecycle okr",
        )
        resumes = _resume_for(job, cfg)
        _make_agent(cfg).run([job], resumes)
        entry = _log_data(cfg).get("linkedin::StartupX::VP Strategy", {})
        ok = entry.get("status") == "submitted"
        return ok, f"status={entry.get('status')} reason={entry.get('reason','')!r}"

    run_scenario(6, "LinkedIn — External 'Apply' button opens popup → Lever form", s6)

    # ── Scenario 7: Naukri — scraperapi returns HTML with ATS URL ────────────
    def s7():
        _clear_log(cfg)
        # Must patch both the API key guard AND the fetch function so the
        # ScraperAPI branch actually executes with our mock response.
        with patch("agents.agent1_job_discovery.SCRAPER_API_KEY", "fake_test_key"), \
             patch("agents.agent1_job_discovery.scraperapi_fetch",
                   side_effect=_mock_scraperapi_fetch):
            job = JobPosting(
                platform="naukri", title="VP Data Analytics", company="NaukriCo",
                location="Pune", url="https://www.naukri.com/mock-job-vp-7",
                jd_text="analytics data sql stakeholder governance",
            )
            resumes = _resume_for(job, cfg)
            _make_agent(cfg).run([job], resumes)
        entry = _log_data(cfg).get("naukri::NaukriCo::VP Data Analytics", {})
        ok = entry.get("status") == "submitted"
        return ok, f"status={entry.get('status')} reason={entry.get('reason','')!r}"

    run_scenario(7, "Naukri — scraperapi returns page with embedded ATS URL", s7)

    # ── Scenario 8: Workday with stored credentials ───────────────────────────
    def s8():
        _clear_log(cfg)
        ats_url = _ats_url("/myworkdayjobs.com/WorkdayCompanyLtd/apply/8")
        PAGES["/indeed/job_workday"] = f"""<html><body>
          <a href="{ats_url}" target="_blank" rel="noopener noreferrer">
            Apply on company site</a>
        </body></html>"""
        job = JobPosting(
            platform="indeed", title="SVP Product", company="Workday Company Ltd",
            location="Pune", url=_ats_url("/indeed/job_workday"),
            jd_text="agile scrum okr kpi product strategy",
        )
        resumes = _resume_for(job, cfg)
        _make_agent(cfg).run([job], resumes)
        entry = _log_data(cfg).get("indeed::Workday Company Ltd::SVP Product", {})
        ok = entry.get("status") == "submitted"
        return ok, f"status={entry.get('status')} reason={entry.get('reason','')!r}"

    run_scenario(8, "Workday — external ATS with stored credentials", s8)

    # ── Scenario 9: BambooHR — GenericAtsHandler, no login wall ──────────────
    def s9():
        _clear_log(cfg)
        bamboo_url = _ats_url("/app.bamboohr.com/careers/apply/9")
        PAGES["/indeed/job_bamboo"] = f"""<html><body>
          <a href="{bamboo_url}" target="_blank" rel="noopener noreferrer">
            Apply on company site</a>
        </body></html>"""
        job = JobPosting(
            platform="indeed", title="VP Operations", company="BambooFirm",
            location="Pune", url=_ats_url("/indeed/job_bamboo"),
            jd_text="operations leadership cross-functional change management",
        )
        resumes = _resume_for(job, cfg)
        _make_agent(cfg).run([job], resumes)
        entry = _log_data(cfg).get("indeed::BambooFirm::VP Operations", {})
        ok = entry.get("status") == "submitted"
        return ok, f"status={entry.get('status')} reason={entry.get('reason','')!r}"

    run_scenario(9, "BambooHR — GenericAtsHandler, no login wall", s9)

    # ── Scenario 10: already_applied() — skipped job IS retried ──────────────
    def s10():
        log = ApplicationLog("logs/test_already.json")
        p = Path("logs/test_already.json")
        if p.exists():
            p.unlink()
        log = ApplicationLog("logs/test_already.json")
        job = JobPosting("indeed", "VP X", "CompA", "Pune", "http://x.com")
        log.record(job, "skipped", "no button")
        blocked = log.already_applied(job)
        ok = not blocked
        return ok, f"after skipped: already_applied()={blocked}  (should be False)"

    run_scenario(10, "already_applied() — previously skipped job IS retried", s10)

    # ── Scenario 11: already_applied() — submitted job NOT retried ────────────
    def s11():
        log = ApplicationLog("logs/test_already2.json")
        p = Path("logs/test_already2.json")
        if p.exists():
            p.unlink()
        log = ApplicationLog("logs/test_already2.json")
        job = JobPosting("indeed", "VP Y", "CompB", "Pune", "http://y.com")
        log.record(job, "submitted")
        blocked = log.already_applied(job)
        ok = blocked
        return ok, f"after submitted: already_applied()={blocked}  (should be True)"

    run_scenario(11, "already_applied() — previously submitted job IS blocked", s11)

    # ── Final report ──────────────────────────────────────────────────────────
    srv.shutdown()
    print("\n" + "=" * 60)
    print("  FINAL RESULTS")
    print("=" * 60)
    passed = sum(1 for _, _, ok, _ in results if ok)
    for n, desc, ok, detail in results:
        status = "✅" if ok else "❌"
        print(f"  {status}  S{n:02d}: {desc}")
        if not ok:
            print(f"         └─ {detail}")
    print(f"\n  {passed}/{len(results)} scenarios passed")
    if passed < len(results):
        print("\n  ⚠️  Failures above need fixes — re-run after patching.")
        sys.exit(1)
    else:
        print("\n  🎉 All scenarios pass — safe to push and run on Windows PC.")


if __name__ == "__main__":
    main()
