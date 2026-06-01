"""
Automated Job Application System — Orchestrator

Coordinates Agent 1 (discovery) → Agent 2 (tailoring) → Agent 3 (application).
Agent 4 (credential management) is called by Agent 3 at runtime.

Usage:
    # First-time setup: create encrypted credential store
    python orchestrator.py --init-creds

    # Daily run
    python orchestrator.py

Environment variables required for a full run:
    CRED_KEY             — Fernet key for the credential store
    TELEGRAM_BOT_TOKEN   — Telegram bot token
    TELEGRAM_CHAT_ID     — Target chat / channel ID
"""

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from agents.agent1_job_discovery import JobDiscoveryAgent, JobPosting
from agents.agent2_resume_tailoring import ResumeTailoringAgent
from agents.agent3_application import ApplicationAgent
from agents.agent4_credentials import CredentialManager, load_key_from_env


CONFIG_PATH = "config/config.yaml"


def load_config() -> dict:
    path = Path(CONFIG_PATH)
    if not path.exists():
        print(f"[Orchestrator] Config file not found at {CONFIG_PATH}")
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# First-time credential setup
# ---------------------------------------------------------------------------

def init_credentials(config: dict) -> None:
    """Interactive wizard to create the encrypted credential store."""
    from cryptography.fernet import Fernet

    print("\n=== Credential Store Initialisation ===\n")
    key = Fernet.generate_key()
    print(f"Generated Fernet key (save this securely — you need it every run):\n\n  {key.decode()}\n")
    print("Set it as: export CRED_KEY='<above key>'\n")

    cred_path = config.get("credentials", {}).get("encrypted_file", "config/credentials.enc")
    manager = CredentialManager(cred_path)

    credentials: dict = {}
    platforms = [p for p, enabled in config.get("search", {}).get("platforms", {}).items() if enabled]
    for platform in platforms:
        print(f"--- {platform.title()} ---")
        username = input(f"  Username / email for {platform}: ").strip()
        password = input(f"  Password for {platform}: ").strip()
        if username:
            credentials[platform] = {"username": username, "password": password}

    if credentials:
        manager.init_store(key, credentials)
        print(f"\n[Orchestrator] Credentials encrypted and saved to {cred_path}")
    else:
        print("[Orchestrator] No credentials entered — nothing saved.")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(config: dict) -> None:
    print("\n" + "=" * 50)
    print("   AUTOMATED JOB APPLICATION SYSTEM")
    print("=" * 50)

    # --- Agent 4: Load credentials key ---
    try:
        cred_key = load_key_from_env("CRED_KEY")
    except EnvironmentError as exc:
        print(f"\n[Orchestrator] {exc}")
        print("Hint: export CRED_KEY='<your Fernet key>'")
        sys.exit(1)

    cred_path = config.get("credentials", {}).get("encrypted_file", "config/credentials.enc")
    credential_manager = CredentialManager(cred_path)

    # --- Agent 1: Job Discovery ---
    print("\n[Step 1/3] Discovering jobs…")
    discovery_agent = JobDiscoveryAgent(config)
    jobs: list[JobPosting] = discovery_agent.discover()

    if not jobs:
        print("[Orchestrator] No jobs discovered. Exiting.")
        return

    _checkpoint("discovered_jobs.json", [vars(j) for j in jobs])

    # --- Agent 2: Resume Tailoring ---
    print(f"\n[Step 2/3] Tailoring resume for {len(jobs)} jobs…")
    tailoring_agent = ResumeTailoringAgent(config)
    tailored_resumes: dict[str, Path] = {}

    for job in jobs:
        key = f"{job.platform}::{job.company}::{job.title}"
        output_path = tailoring_agent.tailor(job)
        if output_path:
            tailored_resumes[key] = output_path

    print(f"[Orchestrator] {len(tailored_resumes)} tailored resumes ready.")

    # --- Agent 3: Application Execution ---
    print(f"\n[Step 3/3] Submitting applications…")
    application_agent = ApplicationAgent(config, credential_manager, cred_key)
    application_agent.run(jobs, tailored_resumes)

    print("\n[Orchestrator] Pipeline complete.")


def _checkpoint(filename: str, data) -> None:
    """Write a JSON checkpoint to logs/ for debugging / resume."""
    path = Path("logs") / filename
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Automated Job Application System")
    parser.add_argument(
        "--init-creds",
        action="store_true",
        help="Interactive wizard to create the encrypted credential store",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run discovery and tailoring only (skip application submission)",
    )
    args = parser.parse_args()

    cfg = load_config()

    if args.init_creds:
        init_credentials(cfg)
    elif args.dry_run:
        print("[Orchestrator] Dry-run mode — application submission skipped.")
        cred_key = load_key_from_env("CRED_KEY") if os.environ.get("CRED_KEY") else b""
        print("\n[Step 1/2] Discovering jobs…")
        jobs = JobDiscoveryAgent(cfg).discover()
        _checkpoint("discovered_jobs.json", [vars(j) for j in jobs])
        print(f"\n[Step 2/2] Tailoring resumes for {len(jobs)} jobs…")
        agent = ResumeTailoringAgent(cfg)
        for job in jobs:
            agent.tailor(job)
        print("[Orchestrator] Dry-run complete.")
    else:
        run_pipeline(cfg)
