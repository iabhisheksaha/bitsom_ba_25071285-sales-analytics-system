"""
One-time interactive setup: stores job-site passwords in config/credentials.enc
and fills in phone / LinkedIn URL in config/config.yaml.

Run AFTER storing your CRED_KEY in Windows Credential Manager (step 3 below),
with CRED_KEY loaded into the environment:

    PowerShell:
        Import-Module CredentialManager
        $env:CRED_KEY = (Get-StoredCredential -Target JobApp_CRED_KEY).GetNetworkCredential().Password
        python init_credentials.py
"""

import getpass
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from agents.agent4_credentials import CredentialManager


def _update_config_yaml(phone: str, linkedin_url: str) -> None:
    cfg = Path("config/config.yaml")
    if not cfg.exists():
        print("[WARN] config/config.yaml not found — skipping phone/LinkedIn update.")
        return
    text = cfg.read_text(encoding="utf-8")
    if phone:
        text = re.sub(r'(phone:\s*)""', f'phone: "{phone}"', text)
    if linkedin_url:
        text = re.sub(r'(linkedin_url:\s*)""', f'linkedin_url: "{linkedin_url}"', text)
    cfg.write_text(text, encoding="utf-8")
    print("  config/config.yaml updated with phone and LinkedIn URL.")


def main() -> None:
    key_str = os.environ.get("CRED_KEY", "").strip()
    if not key_str:
        print("\n[ERROR] CRED_KEY environment variable is not set.")
        print("  In PowerShell:")
        print("    Import-Module CredentialManager")
        print("    $env:CRED_KEY = (Get-StoredCredential -Target JobApp_CRED_KEY)"
              ".GetNetworkCredential().Password")
        sys.exit(1)

    key = key_str.encode()
    mgr = CredentialManager("config/credentials.enc")

    print("\n=== Job Application — One-Time Credential Setup ===\n")
    print("Your passwords are encrypted with your CRED_KEY and stored in")
    print("config/credentials.enc  (the key itself stays only in Windows")
    print("Credential Manager — never written to any file here).\n")

    # ── Job-site credentials ──────────────────────────────────────────
    print("--- Job site logins ---")
    naukri_user   = input("Naukri email       : ").strip()
    naukri_pass   = getpass.getpass("Naukri password    : ")
    linkedin_user = input("LinkedIn email     : ").strip()
    linkedin_pass = getpass.getpass("LinkedIn password  : ")

    creds = {
        "naukri":   {"username": naukri_user,   "password": naukri_pass},
        "linkedin": {"username": linkedin_user, "password": linkedin_pass},
    }
    mgr.init_store(key, creds)
    naukri_pass = linkedin_pass = ""   # unbind — don't keep in memory

    # ── Personal details for application forms ────────────────────────
    print("\n--- Your contact details (used to fill ATS application forms) ---")
    phone = input("Mobile number (e.g. +91 98765 43210) : ").strip()
    linkedin_url = input("LinkedIn profile URL               : ").strip()

    _update_config_yaml(phone, linkedin_url)

    print("\nSetup complete.")
    print("  config/credentials.enc  — encrypted site passwords")
    print("  config/config.yaml      — phone + LinkedIn URL updated")
    print("\nNext: run  python login_setup.py  to save the browser session.")


if __name__ == "__main__":
    main()
