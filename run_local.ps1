# run_local.ps1 — One-shot runner for the Job Application pipeline on Windows.
#
# Requirements:
#   - Python 3.11 installed and on PATH
#   - pip, git on PATH
#   - Windows Credential Manager entries (create once with New-StoredCredential):
#       JobApp_CRED_KEY         — Fernet key (from orchestrator --init-creds)
#       JobApp_TG_BOT_TOKEN     — Telegram bot token
#       JobApp_TG_CHAT_ID       — Telegram chat ID
#       JobApp_ANTHROPIC_KEY    — Anthropic API key (enables LLM Council scoring)
#       JobApp_OPENROUTER_KEY   — OpenRouter API key (optional; enables multi-model council)
#
# Usage:
#   Right-click → "Run with PowerShell"  OR
#   powershell -ExecutionPolicy Bypass -File run_local.ps1
#
# To dry-run (discover + tailor only, no applications submitted):
#   powershell -ExecutionPolicy Bypass -File run_local.ps1 --dry-run

param([switch]$DryRun)

Set-StrictMode -Off
$ErrorActionPreference = "Stop"

# ── 1. Load secrets from Windows Credential Manager ──────────────────────────
try { Import-Module CredentialManager -ErrorAction Stop }
catch { Write-Error "Install-Module CredentialManager first: Install-Module -Name CredentialManager"; exit 1 }

function Get-Cred($target) {
    $c = Get-StoredCredential -Target $target
    if (-not $c) { throw "Credential '$target' not found. Run: New-StoredCredential -Target '$target' -UserName key -Password '<value>' -Persist LocalMachine" }
    return $c.GetNetworkCredential().Password
}

Write-Host "[1/7] Loading secrets from Windows Credential Manager..."
$env:CRED_KEY           = Get-Cred "JobApp_CRED_KEY"
$env:TELEGRAM_BOT_TOKEN = Get-Cred "JobApp_TG_BOT_TOKEN"
$env:TELEGRAM_CHAT_ID   = Get-Cred "JobApp_TG_CHAT_ID"

# LLM Council API keys — optional but strongly recommended for real AI scoring.
# If missing, council falls back to heuristic scores (flat 7.0 for all jobs).
$anthropicCred = Get-StoredCredential -Target "JobApp_ANTHROPIC_KEY"
if ($anthropicCred) {
    $env:ANTHROPIC_API_KEY = $anthropicCred.GetNetworkCredential().Password
    Write-Host "      ANTHROPIC_API_KEY loaded (LLM Council enabled)."
} else {
    Write-Host "      WARNING: JobApp_ANTHROPIC_KEY not found — council scoring will use heuristics."
    Write-Host "               Add it with: New-StoredCredential -Target 'JobApp_ANTHROPIC_KEY' -UserName key -Password '<your-key>' -Persist LocalMachine"
}

$openrouterCred = Get-StoredCredential -Target "JobApp_OPENROUTER_KEY"
if ($openrouterCred) {
    $env:OPENROUTER_API_KEY = $openrouterCred.GetNetworkCredential().Password
    Write-Host "      OPENROUTER_API_KEY loaded (LLM Council enabled via OpenRouter free tier)."
} else {
    Write-Host "      WARNING: JobApp_OPENROUTER_KEY not found — trying Google AI Studio key..."
    Write-Host "               Get a free key at: https://aistudio.google.com/app/apikey"
    Write-Host "               Then: New-StoredCredential -Target 'JobApp_OPENROUTER_KEY' -UserName key -Password '<key>' -Persist LocalMachine"
}

$googleCred = Get-StoredCredential -Target "JobApp_GOOGLE_KEY"
if ($googleCred) {
    $env:GOOGLE_API_KEY = $googleCred.GetNetworkCredential().Password
    Write-Host "      GOOGLE_API_KEY loaded (LLM Council enabled via Google AI Studio)."
}

Write-Host "      Secrets loaded OK."

# ── 2. Locate / clone the repo ────────────────────────────────────────────────
$REPO = "$HOME\bitsom_ba_25071285-sales-analytics-system"
Write-Host "[2/7] Repo path: $REPO"
if (-not (Test-Path $REPO)) {
    Write-Host "      Cloning..."
    git clone https://github.com/iabhisheksaha/bitsom_ba_25071285-sales-analytics-system.git $REPO
}
Set-Location $REPO

# ── 3. Checkout branch and pull latest fixes ──────────────────────────────────
Write-Host "[3/7] Syncing branch claude/automated-job-application-j5V3U..."
git fetch origin claude/automated-job-application-j5V3U
git checkout claude/automated-job-application-j5V3U
git reset --hard origin/claude/automated-job-application-j5V3U
Write-Host "      $(git log -1 --oneline)"

# ── 4. Install Python dependencies ───────────────────────────────────────────
Write-Host "[4/7] Installing Python dependencies..."
pip install -r requirements.txt --quiet
python -m playwright install chromium --quiet
Write-Host "      Dependencies OK."

# ── 5. Bootstrap credentials.enc if missing ───────────────────────────────────
Write-Host "[5/7] Checking credentials store..."
if (-not (Test-Path "config\credentials.enc")) {
    Write-Host "      credentials.enc not found — bootstrapping from env vars..."
    $env:NAUKRI_USER   = "abhisheksaha@live.com"
    $env:NAUKRI_PASS   = "@v33rs@r@"
    $env:LINKEDIN_USER = "abhisheksaha@live.com"
    $env:LINKEDIN_PASS = "@v33rs@r@"
    python orchestrator.py --setup-creds-from-env
    # Wipe plaintext immediately after writing encrypted store
    Remove-Item Env:\NAUKRI_USER, Env:\NAUKRI_PASS, Env:\LINKEDIN_USER, Env:\LINKEDIN_PASS -ErrorAction SilentlyContinue
    Write-Host "      credentials.enc created."
} else {
    Write-Host "      credentials.enc found OK."
}

# ── 6. Browser profile dir ────────────────────────────────────────────────────
Write-Host "[6/7] Setting browser profile..."
$env:BROWSER_PROFILE_DIR = "$HOME\JobApp\browser_profile"
New-Item -ItemType Directory -Force -Path $env:BROWSER_PROFILE_DIR | Out-Null
$env:HEADLESS = "true"

# ── 7. Run ────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=================================================="
Write-Host "   JOB APPLICATION PIPELINE  $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
Write-Host "=================================================="

if ($DryRun) {
    Write-Host "[7/7] DRY-RUN mode — no applications will be submitted."
    python orchestrator.py --dry-run
} else {
    Write-Host "[7/7] LIVE mode — applications will be submitted."
    python orchestrator.py
}
