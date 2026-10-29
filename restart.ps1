<#
.SYNOPSIS
    Restarts the bot API so it picks up code changes. One command.

.DESCRIPTION
    The API loads everything at import and serves jobs from MEMORY, so a code
    change is invisible until the process is replaced. Worse, the tunnel keeps
    answering and '[ok] API healthy' keeps printing, so nothing looks wrong  - 
    on 2026-10-28 four fixes sat unserved for a day for exactly that reason.
    `start_tunnel.ps1 -StartApi` does NOT help: it only starts the API when one
    is not already answering.

    *** THE TUNNEL IS LEFT ALONE BY DEFAULT, ON PURPOSE. ***

    ngrok/ngrok.yml reserves no domain, so the free tier hands out a new
    *.ngrok-free.dev name on every ngrok restart  -  and that URL is the webhook
    Travnooker calls. Restarting the tunnel with the API would mean re-pasting
    a new URL into the web app every single time. So this replaces the API
    under the existing tunnel, which keeps serving on the same public URL.

    Pass -Tunnel only when you actually want a new public URL (or have since
    reserved a domain, in which case the URL is stable and this is free).

.PARAMETER Tunnel
    Also restart ngrok. CHANGES THE PUBLIC URL unless a reserved domain is set
    in ngrok/ngrok.yml  -  the new one is printed at the end; paste it into the
    web app.

.PARAMETER SkipChecks
    Skip the post-restart readiness report.

.EXAMPLE
    .\restart.ps1
    Replace the API, keep the tunnel and its URL.

.EXAMPLE
    .\restart.ps1 -Tunnel
    Replace both. Prints the new public URL.
#>
[CmdletBinding()]
param(
    [switch] $Tunnel,
    [switch] $SkipChecks,
    [int]    $Port = 8000,
    [int]    $TimeoutSeconds = 45
)

$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$python = if (Test-Path "$repo\.venv\Scripts\python.exe") {
    "$repo\.venv\Scripts\python.exe"
} else {
    (Get-Command python).Source
}

function Test-Api {
    try {
        $r = Invoke-RestMethod "http://127.0.0.1:$Port/v1/health" -TimeoutSec 3
        return $r.status -eq 'ok'
    } catch { return $false }
}

function Get-ApiProcesses {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match 'src\.api' }
}

Write-Host ""
Write-Host "Restarting the bot API" -ForegroundColor Cyan
Write-Host "----------------------" -ForegroundColor Cyan

# -- 1. Refuse to kill a run that is mid-booking -------------------------- #
#
# A booking job is a CHILD of the API. Killing the parent kills the child, and
# a child killed between the Pay click and the journal write leaves a payment
# whose outcome nobody can reconstruct. Never automatic: a human decides.
# One id per line, nothing at all when none. Deliberately NOT JSON: in
# Windows PowerShell 5.1, ConvertFrom-Json turns "[]" into a single non-null
# element, so @(...) reported Count 1 and this gate fired on a blank id with
# no booking running at all (seen 2026-10-29, the first real run of this
# script). Line-splitting has no such trap.
$busy = & $python -c @"
import os, sys
sys.path.insert(0, r'$repo')
os.environ.setdefault('VFSAPI_SECRET_TOKEN', 'x' * 64)
try:
    from src.booking import requests as store
    for r in store.list_all(status=store.BOOKING):
        print(r.request_id)
except Exception:
    pass
"@ 2>$null

$booking = @(($busy -split "`r?`n") | Where-Object { $_.Trim() -ne '' })
if ($booking.Count -gt 0) {
    Write-Host ""
    Write-Host "[!] A booking is in flight:" -ForegroundColor Red
    $booking | ForEach-Object { Write-Host "      $_" -ForegroundColor Red }
    Write-Host ""
    Write-Host "    Booking jobs are children of the API  -  restarting kills them." -ForegroundColor Yellow
    Write-Host "    A run killed after the Pay click leaves a payment nobody can" -ForegroundColor Yellow
    Write-Host "    account for. Wait for it, or cancel it in the web app first." -ForegroundColor Yellow
    $answer = Read-Host "    Restart anyway? (type 'yes')"
    if ($answer -ne 'yes') {
        Write-Host "Left running." -ForegroundColor Green
        return
    }
}

# -- 2. Replace the API ---------------------------------------------------- #
$existing = @(Get-ApiProcesses)
if ($existing.Count -eq 0) {
    Write-Host "  no API running  -  starting one."
} else {
    foreach ($p in $existing) {
        Write-Host "  stopping PID $($p.ProcessId) (up since $($p.CreationDate))"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
}

Write-Host "  starting python -m src.api ..."
Start-Process -FilePath $python -ArgumentList '-m', 'src.api' `
              -WorkingDirectory $repo -WindowStyle Hidden

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $deadline -and -not (Test-Api)) {
    Start-Sleep -Milliseconds 700
}
if (-not (Test-Api)) {
    Write-Host ""
    Write-Host "[X] The API did not answer /v1/health within ${TimeoutSeconds}s." -ForegroundColor Red
    Write-Host "    Run it in the foreground to see why:" -ForegroundColor Yellow
    Write-Host "      $python -m src.api" -ForegroundColor Yellow
    exit 1
}
$now = @(Get-ApiProcesses)
Write-Host "  [ok] API healthy on 127.0.0.1:$Port (PID $($now[0].ProcessId))" -ForegroundColor Green

# -- 3. The tunnel, only when asked ---------------------------------------- #
if ($Tunnel) {
    Write-Host ""
    Write-Host "  restarting the tunnel  -  THE PUBLIC URL WILL CHANGE unless" -ForegroundColor Yellow
    Write-Host "  ngrok/ngrok.yml reserves a domain. Paste the new one below" -ForegroundColor Yellow
    Write-Host "  into the web app." -ForegroundColor Yellow
    & "$repo\ngrok\start_tunnel.ps1" -Port $Port
} else {
    $ngrok = Get-Process ngrok -ErrorAction SilentlyContinue
    if ($ngrok) {
        Write-Host "  tunnel left running (same public URL)  -  PID $($ngrok[0].Id)"
    } else {
        Write-Host "  [!] No ngrok tunnel is running: the API is reachable only" -ForegroundColor Yellow
        Write-Host "      on localhost. Start one with .\ngrok\start_tunnel.ps1" -ForegroundColor Yellow
    }
}

# -- 4. Is the rest of the bot actually on? -------------------------------- #
if (-not $SkipChecks) {
    Write-Host ""
    Write-Host "Slot checker" -ForegroundColor Cyan
    Write-Host "------------" -ForegroundColor Cyan
    try {
        $task = Get-ScheduledTask -TaskName "VFS Slot Checker" -ErrorAction Stop
        $info = Get-ScheduledTaskInfo -TaskName "VFS Slot Checker"
        if ($task.State -eq 'Disabled') {
            Write-Host "  [!] DISABLED  -  no slot checks, and no auto-booking." -ForegroundColor Yellow
            Write-Host "      Enable-ScheduledTask -TaskName 'VFS Slot Checker'" -ForegroundColor Yellow
        } else {
            $line = "  [ok] $($task.State). Last run $($info.LastRunTime) " +
                    "(result $($info.LastTaskResult)); next $($info.NextRunTime)"
            Write-Host $line -ForegroundColor Green
        }
    } catch {
        Write-Host "  [!] Task 'VFS Slot Checker' not found  -  run .\setup_task.ps1" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Done. The API is now serving the code on disk." -ForegroundColor Green
Write-Host ""
