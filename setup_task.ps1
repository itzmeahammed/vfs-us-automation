# One-time setup: register the VFS slot checker as a Windows Scheduled Task.
#
# Creates a task that fires TWICE PER HOUR - at :29 and :59 of every hour -
# running run_task.ps1 (which runs the supervisor for all routes). Both runs in
# an hour use the same rotated VFS account (rotation is by clock hour).
#
# The task runs in your INTERACTIVE session (only while you are logged on) so the
# real/headed Chrome the bot drives has a desktop to render on. No password is
# stored. A machine that is asleep/off fires nothing and does not catch up.
#
# Run once:  powershell -ExecutionPolicy Bypass -File .\setup_task.ps1
# Remove:    Unregister-ScheduledTask -TaskName "VFS Slot Checker" -Confirm:$false

$ErrorActionPreference = "Stop"
$Root     = $PSScriptRoot
$TaskName = "VFS Slot Checker"
$Runner   = Join-Path $Root "run_task.ps1"

if (-not (Test-Path $Runner)) { throw "run_task.ps1 not found next to setup_task.ps1." }

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Runner`"" `
    -WorkingDirectory $Root

# Two hourly triggers, offset to :29 and :59. Each repeats every 1 hour,
# indefinitely. Build the repetition on a throwaway trigger (a plain -Once
# trigger has no Repetition object to assign into), then blank the Duration so
# it never stops.
function New-HourlyTrigger($at) {
    $rep = (New-ScheduledTaskTrigger -Once -At $at `
        -RepetitionInterval (New-TimeSpan -Hours 1)).Repetition
    $rep.Duration = ""
    $t = New-ScheduledTaskTrigger -Once -At $at
    $t.Repetition = $rep
    return $t
}
$triggers = @(
    (New-HourlyTrigger ([DateTime]::Today.AddMinutes(29))),
    (New-HourlyTrigger ([DateTime]::Today.AddMinutes(59)))
)

# Run as the current user, only when logged on, in the interactive desktop.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 25)
# -WakeToRun: wake the PC from Sleep (S3) to fire the task. Requires wake timers
# enabled in the power plan (setup_task does that via powercfg below is separate).
# NOTE: this wakes from SLEEP only — NOT from Hibernate or a full Shutdown.

# Replace any existing task of the same name.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Principal $principal -Settings $settings `
    -Description "VFS Global slot checker - runs at :29 and :59 each hour with hour-rotated VFS accounts." | Out-Null

Write-Host ("Registered scheduled task: " + $TaskName + " (fires at minute 29 and 59 every hour).")
Get-ScheduledTask -TaskName $TaskName | Format-List TaskName, State