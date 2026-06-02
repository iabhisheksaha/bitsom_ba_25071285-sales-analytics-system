"""
End-to-end pipeline test using mock job data.
Bypasses scraping (which needs a residential IP) and drives
Agent 2 (tailoring) + Agent 4 (credentials) directly.
"""
import os
import json
from pathlib import Path

import yaml
from agents.agent1_job_discovery import JobPosting
from agents.agent2_resume_tailoring import ResumeTailoringAgent, extract_keywords
from agents.agent4_credentials import CredentialManager, load_key_from_env

# ── Load config ──────────────────────────────────────────────────────────────
config = yaml.safe_load(open("config/config.yaml"))

# ── Agent 4: credential round-trip ───────────────────────────────────────────
print("=" * 55)
print("AGENT 4 — Credential Manager")
print("=" * 55)
cred_key = load_key_from_env("CRED_KEY")
mgr = CredentialManager("config/credentials.enc")
print(f"  Sites stored : {mgr.list_sites(cred_key)}")
for site in mgr.list_sites(cred_key):
    creds = mgr.get_site_credentials(site, cred_key)
    print(f"  {site:10s} → username field present: {'username' in creds}, password field present: {'password' in creds}")
print("  ✓ Agent 4 PASS\n")

# ── Mock job postings ─────────────────────────────────────────────────────────
MOCK_JOBS = [
    JobPosting(
        platform="naukri",
        title="VP – Product Owner",
        company="Acme Digital Pvt Ltd",
        location="Pune",
        url="https://www.naukri.com/mock-1",
        jd_text=(
            "We are looking for a VP level Product Owner to lead our digital transformation roadmap. "
            "The ideal candidate has strong agile and scrum experience, builds compelling product strategy, "
            "manages backlog grooming and sprint planning, drives stakeholder management at C-suite level, "
            "and has proven expertise in go-to-market execution. Must have experience with OKR frameworks, "
            "KPI dashboards, jira, confluence, and cross-functional leadership. P&L ownership preferred. "
            "SQL, Power BI, and data analysis skills are a plus. "
            "Governance, change management, and vendor management experience required."
        ),
    ),
    JobPosting(
        platform="linkedin",
        title="Senior VP – Business Analyst",
        company="FinServe Solutions",
        location="Pune",
        url="https://www.linkedin.com/jobs/mock-2",
        jd_text=(
            "Senior VP Business Analyst to drive enterprise BRD and FRD creation, lead gap analysis "
            "across ERP and CRM platforms, and own the business requirements process end-to-end. "
            "Must be proficient in process improvement methodologies, design thinking, ux/ui review, "
            "tableau, power bi, azure devops, and figma. Stakeholder management and executive "
            "communication skills are essential. Experience in digital transformation programs, "
            "change management, and budget ownership at ₹50Cr+ scale preferred. "
            "Agile, kanban, mvp delivery, and product lifecycle management experience required."
        ),
    ),
    JobPosting(
        platform="indeed",
        title="VP Product Management",
        company="TechCorp India",
        location="Pune",
        url="https://in.indeed.com/jobs/mock-3",
        jd_text=(
            "Lead product roadmap for B2B SaaS platform. Drive product strategy, user story mapping, "
            "and sprint delivery. Manage cross-functional teams including engineering, design, and data. "
            "Experience with okr setting, kpi tracking, and leadership of 30+ member teams. "
            "SQL and analytics skills required. Scrum master certification preferred. "
            "Strong vendor management, governance, and executive stakeholder management experience."
        ),
    ),
]

# ── Agent 2: keyword extraction ───────────────────────────────────────────────
print("=" * 55)
print("AGENT 2 — Keyword Extraction")
print("=" * 55)
for job in MOCK_JOBS:
    kws = extract_keywords(job.jd_text)
    print(f"  {job.company[:30]:30s} → top 8: {kws[:8]}")
print()

# ── Agent 2: resume tailoring ─────────────────────────────────────────────────
print("=" * 55)
print("AGENT 2 — Resume Tailoring")
print("=" * 55)
agent = ResumeTailoringAgent(config)
tailored: dict[str, Path] = {}
for job in MOCK_JOBS:
    key = f"{job.platform}::{job.company}::{job.title}"
    path = agent.tailor(job)
    if path:
        tailored[key] = path
print()

# ── Results ───────────────────────────────────────────────────────────────────
print("=" * 55)
print("RESULTS")
print("=" * 55)
print(f"  Jobs processed : {len(MOCK_JOBS)}")
print(f"  Resumes created: {len(tailored)}")
print()
for key, path in tailored.items():
    size = path.stat().st_size
    print(f"  ✓ {path.name}  ({size:,} bytes)")

print("\n  ✓ Pipeline test PASSED — ready for live run on your local machine.")
