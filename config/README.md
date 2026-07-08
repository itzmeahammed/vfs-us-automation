# Config folder guide

What each file does and every supported flag. **Only `.ini` files are loaded**
(all of them, automatically) — never park notes here with an `.ini` extension,
a non-config `.ini` file crashes the whole bot at startup.

| File | Committed? | Purpose |
|---|---|---|
| `config.ini` | yes | Base settings — safe defaults, NO real secrets. |
| `config.local.ini` | no (gitignored) | Secret overrides: Telegram tokens, OpenAI key, proxy. Read last, wins over `config.ini`. |
| `credentials.local.ini` | no (gitignored) | The VFS account pool with per-route rotation. See `credentials.local.ini.example`. |
| `vfs_urls.ini` | yes | The routes to run: `SRC-DEST = login-url`. Disable a route with a leading `;`. |
| `routes/<SRC-DEST>.json` | yes | Per-route flow schema (see below). Every route in `vfs_urls.ini` needs one. |
| `allroutes.txt` | — | Free-form notes (kept as `.txt` on purpose — see warning above). |

## config.ini sections

- `[browser]` — `type`, `headless`; `proxy` / `cdp_url` are injected or set locally.
- `[vfs-credential]` — single-account fallback, used only when `credentials.local.ini` is absent.
- `[telegram]` — success channel (`bot_token` + `TELEGRAM_chat_id`) and error/summary channel (`TELEGRAM_SUMMARY_*`). Real values in `config.local.ini`.
- `[otp]` — OTP-by-email settings (active only on routes flagged `"otp": true`): `imap_host`, `imap_port`, `search_text`, `timeout_seconds`, `poll_seconds`, `otp_length`. Mailbox login = the route's active credential.
- `[openai]` — reads the OTP from the email's image: `api_key` (set it in `config.local.ini`!), `model`.
- `[logging]` — `level`, `browser_activity`.
- `[turnstile]` — `manual_wait_seconds` for local headed debugging (0 in prod).

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
