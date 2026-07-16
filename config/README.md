# Config folder guide

What each file does and every supported flag. **Every `.ini` file in this folder
is loaded automatically** (base files first, then `*.local.ini` overrides last so
their values win). Never park notes here with an `.ini` extension — a non-config
`.ini` file merges junk sections into the live config. Use `.txt` for notes.

## Files

| File | Committed? | Purpose |
|---|---|---|
| `config.ini` | yes | Base settings — safe defaults, NO real secrets. |
| `config.local.ini` | no (gitignored) | Secret overrides: Telegram tokens, OpenAI key. Read last, wins over `config.ini`. |
| `credentials.local.ini` | no (gitignored) | The VFS account pool with per-route rotation. See `credentials.local.ini.example`. |
| `proxylist.txt` | no (gitignored) | The proxy pool — one `http://user:pass@host:port` per line (provider CSV also accepted). Each account is pinned to one IP. |
| `proxies.local.ini` | no (gitignored) | Optional proxy overrides only (`[proxy-pool]` / `[proxy-routes]`). Empty by default. See `proxies.local.ini.example`. |
| `vfs_urls.ini` | yes | The routes to run: `SRC-DEST = login-url`. Disable a route with a leading `;`. |
| `routes/<SRC-DEST>.json` | yes | Per-route flow schema (see below). Every enabled route in `vfs_urls.ini` needs one. |

## config.ini sections

Grouped as in the file:

**Scheduling & runtime**
- `[schedule]` — `runs_per_hour`, `start_hour`, `end_hour`. Drives both the Task
  Scheduler triggers (re-run `setup_task.ps1` after changes) and account rotation.
- `[browser]` — `type` (default `chromium`), `headless` (default `True`). Ignored
  when the supervisor attaches the bot to a Chrome it launched (`cdp_url` injected
  at runtime, not set in this file).
- `[proxy]` — `enabled` (`true`/`false`) master IP-routing switch. Override per run
  with the supervisor's `--proxy` / `--local` flag.

**Account safety**
- `[account_safety]` — circuit breaker: `hard_cooldown_hours`, `soft_cooldown_hours`,
  `fail_threshold`, `max_attempts`.
- `[turnstile]` — `manual_wait_seconds` for local headed debugging (0 in prod).

**Notifications**
- `[telegram]` — success channel (`bot_token` + `TELEGRAM_chat_id`) and
  error/summary channel (`TELEGRAM_SUMMARY_*`). Real values in `config.local.ini`.

**Integrations**
- `[otp]` — OTP-by-email settings (active only on routes flagged `"otp": true`):
  `imap_port`, `timeout_seconds`, `poll_seconds`, `otp_length` are committed
  defaults; `imap_host` + `search_text` are deployment-specific and live in
  `config.local.ini`. Mailbox login = the route's active credential.
- `[openai]` — reads the OTP from the email's image: `api_key` (set it in
  `config.local.ini`!), `model`.

**Diagnostics**
- `[logging]` — `level`, `browser_activity`.

> Optional single-account fallback: if `credentials.local.ini` is absent, the bot
> falls back to a single `[vfs-credential]` (`email` + `password`) account placed in
> `config.local.ini`. The account pool is the normal path — this is a legacy escape
> hatch and not present in `config.ini`.

## routes/*.json flags

```jsonc
{
  "description": "...",
  "mode": "slot-check",
  "otp": true,                // this portal emails an OTP after Sign In
  "slot_check": {
    "combinations": [
      { "label": "Dubai - Tourism",
        "centre": "Dubai", "category": "Tourism", "sub_category": "" },
      { "label": "Dubai - Business", "disabled": true,   // switched off, shown
        "centre": "Dubai", "category": "Business", "sub_category": "" }
    ]
  }
}
```

- `"otp": true` — after Sign In the bot fetches the emailed OTP (IMAP + OpenAI
  image read) and enters it. Routes without the flag skip the step entirely.
- `"disabled": true` on a combination — skip it without deleting it (JSON has
  no comments; a `//` comment breaks the whole file). Disabled combos are
  listed in the Telegram run summary.
- `label` format is `Centre - Category` or `Centre - Category - SubCategory`;
  the last segment is treated as the visa type in the run summary.
- `"selectors"` (optional) — override the login-form field selectors for this
  route WITHOUT a code change, for when VFS ships different markup on a portal:
  ```jsonc
  "selectors": {
    "username": "input[formcontrolname='username']",
    "password": "input[type='password']",
    "otp":      "input[autocomplete='one-time-code']"
  }
  ```
  Any key you omit falls back to the built-in default (`DEFAULT_*_SELECTOR` in
  `src/vfs_bot/vfs_bot.py`). Omit the whole object to use defaults everywhere.
