# show_schedule.ps1 — print the run schedule (which URL, which account, what time).
#
#   .\show_schedule.ps1
#
# Reflects config.ini [schedule], the active routes in config/vfs_urls.ini, and
# current account availability (benched accounts excluded). Read-only.

$Root   = $PSScriptRoot
Set-Location $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$env:PYTHONWARNINGS = "ignore"   # hide the harmless pkg_resources deprecation note

& $Python -m src.utils.show_schedule
