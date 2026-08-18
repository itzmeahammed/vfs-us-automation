# Adding a Client — Step by Step

How to register a new client for a waitlist. One file per client; nothing else
to touch.

**Worked example:** Fatima, waiting on **Italy** (`AE-ITA`).

---

## Before you start

| | |
|---|---|
| **1 client file = 1 route** | Two countries → two files (`fatima-ita.json`, `fatima-che.json`). Their passport number then lives in two places — keep both in step if you edit one. |
| **The route must be configured** | `config/waitlist/<ROUTE>.json` must exist. `AE-CHE` and `AE-ITA` do. For a new country see *Adding a new route* at the end. |
| **Waitlist accounts are separate** | Nothing is read from `config/credentials.local.ini`. A waitlist entry belongs to the account that created it, so it must be a deliberate choice — never the hourly rotation. |

---

## Step 1 — Find the combination labels

A client names the combination they want, and the label must match **exactly**
(case- and whitespace-insensitive) one in `config/routes/<ROUTE>.json`.

```powershell
python -m src.waitlist add-route --route AE-ITA
```

That prints the available labels. (It also scaffolds a config if none exists —
harmless if one already does, and it will refuse to overwrite.)

For **AE-ITA** the labels are:

```
Dubai - Schengen Visa
Dubai - Short Stay - Tourist visa
```

> These two used to share one label (`Italy Visa Application Center ,Dubai`).
> That was ambiguous — a client naming it would have been registered for
> whichever came first in the file. Now fixed, and a run refuses to proceed if
> it ever recurs.

---

## Step 2 — Create the client file

Copy the template:

```powershell
copy config\registrants\example.json.example config\registrants\fatima.json
```

**The filename is the client id.** Lowercase, letters/digits/`-`/`_` only.
`fatima.json` → `--registrant fatima`.

Then edit it:

```jsonc
{
  "route": "AE-ITA",
  "enabled": true,
  "combos": ["Dubai - Schengen Visa"],

  "account": "waitlist1@example.com",
  "account_password": "the-password",

  "first_name": "FATIMA",
  "last_name": "ALI",
  "nationality": "India",
  "passport_number": "B7654321",
  "date_of_birth": "1992-08-14",
  "phone_country_code": "971",
  "phone_number": "509998888",
  "email": "fatima@example.com",

  "passport_scan": "managed"
}
```

### What each targeting key does

| Key | Notes |
|---|---|
| `route` | One route per file. `AE-ITA`, matching `config/vfs_urls.ini`. |
| `enabled` | `false` parks the client without deleting their data. |
| `combos` | List one or more labels from Step 1. **Nothing registers unless it is named here** — this is one of four independent opt-ins. |
| `account` + `account_password` | All-or-nothing. Omit **both** to use the shared `[waitlist] account` from `config.local.ini`. Several clients may share one account. |
| `passport_scan` | Only for portals that upload a document (Italy). See Step 3. |

### Data fields

Free-form — add whatever the portal asks for. The names must match the
`{{placeholders}}` in `config/waitlist/<ROUTE>.json`; Step 4 tells you if one is
missing.

- **Dates: always ISO** (`1992-08-14`). Route configs reformat per portal.
- **Phone split** into `phone_country_code` + `phone_number` — some portals use
  two inputs. Store `971`, not `+971`.
- **`nationality`** must match the portal's dropdown text (case-insensitive
  substring).

---

## Step 3 — Add the passport scan (Italy only)

Italy asks for no typed details at all: you upload the passport **bio page** and
VFS reads the fields out of it. Switzerland does not need this.

```powershell
python -m src.waitlist documents add --registrant fatima --file "C:\path\to\passport.jpg"
```

This **copies** the file (your original stays put) into:

```
C:\Users\<you>\AppData\Local\vfs-bot\documents\fatima\passport_bio.jpg
```

Then the client file just says `"passport_scan": "managed"`.

| | |
|---|---|
| Formats | `.png` `.jpg` `.jpeg` `.pdf` — checked by content, not just extension |
| Max | **2 MB** (VFS's limit) |
| Content | Bio page only, one page |

**Why "managed":** the document is deleted automatically once the registration
is confirmed. A passport scan kept after that is pure liability. You *can* point
at an absolute path instead, but then nothing cleans it up.

> Never put documents in `config/registrants/` — that is inside the git repo.
> The pre-commit hook blocks images, but do not rely on it.

Check what is held:

```powershell
python -m src.waitlist documents list
```

---

## Step 4 — Validate (no browser)

```powershell
python -m src.waitlist check --registrant fatima
```

This is the cheap check — run it after every edit. It confirms the route config
is sound, the combos exist, the account resolves, and **every `{{placeholder}}`
the route needs has a value**. A missing passport number costs a second here
instead of a half-finished registration three pages in.

```
✓ Route config for AE-ITA: 5 step(s); commit step = 'review_pay'
✓ Client 'fatima': 9 field(s), 1 combo(s)
    ✓ account wa***@example.com (from client file (fatima))
    ✓ Dubai - Schengen Visa
    ✓ every {{placeholder}} resolves
```

Fix anything it flags before going further.

---

## Step 5 — Check the live page

```powershell
python -m src.waitlist doctor --route AE-ITA --walk
```

Logs in and probes every configured selector **without typing or submitting**.
VFS reskins without warning, and this turns a cryptic mid-run timeout into
"review_pay: 'I accept the' matched 3 elements".

`--walk` also ticks the checkbox and advances to the later pages. **No
registration is created** — it stops before the committing step — but the form
does move, so it asks for confirmation.

---

## Step 6 — Dry run

```powershell
python -m src.waitlist run --registrant fatima
```

Walks every step, fills everything, screenshots — and **stops before the
committing submit**. Default behaviour while `[waitlist] dry_run = true`.

---

## Step 7 — Go live

Only when Steps 4–6 are clean.

```powershell
python -m src.waitlist run --registrant fatima --live
```

Prompts for confirmation first. Then:

```powershell
python -m src.waitlist journal --all
```

```
[SUCCESS] AE-ITA · Dubai - Schengen Visa · fatima · ref SWDB79923880977
```

The passport scan is deleted automatically at this point.

---

## Quick reference

```powershell
python -m src.waitlist status                        # every client, by route
python -m src.waitlist check     --registrant NAME   # validate, no browser
python -m src.waitlist doctor    --route AE-ITA      # selectors vs live page
python -m src.waitlist run       --registrant NAME   # dry run
python -m src.waitlist run       --registrant NAME --live
python -m src.waitlist journal   --all               # history
python -m src.waitlist documents list
```

`--route AE-ITA` runs **every** enabled client on that route;
`--registrant NAME` runs just one.

---

## The four gates

Nothing is ever submitted unless **all four** pass. No single mistake can cause
an unintended registration:

1. `[waitlist] register_enabled = true` in `config.local.ini`
2. `config/waitlist/<ROUTE>.json` exists with `"enabled": true`
3. The client file has `"enabled": true`
4. The combination is named in that client's `"combos"`

Plus `dry_run`, `max_per_run` and `max_per_day` on top.

---

## Troubleshooting

| Message | Fix |
|---|---|
| `'X' is not defined in config/routes/…` | The label does not match. Run Step 1 and copy it exactly. |
| `matches N combinations` | Two combos share a label — give each a distinct `"label"` in `config/routes/`. |
| `No VFS account is configured` | Add `account` + `account_password`, or set `[waitlist] account`. Slot-check credentials are never used. |
| `Placeholder {{x}} has no value` | The route needs a field the client file lacks. Add it. |
| `no waitlist offered` | That combo has no waitlist right now — nothing wrong. |
| `SLOTS AVAILABLE — run stopped` | A real slot exists. Book it; waitlisting would be wrong. |
| `already registered on …` | Dedup working. Check `journal --all`. |
| `a previous attempt is unresolved` | Verify on the portal, then `python -m src.waitlist resolve …`. |

---

## Adding a client to Switzerland instead

Identical, with two differences:

- `"route": "AE-CHE"`, combos `Dubai - SCHENGEN` / `Abu Dhabi - SCHENGEN`
- **No `passport_scan`** — Switzerland types the fields, so it needs
  `address_line_1` / `address_line_2` instead

---

## Adding a new route

```powershell
python -m src.waitlist add-route --route AE-FRA
```

Scaffolds `config/waitlist/AE-FRA.json` extending `_default.json`. You then map
the portal by hand: walk it once, note every field on "Your Details" and every
consent on "Review & Pay", fill them in, and work through Steps 4–7. It stays
`"enabled": false` until you flip it deliberately.

---

## Status right now

| Route | State |
|---|---|
| **AE-CHE** | ✅ Working — proven live (`SWDB79923880977`) |
| **AE-ITA** | ⚠️ Configured but `"enabled": false` — its OTP goes to the client's **mobile**, and that relay is not built yet (see [OTP_RELAY_TASKS.md](OTP_RELAY_TASKS.md)). Steps 1–5 work today; 6–7 do not. |
