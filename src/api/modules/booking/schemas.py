"""Booking run, booking status and payment models."""

from __future__ import annotations
import re
from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from src.api.core.schemas import _ROUTE_RE, _ID_RE, _COMBO_RE, _APPLICANT_KEY_RE


class BookingRouteStatus(BaseModel):
    """One route's booking pipeline state."""

    route: str
    enabled: bool = False
    commit_step: str = ""
    steps: List[Dict[str, Any]] = Field(default_factory=list)
class BookingClientStatus(BaseModel):
    """One client's booking phase."""

    client_id: str
    route: str
    status: str = ""
    vfs_reference: Optional[str] = None
    phase: str = ""
    needs_attention: bool = False
class BookingStatusResponse(BaseModel):
    """Booking pipeline overview."""

    routes: List[BookingRouteStatus] = Field(default_factory=list)
    clients: List[BookingClientStatus] = Field(default_factory=list)
class BookingMode(str, Enum):
    """How far a booking run is allowed to go. There is no boolean here.

    `dry_run: bool` was the previous shape and it was the wrong one twice over:
    a caller who sent nothing got a dry run (fine), but a caller who sent
    `dry_run: false` ALSO got a dry run, because to_cli_args never read the
    field. The request said "spend money" and the run did not — the single
    worst failure direction for a payment path.

    An explicit three-value enum fixes the class of bug, not just the instance:
    there is no default that spends money, every value names exactly what it
    does, and COMMIT additionally requires `confirm` below. A typo can no
    longer mean "charge the card".
    """

    PROBE = "probe"          # log in, read the dashboard, click nothing
    WALK = "walk"            # walk the booking pages, stop at the commit step
    COMMIT = "commit"        # book AND pay. Irreversible.
class BookingTriggerRequest(BaseModel):
    """Body of POST /booking/trigger.

    Renders argv for `python -m src.booking probe`. Every flag name here is
    asserted against that parser by tests/test_api_booking_trigger.py — the
    previous version emitted --source/--dest against a parser that defines
    -sc/--source-country, so every booking trigger died in argparse before the
    browser opened. A contract test is the only thing that keeps two files in
    two languages agreeing.
    """

    model_config = ConfigDict(extra="forbid")

    route: str = Field(
        description="Route id, e.g. AE-NOR. Split into --source-country and "
                    "--dest-country.",
        examples=["AE-NOR"],
    )
    mode: BookingMode = Field(
        default=BookingMode.PROBE,
        description="probe: read-only. walk: walk the pages, stop before the "
                    "committing step. commit: BOOK AND PAY, irreversible, and "
                    "requires `confirm` to equal the route.",
    )
    confirm: Optional[str] = Field(
        default=None,
        description="Required when mode=commit: must equal `route` exactly. "
                    "A second, differently-shaped assertion so that no single "
                    "wrong field can trigger a real payment.",
    )
    registrant: Optional[str] = Field(
        default=None, description="Client id, for the expected-row match.",
    )
    request_id: Optional[str] = Field(
        default=None,
        description="A stored booking request (POST /booking-requests) to run. "
                    "Supplies the applicant, account, combo and date window, so "
                    "nothing else is needed. Exclusive with registrant/applicant.",
    )
    combo: Optional[str] = Field(
        default=None,
        description="Centre/category/sub-category label, e.g. 'Norway Visa "
                    "Application Center - Dubai - Tourist'. Required for "
                    "entry=new.",
    )
    entry: Optional[Literal["waitlist", "new"]] = Field(
        default=None,
        description="waitlist: resume an invited application. new: create one "
                    "from a live slot. Defaults to what the route supports.",
    )
    applicant: Dict[str, str] = Field(
        default_factory=dict,
        description="Applicant fields for the walk, e.g. {\"first_name\": "
                    "\"Zaid\"}. The live-slot flow has no client roster, so "
                    "without these a walk with entry=new dies on 'Your "
                    "Details'.",
    )
    capture: Literal["off", "failure", "full"] = Field(
        default="failure",
        description="What to leave on disk. 'full' adds the rendered DOM of "
                    "every page — never use it on a commit run: the payment "
                    "page's DOM contains the card number.",
    )
    to_step: Optional[str] = Field(
        default=None,
        description="Stop after this step, e.g. select_slot.",
    )
    reason: Optional[str] = Field(
        default=None, max_length=280,
        description="Free-text note recorded with the job. Not passed to argv.",
    )

    @field_validator("route")
    @classmethod
    def _validate_route(cls, v: str) -> str:
        v = (v or "").strip().upper()
        if not _ROUTE_RE.match(v):
            raise ValueError(
                "route must look like 'AE-NOR' — 2 letters, dash, 2-4 letters."
            )
        return v

    @field_validator("registrant")
    @classmethod
    def _validate_registrant(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().lower()
        if not _ID_RE.match(v):
            raise ValueError("registrant must be a strict slug.")
        return v

    @field_validator("combo")
    @classmethod
    def _validate_combo(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = " ".join(v.split())
        if not _COMBO_RE.match(v):
            raise ValueError(
                "combo must be a label like 'Dubai - SCHENGEN' (letters, "
                "digits, spaces, and - / . & ( ) only; max 80 characters)."
            )
        return v

    @field_validator("to_step")
    @classmethod
    def _validate_to_step(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not _ID_RE.match(v.lower()):
            raise ValueError("to_step must be a step name like 'select_slot'.")
        return v

    @field_validator("applicant")
    @classmethod
    def _validate_applicant(cls, v: Dict[str, str]) -> Dict[str, str]:
        """Each pair becomes `--applicant key=value`, so both halves are checked.

        A key containing '=' would silently re-split into a different field on
        the CLI side; a non-slug key cannot name a real form field. Rejecting
        here gives a 422 naming the bad key instead of a confusing failure five
        pages into a live browser run.
        """
        if not v:
            return {}
        if len(v) > 40:
            raise ValueError("at most 40 applicant fields.")
        clean: Dict[str, str] = {}
        for key, value in v.items():
            key = str(key).strip().lower()
            if not _APPLICANT_KEY_RE.match(key):
                raise ValueError(
                    f"applicant key {key!r} must be 1-40 characters of "
                    "lowercase letters, digits or underscore."
                )
            value = str(value).strip()
            if len(value) > 200 or "\n" in value or "\r" in value:
                raise ValueError(
                    f"applicant value for {key!r} must be at most 200 "
                    "characters on a single line."
                )
            clean[key] = value
        return clean

    @model_validator(mode="after")
    def _guard_commit(self) -> "BookingTriggerRequest":
        """Everything that must be true before a run is allowed to spend money.

        These are refusals, not warnings, and they happen here — before a job
        is spawned, before a browser opens, before an account session is spent.
        """
        if self.mode is BookingMode.COMMIT:
            if self.confirm != self.route:
                raise ValueError(
                    "mode=commit requires confirm to equal route exactly "
                    f"(expected {self.route!r}). This run books a real "
                    "appointment and submits a real payment; there is no undo."
                )
            if self.capture == "full":
                # Memory: never dump DOM on the payment page — page.content()
                # of a filled card form writes the PAN to disk.
                raise ValueError(
                    "capture='full' is refused with mode=commit: it dumps the "
                    "rendered DOM of every page, and the payment page's DOM "
                    "contains the card number. Use capture='failure'."
                )
            if self.to_step:
                raise ValueError(
                    "to_step cannot be combined with mode=commit: stopping "
                    "early would leave the booking half-made."
                )
        elif self.confirm is not None:
            raise ValueError(
                "confirm is only meaningful with mode=commit; remove it."
            )

        if self.request_id is not None:
            rid = self.request_id.strip().lower()
            if not _ID_RE.match(rid):
                raise ValueError("request_id must be a strict slug.")
            if self.registrant or self.applicant:
                raise ValueError(
                    "request_id already supplies the applicant: send it without "
                    "registrant or applicant.")
            self.request_id = rid

        if self.entry == "new" and not self.combo and not self.request_id:
            raise ValueError(
                "entry='new' requires combo: the live-slot flow has no "
                "invitation to read the centre and category from."
            )
        return self

    def to_cli_args(self) -> List[str]:
        """Render as argv for `python -m src.booking probe`.

        FLAG NAMES HERE MUST MATCH src/booking/__main__.py. They are asserted
        against that parser in tests rather than trusted.
        """
        source, _, dest = self.route.partition("-")
        # -sc / -dc, NOT --source / --dest. The long forms are
        # --source-country / --dest-country; the short ones are used here
        # because they are what the parser marks required.
        args: List[str] = ["-sc", source, "-dc", dest]

        if self.registrant:
            args += ["--registrant", self.registrant]
        if self.request_id:
            args += ["--request", self.request_id]
        if self.combo:
            args += ["--combo", self.combo]
        if self.entry:
            args += ["--entry", self.entry]
        args += ["--capture", self.capture]
        for key, value in self.applicant.items():
            args += ["--applicant", f"{key}={value}"]
        if self.to_step:
            args += ["--to", self.to_step]

        # --commit REQUIRES --walk, so COMMIT implies both. --yes is always
        # sent with --commit: there is no operator at a terminal to answer the
        # confirmation prompt, and the `confirm` field above is what replaced it.
        if self.mode in (BookingMode.WALK, BookingMode.COMMIT):
            args.append("--walk")
        if self.mode is BookingMode.COMMIT:
            args += ["--commit", "--yes"]
        return args
class UnansweredPayment(BaseModel):
    """A payment that was submitted with no outcome ever recorded after it.

    Every field here comes from the write-ahead row in the payment journal
    (state/payments.jsonl).
    There is deliberately no card data to model: the journal's `_FORBIDDEN`
    filter drops anything card-shaped before the row is written, so there is
    nothing sensitive to leak through this response.
    """

    run_id: Optional[str] = Field(
        default=None,
        description="The run that submitted it. Use GET /jobs/{run_id} and "
                    "GET /jobs/{run_id}/logs to see what that run did.",
    )
    booking_ref: Optional[str] = Field(
        default=None, description="VFS booking reference, when one was known.",
    )
    at: Optional[str] = Field(
        default=None, description="When the submit was about to happen (UTC).",
    )
    url: Optional[str] = Field(
        default=None, description="The gateway URL at submit time.",
    )
    route: Optional[str] = None
    registrant_id: Optional[str] = None
class UnansweredPaymentsResponse(BaseModel):
    """The single most important thing to check after any unexpected crash.

    A non-empty `payments` list means one or more real cards MAY have been
    charged with no recorded result. None of them may be retried until a human
    confirms at the gateway — see RUNBOOK.md.
    """

    count: int = Field(description="How many payments are unresolved.")
    needs_attention: bool = Field(
        description="True when count > 0. A monitor can alert on this one "
                    "boolean without parsing the list.",
    )
    payments: List[UnansweredPayment] = Field(default_factory=list)
    journal_file: str = Field(
        description="Path to the journal on the API host, for an operator with "
                    "shell access.",
    )
    journal_readable: bool = Field(
        default=True,
        description="False when the journal could not be read at all. Treat "
                    "this as NEEDING ATTENTION, not as 'nothing unanswered': "
                    "an unreadable payment journal is indistinguishable from "
                    "one full of unanswered payments.",
    )
