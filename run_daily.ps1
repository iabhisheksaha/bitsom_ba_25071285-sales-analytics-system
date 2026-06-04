# run_daily.ps1
# Wrapper script called by Windows Task Scheduler every day at 9AM.
# Place this file in your JobApp folder.

$JobAppDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

Set-Location $JobAppDir

# Load env vars (edit these if your key ever changes)
$env:CRED_KEY            = [System.Environment]::GetEnvironmentVariable("CRED_KEY", "User")
$env:TELEGRAM_BOT_TOKEN  = [System.Environment]::GetEnvironmentVariable("TELEGRAM_BOT_TOKEN", "User")
$env:TELEGRAM_CHAT_ID    = [System.Environment]::GetEnvironmentVariable("TELEGRAM_CHAT_ID", "User")

# Pull latest code before running
git pull origin claude/automated-job-application-j5V3U

# Run full pipeline
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"[$timestamp] Starting daily run..." | Out-File -Append -FilePath "$JobAppDir\logs\scheduler.log"

python orchestrator.py 2>&1 | Tee-Object -Append -FilePath "$JobAppDir\logs\scheduler.log"

"[$timestamp] Run complete." | Out-File -Append -FilePath "$JobAppDir\logs\scheduler.log"
