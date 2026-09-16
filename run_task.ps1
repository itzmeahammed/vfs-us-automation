# Windows Task Scheduler entrypoint for the VFS slot checker.
#
# Runs the self-healing supervisor (ALL routes in config/vfs_urls.ini), each in
# its own fresh Chrome that the supervisor launches and kills. Scheduled to run
# twice per hour (:29 and :59); both runs in an hour use the SAME rotated VFS
# account (rotation is by clock hour - see src/utils/credentials.py).
#
# OVERLAP GUARD: owned by PYTHON, not by this script. src/supervisor.py takes the
# Global\VfsSlotChecker mutex itself (src/utils/runlock.py) and skips the tick if
# another browser-driving run holds it.
#
# This wrapper used to take that same mutex before launching the child - which
# deadlocked the moment the supervisor started taking it too: the parent held it,
# so the child could never get it and EVERY tick skipped while this script still
# logged "Run finished (exit 0)". That silently killed the slot bot for 20 hours
# on 2026-08-28. Do not reintroduce a lock here.
#
# Python has to be the owner rather than this script, because this script cannot
# see a waitlist run started by the API or by hand - and those drive a browser too.
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

# No lock here - the supervisor takes it (see the note at the top of this file).
Write-Log "Starting VFS slot-check run (supervisor)... detailed log in app.log"
$proc = Start-Process -FilePath $Python -ArgumentList @("-m", "src.supervisor") `
    -WorkingDirectory $Root -NoNewWindow -Wait -PassThru `
    -RedirectStandardError $ErrLog
Write-Log "Run finished (exit $($proc.ExitCode))."
