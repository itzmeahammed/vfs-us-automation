# QA Handover — 2026-10-20

Scope: the three main workflows, judged on what has been **proven live** versus what is only written.
Evidence: code, `state/`, `logs/`, `runs/`, and a full test run on this machine.
Previous session notes (the unresolved payment) are in [HANDOVER.md](HANDOVER.md).

## Verdict in one table

| Workflow | Status | Proven live? |
|---|---|---|
| Slot check → Telegram | Working | Yes, every 30 min, 09:00–23:30 |
| 1. Waitlist registration to reference number | Works per route, not fault-tolerant | Yes: CHE, DEU, NLD, ITA (Oct 14) |
| 2. Invite email → book → pay → confirm | Not working end to end | No. Never ran on a real invite |
| 3. Live slot → auto-book → pay | Partly working | Reached 3-D Secure (Oct 15). No payment has ever succeeded |

Tests: **2210 passed, 8 failed, 2 skipped**. All 8 failures are config drift (see §5), not logic.

## 1. Waitlist registration

**What works.** Login, Turnstile, form fill, submit, success-text check. Real references exist for CHE (3), DEU, NLD, ITA. Double-registration is blocked by a run lock, journal guards, and API idempotency.

**What happens when attempt 1 fails.** There is **no retry, no backoff, no account or IP rotation** in the runner. Each failure type lands differently:

- Login / Turnstile / 429 block: exception, **no journal row**, client stays unmarked and is retried on every trigger forever.
- Form or step failure before the final click: `failed` row, client stays retryable.
- Final click timed out or browser died after the click: `unknown` or `pending` row. Client is **blocked until a human resolves it**. This is deliberate and correct.

**Reliability gaps to know about.**
- The reference-number regex expects 4 letters. DEU, NLD, ITA, CZE return 3-letter refs, so success is saved with `vfs_reference: null`. Success is still reported.
- A failed login still clears the account's strike counter (`runner.py`, bot assigned before login).
- The journal on this machine has **one dry-run row**. The Oct 14 registrations are recorded only in `config/PROGRSS.md`. The dedup guard therefore cannot see them.
- Switches: `waitlist = false`, auto-trigger off. Nothing registers unattended today; today's log shows "waitlist opened but flow is OFF" 21 times.

## 2. Invite email → booking → payment → confirmation

**What exists.** IMAP watcher (read-only), matchers for CHE/CZE/GRC/ITA/NLD, invite-to-client matching, a detached booking child, a daily cap, 3-hour stuck detection.

**Why it does not work today, in order.**
1. `invite_booking = false` on this machine. The watcher has **never run** here, and it reports zero mailboxes to watch (no enabled client files).
2. The watcher runs once per supervisor tick, so detection latency is up to 30 min plus the 9.5 h overnight gap.
3. The post-invite walk (`dashboard_resume`, `verify_identity`) uses placeholder selectors from `_default.json`. No invited account has ever been walked.
4. Payment has never succeeded (see §3).
5. **Confirmed defect:** after any payment submit, the walk marks the step not-ok, and the classifier requires the walk to be ok before it writes `booked`. So even a successful payment would land in `needs_attention`, never `booked`.
6. No confirmation page or VFS reference is read after payment. The `appointment_confirmed` email is classified but nothing consumes it. "Confirm success" is not implemented.

## 3. Live booking automation

**What works.** After every slot check, an in-window earliest date on an armed request spawns a booking child automatically. On Oct 15 a manual AE-NOR run passed 8 of 8 pages, filled the card, and raised 3-D Secure in about 90 s of walking.

**What is missing.**
- **No warm-up.** There is no persistent logged-in session. Detection is a cold login every 30 min, and the booking child does a second cold login. Nothing polls faster.
- Blind window 01:00–09:00 (an accepted decision).
- **Payment has never completed.** Three real attempts: Oct 8 unknown, Oct 14 declined, Oct 15 OTP fill failed. The Oct 15 payment (AED 83.30, appointment 16 Oct 10:15) is **still unanswered** and the API warns on it every 30 s.
- OTP auto-fill still does not compare the SMS reference code with the screen, and the WIO card may send codes in-app only.
- Only the route's earliest date triggers; a later in-window date never does.
- The spawn gate ignores account health, so a disabled account still triggers a child (happened Oct 15).
- Only one request exists and it is `needs_attention`, so nothing is armed right now.

**Safety that does hold.** Run locks, one Chrome per account, daily cap, max attempts, `needs_attention` is never auto-retried, commit refused without matching `confirm`, idempotency keys.

## 4. Risk flagged for the owner

`live_booking = true` on this machine while **AE-FRA, AE-HUN, AE-NLD, AE-SWE are enabled but never walked** (the test suite says so). A matching slot on those routes would start a real booking on unverified selectors with the company card. Either walk them or disable them before leaving the bot unattended.

## 5. Failing tests (all config, not logic)

- `test_only_a_fully_walked_route_is_armed`: FRA enabled without a walk (§4).
- `test_every_mandatory_gateway_field_has_a_client_source`: client `u10432-che` lacks city, country code, postcode.
- Five ITA tests and one route-fields test: the ITA waitlist config was changed from upload-based to typed fields on Oct 14, and the tests were not updated.

## 6. Changed on 2026-10-20 (after this review)

- **Waitlist login retried on a different IP, same account.** Up to `login_ip_tries` (default 2). Account faults (wrong password, locked) are never retried. Only login is retried; nothing after it is ever re-submitted.
- **Every failed login now writes a `failed` journal row** with the reason, so it shows in the client timeline and the API. Failed logins no longer clear the account's strike counter.
- **Per-client daily failure cap** (`max_failures_per_client_per_day`, default 3). Guard, runner and auto-trigger all honour it, so a client is no longer retried on every trigger forever.
- **Reference pattern accepts 3-letter prefixes** (DEU, NEU, ITD), so success rows carry the reference.
- **Paid bookings are classified `booked`.** The classifier now trusts the gateway's success row and no longer requires the walk's payment step to be "ok".
- **Appointment-confirmation emails are consumed.** `src/booking/confirmations.py` marks the matching invitation or booking request `booked` with the VFS reference and answers the unanswered payment. Exact name match only; anything else is filed `unmatched` or `ambiguous` for a human.
- **The inbox watcher runs inside the API** every 120 s (`[inbox] background_watch`), watches every pool account with a mailbox password (`watch_all_accounts`), and reports through `GET /v1/inbox/status`, `GET /v1/inbox/stream` (SSE), `GET /v1/inbox/confirmations`, `POST /v1/inbox/pass`. The supervisor's own pass runs only when that heartbeat is stale. See [API_SWITCHES_AND_INBOX.md](API_SWITCHES_AND_INBOX.md).
- **Not changed:** `invite_booking` is still off. Turn it on with `PATCH /v1/switches {"invite_booking": true}` when you are ready for real bookings from invitations.

## 7. Stale documentation

README and ARCHITECTURE still say "never books anything"; `run_task.ps1` says :29/:59 (it is :00/:30); ARCHITECTURE says the wrapper holds the mutex (it no longer does). The country status table in `config/PROGRSS.md` is accurate and is the best starting point.
