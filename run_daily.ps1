# run_daily.ps1
# Wrapper script called by Windows Task Scheduler every day at 9AM.
# Secrets are read from Windows Credential Manager - never stored in plain text.

$JobAppDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $JobAppDir

# -- Load CredentialManager module (install once if missing) ----------------
if (-not (Get-Module -ListAvailable -Name CredentialManager)) {
    Write-Host "[setup] Installing CredentialManager module..." -ForegroundColor Yellow
    Install-Module -Name CredentialManager -Force -Scope CurrentUser -AllowClobber
}
Import-Module CredentialManager -ErrorAction Stop

# -- Read secrets from Windows Credential Manager ---------------------------
function Get-VaultSecret([string]$Target) {
    $c = Get-StoredCredential -Target $Target -ErrorAction SilentlyContinue
    if (-not $c) {
        Write-Host "[ERROR] Credential '$Target' not found in Credential Manager." -ForegroundColor Red
        Write-Host "        Run: cmdkey /generic:$Target /user:KEY_NAME /pass:<value>" -ForegroundColor Yellow
        exit 1
    }
    return $c.GetNetworkCredential().Password
}

$env:CRED_KEY            = Get-VaultSecret "JobApp_CRED_KEY"
$env:TELEGRAM_BOT_TOKEN  = Get-VaultSecret "JobApp_TELEGRAM_BOT_TOKEN"
$env:TELEGRAM_CHAT_ID    = Get-VaultSecret "JobApp_TELEGRAM_CHAT_ID"
$env:ANTHROPIC_API_KEY   = Get-VaultSecret "JobApp_ANTHROPIC_API_KEY"

# -- Local-run browser settings ----------------------------------------------
# Running on your own machine (residential IP) is what makes LinkedIn/Naukri
# actually work. Use a VISIBLE browser with a persistent profile so the
# logged-in session (set up once via login_setup.py) is reused every run.
$env:HEADLESS            = "false"
$env:BROWSER_PROFILE_DIR = "$JobAppDir\browser_profile"
# Not using ScraperAPI for local runs - clear it so the browser isn't proxied.
Remove-Item Env:\SCRAPER_API_KEY -ErrorAction SilentlyContinue

# -- Pull latest code --------------------------------------------------------
git pull origin claude/automated-job-application-j5V3U

# -- Python encoding: force UTF-8 stdout/stderr so Unicode log lines don't crash
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8       = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# -- Run pipeline ------------------------------------------------------------
New-Item -ItemType Directory -Force -Path "$JobAppDir\logs" | Out-Null
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"[$timestamp] Starting daily run..." | Out-File -Append -Encoding UTF8 -FilePath "$JobAppDir\logs\scheduler.log"

python orchestrator.py 2>&1 | Tee-Object -Append -FilePath "$JobAppDir\logs\scheduler.log"

"[$timestamp] Run complete." | Out-File -Append -Encoding UTF8 -FilePath "$JobAppDir\logs\scheduler.log"

# -- Clear secrets from process memory when done -----------------------------
Remove-Item Env:\CRED_KEY            -ErrorAction SilentlyContinue
Remove-Item Env:\TELEGRAM_BOT_TOKEN  -ErrorAction SilentlyContinue
Remove-Item Env:\TELEGRAM_CHAT_ID    -ErrorAction SilentlyContinue
Remove-Item Env:\ANTHROPIC_API_KEY   -ErrorAction SilentlyContinue
Remove-Item Env:\BROWSER_PROFILE_DIR -ErrorAction SilentlyContinue
Remove-Item Env:\HEADLESS            -ErrorAction SilentlyContinue
