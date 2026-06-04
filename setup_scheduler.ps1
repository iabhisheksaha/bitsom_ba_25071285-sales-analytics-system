# setup_scheduler.ps1
# Run this ONCE in PowerShell (as Administrator) to register the daily 9AM task.
# After that it runs automatically every day — no manual action needed.

$JobAppDir   = "C:\Users\Ekadyu\JobApp"
$ScriptPath  = "$JobAppDir\run_daily.ps1"
$TaskName    = "JobApplicationAgents_DailyRun"
$LogonUser   = $env:USERNAME

# --- Action: run PowerShell with our wrapper script ---
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NonInteractive -ExecutionPolicy Bypass -File `"$ScriptPath`"" `
    -WorkingDirectory $JobAppDir

# --- Trigger: every day at 9:00 AM ---
$Trigger = New-ScheduledTaskTrigger -Daily -At "09:00AM"

# --- Settings ---
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable   # runs at next opportunity if machine was off at 9AM

# --- Register ---
Register-ScheduledTask `
    -TaskName    $TaskName `
    -Action      $Action `
    -Trigger     $Trigger `
    -Settings    $Settings `
    -RunLevel    Highest `
    -Force

Write-Host ""
Write-Host "Scheduled task '$TaskName' registered successfully." -ForegroundColor Green
Write-Host "It will run every day at 9:00 AM." -ForegroundColor Green
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  View task    : Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Run now      : Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Disable      : Disable-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Remove       : Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
Write-Host "  View logs    : Get-Content '$JobAppDir\logs\scheduler.log' -Tail 50"
