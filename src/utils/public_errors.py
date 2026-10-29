"""One-line error messages for the web app.

The web app shows these to sales agents. They get ONE sentence that says what
VFS did — never a traceback, a selector, a Playwright call log or an internal
command. The full text stays where operators read it: logs/, the job log, and
the request's history on disk.

    public_message(raw)  ->  "VFS blocked access from this connection (403 Permission Issues)."

Rules are matched top to bottom, first hit wins, so the most specific and the
most consequential (anything about MONEY) come first. Payment outcomes are
never folded into the generic fallback: "a payment was sent and VFS has not
confirmed it" must reach the agent as exactly that, or someone re-runs it and
the card is charged twice.
"""

from __future__ import annotations

import re
from typing import Iterable, Tuple

#: When nothing below matches. Wording requested by the operator, 2026-10-23.
FALLBACK = "Proxy connection failed trying after 15 minutes again"

#: A registration/booking was SENT and VFS has not confirmed it. Never the
#: fallback: "trying again" here is how a client gets registered twice.
UNCONFIRMED = ("The request was sent to VFS and VFS has not confirmed it yet. "
               "Check the VFS account before running this client again.")

#: An unexpected crash nothing below recognises (src/utils/cli_guard.py).
#: Wording requested by the operator, 2026-10-23: never the traceback itself.
UNIDENTIFIED = "Problem not identified: the VFS run stopped unexpectedly."

_RULES: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    # Our own crash line (cli_guard) — matched first so a word inside it
    # ("stopped" holds no rule word today, but wording drifts) never re-routes it.
    (("problem not identified",), UNIDENTIFIED),
    # ── money first ──────────────────────────────────────────────────────────
    (("payment declined", "reported the payment failed", "paymentstatus=false"),
     "VFS's payment gateway declined the payment. Check the VFS account "
     "before trying again — VFS says funds can still be held."),
    (("payment submitted", "payment was submitted", "gateway's answer was"),
     "The payment was sent to VFS's gateway and VFS has not confirmed the "
     "result yet. Check the VFS account before trying again."),
    (("submit is unresolved", "pending/unknown", "outcome unconfirmed"),
     UNCONFIRMED),
    (("booking in progress", "already has a booking", "blockingdialog"),
     "VFS reports this account already has a booking in progress."),
    (("never reached the payment gateway", "could not dismiss vfs's payment"),
     "VFS did not open its payment page after the appointment was selected."),
    (("billing", "card form", "unified checkout", "card number did not land",
      "checkout accepts"),
     "VFS's payment page did not accept the payment details."),
    # ── VFS ended the session ────────────────────────────────────────────────
    (("page-not-found", "page not found"),
     "VFS ended the session and redirected to its 'page not found' page."),
    (("session expired", "401101", "session_expired"),
     "VFS ended the session (Session Expired or Invalid)."),
    (("403203", "permission issues", "geoblocked"),
     "VFS blocked access from this connection (403 Permission Issues)."),
    (("403201", "ipblocked", "ip blocked"),
     "VFS blocked the connection's IP address (403)."),
    (("account locked", "accountlocked", "429", "exceeds the defined thres"),
     "VFS temporarily locked this account for too many requests."),
    (("benched",),
     "VFS recently blocked this account; it is paused before the next try."),
    (("not registered with us", "emailnotregistered"),
     "VFS says this email is not registered on its portal."),
    (("is disabled", "invalidcredentials", "wrong credentials",
      "account disabled"),
     "VFS rejected this account's login (email or password)."),
    (("accessrestricted", "accountblocked", "access restricted"),
     "VFS has restricted this account."),
    # ── VFS login / Cloudflare ───────────────────────────────────────────────
    (("verify you are human", "turnstile", "signindisabled", "cloudflare not"),
     "VFS's security check (Cloudflare) did not let the login through."),
    (("login form never appeared", "loginformnotready", "login failed",
      "loginbounced", "dashboardnotreached"),
     "VFS's login page did not load."),
    # The MID-BOOKING OTP (src/booking/otp_step.py), which is a different
    # failure from the sign-in one below and has a different fix. Listed first
    # because the table is first-hit-wins and "otp" below would swallow it.
    # Reported as "VFS's login code (OTP) could not be completed" on
    # 2026-10-29 it read as a login problem, on a run that had logged in fine
    # and filled three pages.
    (("mailbox password", "read the code", "otp email", "generate otp",
      "emailed otp", "no otp input", "nothing to read with"),
     "VFS asked for a one-time password during booking and the bot could not "
     "complete it. Nothing was booked and nothing was charged."),
    (("otp", "one-time"),
     "VFS's one-time password (OTP) step could not be completed."),
    # ── VFS availability ─────────────────────────────────────────────────────
    (("no appointment is available", "no date inside the window",
      "nothing inside the window", "no dates in range"),
     "VFS has no appointment available in the requested dates."),
    (("no time slots", "no bookable time slot"),
     "VFS showed no bookable time for the chosen date."),
    # VFS's own "nothing to book" banner and modal — walk.NO_SLOTS_PHRASES.
    # Matched HERE and not left to the fallback: this is the single most
    # common reason a booking run stops, it is nobody's fault, and it needs no
    # action. Reported as a proxy failure (2026-10-28) it read as a bug in the
    # bot and sent the operator looking at the proxy pool.
    (("no appointment slots are currently available",
      "no appointment slots available", "nothing to book",
      "new slots open at regular intervals", "no slots are currently available",
      "currently no slots are available"),
     "VFS has no appointment slots for this centre and category right now. "
     "Nothing was booked and nothing was charged; the bot will try again."),
    # A submit VFS accepted and discarded, and the page gate that catches it.
    # Both mean the same thing to an agent: the form did not move on.
    (("did not move", "page never showed", "discarded the click",
      "minimum wait has not elapsed"),
     "VFS did not accept the booking form and stayed on the same page."),
    (("could not select '",),
     "VFS did not offer the requested option on its booking form."),
    # ── the connection to VFS ────────────────────────────────────────────────
    (("err_tunnel", "err_proxy", "proxy refused", "tunnel connection",
      "proxy authentication", "no working", "proxies failed"),
     FALLBACK),
    (("target page, context or browser has been closed", "targetclosed"),
     "VFS's page closed before the booking finished."),
    (("err_timed_out", "timeout", "timed out"),
     "VFS did not respond in time."),
    (("err_connection", "connection reset", "10054", "err_name_not_resolved",
      "err_internet"),
     "The connection to VFS was interrupted."),
)

_MAX = 200


def public_message(raw: object, fallback: str = FALLBACK) -> str:
    """The one sentence the web app shows for `raw`. Never empty."""
    text = " ".join(str(raw or "").split()).lower()
    if not text:
        return fallback
    for needles, message in _RULES:
        if any(n in text for n in needles):
            return message
    return fallback


def public_messages(raws: Iterable[object]) -> str:
    """The message for the most important of several raw errors."""
    found = [public_message(r, fallback="") for r in raws if r]
    for message in found:
        if message:
            return message
    return FALLBACK


def one_line(text: object, limit: int = _MAX) -> str:
    """Any text squeezed to one line — for fields that must stay free-form."""
    line = re.sub(r"\s+", " ", str(text or "")).strip()
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Per-channel helpers — what each API surface sends instead of raw text        #
# --------------------------------------------------------------------------- #

#: History events whose detail is an ERROR (translated). Every other event's
#: detail is a note ("parked", a field list, a human's resolve reason) and is
#: only squeezed to one line.
ERROR_EVENTS = frozenset({
    "attempt_failed", "needs_attention", "manual_walk_failed",
    "test_failed", "failed", "crashed",
})

#: Waitlist / result statuses that are NOT errors.
#:
#: "payment_submitted" is in here because it is not a failure: the click
#: happened and the money may have moved, which is a thing to CHECK, not a
#: thing that went wrong. Being outside this set would send its detail through
#: public_message(), which is built to translate VFS's error pages and would
#: replace a sentence about a successful payment with a generic apology.
#:
#: "payment_declined" is deliberately NOT in here. The gateway did refuse, and
#: that belongs in the error family — but see AUTHORED_DETAIL_STATUSES: its
#: wording is already written for an operator and must not be rewritten
#: either.
OK_STATUSES = frozenset({"success", "succeeded", "dry_run", "skipped",
                         "booked", "passed", "slots_available", "running",
                         "queued", "cancelled", "payment_submitted"})

#: Statuses whose `detail` the API author wrote FOR AN OPERATOR, so the
#: public-message translator must leave it alone.
#:
#: public_message() exists to turn a Playwright traceback or a VFS error page
#: into one plain sentence. Applied to a sentence that is already plain, it can
#: only make it worse — and on these two the sentence is about whether a card
#: was charged, which is the last text in the system that should be replaced
#: by a fallback.
AUTHORED_DETAIL_STATUSES = frozenset({"payment_submitted", "payment_declined"})


def public_history(history: Iterable[dict]) -> list:
    out = []
    for entry in history or []:
        entry = dict(entry or {})
        detail = entry.get("detail")
        if detail:
            event = str(entry.get("event") or "").lower()
            entry["detail"] = (public_message(detail) if event in ERROR_EVENTS
                               else one_line(detail))
        out.append(entry)
    return out


def public_reason(reason: object, status: object) -> str:
    """A result row's reason: translated if the row is a failure."""
    status = str(status or "").lower()
    if status in ("pending", "unknown"):
        return UNCONFIRMED
    if not reason:
        return ""
    if status in OK_STATUSES:
        return one_line(reason)
    return public_message(reason)


_LOG_LINE = re.compile(r"^(\d{2}:\d{2}:\d{2}) \| (?:[0-9a-f]{6,} \| )?"
                       r"(INFO|WARNING|ERROR|CRITICAL|DEBUG) +\| ?(.*)$")
#: INFO lines that are really internals (Playwright call logs, selector
#: timeouts) — not shown.
_INTERNAL = ("locator.", "call log", "traceback", "timeout ", "ms exceeded",
             "exception", "error:")


def public_log_lines(lines: Iterable[str]) -> list:
    """The job log as the web app should see it.

    Progress (INFO) lines pass. A WARNING/ERROR line becomes ONE sentence
    about what VFS did — or is dropped when it is not a recognised VFS
    problem, because most warnings are internal housekeeping and the run's
    real verdict arrives in the job's `detail`. Everything without a log
    timestamp (tracebacks, Playwright call logs, the end-of-run report) is
    dropped. Consecutive duplicates collapse.
    """
    out = []
    for line in lines or []:
        m = _LOG_LINE.match(str(line).rstrip())
        if not m:
            continue
        stamp, level, text = m.groups()
        if level == "DEBUG":
            continue
        if level == "INFO":
            low = text.lower()
            if any(k in low for k in _INTERNAL):
                continue
            shown = f"{stamp} | INFO | {one_line(text)}"
        else:
            message = public_message(text, fallback="")
            if not message:
                continue
            shown = f"{stamp} | ERROR | {message}"
        if not out or out[-1].split(" | ", 1)[1] != shown.split(" | ", 1)[1]:
            out.append(shown)
    return out
