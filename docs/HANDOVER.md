# Handover — 2026-10-15

Branch `payment/3ds-otp-capture`. Written at 13:52 local (09:52 UTC).

---

## 1. ⚠️ FIRST: AN UNRESOLVED PAYMENT. RESOLVE BEFORE ANYTHING ELSE.

A **real payment was submitted and has no recorded outcome.**

| | |
|---|---|
| When | `2026-10-15T09:36:29Z` |
| Amount | **AED 83.30** |
| Card | visa ****9340 — **issued by WIO**, merchant is RAKBANK |
| Appointment | AE-NOR, **16 Oct 2026 10:15**, Abu Dhabi / Short Stay / Tourist |
| Applicant | `trav-nook-ae-nor-8c18ed` |
| Run id | `6ad09e180783e1` |
| 3-D Secure ref | `CGJ` |
| Journal | last row is `payment_otp_fill_failed`; **no `payment_result`** |

**The card may or may not have been charged, and the appointment may or may not
exist.** The browser has since closed, so the challenge can no longer be
completed.

```powershell
python -m src.payment status        # will list this as UNANSWERED
```

**Do this, in order, and do NOT re-run the booking first:**

1. Check the **WIO** app/statement for an AED 83.30 authorisation.
2. Log into VFS (`mufaddal@travnook.com`) and look for a 16 Oct 10:15 Norway
   appointment.
3. Only once both are known, decide whether to re-book. **A retry before that
   check double-charges** — VFS's own failure page says to wait 30 minutes and
   check before paying again.

The booking request is currently stuck mid-flight:

```
trav-nook-ae-nor-8c18ed  AE-NOR  booking  on  2026-10-15..2026-10-31  attempts=2
```

Status `booking` with `attempts=2`. It will need resetting to `waiting` (or
parking) once the payment above is resolved.

---

## 2. What the live run proved

The full chain works. `python -m src.booking probe -sc AE -dc NOR --walk
--commit --yes --request trav-nook-ae-nor-8c18ed --keep-open --hold 1800
--capture failure` ran **8/8 steps**: login (Turnstile solved), appointment
details, your details, summary, slot pick (11 dates offered, chose earliest in
window), services, review+pay, and the gateway — billing filled, card entered,
payment submitted, 3-D Secure raised.

It failed at exactly one point: **typing the OTP**.

### Why it failed — three separate things

1. **The selectors were guesses.** The code box is
   `input#CredentialValidateInput` (`name="Credential.Value"`), submit is
   `button#ValidateButton`. It matched **none** of the eight hypotheses in
   `otp_field`: no `autocomplete`, no `maxlength` (length lives in
   `data-val-length-max`), and neither id nor name contains "otp"/"code".
   **FIXED** — both are now first in `config/payment/CYBERSOURCE.json`, read off
   the live DOM.

2. **A stale code was read and typed.** The relay held an SMS with
   `(Ref: XWE)`; this transaction was `Ref: CGJ`. It arrived 3s *after*
   submission, so the freshness check passed it legitimately. It was almost
   certainly a test message the operator sent to the relay.
   **NOT FIXED — see §4.**

3. **The real code never reached the relay.** Zero messages arrived, including
   after a resend. **WIO issues the card and sends the code "via SMS/In-App"** —
   if WIO chose in-app, no SMS exists for any relay to read.

### Known-good selectors (verified live, inside `#Cardinal-CCA-IFrame`)

| Purpose | Selector |
|---|---|
| Code field | `input#CredentialValidateInput` |
| Submit | `button#ValidateButton` |
| Resend | `#ResendLink` |
| Ref code | `#referenceCode` |
| Error text | `#ValidationErrorMessage` |

Proven by driving them over CDP: fill + click succeeded and the bank replied
"Please re-enter your Valid OTP" — i.e. the submit worked, only the code was
wrong.

### Two issuer behaviours are now on record

Neither may be assumed; the code stays blind to the difference on purpose.

- **2026-10-14, Mashreq** — out-of-band app approval. 11 inputs, *all* hidden,
  `oobForm` + `oobContinue`, **no code field**. `find_code_field` correctly
  returns None and the cardholder finishes it.
- **2026-10-15, Visa ACS / WIO** — typed code in `#CredentialValidateInput`.

---

## 3. What was built this session

### `config/card.local.txt` — the card file
Gitignored. `src/payment/card.py` now reads the environment **first**, then the
file, merged per field. Two hard refusals before it will read one: `git
check-ignore` says it is committable, or (POSIX) other users can read it. The
`.gitignore` entries are load-bearing — deleting them disables the card file.
Pre-commit hook blocks card files by name and Luhn-valid PANs by content.

```powershell
python -m src.payment card      # offline check; prints only "visa ****9340"
```

### `src/otp_relay/` — the SMS relay
Standalone, per the contract in `docs/archive/OTP_RELAY_TASKS.md`; imports
nothing from payments or waitlists.

- `extract.py` — PURE. **Refuses rather than guesses**: ambiguous digit runs
  with no code word return None.
- `relayion.py` — HTTP client (`urllib`). Note `"status": "ONLINE"` is a
  **string**, not a boolean — an earlier cut checked `isOnline` and reported a
  live handset as offline.
- `inbox.py` — **armed** waiting. `arm()` is called in `submit_payment`
  immediately before the click; 90s clock grace; consumed-id set.
- `src/payment/otp.py` — fills the bank's screen. Three guards against
  re-clicking the gateway's Pay button (frame-scope → same-frame submit →
  explicit veto).

```powershell
python -m src.otp_relay devices        # RUN FIRST. exits non-zero if offline
python -m src.otp_relay watch --minutes 60
python -m src.otp_relay wait --timeout 180
python -m src.otp_relay parse "<sms body>"      # offline, no key needed
```

### Config/data fixes
- `city`/`country_code`/`postcode` added to `example.json.example` and fixed in
  the booking request (was `Lebanon` / `United Arab Emirates` / `1001` →
  `Dubai` / `AE` / `000000`). No code change needed —
  `Registrant.as_context()` passes flattened JSON straight through.
- `_comment_missing_client_fields` in CYBERSOURCE.json rewritten as resolved.

**Tests: 132 passing** across `test_otp_extract`, `test_otp_inbox`,
`test_otp_relayion`, `test_payment_otp_fill`, `test_payment_card`.

---

## 4. Open work, highest value first

1. **Match the reference code.** The single most valuable fix. The SMS carries
   `Ref: XWE`, the screen shows `#referenceCode` = `CGJ`, and nothing compares
   them. Extract the ref from the challenge frame, require the SMS to contain
   it, and **refuse on mismatch**. Turns "a code arrived recently" into "this
   transaction's code" — which is what the check should always have been.
2. **Confirm where WIO actually sends the code.** If it is in-app only, auto-fill
   cannot work for this card and the relay is the wrong tool; a different card
   or a different issuer channel is needed. Settle this before trusting
   auto-fill again.
3. **`otp_senders`** is still `[]` (any sender). Once a *genuine* issuer SMS is
   observed, put its sender there. Do not guess it.
4. **`tests/test_payment_gateway.py::test_every_mandatory_gateway_field_has_a_client_source`
   still fails** — `u10432-che` lacks the three billing fields. Pre-existing,
   unrelated to this work; goes green when that record gets them.
5. `config/booking/AE-SWE.json` is **empty/corrupt** — `python -m src.booking
   check` reports it. Pre-existing.

---

## 5. Operational notes

- **Account health**: `mufaddal@`, `mukarram@`, `zainab@` have all been hit with
  VFS `429002` "unauthorised activity" — one per day, 12/14/15 Oct. `mufaddal@`
  was manually cleared and logged in fine today. State in
  `state/account_health.json`; clear with `python -m src.utils.account_health
  clear <email>`. **Clearing only wipes the local marker — it does not unblock
  VFS.**
- **Never `--capture full` with `--commit`** — the payment page's DOM holds the
  card number. Use `--capture failure`.
- `--keep-open --hold <seconds>` keeps the browser alive for manual completion.
  The session is deliberately not spent; closing it costs another login, and
  repeated logins trigger VFS `429001`.
- The relay device is **POCO C85**, SIM slot 0 = `+971582771608`.
- Driving the live browser by hand works over CDP — the run logs its port
  (`9222` if free, else e.g. `63322`); `pw.chromium.connect_over_cdp(...)`.

## 6. Secrets — where they live (none are in git)

| What | Where |
|---|---|
| Card | `config/card.local.txt` (gitignored) |
| Relayion key | `config/config.local.ini` `[relayion] relayion_key`, or `RELAYION_API_KEY` |
| VFS accounts | `config/credentials.local.ini` (`mufaddal@` is `[cred3]`) |
| Applicant PII | `config/booking_requests/` and `config/registrants/` (both gitignored) |
