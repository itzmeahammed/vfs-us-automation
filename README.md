# VFS Slot Checker

A self-healing bot that logs in to **VFS Global** visa portals, reaches the
*Appointment Details* step, reads the **"Earliest available slot"** for each
configured centre / category / sub-category, and reports the results to
**Telegram**. It never books anything — it only checks and reports.

One run checks **every URL** you configure (e.g. UAE → Malta, UAE → Luxembourg,
UAE → Switzerland) and sends a separate Telegram message per portal. It is built
to run **unattended every hour on an EC2 server**, recovering from failures on
its own.

> 📘 **[ARCHITECTURE.md](ARCHITECTURE.md) — the master reference.** Full end-to-end
> behaviour: scheduling, account/IP selection, the three Cloudflare gates, every
> outcome and how it's classified, the circuit breaker, and the full config reference.

---

## What it does

For each configured route, in one run:

```
launch a fresh Chrome
  → open the VFS login page
  → accept cookies, pass the Cloudflare Turnstile challenge
  → log in
  → open "Start New Booking" → Appointment Details
  → for each centre/category/sub-category combination:
        select the dropdowns → read the "Earliest available slot" banner
  → send the collected slots to Telegram
  → kill Chrome
```

Multiple routes run back-to-back:

```
cron tick
  AE-MT   → fresh Chrome → slots → Telegram message
  AE-LUX  → fresh Chrome → slots → Telegram message
  AE-CHE  → fresh Chrome → slots → Telegram message
```

Each route is independent: it gets its **own fresh Chrome**, its **own retries**,
and its **own Telegram report**. One route failing does not stop the others.

### Why it's built this way

VFS sits behind **Cloudflare**, which blocks plain headless/automation browsers
(you get an HTTP 403 or an endless loading spinner). So the bot drives a **real,
headed Google Chrome** over the DevTools Protocol (CDP). On a server with no
monitor, that headed Chrome runs under a **virtual display (Xvfb)**.

---

## Tech stack

| Piece | What / why |
|---|---|
| **Python 3.9+** | The bot and supervisor. |
| **Playwright (Python)** | Drives Chrome over CDP (attaches to a real Chrome; does not launch its own browser in production). |
| **Google Chrome (system)** | The real browser that passes Cloudflare. Launched & killed by the bot. |
| **Xvfb** | Virtual X display so headed Chrome runs on a headless server. |
| **cron + flock** | Hourly scheduling; lockfile prevents overlapping runs. |
| **Telegram Bot API** | Delivers slot reports and failure alerts. |
| **INI + JSON config** | `config/*.ini` for settings/credentials/URLs; `config/routes/*.json` for what to check per portal. |

---

## Project layout

```
src/
  main.py                  # logger setup + single-route CLI entry
  supervisor.py            # self-healing runner: all routes, retries, Telegram alerts
  utils/
    config_reader.py       # reads config/*.ini (+ VFS_BOT_CONFIG_PATH override)
    route_schema.py        # loads config/routes/<SRC>-<DST>.json
    chrome_launcher.py     # launches/owns/kills a real Chrome (CDP), cross-platform
    telegram.py            # sends reports via the Telegram Bot API
    telegram_message.py    # EDITABLE message templates (route-aware: flag, link)
    credentials.py         # multiple-account hour-based rotation
  vfs_bot/
    vfs_bot.py             # the flow: login → Turnstile → Step 1 → read slots
    vfs_bot_factory.py     # builds the schema-driven bot for a route
config/
  config.ini               # [browser] [logging] [vfs-credential] [telegram]
  config.local.ini         # gitignored secret overrides (real creds, Telegram)
  vfs_urls.ini             # [vfs-url] one login URL per route (SRC-DST = url)
  routes/<SRC>-<DST>.json  # the combinations to check for that portal
run_ec2.sh                 # EC2 entry: flock + xvfb-run + supervisor
run_loop.py                # optional local interval scheduler (no cron)
```

---

## Setup

Requires **Python 3.9+** and **Google Chrome**.

```bash
python -m venv .venv
# Windows: .\.venv\Scripts\Activate.ps1     Linux/mac: . .venv/bin/activate
pip install -r requirements.txt
```

> Production uses the **system Google Chrome** over CDP, so Playwright's bundled
> browser is *not* required. (You only need `python -m playwright install chromium`
> if you want to run the legacy own-browser path.)

### Configure credentials & Telegram (never commit these)

Put real secrets in **`config/config.local.ini`** (gitignored). It is read last,
so its values win over the committed `config.ini`:

```ini
[vfs-credential]
email = your.registered.email@example.com
password = your-password

[telegram]
bot_token = <your-bot-token>       ; from @BotFather
chat_id   = <your-chat-id>         ; from @userinfobot
```

Leave `[telegram]` blank to only log the report (no message sent).

---

## How to run (locally)

The browser is **headed** (a real Chrome window opens). On a residential IP the
Cloudflare Turnstile usually auto-passes; if it shows a checkbox, you can solve it
by hand (see `manual_wait_seconds` below).

```bash
# Run every route in config/vfs_urls.ini:
python -m src.supervisor

# Run a single route only:
python -m src.supervisor -sc AE -dc MT
```

**Local manual Turnstile solve.** If a "Verify you are human" checkbox appears,
set this in `config/config.local.ini` to get time to click it yourself:

```ini
[turnstile]
manual_wait_seconds = 120     ; 0 = off (unattended/EC2 default)
```

Output goes to the console and `app.log`. Each route's report is sent to Telegram
at the end of its run.

---

## How to add a new URL / portal

Each portal has different dropdown names, so adding one is **two small steps —
no code changes**:

**1. Add the login URL** to `config/vfs_urls.ini` under `[vfs-url]`, keyed by
`SOURCE-DEST` (ISO-style codes you choose):

```ini
[vfs-url]
AE-MT  = https://visa.vfsglobal.com/are/en/mlt/login
AE-LUX = https://visa.vfsglobal.com/are/en/lux/login
AE-CHE = https://visa.vfsglobal.com/are/en/che/login
```

**2. Add a route file** `config/routes/SOURCE-DEST.json` listing the combinations
to check. Open the portal's *Appointment Details* page to read the exact dropdown
option text, then list each combination. Matching is **case-insensitive
substring**, so a partial centre name (e.g. `"Dubai"`) matches the full option
(`"Switzerland Visa Application Center-Dubai"`). Leave a level `""` if the portal
has only one option there (it auto-selects).

```json
{
  "description": "UAE -> Switzerland. Slot-check only.",
  "mode": "slot-check",
  "slot_check": {
    "combinations": [
      { "label": "Abu Dhabi - Business Visa",
        "centre": "Abu Dhabi", "category": "Business Visa", "sub_category": "" },
      { "label": "Dubai - SCHENGEN",
        "centre": "Dubai", "category": "SCHENGEN", "sub_category": "" }
    ]
  }
}
```

That's it — the next run picks the route up automatically. If a URL has **no**
route file, that route runs but reports nothing (logged, not a crash).

> The three dropdowns map to VFS's stable Angular form controls (`centerCode`,
> `selectedSubvisaCategory`, `visaCategoryCode`), which are the **same on every
> VFS portal** — only the option *text* differs, which is why no code changes are
> needed per portal.

---

## Customizing the Telegram message

All message wording lives in **one editable file**:
[`src/utils/telegram_message.py`](src/utils/telegram_message.py). Edit it to change
the layout — the bot logic never touches the formatting.

Each route sends its own **route-aware** message: the destination's flag, each
combination's label + the raw "Earliest available slot" text, and a clickable link
to that portal's login page (pulled from `config/vfs_urls.ini`). Example:

```
🇩🇰 Dubai - Denmark - Tourism:
  Earliest available slot for 1 Applicants is : 24-06-2026

Link to visa center site (https://visa.vfsglobal.com/are/en/dnk/login)
```

When you add a new route, add its flag to `DESTINATION_FLAGS` in that file:

```python
DESTINATION_FLAGS = {
    "MT": "🇲🇹", "LUX": "🇱🇺", "CHE": "🇨🇭", "DNK": "🇩🇰",
    "FRA": "🇫🇷",   # <- new route
}
```

If you forget, the message simply omits the flag (still correct — it never
mislabels). Functions in the file: `slot_report(...)` (the slot message) and
`failure_alert(...)` (the failure message).

---

## Multiple accounts (hour-based rotation)

You can run under several VFS accounts and rotate them by the clock hour — useful
to spread load / avoid per-account rate limits.

Create a gitignored `config/credentials.local.ini` (copy
[`config/credentials.local.ini.example`](config/credentials.local.ini.example)):

```ini
[cred1]
email = account1@example.com
password = your-password-1

[cred2]
email = account2@example.com
password = your-password-2
; ...as many [credN] as you like, in order
```

**Rotation:** `cred1` is used at the first run hour of the day (**06:00**), `cred2`
at 07:00, and so on, wrapping after the last account
(`index = (hour - 6) % N`). Both runs within an hour (`:29` and `:59`) use the
**same** account, and **all URLs in a run** use that account.

If the file is absent, the bot falls back to the single `[vfs-credential]`
account — existing setups keep working unchanged. The active account is logged
each run (email masked). Preview the schedule:

```bash
python -c "from src.utils.config_reader import initialize_config as i; i(); \
from src.utils import credentials as c; \
[print(h, 'cred'+str(idx), em) for h,idx,em in c.rotation_schedule()]"
```

---

## How to run on EC2

VFS's Cloudflare blocks datacenter IPs harder, so the bot uses the coordinate-click
+ token approach under a real headed Chrome. A **t3.small (2 GB)** or larger is
recommended (Chrome on the heavy Angular site is sluggish on a t2/t3.micro).

### 1. Install dependencies (Ubuntu)

```bash
sudo apt update
sudo apt install -y xvfb git python3-venv python3-pip wget

# Google Chrome (stable)
wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y ./google-chrome-stable_current_amd64.deb
google-chrome --version
```

### 2. Clone & set up

```bash
sudo mkdir -p /opt/vfs-malta-slot-checker
sudo chown $USER:$USER /opt/vfs-malta-slot-checker
git clone <your-repo> /opt/vfs-malta-slot-checker
cd /opt/vfs-malta-slot-checker

python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure & make the entry script executable

```bash
# Create config/config.local.ini with [vfs-credential] + [telegram] (see Setup).
chmod +x run_ec2.sh
grep headless config/config.ini      # should be: headless = false (headed under Xvfb)
```

### 4. Test one run manually

```bash
./run_ec2.sh
tail -n 60 app.log                   # watch the result
```

`run_ec2.sh` wraps the supervisor with **`xvfb-run -a`** (a fresh virtual display
per run) and **`flock`** (a lockfile so an overrun can't stack a second browser).

---

## How to set the cron (hourly)

Once a manual `./run_ec2.sh` succeeds, schedule it:

```bash
crontab -e        # choose nano if prompted
```

Add this line (runs at minute 0 of every hour):

```cron
0 * * * * /opt/vfs-malta-slot-checker/run_ec2.sh >> /opt/vfs-malta-slot-checker/app.log 2>&1
```

Verify and watch:

```bash
crontab -l                                         # confirm it's scheduled
tail -f /opt/vfs-malta-slot-checker/app.log        # live log (Ctrl+C stops watching, not the bot)
```

> The cron only runs while the **instance is running** — a stopped instance fires
> nothing and does not "catch up" missed hours. Keep the instance on for 24/7
> hourly checks.

**Tip — confirm cron-safety first.** cron runs with a minimal environment; test
the bare-environment invocation before relying on the schedule:

```bash
cd /opt/vfs-malta-slot-checker
env -i HOME=$HOME /opt/vfs-malta-slot-checker/run_ec2.sh
```

If that succeeds, cron will too.

---

## How it self-heals

The **supervisor** ([`src/supervisor.py`](src/supervisor.py)) makes each run robust:

- **Owns the browser** — launches a fresh Chrome it controls and **kills it on the
  way out, success or failure**, so no zombie Chrome processes pile up across runs.
- **Retries** the whole flow with a brand-new browser on any failure (Cloudflare
  not passed, Sign In disabled, page closed, dashboard not reached, CDP connect
  failure) — up to **`MAX_ATTEMPTS`** (default 3) with a backoff.
- **Inner Turnstile retries** — refreshes the page a couple of times to unstick a
  stuck challenge before relaunching the browser.
- **Telegram alert** if all attempts for a route fail, so you know it needs
  attention.

---

## Configuration reference

| File | Section | Keys |
|---|---|---|
| `config/config.ini` | `[browser]` | `headless` (false under Xvfb), `cdp_url`, `engine` |
| | `[logging]` | `level` (INFO/DEBUG), `browser_activity` (verbose network logs) |
| `config/config.local.ini` | `[vfs-credential]` | `email`, `password` |
| | `[telegram]` | `bot_token`, `chat_id` |
| | `[turnstile]` | `manual_wait_seconds` (local hand-solve; 0 = unattended) |
| `config/vfs_urls.ini` | `[vfs-url]` | `SRC-DST = <login url>` per route |
| `config/routes/*.json` | — | `slot_check.combinations` per portal |

Environment overrides: `VFS_BOT_CONFIG_PATH` (extra config read last),
`LOG_LEVEL`, `BROWSER_ACTIVITY_LOG`, `BROWSER_ENGINE`, `CHROME_PATH`.

---

## Notes / gotchas

- **Cloudflare Turnstile** is the crux. On residential IPs it usually auto-passes;
  on datacenter IPs (EC2) the bot uses a coordinate-click + token wait. A rare
  *interactive* challenge can't be automated and would fail that attempt (then
  retry / alert).
- **Slot dates shift in real time** — the bot reports whatever is live at run time.
- **Never commit real credentials.** They belong only in the gitignored
  `config/config.local.ini`.
- **`app.log` grows over time** — add log rotation for long-running deployments.
```
