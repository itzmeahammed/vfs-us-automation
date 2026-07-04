# Windows Task Scheduler entrypoint for the VFS slot checker.
#
# Runs the self-healing supervisor (ALL routes in config/vfs_urls.ini), each in
# its own fresh Chrome that the supervisor launches and kills. Scheduled to run
# twice per hour (:29 and :59); both runs in an hour use the SAME rotated VFS
# account (rotation is by clock hour - see src/utils/credentials.py).
#
# A global mutex is the Windows equivalent of run_ec2.sh's flock: if a previous
# run is still going when the next tick fires, this one skips instead of stacking
# a second Chrome.
#
# Logs (only 3, all in the project root):
#   app.log         - Python's own structured step-by-step log. THE log to read.
#   task_runner.log - this wrapper's start/finish/skip lines: proof the task
#                     fired, its exit code, or that a tick was skipped (overlap).
#   task_stderr.log - the child's raw stderr (overwritten each run). Safety net
#                     for a crash BEFORE Python's logging starts (import/config
#                     error); normally holds only a harmless deprecation warning.
# (Child stdout is not captured - it just duplicates app.log.)
#
# NOTE: Python is launched via Start-Process, NOT `& python ... *>> file`. In
# Windows PowerShell 5.1, redirecting a native exe's stderr routes it through the
# error stream; with ErrorActionPreference=Stop, Python's harmless startup
# DeprecationWarning (on stderr) would then abort this script before Chrome ever
# launches. Start-Process with -RedirectStandardError keeps the streams separate.
#
# Registered by setup_task.ps1. To run one tick by hand:  .\run_task.ps1

$ErrorActionPreference = "Stop"
$Root      = $PSScriptRoot
Set-Location $Root
$Python    = Join-Path $Root ".venv\Scripts\python.exe"
$RunnerLog = Join-Path $Root "task_runner.log"
$ErrLog    = Join-Path $Root "task_stderr.log"

function Write-Log($msg) {
    "$([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss')) $msg" |
        Out-File -FilePath $RunnerLog -Append -Encoding utf8
}

# Overlap guard (flock equivalent): a global named mutex.
$mutex = New-Object System.Threading.Mutex($false, "Global\VfsSlotChecker")
$haveLock = $false
try {
    $haveLock = $mutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    # Previous holder died without releasing - we still own it now.
    $haveLock = $true
}

if (-not $haveLock) {
    Write-Log "Previous run still in progress - skipping this tick."
    exit 0
}

try {
    Write-Log "Starting VFS slot-check run (supervisor)... detailed log in app.log"
    $proc = Start-Process -FilePath $Python -ArgumentList @("-m", "src.supervisor") `
        -WorkingDirectory $Root -NoNewWindow -Wait -PassThru `
        -RedirectStandardError $ErrLog
    Write-Log "Run finished (exit $($proc.ExitCode))."
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
