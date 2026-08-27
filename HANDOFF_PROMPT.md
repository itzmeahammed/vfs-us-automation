# Prompt for a fresh chat

Copy everything below the line into a new conversation.

---

I need you to teach me the safety switches ("knobs") in this codebase properly.
I keep getting confused, and a previous session gave me one wrong explanation
that had to be corrected — so **verify everything against the actual code
before you explain it. Do not describe behaviour from memory or from the
comments.** If a doc and the code disagree, the code wins, and tell me.

## Who I am

I own and operate this bot. I am not the person who wrote most of it. I can
read code but I want to understand the *system* — what each switch does, how
they combine, and which combinations are dangerous. I am about to start using
this for real clients, so a wrong mental model here costs real people real
appointment slots.

## What this system does

It watches VFS Global visa appointment portals for a route like `AE-CHE`
(applying from the UAE, to Switzerland). When appointments are unavailable, VFS
sometimes offers a **waitlist**. Two bots:

- **Slot bot** — scheduled, watches for openings, notifies via Telegram. Never
  registers anything.
- **Waitlist bot** — logs into a real VFS account, fills a multi-step form, and
  submits to put a real person on a real waitlist. **This is irreversible from
  the bot's side** — cancelling means logging into the VFS portal by hand.

There is also a local FastAPI wrapper (`src/api/`) so my web app
(travnooker.com) can create clients and trigger runs, plus an ngrok tunnel and
a browser console at `/console`.

## What I want from you

Teach me, in this order:

1. **Every knob**, one at a time. For each: where it lives, what it does, what
   breaks if I get it wrong, and what a wrong setting looks like in practice.
2. **How they combine** — especially the ones that interact non-obviously.
3. **A decision guide**: given what I want to happen, which knobs do I set?
4. **The failure modes**: which combination could actually put someone on a
   waitlist when I didn't intend it.

Use my real current values throughout (below), not hypotheticals. Where a
concept has a "sounds like it means X but actually means Y", say so explicitly
— those are where I keep going wrong.

## The knobs

In `config/config.ini`, overridden by `config/config.local.ini` (gitignored).
Section `[waitlist]`:

- `register_enabled`
- `dry_run`
- `auto_trigger_enabled`
- `auto_trigger_dry_run`
- `max_per_run`
- `max_per_day`

Plus two **`enabled`** flags at different levels, which confused me badly:
- client-level: `config/registrants/<id>.json` → `"enabled"`
- route-level: `config/waitlist/<ROUTE>.json` → `"enabled"`

## My current state (verified 2026-08-21)

```
register_enabled       = True
dry_run                = True     <- note: a LIVE run still happened, see below
auto_trigger_enabled   = False
auto_trigger_dry_run   = True
max_per_run            = 3
max_per_day            = 20

clients:
  ahmed-nld    AE-NLD   enabled=False   (parked)
  test-che     AE-CHE   enabled=True    (armed, and now BLOCKED by guard 6b)

dangling journal entries: 0
```

`test-che` is a test client but it holds a **real VFS password** for a real
account (`mufaddal@travnook.com`), on `AE-CHE`, which is an enabled route.

### I have a REAL registration on the portal

This is the most important thing to understand about my current state.

```
2026-08-21T11:32:57  test-che  pending  ref=None
2026-08-21T11:33:54  test-che  success  ref=SWDB80350743950
```

**`test-che` is genuinely on the Swiss waitlist**, reference
`SWDB80350743950`, using account `mufaddal@travnook.com`. This came from a job
launched with `--live --yes`.

The thing I want explained properly: **config said `dry_run = true` at the
time, and the run still submitted.** The previous session traced it to
`__main__.py:301-305`:

```python
force_dry_run = None
if args.live:      force_dry_run = False
elif args.dry_run: force_dry_run = True
```

So `--live` sets `force_dry_run=False`, which wins over the config default at
`register.py:474` — the **same override mechanism** as `auto_trigger_dry_run`.

**Verify this yourself, then explain the general rule to me**, because the
implication is important: `dry_run = true` in config is NOT a guarantee that
nothing can be submitted. There appear to be THREE ways a run's dry-run status
gets decided (config default, `--live`/`--dry-run` flag, auto-trigger switch),
and I want the precedence spelled out as one table.

Consequences to explain to me:
- Guard 6b now blocks `test-che` from any further AE-CHE registration.
- To free it, I must cancel on the VFS portal by hand, then run
  `python -m src.waitlist resolve --route AE-CHE --combo "Dubai - SCHENGEN" --registrant test-che --status failed`.
- Tell me whether I should cancel it (it is a test entry holding a real
  waitlist place on my own account).

Earlier the same day I also completed a clean Stage 2 dry run: it drove the
real portal, logged in, solved Cloudflare, filled the centre and category, and
stopped without ticking the accept checkbox. Screenshots in `screenshots/`.

## Things I was told that I want you to independently verify

A previous session told me these. Check each against the code and tell me if
any is wrong:

1. **`register_enabled = false` blocks everything**, on every path, no matter
   what the other switches say.
2. **`dry_run` and `auto_trigger_dry_run` do NOT stack — one replaces the
   other.** The claim is that `register.py:474` decides:
   ```python
   dry_run = guards.dry_run() if force_dry_run is None else force_dry_run
   ```
   with `force_dry_run=None` for manual runs and
   `force_dry_run=auto_trigger_dry_run` for auto-triggered ones
   (`autotrigger.py:329`).
3. **The dangerous corner**: `dry_run = true` + `auto_trigger_dry_run = false`
   means an auto-triggered run **submits for real**, even though `dry_run=true`
   reads as safe.
4. **`max_per_run` resets every invocation**, so it does not cap daily
   throughput — `max_per_day` is the real ceiling.
5. **A client's `combos` list is a preference order, not a shopping list** —
   one client gets ONE waitlist entry per route, enforced by "guard 6b" in
   `src/waitlist/guards.py`.
6. **The API caches config at startup** — editing `config.local.ini` does
   nothing until the API process is restarted.

## Where to look

Read these rather than trusting the docs:

| File | What decides there |
|---|---|
| `src/waitlist/guards.py` | The 8 gates, in order. Gate 1 is the master switch; gate 6b is one-entry-per-route. |
| `src/waitlist/register.py:474` | Which dry-run switch actually applies |
| `src/waitlist/autotrigger.py:329` | What the auto-trigger passes as `force_dry_run` |
| `src/api/status.py:74` | How posture (PARKED/MANUAL/AUTO) is computed |
| `src/settings.py` | Defaults and the settings objects |
| `config/config.ini` | The commented switch definitions |

Existing docs — **treat as possibly stale, verify before repeating**:
`GLOSSARY.md`, `MANUAL_TEST_RUNBOOK.md`, `SYSTEM_GUIDE.md`, `ARCHITECTURE.md`,
`API_REFERENCE.md`.

## Useful commands

```powershell
python -m src.waitlist status              # effective switches + clients
python -m src.waitlist check --route AE-CHE # validate config, no browser
python -m src.waitlist journal --all        # every registration attempt
python -m pytest -q                         # 679 tests currently pass
```

The gate battery for one client, without a browser:

```powershell
python -c "import sys; sys.path.insert(0,'.'); from src.utils.config_reader import initialize_config; initialize_config(); from src.waitlist import guards, registrant as R; r=R.load('test-che'); v=guards.check(r.route, r.combos[0], r); print('ALLOW' if v.allowed else 'BLOCK'); print(v.reason)"
```

## Ground rules

- **Verify before asserting.** Run the code, print real values, read the actual
  lines. If you are inferring rather than confirming, say which.
- **Do not change any config or arm/park any client** without asking me first.
  Explaining is the job; changing state is not.
- Build me a truth table for the dry-run interaction rather than describing it
  in prose — prose is what confused me.
- Tell me plainly if something I believe is wrong.
