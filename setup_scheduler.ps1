# setup_scheduler.ps1
# Run ONCE to register a daily 9 AM Windows Task Scheduler job.
# After this the pipeline runs automatically every day — no manual action needed.
#
# The task runs as the currently logged-on user (no admin password stored anywhere).
# Requirements: PowerShell 5+, run from the JobApp folder.

$JobAppDir  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ScriptPath = "$JobAppDir\run_daily.ps1"
$TaskName   = "JobApplicationAgents_DailyRun"
$LogDir     = "$JobAppDir\logs"

# Create logs folder if missing
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# ── Action ─────────────────────────────────────────────────────────────────
$Action = New-ScheduledTaskAction `
    -Execute        "powershell.exe" `
    -Argument       "-NonInteractive -ExecutionPolicy Bypass -File `"$ScriptPath`"" `
    -WorkingDirectory $JobAppDir

# ── Trigger: every day at 9 AM ─────────────────────────────────────────────
$Trigger = New-ScheduledTaskTrigger -Daily -At "09:00AM"

# ── Settings ───────────────────────────────────────────────────────────────
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit  (New-TimeSpan -Hours 2) `
    -RestartCount        1 `
    -RestartInterval     (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable          # catches up if machine was off at 9 AM

# ── Principal: run as current user, interactive session ────────────────────
$Principal = New-ScheduledTaskPrincipal `
    -UserId    $env:USERNAME `
    -LogonType Interactive `
    -RunLevel  Limited       # no UAC elevation needed

# ── Register (overwrites if already exists) ────────────────────────────────
Register-ScheduledTask `
    -TaskName  $TaskName `
    -Action    $Action `
    -Trigger   $Trigger `
    -Settings  $Settings `
    -Principal $Principal `
    -Force

Write-Host ""
Write-Host "Task '$TaskName' registered successfully." -ForegroundColor Green
Write-Host "Folder  : $JobAppDir"
Write-Host "Schedule: daily at 9:00 AM"
Write-Host "Log file: $LogDir\scheduler.log"
Write-Host ""
Write-Host "Useful commands:" -ForegroundColor Cyan
Write-Host "  Run now    : Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Pause      : Disable-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Resume     : Enable-ScheduledTask  -TaskName '$TaskName'"
Write-Host "  Remove     : Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
Write-Host "  View logs  : Get-Content '$LogDir\scheduler.log' -Tail 50"
