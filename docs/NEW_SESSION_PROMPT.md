# Prompt for a new session

Paste everything below the line into a fresh Claude Code session in this repo.

---

You are working on the **VFS slot checker and booking bot** (Python 3.13 + Playwright, Windows, venv at `.venv`). Read `ARCHITECTURE.md` section 13-14, `QUICK_COMMANDS.md` and `config/booking/_default.json` before changing anything.

**My task this session: add new routes.** I will capture the HTML of each VFS page, and I want you to build every config file a route needs, driven by real selectors rather than guesses.

## What the system does

| Flow | Entry point | Runs when | Money |
|---|---|---|---|
| **Slot check** | `python -m src.supervisor` | Scheduled task `VFS Slot Checker`, every 30 min, all routes | No |
| **Waitlist registration** | `python -m src.waitlist run` | Only when I run it | No |
| **Booking walk** | `python -m src.booking probe --walk` | Manual, or the API | No — stops before payment |
| **Booking commit** | same, `--walk --commit` | Manual, or auto-book after a slot check | **Yes, irreversible** |
| **API** | `python -m src.api` | Backend for the web app; also watches VFS invitation emails | Starts the above |

A route is `AE-<DEST>` (ISO alpha-3 destination, e.g. `AE-ESP`). Every route is the same Angular VFS portal, so an existing route is the best template — `AE-NOR` is the most complete, `AE-SWE` was built from it.

## The config layers a route needs

| File | Purpose | Needed for |
|---|---|---|
| `config/vfs_urls.ini` | the login URL; a leading `;` pauses the route | everything |
| `config/routes/<ROUTE>.json` | centre / category / sub-category combinations to check; `"otp": true` if the portal emails a code | slot check |
| `config/waitlist/<ROUTE>.json` | registration pages (`extends` a base) | waitlist only |
| `config/booking/<ROUTE>.json` | the booking walk: steps, fields, selectors, commit boundary | booking |
| `config/payment/<PROCESSOR>.json` | gateway selectors; `CYBERSOURCE` (most routes) or `CYBERSOURCE_UC` (Hungary only) | commit |
| `src/utils/telegram_message.py` | `DESTINATION_FLAGS` + `DESTINATION_NAMES` | reporting |

Also give at least one account the route in its `routes` list (`config/credentials.local.ini`).

## Rules for writing a booking config

- **`extends: "_default"`**, then override only what differs. Don't copy a whole file when three steps differ.
- **Step types** are a closed set, validated at load: `dashboard_resume`, `identity_assert`, `start_booking`, `form`, `slot_pick`, `payment`, `confirm` (see `src/booking/config.py`).
- **Field widgets** are a registry in `src/waitlist/fields.py`: `text`, `textarea`, `mat-select`, `select`, `checkbox`, `radio`, `date`, `file`.
- **Prefer `control` (the Angular `formcontrolname`) over `selector`.** VFS reskins break ids like `mat-input-3`; form-control names survive. Next best is `label`, matched inside the `app-dynamic-control` wrapper. Raw CSS last.
- **Values are templates**: `{{first_name|upper}}`, `{{passport_expiry|date:%d/%m/%Y}}`, resolved by `src/waitlist/context.py`.
- `"required": false` or `"if_present": true` for a control that may not render.
- **`"commits": true` marks the irreversible step** — the `payment` step once the route's later pages are confirmed, otherwise `slot_pick`. Overstating the boundary is the safe direction.
- **Leave `"enabled": false`** until the walk has been proven end to end.
- Add a `_comment` explaining anything a selector doesn't make obvious. This codebase comments the *why*; match that density.

## How to get the selectors

```powershell
# Walk as far as the pages allow and save the DOM of every page
python -m src.booking probe -sc AE -dc ESP --walk --capture full --keep-open
# Stop after one step, to map a page at a time
python -m src.booking probe -sc AE -dc ESP --walk --capture full --to appointment_details
# Read the real dropdown options and write them into the route config
python -m src.waitlist doctor --route AE-ESP --walk --harvest
```

Captures land in `runs/<ROUTE>/<run_id>/`. I may also paste page HTML straight into the chat.

## Validate before I run anything live

```powershell
python -m src.booking check                  # validates config/booking/*.json, offline
python -m src.waitlist check --route AE-ESP   # validates combos and placeholders, offline
python -m pytest -q                           # ~2400 tests; about 10 fail already from my local config
```

A config error must surface offline. A login is the scarce resource: VFS blocks an account after a few in quick succession.

## Hard rules

1. **Never run a `--commit` booking yourself.** It charges a real card and books a real appointment. Propose the command; I run it.
2. **Never put a card number in a file, a log, a test or the chat.** The card comes from `VFS_CARD_*` env vars or the gitignored `config/card.local.txt`.
3. **No tracebacks reach the console, the job log or the web app.** Every error is one line that blames VFS; unknown ones say "Problem not identified". See `src/utils/public_errors.py` and `src/utils/cli_guard.py`.
4. **Don't use `git stash`** in this repo — it lost my staged work once.
5. **Write tests for new behaviour**, in the style of the existing ones: the docstring says what is at risk, not what the function does.
6. Keep your replies short and high-signal. Verdict tables over prose. Tell me plainly when something failed.

Start by reading an existing route's booking config end to end and telling me which pages you still need HTML for.
