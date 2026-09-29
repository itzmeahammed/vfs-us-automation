# Glossary — the terms, and how they combine

Written against your live config on 2026-08-21. Every example below is real.

---

## The one-sentence version

**Four independent switches must ALL be right before anyone is registered.**
Any single one being wrong means nothing happens. That is the design, not a
bug — it takes four deliberate acts to put a real person on a real waitlist.

---

## Part 1 — The two words that get confused

These sound similar and are completely different. Almost every "why didn't it
register?" question is one of these two.

### `enabled` — "is this thing switched on?"

A property of **a thing**: one client, or one route. It answers *"should this
particular item be included at all?"*

There are **two separate `enabled` flags**, at different levels:

| Where | File | Means |
|---|---|---|
| **Client** | `config/registrants/test-che.json` | Include *this person* in runs |
| **Route** | `config/waitlist/AE-CHE.json` | Accept registrations for *this destination* |

Both must be `true`. A client marked `enabled: true` on a route whose config
says `"enabled": false` still does nothing.

### `dry_run` — "do it for real, or rehearse?"

A property of **the run**, not of any thing. It answers *"when we get to the
button that actually submits, do we click it?"*

- `dry_run = true` → walk the entire flow: log in, solve Cloudflare, pick the
  centre, fill every form field, take screenshots — then **stop just before the
  committing click**. Nothing reaches VFS.
- `dry_run = false` → same flow, and it clicks. **A real person is now on a
  real waitlist.**

> **The distinction that matters:** `enabled` decides *who takes part*.
> `dry_run` decides *whether it's real*. A fully enabled client in a dry run
> submits nothing.

---

## Part 2 — "Parked" vs "Armed"

These are not config values — they are the words this project uses when talking
about a client's `enabled` state. You will see them in API responses and in the
console.

| Word | Means | In the file |
|---|---|---|
| **Parked** | Data is stored, but runs skip this person | `"enabled": false` |
| **Armed** | This person will be included in runs | `"enabled": true` |

**Creating a client never arms it.** `POST /clients` always writes
`enabled: false` unless you explicitly send `enabled: true`. Arming is a
separate call — `POST /clients/{id}/enable`. This is deliberate: a bug in your
web app must not be able to arm a fleet of clients for live registration.

Your `test-che.json` currently says `"enabled": true` — it is **armed**.

> **Armed does not mean it will register.** It means it is *eligible*. The
> master switch still has to be on, and the run still has to be live. Right now
> `register_enabled = false`, so `test-che` is armed and going nowhere.

---

## Part 3 — The four switches, in order of power

All live in `config/config.ini`, overridden by `config/config.local.ini`.

### 1. `register_enabled` — the master kill switch

**Currently: `false`**

The single most important setting. While `false`, **nothing can ever be
submitted**, no matter what every other setting says. The slot bot still runs,
still detects waitlists, still sends Telegram alerts. It just cannot register.

This is gate 1, and it short-circuits before anything else is even checked.

### 2. `dry_run` — rehearsal vs real

**Currently: `true`**

Explained above. Only matters once `register_enabled = true`.

### 3. `auto_trigger_enabled` — who starts a run?

**Currently: `false`**

- `false` → **manual**. A run happens only when *you* start it (CLI or
  `POST /trigger/waitlist`).
- `true` → **automatic**. When the slot checker finds an open waitlist, it
  fires the waitlist bot itself, without you.

### 4. `auto_trigger_dry_run` — rehearsal for auto-triggered runs

**Currently: `true`**

The same idea as `dry_run`, but applied specifically to runs the bot starts by
itself. Lets you turn automation on while keeping it harmless.

---

## Part 3b — How `auto_trigger` and `dry_run` actually interact

This one is counter-intuitive, so it is worth stating exactly. Verified against
`register.py:474`, which is the single line that decides:

```python
dry_run = guards.dry_run() if force_dry_run is None else force_dry_run
```

`force_dry_run` is `None` for a manual run, and is set to
`auto_trigger_dry_run` for an auto-triggered one.

**They are not layered. One replaces the other.**

| Who started the run | Which switch decides | The other switch |
|---|---|---|
| **You** (CLI / API) | `dry_run` | `auto_trigger_dry_run` is **ignored** |
| **The bot** (auto-trigger) | `auto_trigger_dry_run` | `dry_run` is **ignored** |

### The truth table

| Trigger | `dry_run` | `auto_trigger_dry_run` | Result |
|---|:--:|:--:|---|
| MANUAL | true | true | rehearsal |
| MANUAL | true | false | rehearsal |
| MANUAL | false | true | **SUBMITS** |
| MANUAL | false | false | **SUBMITS** |
| AUTO | true | true | rehearsal |
| AUTO | true | **false** | **SUBMITS** ⚠️ |
| AUTO | false | true | rehearsal |
| AUTO | false | false | **SUBMITS** |

**Read row 6 twice.** With `dry_run = true` — which looks like "we are safe" —
an auto-triggered run **still submits for real** if `auto_trigger_dry_run` is
false. Setting `dry_run = true` does **not** protect you from the auto-trigger.

Row 7 is the mirror image, and is genuinely useful: `dry_run = false` (you can
register by hand) while `auto_trigger_dry_run = true` (the bot only rehearses).
That is the sane way to run automation before you fully trust it.

### Why it is built this way

Because "let me register someone by hand" and "let the bot register people
unattended" are different levels of trust, and you need to grant them
separately. If they shared one switch, turning on manual live registration
would silently arm the unattended path too.

### The one switch that overrides both

`register_enabled = false` blocks **everything**, on every path. Verified: with
`auto_trigger_enabled = true`, `dry_run = false` and
`auto_trigger_dry_run = false`, the gate battery still returns:

```
BLOCK: waitlist registration is switched off ([waitlist] register_enabled = false)
```

So the real hierarchy is:

```
register_enabled ─── false ──▶ nothing registers, ever. Full stop.
       │
      true
       │
       ├── run started by YOU  ──▶ dry_run decides
       └── run started by BOT  ──▶ auto_trigger_dry_run decides
```

---

## Part 4 — Posture: reading all four at once

`GET /status` collapses the four switches into one word. This is the fastest
way to know where you stand.

| Posture | Means | Switches |
|---|---|---|
| **PARKED** | Nothing can register. **You are here.** | `register_enabled = false` |
| **MANUAL** | You can trigger real registrations by hand | `register_enabled = true`, `auto_trigger_enabled = false` |
| **AUTO (DRY RUN)** | Bot fires runs itself, but submits nothing | `auto_trigger_enabled = true`, `auto_trigger_dry_run = true` |
| **AUTO (LIVE)** | Fully autonomous. Real submissions, unattended. | everything on |

---

## Part 5 — Words about a registration's outcome

Written to `state/waitlist_journal.jsonl`, and shown in job results.

| Status | Means | Did anything reach VFS? |
|---|---|---|
| `skipped` | A gate blocked it before starting | No |
| `dry_run` | Rehearsed to the commit point and stopped | No |
| `success` | Registered. There is a `vfs_reference`. | **Yes** |
| `failed` | It tried and did not get on | No |
| `pending` | Written **immediately before** the submit click | **Maybe** |
| `unknown` | Submitted, but the result could not be read | **Maybe** |

### The two that need a human

`pending` and `unknown` are grouped as **`needs_attention`**, and they are the
reason this system has a journal at all.

They mean: *a submit may have landed, and we cannot tell.* Retrying could
register the same person twice — two waitlist places for one trip, which VFS
may void both of. So the client is **blocked** until a human checks the VFS
account and records what they found:

```powershell
python -m src.waitlist resolve --route AE-CHE --combo "Dubai - SCHENGEN" `
  --registrant test-che --status success   # or: failed
```

A row in this state is called **dangling**.

---

## Part 6 — `combos`: a preference order, not a shopping list

This one is worth reading twice.

```json
"combos": ["Dubai - SCHENGEN", "Abu Dhabi - SCHENGEN"]
```

That means: *"I'll take Dubai if it opens; otherwise Abu Dhabi."*

It does **not** mean "register me for both". One person wants **one**
appointment. Two entries would hold two places for one need, deny one to
somebody else, and risk VFS voiding both as duplicates.

**Guard 6b enforces this.** The first committed entry on a route ends that
client's run for that route — in this run and every future one — until the
entry is resolved or cancelled.

A **combo** (or "combination") is one centre + category pair, written exactly
as the VFS portal spells it: `"Dubai - SCHENGEN"`. The label must match the
portal's dropdown text character for character, which is why
`GET /routes/{route}/readiness` exists — always populate your UI from that.

---

## Part 7 — The caps

| Setting | Currently | Means |
|---|---|---|
| `max_per_run` | 3 | Most registrations **one invocation** may commit |
| `max_per_day` | 20 | Most registrations **per calendar day**, across all runs |

`max_per_run` resets every time a run starts, so it does not limit your daily
throughput — it limits one burst. **`max_per_day` is the real ceiling.**

Neither counts `skipped` or `dry_run` outcomes: nothing touched VFS.

---

## Part 8 — Other terms you will meet

| Term | Means |
|---|---|
| **Route** | A destination, as `AE-CHE` (from UAE, to Switzerland). |
| **Client / registrant** | One applicant. One JSON file in `config/registrants/`. |
| **Slot bot** | The scheduled checker. Watches for openings, notifies. Never registers. |
| **Waitlist bot** | The registrar. Fills the form and submits. Only this can register. |
| **Job** | One background run started via the API. Has a `job_id`, logs, results. |
| **Single-flight** | Only one job at a time. A second trigger gets `409`. |
| **Run lock** | An OS-level mutex so the scheduler and a manual run cannot overlap and corrupt the journal. |
| **Idempotency-Key** | Send the same key twice, get the **same job** back — protects against double-submits when your app retries. |
| **Journal** | `state/waitlist_journal.jsonl`. Append-only record of every attempt. The source of truth. |
| **Dangling** | A journal row stuck in `pending`/`unknown`, needing a human. |
| **Slots available** | Better than success: a real bookable appointment appeared, so the run stopped instead of waitlisting. Tell the client to **book**. |
| **Posture** | The one-word summary of the four switches. |
| **Readiness** | Whether a route can currently accept registrations, plus its valid combos. |

---

## Part 9 — Worked example: your current state

`test-che.json` says `"enabled": true`. Will it register?

**No.** Here is every gate, in the order they run:

| # | Gate | Result |
|---|---|---|
| 1 | `register_enabled` | ❌ **`false` — blocked here** |
| 2 | Route `AE-CHE` enabled | ✅ would pass |
| 3 | Client `enabled` | ✅ would pass (armed) |
| 4 | Client wants this combo | ✅ would pass |
| 5 | Dangling entry? | ✅ none |
| 6 | Already registered? | ✅ no |
| 6b | Holds another entry on this route? | ✅ no |
| 7 | `max_per_run` | ✅ 3 |
| 8 | `max_per_day` | ✅ 20 |

It fails at gate 1 and never reaches the rest. The message you would see:

```
waitlist registration is switched off ([waitlist] register_enabled = false)
```

**To actually register `test-che` you would need to:**

1. Set `register_enabled = true` — now it is MANUAL, dry runs only
2. Set `dry_run = false` — now it is live
3. Trigger a run, or set `auto_trigger_enabled = true` to let the bot do it

Three deliberate acts, in `config/config.local.ini`. That is the safety.

---

## The mental model

Think of it as a **series circuit** — every switch must be closed for current
to flow:

```
register_enabled ──── route enabled ──── client enabled ──── !dry_run ──── REGISTERS
    (false)              (true)              (true)           (false)
       ↑
   open here, so nothing flows
```

Right now the first switch is open. Nothing downstream matters until it closes.

---

## See also

- [MANUAL_TEST_RUNBOOK.md](MANUAL_TEST_RUNBOOK.md) — verify each layer yourself
- [SYSTEM_GUIDE.md](SYSTEM_GUIDE.md) — the architecture behind these terms
- [API_REFERENCE.md](API_REFERENCE.md) — every endpoint
