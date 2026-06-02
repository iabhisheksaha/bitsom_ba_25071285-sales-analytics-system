"""One-shot credential store setup — writes key to .env and store to config/."""
from cryptography.fernet import Fernet
from agents.agent4_credentials import CredentialManager
import os

key = Fernet.generate_key()
manager = CredentialManager("config/credentials.enc")

# Placeholder credentials — user replaces these before a real run
manager.init_store(key, {
    "naukri":   {"username": "YOUR_NAUKRI_EMAIL",   "password": "YOUR_NAUKRI_PASSWORD"},
    "indeed":   {"username": "YOUR_INDEED_EMAIL",   "password": "YOUR_INDEED_PASSWORD"},
    "linkedin": {"username": "YOUR_LINKEDIN_EMAIL", "password": "YOUR_LINKEDIN_PASSWORD"},
})

env_line = f'CRED_KEY="{key.decode()}"\n'
with open(".env", "w") as f:
    f.write(env_line)
    f.write('TELEGRAM_BOT_TOKEN=""\n')
    f.write('TELEGRAM_CHAT_ID=""\n')

print(f"Key written to .env")
print(f"Credential store written to config/credentials.enc")
print(f"\nTo activate: export $(cat .env | xargs)")
