# Runbook

What to do when something went wrong, or when you are about to spend money.

Read [COMMANDS.md](COMMANDS.md) for what to type day to day. This file is for
the moments that matter.

---

## 0. The one thing to check first, always

After **any** unexpected crash, kill, reboot, or run that ended without saying
what happened:

```bash
python -m src.payment status          # on the machine
curl -sH "X-Webhook-Secret-Token: $TOKEN" localhost:8000/payments/unanswered | jq
```

An **unanswered payment** is one that was submitted with no outcome ever
recorded after it. It may be a real charge on a real card.

**Do not retry it.** Retrying is what double-charges. Open the payment gateway,
find out what actually happened, then record it.

If the endpoint returns `"journal_readable": false`, treat that as an
emergency, not an all-clear: an unreadable journal and an empty one look
identical over JSON, which is why the API reports the unreadable case as
`needs_attention: true`.

---

## 1. The run_id: how to investigate anything

Every run — API-triggered or hand-typed — has one `run_id`, and **every record
of that run carries it**. This is the whole investigation procedure:

```bash
RUN=6abba7f4534aae          # printed on the CLI's first line; run_id in any API response

# every log line the run wrote
jq "select(.run_id==\"$RUN\")" logs/app.jsonl

# only its errors
jq "select(.run_id==\"$RUN\" and .level==\"ERROR\")" logs/app.jsonl

# did it register anyone?
jq "select(.run_id==\"$RUN\")" state/waitlist_journal.jsonl

# did it try to pay?
jq "select(.run_id==\"$RUN\")" state/payments.jsonl

# what did it see?  screenshots + any captured DOM
ls runs/*/$RUN/
```

Over the API, the same id answers:

| Call | Answers |
|---|---|
| `GET /jobs/{run_id}` | status, exit code, per-client results |
| `GET /jobs/{run_id}/stream` | follow it live (SSE) |
| `GET /jobs/{run_id}/logs` | the log after it finishes |

For an API-triggered run `job_id == run_id`, so there is no mapping to look up.

---

## 2. Where everything lives

```
config/     hand-edited, in git          you write these
state/      machine-written, gitignored  BACK THIS UP — journals live here
logs/       app.jsonl (queryable) + app-YYYY-MM-DD.log + api_jobs/
runs/       runs/<ROUTE>/<run_id>/       screenshots and DOM per run
docs/       archive/ (old docs) + research/ (scratch)
```

`state/` holds `waitlist_journal.jsonl` and `payments.jsonl` — the durable
record that a registration or a charge may have happened. Everything else in
the system can be rebuilt; **these cannot**. Back them up.

`logs/` and `runs/` are prunable. A run folder is the unit of deletion.

---

## 3. Booking through the API

Three modes, increasing risk. Nothing but `mode` decides how far a run goes.

```bash
TOKEN=...   # VFSAPI_SECRET_TOKEN
API=localhost:8000

# 1. READ-ONLY — log in, read the dashboard, click nothing
curl -sX POST $API/booking/trigger \
  -H "X-Webhook-Secret-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"route":"AE-NOR","mode":"probe"}' | jq

# 2. REVERSIBLE — walk the pages, stop in front of the committing step
curl -sX POST $API/booking/trigger \
  -H "X-Webhook-Secret-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"route":"AE-NOR","mode":"walk","registrant":"mufaddal-nor"}' | jq

# 3. NO UNDO — books the appointment AND submits a real payment
curl -sX POST $API/booking/trigger \
  -H "X-Webhook-Secret-Token: $TOKEN" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{
        "route":"AE-NOR",
        "mode":"commit",
        "confirm":"AE-NOR",
        "registrant":"mufaddal-nor",
        "combo":"Norway Visa Application Center - Dubai - Tourist",
        "reason":"invitation received 2026-09-29"
      }' | jq
```

Then follow it:

```bash
RUN=$(...)   # run_id from the response
curl -sN -H "X-Webhook-Secret-Token: $TOKEN" $API/jobs/$RUN/stream
```

### Rules for `mode: commit`

- **`confirm` must equal `route`.** Two differently-shaped assertions, so no
  single wrong field can start a payment.
- **Always send an `Idempotency-Key`.** A retried POST without one is how you
  book twice. A repeat with the same key returns the original job.
- **`capture: "full"` is refused.** It dumps every page's DOM, and the payment
  page's DOM contains the card number.
- **`to_step` is refused.** Stopping early leaves a half-made booking.
- `entry: "new"` requires `combo`, and needs `applicant` fields — the live-slot
  flow has no client roster to read a name from.

A malformed commit is a **422 before any job is spawned** — before a browser
opens and before an account session is spent.

---

## 4. Specific failures

### A booking failed after the appointment was made

The slot is **already booked**; only the payment failed. Do not re-run the
walk — it would book a second appointment.

1. `GET /payments/unanswered`.
2. Look at `runs/<ROUTE>/<run_id>/` for the failure screenshot.
3. Resolve at the gateway by hand.

### A run was interrupted mid-submit

The waitlist journal has a `pending` row, which **blocks that client from being
retried** until a human says what happened. That block is the feature.

```bash
python -m src.waitlist journal            # find the dangling row
python -m src.waitlist resolve --route AE-CHE --combo "..." \
    --registrant <id> --status success|failed --reason "checked the portal"
```

Check the portal **before** resolving. `success` blocks the client permanently;
`failed` frees them to retry.

### The API says a job is already running (409)

Runs are serialised on purpose: two at once can double-register a client. Wait,
or `POST /jobs/{id}/cancel`. A manual `python -m src.waitlist run` holds the
same machine lock, so the API will refuse while one is in flight.

### The API restarted mid-job

Jobs left `running` are reconciled to `unknown` and flagged, because the child
may have completed a real registration nobody recorded:

```bash
curl -sH "X-Webhook-Secret-Token: $TOKEN" "$API/jobs?needs_attention=true" | jq
```

### VFS returns 429001 / an account is benched

A fresh login per run is what trips it. `state/account_health.json` records the
cooldown. Use `--keep-open --hold N` to keep **one** login alive across several
inspections rather than logging in repeatedly.

---

## 5. Before going to production

- [ ] `pytest -q` green
- [ ] `state/` is backed up, and the backup has been restored once as a test
- [ ] `VFSAPI_SECRET_TOKEN` is 64+ random chars, not in git
- [ ] `VFSAPI_ENABLE_DOCS` unset in production (an open schema maps your API)
- [ ] The API binds loopback only; the tunnel is the public edge
- [ ] A retention sweep prunes `runs/` and `logs/`
- [ ] `GET /payments/unanswered` is **monitored** — alert on
      `needs_attention: true`
- [ ] One real `mode: commit` run has been done and verified end to end

The last item is the one that is still open: the pages after `/che/services`
have never been seen on an invited account, so the commit path beyond the walk
boundary is written but unexercised. See [TASKS.md](TASKS.md).
