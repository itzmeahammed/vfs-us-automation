# Telegram OTP Relay — Implementation Plan

**Status:** NOT STARTED. Written up so it can be picked up cleanly later.

**Blocks:** `AE-ITA` waitlist registration (its OTP step is `disabled` and the
route is `"enabled": false` until this exists).

---

## The problem

Some VFS flows send a one-time password to the client's **registered mobile
number**, not to the mailbox the bot logs in with. The existing
`src/vfs_bot/otp_flow.py` reads the SIGN-IN code out of IMAP — it cannot help
here, because the code never reaches an inbox the bot can see.

A human has to read that SMS. So the bot needs a way to ask one.

```
bot reaches the OTP page
   │
   ├─ clicks "Generate OTP"        VFS texts the client's mobile
   │
   ├─ asks via Telegram            "OTP for Italy waitlist (ahmed)?"
   │
   ├─ human replies "483920"       in the chat
   │
   ├─ bot reads it, types it, clicks Verify
   │
   └─ Continue → review-pay
```

---

## Design principle

**A standalone service that knows nothing about waitlists.** Its whole contract:

> ask a human for a code; return it, or time out.

That is what makes it reusable for Italy's waitlist, for other routes, and for
slot-checking later. It must not import anything from `src/waitlist/`.

```
src/otp_relay/
  __init__.py     request_code(prompt, timeout_s, expect_digits) -> str | None
  telegram.py     send the ask; poll getUpdates for the reply
  session.py      correlate reply -> request; ignore stale messages
  errors.py       OtpTimeout, OtpRelayUnavailable, OtpCancelled
```

Deliberately NOT under `src/waitlist/` — a shared service should not live inside
one of its consumers.

---

## Steps

### 1. Prove the transport, in isolation  ⚠️ do this first

A throwaway script: send a message, poll `getUpdates`, print the reply.

**Why first:** the project's existing `src/utils/telegram.py` is SEND-ONLY.
Receiving is a different API surface and the likeliest place to hit a surprise.

- [ ] Confirm the bot token can read updates (`getUpdates` returns 200)
- [ ] Confirm the chat id is right, and that a reply is visible to the bot
- [ ] **Check for a polling conflict** — Telegram allows ONE `getUpdates`
      consumer per token. If anything else polls, or a webhook is set, this
      needs its own bot token. `getWebhookInfo` reveals a webhook.
- [ ] Note the `update_id` semantics: offsets must be acknowledged or the same
      update is returned forever

### 2. Build `src/otp_relay/`, no browser involved

Testable entirely offline with a faked transport.

- [ ] `request_code(prompt, timeout_s, expect_digits)` public API
- [ ] **Correlation** — only accept replies that arrive AFTER the ask was sent
      (record the `update_id` offset at ask time). A code pasted before the
      question was asked belongs to a previous run.
- [ ] **Validation** — must be exactly `expect_digits` digits. On garbage, tell
      the human what was wrong and keep waiting rather than failing the run.
- [ ] **Timeout** returns `None`; the caller decides what that means
- [ ] **Cancel word** ("cancel"/"skip") so a human can abandon cleanly instead
      of waiting out the timeout
- [ ] Strip whitespace and common formatting (`483 920`, `483-920`)
- [ ] Unit tests: happy path, stale reply ignored, invalid then valid, timeout,
      cancel

### 3. Add a declarative `otp` step type

Config-driven, so no country-specific code. The shape is already recorded in
`config/waitlist/AE-ITA.json` under the `otp` step's `_planned_*` keys.

```jsonc
{
  "name": "otp",
  "type": "otp",
  "after": "details_summary",
  "generate": { "role": "button", "name": "Generate OTP" },
  "input": "input[placeholder='OTP']",
  "verify": { "role": "button", "name": "Verify" },
  "submit": { "role": "button", "name": "Continue" },
  "validity_seconds": 180,
  "max_regenerations": 3
}
```

- [ ] Step handler: click generate → `request_code()` → type → Verify → Continue
- [ ] Detect a rejected code and regenerate (**counting** — see limits below)
- [ ] Screenshot around the step like every other

### 4. Wire Italy up

- [ ] Remove `"disabled": true` from the `otp` step in `AE-ITA.json`
- [ ] Fill the real selectors in from the `_planned_*` keys
- [ ] `doctor --route AE-ITA --walk`
- [ ] Dry run, then `--live`
- [ ] Set `"enabled": true` only after a verified live registration

---

## Constraints that must not be missed

These are the things that will bite if they are skipped.

**The 3-minute expiry is a hard deadline.** VFS states the OTP is valid for 3
minutes. The relay timeout must be comfortably UNDER that (~170s), or the bot
will type a code that has already died and burn a regeneration for nothing.

**Regeneration is capped at 3, then VFS LOGS THE SESSION OFF.** The page says
so explicitly. Count regenerations, and treat exhaustion as a clean abandon —
never a retry loop. Losing the session mid-flow is worse than giving up.

**This step is PRE-COMMIT.** It sits before `review_pay`, so a timeout is a
`WaitlistStepError`: abandon quietly, nothing was submitted. It must NOT raise
`WaitlistCommittedError`.

**The human is slow.** 170s of a browser sitting idle on a live VFS session is
long enough for a session timeout or a fresh Cloudflare challenge. Worth
checking whether the page needs keeping alive, and worth logging the wait
clearly so the operator knows the bot is not hung.

**Do not log the code.** It goes through `redaction`-adjacent paths; the OTP is
short-lived but should still never land in `app.log` or an archive.

---

## Configuration (all default OFF)

```ini
[otp_relay]
; Human-in-the-loop OTP over Telegram. OFF by default: a misconfigured chat
; would leave a run hanging on a question nobody sees.
enabled = false

; Its OWN bot token if the main one already polls or has a webhook set —
; Telegram allows only one getUpdates consumer per token.
bot_token =
chat_id =

; Must stay UNDER the portal's OTP validity (VFS Italy: 180s).
timeout_seconds = 170
poll_seconds = 3

; What a human may reply to abandon the run cleanly.
cancel_words = cancel, skip, stop
```

---

## Open question to resolve at step 1

**Does anything already poll `getUpdates` on the main bot token, or is a
webhook set?** `src/utils/telegram.py` only ever sends, so probably not — but a
webhook configured elsewhere would silently break polling. `getWebhookInfo`
answers it in one call. If there is a conflict, use a separate bot token; that
is also cleaner operationally, since the OTP bot is interactive and the existing
one is broadcast-only.

---

## Why not other transports

Recorded so it is not re-litigated:

- **Read the SMS directly** (Twilio, an Android relay) — needs the client's SIM
  or number porting. Not available.
- **Email-to-SMS gateway** — carrier dependent, unreliable in the UAE.
- **A prompt in the terminal** — works, but only if someone is watching that
  exact terminal. Telegram reaches you anywhere, and is already the project's
  notification channel.
