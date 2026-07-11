# One-time setup: register the VFS slot checker as a Windows Scheduled Task.
#
# The cadence comes from [schedule] in config.ini (runs_per_hour, start_hour,
# end_hour) — this script reads those and builds the triggers to match, so you
# change the schedule in ONE place then re-run this script. Default 3/hour,
# 09:00-19:00 -> runs at :00/:20/:40 from 09:00 to 18:40.
#
# The task runs in your INTERACTIVE session (only while you are logged on) so the
# real/headed Chrome the bot drives has a desktop to render on. No password is
# stored. A machine that is asleep/off fires nothing and does not catch up.
#
# Run once (re-run after changing [schedule]):
#   powershell -ExecutionPolicy Bypass -File .\setup_task.ps1
# Remove:    Unregister-ScheduledTask -TaskName "VFS Slot Checker" -Confirm:$false

$ErrorActionPreference = "Stop"
$Root     = $PSScriptRoot
$TaskName = "VFS Slot Checker"
$Runner   = Join-Path $Root "run_task.ps1"
$Python   = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Runner)) { throw "run_task.ps1 not found next to setup_task.ps1." }

# Read the schedule from config.ini (single source of truth), via the venv python.
$sched = & $Python -c "from src.utils.config_reader import initialize_config as i; i(); from src.utils.credentials import _sched; r,s,e=_sched(); print(r,s,e)"
$parts = $sched.Trim().Split(" ")
$runsPerHour = [int]$parts[0]
$startHour   = [int]$parts[1]
$endHour     = [int]$parts[2]
$stepMin     = [int](60 / $runsPerHour)
$windowHours = $endHour - $startHour

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Runner`"" `
    -WorkingDirectory $Root

# One daily trigger at start_hour, repeating every stepMin minutes for the length
# of the active window — so it fires runs_per_hour times an hour, only within the
# window, every day. (Build repetition on a throwaway trigger, then set duration.)
$startAt = [DateTime]::Today.AddHours($startHour)
$rep = (New-ScheduledTaskTrigger -Once -At $startAt `
    -RepetitionInterval (New-TimeSpan -Minutes $stepMin) `
    -RepetitionDuration (New-TimeSpan -Hours $windowHours)).Repetition
$trigger = New-ScheduledTaskTrigger -Daily -At $startAt
$trigger.Repetition = $rep
$triggers = @($trigger)

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
    -Description "VFS Global slot checker - $runsPerHour run(s)/hour, ${startHour}:00-${endHour}:00, run-of-day rotated accounts." | Out-Null

Write-Host ("Registered scheduled task: " + $TaskName + " ($runsPerHour/hour, every $stepMin min, ${startHour}:00-${endHour}:00).")
Get-ScheduledTask -TaskName $TaskName | Format-List TaskName, State
(Get-ScheduledTask -TaskName $TaskName).Triggers |
  ForEach-Object { [PSCustomObject]@{ StartsAt=$_.StartBoundary; Every=$_.Repetition.Interval; For=$_.Repetition.Duration } }