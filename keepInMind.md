# keepInMind.md — the inbox watcher, for the person who owns it

You did not write this code. This file is what you need to hold in your head to
run it, judge it, and not be bluffed by it — nothing more.

Read once. Come back to §7 when something looks wrong.

---

## 1. What it does, and what it deliberately does not

`src/inbox/` watches the VFS account mailboxes over IMAP and says what each
email **is**. That's all.

**It does not** open a browser, log into VFS, book anything, or change anything
on VFS's side. It reads mail (read-only — it never even marks a message as
*seen*) and writes to two local files.

The one command that writes anything meaningful is `reconcile`, and it is a
**dry run unless you pass `--apply`**.

Why so passive: the whole point of Phase 1 is to collect *real* email wording
from every country before anything acts on it. Designing twelve countries'
matchers from one Italian sample would be guesswork. **That already paid off —
see §5.**

---

## 2. The one idea the design rests on

> **The email is a trigger, not an instruction.**

The VFS invitation contains **no application ID and no deep link** — "Click Here"
is just the normal portal login page. It carries a name and a category, and
that is all.

So the watcher never decides *which* application an email is about. It records
what arrived; later phases settle identity against the dashboard.

**Update, 2026-09-02 — the identity chain is now closed.** The reference number
in the confirmation email is *the same value* the dashboard shows as "Group
Reference Number" (you confirmed this for Greece):

```
confirmation email  →  "Unique Reference Number is GRC127086415238"
dashboard row       →  "Group Reference Number - GRC127086415238"   ← same
invitation email    →  "Dear EMAN SAMIR MOHAMED IBRAHIM"            ← name only
```

That makes the eventual booking lookup **exact** rather than fuzzy: store the
reference at registration, and when an invitation names a client, select the
dashboard row by their stored reference. Name matching drops from "the only
thing standing between us and booking the wrong person" to a cross-check.

It does **not** change what this package does. The watcher still just reads and
records. But it means the risky part of Phases 4–7 got much safer.

---

## 3. The files that matter

Six modules. Only three are worth understanding deeply.

| File | What it is | Care level |
|---|---|---|
| `src/inbox/matcher.py` | **The brain.** Takes an email, returns "this is an invitation, for this name". Pure — no network, no files, no clock. | **High** |
| `src/inbox/seen.py` | Remembers which messages were already read. | **High** — see §7 |
| `src/inbox/reconcile.py` | The only thing that writes to the journal. | **High** |
| `src/inbox/watcher.py` | IMAP plumbing. Fetches mail, hands it to the matcher. | Medium |
| `src/inbox/config.py` | Loads `config/inbox/*.json`. | Low |
| `src/inbox/report.py` | Builds the digest, sends Telegram. | Low |

**`matcher.py` is pure on purpose.** It means the entire classification logic can
be tested against saved emails with no mailbox, no network, and no risk. That is
why `python -m src.inbox test` exists and why you should trust it.

### Config, which is where you'll actually work

```
config/inbox/_default.json    shared matchers — most of the logic lives here
config/inbox/AE-ITA.json      Italy's differences only
```

`AE-ITA.json` inherits `_default.json` via `"extends"` and overrides matchers
**by name**. Same mechanism as `config/waitlist/*.json`, deliberately.

**Adding a country = one new JSON file. No code.** That is the design goal.

---

## 4. The five commands

```powershell
python -m src.inbox check       # validate the JSON configs        OFFLINE, safe
python -m src.inbox test        # run matchers on saved emails     OFFLINE, safe
python -m src.inbox status      # what's configured, which mailboxes
python -m src.inbox watch       # read real mail, report           read-only
python -m src.inbox watch --once   # one pass, then stop
python -m src.inbox reconcile   # settle uncertain journal rows    DRY RUN
python -m src.inbox reconcile --apply    # ← the only command that writes
```

**`check` and `test` touch no network.** That is your iteration loop: edit a
matcher, run `test`, see if it still classifies the saved emails correctly.

**Exit codes are meaningful.** `check` exits 1 on a broken config; `test` exits 1
if a saved email goes unmatched; `watch --once` exits 1 if a mailbox failed. Safe
to put in a scheduled task.

---

## 5. What the first live run actually found (2026-09-02)

This is the part to remember, because it corrected two things I had assumed.

**310 messages examined across 3 mailboxes. 123 were VFS mail.**

| Count | Subject |
|---|---|
| 111 | One Time Password |
| 5 | Welcome |
| 3 | **Waitlist cancellation** |
| 1 each | Appointment Cancellation Confirmation, Refund Update, Refund Initiated with PG, Refund Processed |

**Zero invitations. Zero registration confirmations.** Nothing is broken — those
emails simply are not in these mailboxes right now. The Italian samples you gave
me came from elsewhere, and they still classify correctly (`test` proves it).

### Correction 1 — the sender is not what the email says it is

Every VFS message came from **`donotreply@vfshelpline.com`** or
`donotreply@vfsglobal.com`. The `info.italyuae@vfshelpline.com` address printed
in the email's signature block is **not** the actual sender.

I had originally pinned the signature address in `AE-ITA.json`. **That would have
matched nothing.** Now fixed to pin the domains only. This is exactly the class of
error that observational-first exists to catch, and it caught it on day one.

### Correction 2 — an email type that was going unrecognised

**"Waitlist cancellation"** — sent when a waitlist entry is cancelled. The three
in your mailboxes are ones **you cancelled yourself**, so nothing is wrong; I
overstated this initially as a discovery when it is really just an email type
that had no matcher.

What it changed: it now has one, so the email is recognised and its reference
captured instead of landing in the unrecognised pile. **No behaviour changed.**

The genuine (low-priority) gap it points at: a cancelled entry means a journal
row marked `success` is no longer true. `SWDB79923880977` reads as `success`
today but is cancelled on VFS's side. That matters only once the booking flow
trusts the journal to decide who to act on. Logged in TASKS.md.

One trap worth knowing: this email's greeting has **no comma** (`Dear TRAV NOOK
Your appointment...`) unlike the invitation and confirmation, which do. A pattern
anchored on the comma captures nothing. There is a test pinning this.

### Correction 3 — two bugs found by adding a second and third country

Adding Greece and Netherlands surfaced two real bugs that one country could
never have revealed. Both are fixed and have regression tests.

**The 36-vs-48 bug.** Greece and Netherlands say *"valid for 36 hours"*; Italy
says 48. Every country's invitation shares the same subject and nearly identical
wording, so an Italian email matched Greece's matcher (routes tried
alphabetically) and inherited **Greece's 36-hour window**. Silent, and it would
have the system believe a window was open 12 hours after it closed.

Fixed by requiring the portal URL in the body — `/are/en/ita/` vs `/are/en/grc/`
— as the country discriminator. It is the only part of the message that names
the country unambiguously.

**The shadowing bug.** Every route inherits the generic catch-alls from
`_default`. Tried route-by-route, Greece's inherited `vfs_other` swallowed the
Italian invitation before Italy's own matcher was reached. `classify_all` now
runs *specific* matchers across all routes first, and only then the catch-alls.

**The lesson to keep:** one country's config proves nothing about multi-country
behaviour. Both bugs were invisible with only Italy configured.

---

## 5b. How the scan actually works (and why it's fast now)

Two filters doing different jobs. Worth understanding, because mixing them up is
how mail gets silently skipped.

| | Job | Granularity |
|---|---|---|
| **IMAP `SINCE`** | **Narrows** — stops the server listing the whole mailbox | one day |
| **UID high-water mark** | **Decides** what has actually been read | exact |

Time must never be the decider. Clocks skew, servers deliver out of order, and a
message can be filed with a timestamp earlier than the pass that missed it. A
UID is monotonic per mailbox, so it is the only safe test. **Narrow with time;
decide with UIDs.**

The first pass over a mailbox looks back `first_pass_days` (30). Every pass after
that searches only since the previous pass — backed off by one day, because
`SINCE` is day-granular and the mail server may be in another timezone.

Measured on your real mailboxes:

| | Server offered | Duration |
|---|---|---|
| First pass | **312 messages** | ~3 minutes |
| Next pass | **30 messages** | ~6 seconds |

A failed pass does **not** advance the window, so the next one re-covers what it
missed rather than skipping it.

### Seeing what it's reading

```powershell
python -m src.inbox watch --once       # one summary line per mailbox
python -m src.inbox watch --once -v    # one line per message examined
```

The `-v` form prints every message with its subject and which matcher caught it.
That is the answer to "what is it actually looking at?". It's at DEBUG because a
first pass over 312 messages would otherwise bury the digest.

---

## 6. The knobs

### Config — `config/config.local.ini`

```ini
[otp]
imap_host = mail.travnook.com    ; ← you set this; shared with the OTP flow
                                 ; port 993 comes from config.ini
[inbox]
poll_seconds = 300               ; gap between passes when watching
first_pass_days = 30             ; how far back a NEW mailbox is read
max_per_pass = 200               ; cap per mailbox per pass
```

`poll_seconds = 300` is deliberately slow. An invitation is valid for **48
hours**, so five minutes of latency is irrelevant, and a low rate keeps you a
negligible load on the mail server. **Do not tune this down** without a reason.

### Which mailboxes get read

Not configured directly — derived from `config/registrants/*.json`. Each client
file names a VFS account; the watcher reads those accounts' mailboxes, dedup'd.

`python -m src.inbox status` shows the resolved list.

### State — two files, both gitignored

```
state/inbox_seen.json      which messages have been read
state/waitlist_journal.jsonl   the existing registration journal (reconcile writes here)
```

**Deleting `inbox_seen.json` is safe** — the next pass re-reads the backlog and
re-reports it. Noisy, not harmful. Useful when testing.

---

## 7. When something looks wrong

### "Is it even running?"

`python -m src.inbox watch` is a **foreground process that never exits** — pass,
sleep 300s, repeat. Close the terminal and it dies. There is no service and no
auto-restart; I have not built one.

So for a multi-week observational run, don't leave a terminal open. Use
`watch --once` on a schedule, the way `run_task.ps1` runs the slot checker. One
pass, exits, reports — survives reboots, needs no terminal.

Either way, this answers the question:

```powershell
python -m src.inbox status
```

```
Mailboxes to watch (4):
    ah***@travnook.com     last read   22s ago   (up to uid 0)
    na***@gmail.com        never read
Most recent pass: 16s ago
```

`Most recent pass` is the liveness signal. Hours old when you expect minutes
means it stopped.

### "It found nothing"

Almost always correct, not broken. Check in this order:

```powershell
python -m src.inbox status      # is imap_host set? are mailboxes resolved?
python -m src.inbox test        # do the matchers still work on saved emails?
python -m src.inbox watch --once -v     # what did it actually see?
```

If `test` passes and `watch` finds nothing, **there is genuinely nothing new.**

### "It reported the same email twice"

`state/inbox_seen.json` was deleted, or the mail server renumbered the mailbox
(UIDVALIDITY changed — logged as a warning). Harmless; it settles after one pass.

### "One mailbox keeps failing"

Expected and handled — **one bad mailbox never stops the others.** Right now
`na***@gmail.com` fails with `AUTHENTICATIONFAILED`, because it is a Gmail
address being logged into `mail.travnook.com`. That is a **config issue in the
registrant file**, not a bug. Fix the account or accept that mailbox is unwatched.

The digest always names failed mailboxes — silence about an unread mailbox would
read as "no mail", which is the exact failure this package exists to prevent.

### "Did it leak a client's name to Telegram?"

Everything user-facing goes through `waitlist/redaction.scrub()` first, and
**message bodies are never rendered** — only subjects and extracted fields. If
redaction itself fails, the value is withheld rather than printed. There is a
test for that specific case.

Only **invitations** and **failed mailboxes** push to Telegram, and to the
*summary* channel, not the success channel. Everything else is log-only —
otherwise the channel becomes noise and you stop reading it.

---

## 8. How much to trust this

**170 tests, 85% line coverage of `src/inbox/`, all offline.**

Worth knowing how that number got there: my first report said "58 tests passing",
which was true but misleading — coverage was **33%**, with `seen.py` at 8% and
two modules at 0%. You asked for a proper check; the audit found it. **Ask for
the coverage number, not the test count.**

Two real bugs were found by tests during this work:

1. `normalise_name` split apostrophes, so `O'BRIEN` never matched `OBrien`.
2. Four of my own new tests asserted the wrong behaviour about read-state. I
   checked the code first — the code was right, the tests were wrong.

What is **not** covered: the `watch()` sleep loop, and defensive `except` branches
that shouldn't happen in practice.

**What tests cannot tell you:** whether the matchers work on emails from
countries you have not yet seen. That is what the observational run is for, and
why "3 countries" is the real definition of done rather than "tests pass".

---

## 9. Honest status

**Phase 1 — code done, 3 countries configured.**

- ✅ Built, tested (**91% coverage**), running against real mailboxes
- ✅ `imap_host` set and verified connecting
- ✅ **3 countries configured** — Italy (48h), Greece (36h), Netherlands (36h)
- ✅ Incremental scanning: 312 messages → 30 per pass
- ✅ Corrected four wrong assumptions from real data (sender, cancellation type,
  36-vs-48 window, catch-all shadowing)
- ⏳ No invitation or confirmation has been seen **live** yet — the samples came
  from you, not from a message this system read itself

**Phase 2 — code done, unexercised.** `reconcile` works and is tested, but it has
had nothing to reconcile: no confirmation emails have arrived, and there are no
dangling journal rows to settle. It will earn its keep the first time a
registration submits and loses the page.

**The honest position:** the code is solid and well-tested. What it has *not* had
is contact with the variety of real mail across countries. That is time, not
work — leave `watch` running and the corpus builds itself.

---

## 10. Open gaps — deliberately not solved

1. **Waitlist cancellations are recognised but not acted on.** A cancelled entry
   makes a `success` journal row false. Nothing updates it. (§5)
2. **One mailbox unwatchable** — the Gmail account. Config, not code.
3. **Whose mailbox gets the invitation?** Confirmed: the VFS *account* address,
   for Italy. If some country mails the *applicant* instead, the watcher cannot
   see it and the dashboard poll planned for later is the only safety net.
4. **No invitation seen live yet.** The wording is from your samples, not from a
   message this system has read itself.

---

## 11. Before Phase 3

Phase 3 rewrites the journal into SQLite and adds the client lifecycle. Two
things worth knowing:

- It is **independent** of the recon questions in `TASKS.md` Phase 0 — it can
  proceed while you leave `watch` running.
- It **modifies existing waitlist code** (`journal.py`, `result.py`,
  `guards.py`), unlike Phases 1–2 which only added new files. Higher blast
  radius. Worth reading the diff carefully.

The recon session (Phase 0) is still the highest-value thing you personally can
do, and only you can do it: **does selecting a slot without paying hold it or
burn it?** That answer shapes Phases 4–7.
