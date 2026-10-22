"""VFS appointment-confirmation emails -> the end of the journey, recorded.

When a booked appointment is confirmed VFS mails the ACCOUNT: 'Your <Country>
Visa Appointment is Confirmed!' or a plainer 'Appointment Confirmation', each
naming the applicant and carrying the reference (matcher `appointment_confirmed`
in config/inbox/_default.json). That email is the one independent source for
the fact the browser is worst at capturing — the walk ends on the payment
gateway and never reads a confirmation page.

This module CONSUMES those emails. For each one it settles, conservatively:

  * open INVITATIONS (Flow 2) on the same route and account whose matched
    client's name is the applicant's   -> booked, with the VFS reference
  * BOOKING REQUESTS (Flow 3) on the same route whose applicant is the one
    named (and whose account matches when the request names one)
                                        -> booked, with the VFS reference
  * the PAYMENT JOURNAL: an unanswered `payment_submitting` row for that client
    gets a `payment_confirmed` row carrying the same run_id, so the payment
    stops being reported as unanswered

Matching is exact on the normalised name (inbox.reconcile.names_match) — no
guessing. A confirmation that matches nothing, or more than one thing, is
filed as such so a human can see it; it never settles anything by itself.

Every email is filed once in state/confirmations/<key>.json with what it
settled. Writing happens from the inbox watcher's pass, so ANY pass — the
API's background watcher, the supervisor's, or a manual `watch` — feeds it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from src.booking import autobook, invites
from src.booking import requests as req_store
from src.inbox.reconcile import client_name, names_match

log = logging.getLogger(__name__)

CONFIRM_DIR = os.path.join(req_store.REPO_ROOT, "state", "confirmations")

#: The inbox matcher whose hits this module consumes. It is classified
#: 'other' in the inbox config (it is not a waitlist confirmation), so the
#: matcher NAME is the identity, not the classification.
MATCHER_NAME = "appointment_confirmed"

MATCHED = "matched"          # settled at least one record
UNMATCHED = "unmatched"      # nothing on file for this person — a human looks
AMBIGUOUS = "ambiguous"      # more than one candidate; refused to guess
STATUSES = (MATCHED, UNMATCHED, AMBIGUOUS)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _key(account: str, route: str, uid: str, mailbox: str = "") -> str:
    raw = f"{(account or '').lower()}|{route}|{uid}|{mailbox}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def path_for(key: str) -> str:
    return os.path.join(CONFIRM_DIR, f"{key}.json")


def get(key: str) -> Dict[str, Any]:
    with open(path_for(key), encoding="utf-8") as fh:
        return json.load(fh)


def save(item: Dict[str, Any]) -> None:
    os.makedirs(CONFIRM_DIR, exist_ok=True)
    path = path_for(item["key"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(item, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def list_all(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Newest first."""
    if not os.path.isdir(CONFIRM_DIR):
        return []
    out = []
    for name in os.listdir(CONFIRM_DIR):
        if not name.endswith(".json") or name.startswith("."):
            continue
        try:
            item = get(name[:-5])
        except Exception:                                   # noqa: BLE001
            continue
        if status is None or item.get("status") == status:
            out.append(item)
    out.sort(key=lambda i: i.get("received_epoch") or 0, reverse=True)
    return out


def is_appointment_confirmation(observation: Any) -> bool:
    match = getattr(observation, "match", None)
    return bool(match) and getattr(match, "matcher_name", "") == MATCHER_NAME


# --------------------------------------------------------------------------- #
# Recording (called by the inbox watcher)                                      #
# --------------------------------------------------------------------------- #

def record(observations: List[Any]) -> List[str]:
    """File every new confirmation email and settle what it proves. NEVER
    raises. Returns the keys created."""
    created: List[str] = []
    for obs in observations or []:
        try:
            if not is_appointment_confirmation(obs):
                continue
            email = obs.email
            fields = getattr(obs.match, "fields", None) or {}
            account = str(getattr(obs, "account", "") or email.mailbox or "").strip().lower()
            key = _key(account, obs.match.route, str(email.uid), email.mailbox)
            if os.path.exists(path_for(key)):
                continue
            item = {
                "key": key,
                "route": obs.match.route,
                "account": account,
                "applicant_name": (fields.get("applicant_name") or "").strip(),
                "reference": (fields.get("reference") or "").strip().upper(),
                "received_epoch": email.received_epoch or time.time(),
                "uid": str(email.uid),
                "status": UNMATCHED,
                "settled": [],
                "notes": [],
                "created_at": _now(),
            }
            settle(item)
            save(item)
            created.append(key)
            log.warning("Appointment confirmation recorded: %s on %s (%s) -> %s",
                        key, item["route"], invites._mask(account), item["status"])
            _notify(item)
        except Exception as exc:                            # noqa: BLE001
            log.exception("Could not record an appointment confirmation: %s", exc)
    return created


# --------------------------------------------------------------------------- #
# Settling — exact matches only                                                #
# --------------------------------------------------------------------------- #

def settle(item: Dict[str, Any]) -> Dict[str, Any]:
    """Fill item['settled'], item['notes'] and item['status']. Mutates and
    returns the item. Each part is independent and best-effort."""
    settled: List[Dict[str, Any]] = []
    notes: List[str] = []
    ids: Set[str] = set()

    if not item.get("applicant_name"):
        notes.append("the email names no applicant; nothing can be matched "
                     "without guessing")
    else:
        for fn in (_settle_invitations, _settle_requests):
            try:
                fn(item, settled, notes, ids)
            except Exception as exc:                        # noqa: BLE001
                log.exception("Confirmation settling step failed: %s", exc)
                notes.append(f"{fn.__name__} failed: {exc}")
        if ids:
            try:
                _settle_payments(item, settled, notes, ids)
            except Exception as exc:                        # noqa: BLE001
                log.exception("Could not settle payments: %s", exc)
                notes.append(f"payments: {exc}")

    item["settled"] = settled
    item["notes"] = notes
    if settled:
        item["status"] = MATCHED
    elif any(n.startswith("ambiguous") for n in notes):
        item["status"] = AMBIGUOUS
    else:
        item["status"] = UNMATCHED
    return item


def _load_person(client_id: str):
    try:
        from src.waitlist import registrant as registrant_mod
        return registrant_mod.load(client_id)
    except Exception:                                       # noqa: BLE001
        return None


def _detail(item: Dict[str, Any], who: str) -> str:
    ref = item.get("reference") or "no reference"
    return f"VFS confirmation email: appointment {ref} for {who}"


def _settle_invitations(item, settled, notes, ids) -> None:
    applicant = item["applicant_name"]
    for inv in invites.list_all():
        if inv.get("route") != item["route"]:
            continue
        if str(inv.get("account") or "").lower() != item["account"]:
            continue
        if inv.get("status") in (invites.EXPIRED, invites.DISMISSED):
            continue
        clients = dict(inv.get("clients") or {})
        named = [cid for cid in clients
                 if (p := _load_person(cid)) is not None
                 and names_match(applicant, client_name(p))]
        if len(named) > 1:
            notes.append(f"ambiguous: invitation {inv['key']} has "
                         f"{len(named)} clients named like the applicant")
            continue
        if not named:
            continue
        cid = named[0]
        previous = inv.get("status")
        clients[cid] = {**(clients.get(cid) or {}), "status": invites.BOOKED,
                        "vfs_reference": item.get("reference") or ""}
        inv["clients"] = clients
        if previous == invites.BOOKED:
            invites.event(inv, "confirmed_by_email", _detail(item, cid),
                          vfs_reference=item.get("reference") or "")
        else:
            invites.event(inv, "confirmed_by_email", _detail(item, cid),
                          status=invites.BOOKED, client_id=cid,
                          vfs_reference=item.get("reference") or "")
        settled.append({"kind": "invitation", "key": inv["key"],
                        "client_id": cid, "previous_status": previous})
        ids.add(cid)


def _settle_requests(item, settled, notes, ids) -> None:
    applicant = item["applicant_name"]
    candidates = []
    for req in req_store.list_all(route=item["route"]):
        if req.status == req_store.EXPIRED:
            continue
        name = f"{req.data.get('first_name') or ''} {req.data.get('last_name') or ''}"
        if not names_match(applicant, name):
            continue
        acct = str(req.data.get("account") or "").strip().lower()
        if acct and item["account"] and acct != item["account"]:
            continue
        # A request that is still WAITING and has never run is matched only
        # when the mailbox IS its account: a cold name match alone must not
        # close a request the bot has not touched.
        if (req.status == req_store.WAITING
                and int(req.data.get("attempts") or 0) == 0
                and acct != item["account"]):
            continue
        candidates.append(req)

    if len(candidates) > 1:
        open_ones = [r for r in candidates if r.status != req_store.BOOKED]
        candidates = open_ones or candidates
    if len(candidates) > 1:
        notes.append(f"ambiguous: {len(candidates)} booking requests match "
                     f"the applicant on {item['route']}")
        return
    if not candidates:
        return

    req = candidates[0]
    previous = req.status
    if previous == req_store.BOOKED:
        req_store.record_event(req.request_id, "confirmed_by_email",
                               _detail(item, req.request_id),
                               vfs_reference=item.get("reference") or "")
    else:
        req_store.record_event(req.request_id, "booked",
                               _detail(item, req.request_id),
                               status=req_store.BOOKED,
                               vfs_reference=item.get("reference") or "",
                               confirmed_by="vfs_email")
    settled.append({"kind": "booking_request", "request_id": req.request_id,
                    "previous_status": previous})
    ids.add(req.request_id)


def _settle_payments(item, settled, notes, ids) -> None:
    """Answer any unanswered submit for the people this email confirms.

    The row MUST carry the original submit's run_id — that is how
    payment_journal.unanswered() pairs an answer with its question.
    """
    from src.payment import journal as payment_journal

    for row in payment_journal.unanswered():
        if row.get("route") != item["route"]:
            continue
        if str(row.get("registrant_id") or "") not in ids:
            continue
        payment_journal.append({
            "event": "payment_confirmed",
            "route": item["route"],
            "registrant_id": row.get("registrant_id"),
            "run_id": row.get("run_id"),
            "booking_ref": row.get("booking_ref"),
            "vfs_reference": item.get("reference") or "",
            "source": "vfs_confirmation_email",
            "confirmation_key": item["key"],
        })
        settled.append({"kind": "payment", "run_id": row.get("run_id"),
                        "registrant_id": row.get("registrant_id")})


# --------------------------------------------------------------------------- #
# Telling someone                                                              #
# --------------------------------------------------------------------------- #

def _notify(item: Dict[str, Any]) -> None:
    ref = item.get("reference") or "no reference in the email"
    who = item.get("applicant_name") or "unnamed applicant"
    head = {MATCHED: "✅ APPOINTMENT CONFIRMED by VFS email",
            UNMATCHED: "ℹ️ VFS appointment confirmation — matched NOTHING on file",
            AMBIGUOUS: "⚠️ VFS appointment confirmation — AMBIGUOUS, not applied"
            }.get(item.get("status"), "VFS appointment confirmation")
    lines = [head, f"{item['route']} · {who} · ref {ref}",
             f"mailbox {invites._mask(item.get('account', ''))}"]
    for s in item.get("settled") or []:
        if s["kind"] == "invitation":
            lines.append(f"• invitation {s['key']} -> booked ({s['client_id']})")
        elif s["kind"] == "booking_request":
            lines.append(f"• request {s['request_id']} -> booked "
                         f"(was {s['previous_status']})")
        elif s["kind"] == "payment":
            lines.append(f"• payment run {s['run_id']} -> confirmed")
    for n in item.get("notes") or []:
        lines.append(f"• {n}")
    try:
        autobook._telegram("\n".join(lines))
    except Exception as exc:                                # noqa: BLE001
        log.debug("Could not send the confirmation Telegram: %s", exc)
