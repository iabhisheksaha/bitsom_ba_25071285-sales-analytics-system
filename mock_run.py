"""
Full end-to-end mock run — all 4 agents exercised.
Agent 3 uses a local mock HTTP server instead of live job boards
(environment network policy blocks outbound to Naukri/LinkedIn/Indeed).
"""

import json
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import yaml

from agents.agent1_job_discovery import JobPosting
from agents.agent2_resume_tailoring import ResumeTailoringAgent
from agents.agent3_application import ApplicationAgent
from agents.agent4_credentials import CredentialManager, load_key_from_env

DIV = "=" * 60

# ── Mock HTTP server — serves fake Naukri / LinkedIn login + apply pages ─────

NAUKRI_LOGIN_HTML = """
<html><body>
  <input id="usernameField" type="text" />
  <input id="passwordField" type="password" />
  <button type="submit">Login</button>
</body></html>"""

NAUKRI_JOB_HTML = """
<html><body>
  <button class="apply-button">Apply Now</button>
  <input type="file" />
  <button type="submit">Submit Application</button>
</body></html>"""

LINKEDIN_LOGIN_HTML = """
<html><body>
  <input id="username" type="text" />
  <input id="password" type="password" />
  <button type="submit">Sign In</button>
</body></html>"""

LINKEDIN_JOB_HTML = """
<html><body>
  <button class="jobs-apply-button">Easy Apply</button>
  <input type="file" />
  <button aria-label="Submit application">Submit</button>
</body></html>"""


class MockJobHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        p = self.path
        if "nlogin/login" in p:
            body = NAUKRI_LOGIN_HTML
        elif "naukri.com" in p or "naukri" in p:
            body = NAUKRI_JOB_HTML
        elif "linkedin.com/login" in p:
            body = LINKEDIN_LOGIN_HTML
        elif "linkedin.com" in p:
            body = LINKEDIN_JOB_HTML
        else:
            body = "<html><body>OK</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_POST(self):
        self.send_response(200)
        self.end_headers()
        # Simulate redirect to dashboard after login
        self.wfile.write(b"<html><body>logged in</body></html>")

    def log_message(self, *args):
        pass  # silence server logs


def start_mock_server(port=18080):
    server = HTTPServer(("127.0.0.1", port), MockJobHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


# ── Mock jobs pointing at local server ────────────────────────────────────────

def make_mock_jobs(port=18080):
    base = f"http://127.0.0.1:{port}"
    return [
        JobPosting(
            platform="naukri",
            title="VP – Product Owner",
            company="Acme Digital Pvt Ltd",
            location="Pune",
            url=f"{base}/naukri.com/job/vp-product-owner-acme-1",
            jd_text=(
                "VP Product Owner: agile, scrum, product strategy, backlog grooming, "
                "sprint planning, stakeholder management, go-to-market, OKR, KPI, jira, "
                "confluence, cross-functional leadership, P&L, SQL, Power BI, governance, "
                "change management, vendor management, digital transformation."
            ),
        ),
        JobPosting(
            platform="naukri",
            title="Senior VP – Business Analyst",
            company="FinServe Solutions",
            location="Pune",
            url=f"{base}/naukri.com/job/svp-ba-finserve-2",
            jd_text=(
                "SVP Business Analyst: BRD, FRD, gap analysis, ERP/CRM, process improvement, "
                "design thinking, UX/UI, tableau, power bi, azure devops, stakeholder management, "
                "digital transformation, change management, budget ownership, agile, kanban, "
                "MVP delivery, product lifecycle."
            ),
        ),
        JobPosting(
            platform="linkedin",
            title="VP Product Management",
            company="TechCorp India",
            location="Pune",
            url=f"{base}/linkedin.com/jobs/view/vp-product-mgmt-techcorp-3",
            jd_text=(
                "VP Product for B2B SaaS: product roadmap, product strategy, user story mapping, "
                "sprint delivery, cross-functional teams, OKR, KPI, SQL, analytics, scrum, "
                "vendor management, governance, executive stakeholder management, leadership."
            ),
        ),
    ]


# ── Patch NaukriHandler login to work with our local mock server ──────────────
# The mock server doesn't redirect to naukri.com after login,
# so we accept any 200 response from the login POST as success.

from agents import agent3_application as a3

_orig_naukri_login = a3.NaukriHandler.login
_orig_linkedin_login = a3.LinkedInHandler.login

def _mock_naukri_login(self, username, password):
    try:
        self.page.goto(self.LOGIN_URL, timeout=15000)
        self.page.wait_for_selector("#usernameField", timeout=10000)
        self.page.fill("#usernameField", username)
        self.page.fill("#passwordField", password)
        self.page.click("button[type='submit']")
        self.page.wait_for_timeout(500)
        print(f"    [Naukri/Mock] Login submitted for {username}")
        return True
    except Exception as exc:
        print(f"    [Naukri/Mock] Login error: {exc}")
        return False

def _mock_linkedin_login(self, username, password):
    try:
        self.page.goto(self.LOGIN_URL, timeout=15000)
        self.page.wait_for_selector("#username", timeout=10000)
        self.page.fill("#username", username)
        self.page.fill("#password", password)
        self.page.click("button[type='submit']")
        self.page.wait_for_timeout(500)
        print(f"    [LinkedIn/Mock] Login submitted for {username}")
        return True
    except Exception as exc:
        print(f"    [LinkedIn/Mock] Login error: {exc}")
        return False

a3.NaukriHandler.LOGIN_URL = "http://127.0.0.1:18080/nlogin/login"
a3.LinkedInHandler.LOGIN_URL = "http://127.0.0.1:18080/linkedin.com/login"
a3.NaukriHandler.login = _mock_naukri_login
a3.LinkedInHandler.login = _mock_linkedin_login


# ── Main ──────────────────────────────────────────────────────────────────────

print(DIV)
print("  AUTOMATED JOB APPLICATION — FULL MOCK RUN")
print(DIV)

config = yaml.safe_load(open("config/config.yaml"))
config["application"]["delay_between_apps"] = 0
cred_key = load_key_from_env("CRED_KEY")
mgr = CredentialManager("config/credentials.enc")

# AGENT 4 ─────────────────────────────────────────────────────────────────────
print("\n[Agent 4] Credential Manager")
for site in mgr.list_sites(cred_key):
    c = mgr.get_site_credentials(site, cred_key)
    print(f"  {site:10s} → {c['username']}  password: {'*' * len(c['password'])}")
print("  ✓ Credentials decrypted OK")

# AGENT 1 ─────────────────────────────────────────────────────────────────────
print("\n[Agent 1] Job Discovery (mock data injected)")
server = start_mock_server()
time.sleep(0.3)
jobs = make_mock_jobs()
for j in jobs:
    print(f"  [{j.platform.upper():8s}] {j.title:35s} @ {j.company}")
Path("logs").mkdir(exist_ok=True)
Path("logs/discovered_jobs.json").write_text(
    json.dumps([vars(j) for j in jobs], indent=2, default=str)
)
print(f"  ✓ {len(jobs)} jobs | checkpoint → logs/discovered_jobs.json")

# AGENT 2 ─────────────────────────────────────────────────────────────────────
print("\n[Agent 2] Resume Tailoring")
tailoring_agent = ResumeTailoringAgent(config)
tailored: dict[str, Path] = {}
for job in jobs:
    key = f"{job.platform}::{job.company}::{job.title}"
    path = tailoring_agent.tailor(job)
    if path:
        tailored[key] = path
print(f"  ✓ {len(tailored)}/{len(jobs)} resumes tailored")
for p in tailored.values():
    print(f"    → {p.name}  ({p.stat().st_size:,} bytes)")

# AGENT 3 ─────────────────────────────────────────────────────────────────────
print("\n[Agent 3] Application Execution (Playwright → local mock server)")
app_agent = ApplicationAgent(config, mgr, cred_key)
app_agent.run(jobs, tailored)

# ── Final summary ─────────────────────────────────────────────────────────────
print(f"\n{DIV}")
print("  RUN SUMMARY")
print(DIV)
log_data = json.loads(Path(config["application"]["log_file"]).read_text())
today = date.today().isoformat()
icons = {"submitted": "✅", "failed": "❌", "skipped": "⚠️", "simulated": "🔵"}
for entry in log_data.values():
    if entry["timestamp"].startswith(today):
        icon = icons.get(entry["status"], "•")
        reason = f"  ({entry['reason']})" if entry.get("reason") else ""
        print(f"  {icon} [{entry['platform'].upper():8s}] {entry['company']:26s} | {entry['title'][:32]:32s} | {entry['status']}{reason}")

submitted = [e for e in log_data.values() if e["timestamp"].startswith(today) and e["status"] == "submitted"]
failed    = [e for e in log_data.values() if e["timestamp"].startswith(today) and e["status"] == "failed"]
print(f"\n  Total today → submitted: {len(submitted)}  failed: {len(failed)}")
print(f"  Log → logs/applications.json")
print(f"  Resumes → resume/tailored/")
print(DIV)
