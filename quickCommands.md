# Quick Commands

Handy one-off commands for the VFS Slot Checker. Run from the project root in
**PowerShell**. Swap the route code (`FRA`, `ITA`, `GRC`, `NOR`, `CHE`, `CZE`,
`DEU`, `HUN`, …) and account email as needed.

---

## Run one route

```powershell
& .venv\Scripts\python.exe -m src.supervisor -sc AE -dc FRA
```

Runs the full flow (login → Turnstile → OTP → dashboard → slot check → Telegram)
for a single route. Account is auto-selected by rotation (skips benched/disabled).

---

## Run one route (verbose logs)

```powershell
& .venv\Scripts\python.exe -m src.supervisor -sc AE -dc FRA -v
```

Same as above with detailed step-by-step DEBUG logging.

---

## Run ALL routes

```powershell
& .venv\Scripts\python.exe -m src.supervisor
```

Runs every route in `config/vfs_urls.ini`, one after another — the same thing the
scheduled task does.

---

## Run a route with a SPECIFIC account

```powershell
& .venv\Scripts\python.exe -m src.supervisor -sc AE -dc FRA --email osama@travnook.com
```

Forces that account and **bypasses its cooldown/bench**. Password is looked up
from `credentials.local.ini` (add `--password <pw>` only if it isn't there).

---

## Force PROXY on for a route

```powershell
& .venv\Scripts\python.exe -m src.supervisor -sc AE -dc FRA --proxy
```

Forces proxy-seller residential IPs for this run (overrides `[proxy] enabled`),
using the selected account's pinned IP.

---

## Force LOCAL IP (no proxy) for a route

```powershell
& .venv\Scripts\python.exe -m src.supervisor -sc AE -dc FRA --local
```

Runs through this PC's own IP — no proxy — for this run only.

---

## Run a route through ONE specific proxy IP

```powershell
& .venv\Scripts\python.exe -m src.supervisor -sc AE -dc FRA --proxy-url "http://e65bbcf31555e1cf:V9bP2K5uhIXHSfkc@res.proxy-seller.com:10002"
```

Pins the run to exactly this proxy IP. Note the URL form is
`user:pass@host:port` (NOT the proxylist `user:pass:host:port`).

---

## Single route + specific account + specific IP (full control)

```powershell
& .venv\Scripts\python.exe -m src.supervisor -sc AE -dc FRA --email osama@travnook.com --proxy-url "http://e65bbcf31555e1cf:V9bP2K5uhIXHSfkc@res.proxy-seller.com:10002" -v
```

Everything pinned: France route, osama's account, IP `10002`, verbose.

---

## Open a URL manually in Chrome via a proxy (and count MB)

```powershell
& .venv\Scripts\python.exe open_with_proxy.py "https://visa.vfsglobal.com/are/en/fra/login"
```

Opens the URL in a **visible** Chrome through a proxy IP (auto-rotates each run),
prints the egress IP, and leaves the browser open so **you** drive it. Press
**Enter** in the terminal to close it — then it prints the billed proxy MB
(total + per-host). Pin a specific IP with `--index N` (0-based into
`proxylist.txt`), e.g. `--index 2`.

---

## Account health — list all records

```powershell
& .venv\Scripts\python.exe -m src.utils.account_health
```

Shows every account's state: ok / cooldown until <time> / DISABLED.

---

## Account health — clear one account (flag healthy)

```powershell
& .venv\Scripts\python.exe -m src.utils.account_health clear osama@travnook.com
```

Removes an account's cooldown/disable so it's eligible again. Use this after
fixing a 429002-DISABLED account.

---

## Account health — clear ALL

```powershell
& .venv\Scripts\python.exe -m src.utils.account_health clear-all
```

Wipes every health record (all accounts back to healthy). Use with care.

---

## Account health — manually bench an account

```powershell
& .venv\Scripts\python.exe -m src.utils.account_health bench osama@travnook.com 12
```

Benches the account for N hours (omit the number to use `hard_cooldown_hours`).

---

## Edit config (web UI)

```powershell
& .venv\Scripts\python.exe config_editor.py
```

Opens a local page (127.0.0.1) to edit `config/config.local.ini` — timeouts,
proxy, bandwidth, telegram, etc.

---

## Edit accounts / routes / scheduler (web UI)

```powershell
& .venv\Scripts\python.exe credentials_editor.py
```

Opens a local page to manage accounts (`credentials.local.ini`), toggle
countries/routes (`vfs_urls.ini`), see the per-country account counts, and
control the Windows scheduled task (start/stop/enable/disable/run-now + sleep
diagnostics).
