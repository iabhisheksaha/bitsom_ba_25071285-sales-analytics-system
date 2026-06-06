# first_time_setup.ps1
#
# Run ONCE on your Windows PC to set everything up.
# After this the system applies to jobs automatically every morning at 9 AM
# and sends a Telegram summary - no manual action needed.
#
# HOW TO RUN (copy-paste into PowerShell):
#   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned -Force
#   cd C:\Users\Ekadyu\JobApp
#   .\first_time_setup.ps1

$ErrorActionPreference = "Stop"
$JobAppDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $JobAppDir

function Step([string]$msg) {
    Write-Host ""
    Write-Host "===========================================================" -ForegroundColor Cyan
    Write-Host " $msg" -ForegroundColor Cyan
    Write-Host "===========================================================" -ForegroundColor Cyan
}

# -- Step 1: Pull latest code -------------------------------------------------
Step "1/6  Pull latest code from GitHub"
git pull origin claude/automated-job-application-j5V3U
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] git pull failed. Check your network / Git setup." -ForegroundColor Red
    exit 1
}

# -- Step 2: Install Python packages ------------------------------------------
Step "2/6  Install Python packages"
pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] pip install failed." -ForegroundColor Red
    exit 1
}
python -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] playwright install failed." -ForegroundColor Red
    exit 1
}
Write-Host "Python packages OK." -ForegroundColor Green

# -- Step 3: Store secrets in Windows Credential Manager ----------------------
Step "3/6  Store secrets in Windows Credential Manager"
Write-Host "You will be prompted for 4 secret values."
Write-Host "These are stored in Windows Credential Manager - never in any file."
Write-Host ""

function Store-Secret {
    param([string]$Target, [string]$Label)
    $existing = cmdkey /list | Select-String $Target -Quiet
    if ($existing) {
        $ans = Read-Host "  $Target is already stored. Overwrite? (y/N)"
        if ($ans -ne 'y') {
            Write-Host "  Keeping existing $Target." -ForegroundColor Yellow
            return
        }
    }
    $sec   = Read-Host "  Enter $Label" -AsSecureString
    $plain = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto(
                 [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    cmdkey /generic:$Target /user:KEY /pass:$plain | Out-Null
    Write-Host "  Stored $Target." -ForegroundColor Green
    $plain = ""
}

Store-Secret "JobApp_CRED_KEY"           "CRED_KEY (Fernet encryption key)"
Store-Secret "JobApp_TELEGRAM_BOT_TOKEN" "Telegram bot token"
Store-Secret "JobApp_TELEGRAM_CHAT_ID"   "Telegram chat ID (numeric)"
Store-Secret "JobApp_ANTHROPIC_API_KEY"  "Anthropic API key (for AI form filling)"

# -- Step 4: Create credentials.enc -------------------------------------------
Step "4/6  Encrypt and store job-site passwords"
Write-Host "Loading your CRED_KEY from Credential Manager..."

if (-not (Get-Module -ListAvailable -Name CredentialManager)) {
    Write-Host "  Installing CredentialManager PowerShell module..."
    Install-Module -Name CredentialManager -Force -Scope CurrentUser -AllowClobber
}
Import-Module CredentialManager -ErrorAction Stop

$cred = Get-StoredCredential -Target "JobApp_CRED_KEY" -ErrorAction SilentlyContinue
if (-not $cred) {
    Write-Host "[ERROR] JobApp_CRED_KEY not found in Credential Manager." -ForegroundColor Red
    exit 1
}
$env:CRED_KEY = $cred.GetNetworkCredential().Password

python init_credentials.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] init_credentials.py failed." -ForegroundColor Red
    exit 1
}

Remove-Item Env:\CRED_KEY -ErrorAction SilentlyContinue

# -- Step 5: Save browser login session ---------------------------------------
Step "5/6  Save LinkedIn and Naukri login sessions"
Write-Host "A Chrome browser window will open."
Write-Host "Log into BOTH LinkedIn and Naukri - solve any CAPTCHA or OTP."
Write-Host "Once both show your home feed, come back here and press Enter."
Write-Host ""
$env:BROWSER_PROFILE_DIR = "$JobAppDir\browser_profile"
python login_setup.py
Remove-Item Env:\BROWSER_PROFILE_DIR -ErrorAction SilentlyContinue

# -- Step 6: Register daily Task Scheduler job --------------------------------
Step "6/6  Register daily 9 AM Task Scheduler job"
powershell -ExecutionPolicy Bypass -File "$JobAppDir\setup_scheduler.ps1"

# -- Done ---------------------------------------------------------------------
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " ALL DONE - setup complete!" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "The system will now run itself every day at 9 AM." -ForegroundColor White
Write-Host "You will get a Telegram message with the daily summary." -ForegroundColor White
Write-Host ""
Write-Host "If a new job site needs a login, Telegram will ask:" -ForegroundColor White
Write-Host "  reply:  /creds your@email.com:password" -ForegroundColor White
Write-Host "  The password is saved automatically for future runs." -ForegroundColor White
Write-Host ""
Write-Host "Useful commands:" -ForegroundColor Cyan
$tn = "JobApplicationAgents_DailyRun"
Write-Host "  Run now   : Start-ScheduledTask -TaskName $tn"
Write-Host "  Watch log : Get-Content $JobAppDir\logs\scheduler.log -Wait"
Write-Host "  Pause     : Disable-ScheduledTask -TaskName $tn"
Write-Host "  Resume    : Enable-ScheduledTask  -TaskName $tn"
Write-Host "  Remove    : Unregister-ScheduledTask -TaskName $tn -Confirm:`$false"
