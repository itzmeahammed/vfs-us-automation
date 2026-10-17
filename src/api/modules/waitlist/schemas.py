"""Waitlist (Flow 1) request and response models."""

from __future__ import annotations
import re
from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

from src.api.core.schemas import _ROUTE_RE, _ID_RE, _COMBO_RE
from src.api.modules.system.schemas import SwitchState


class WaitlistRunMode(str, Enum):
    """How a waitlist run behaves. There is no money-spending default.

    Mirrors BookingMode in the booking module, which replaced a bare
    `dry_run: bool` for exactly the reason below.
    """

    REHEARSE = "rehearse"   # walk everything, submit nothing
    LIVE = "live"           # submit real registrations to VFS


class TriggerRequest(BaseModel):
    """Body of POST /v1/waitlist/runs.

    SAFETY SHAPE — read before changing any of this.

    A live run takes real, irreversible VFS waitlist places under real clients'
    names, and a duplicate can make VFS void both entries. The old shape was a
    single `dry_run: bool = True`, which pydantic coerces loosely: "no", "off",
    "0", "false", "f", "n" and 0 all became False, and `to_cli_args` then
    emitted `--live --yes`, skipping even the CLI's own confirmation prompt. So
    any caller that stringifies booleans — a shell script, a form-encoded
    bridge, a hand-written Postman body — could ask for a rehearsal and get a
    live registration across every armed client.

    Travnooker's proxy happened to block that by validating `dry_run` as a
    strict boolean, which meant the whole safety property lived in one layer
    this API cannot see. A second consumer already exists. So it is enforced
    here now:

      * `mode` is an explicit enum; there is no value that "falls through" to
        live, and the default is REHEARSE.
      * `dry_run` is still accepted as a DEPRECATED alias so the existing proxy
        keeps working, but it is typed StrictBool — a string is now a 422, not
        a live run.
      * a live run must name a `registrant`. A live run naming nobody covers
        every armed client, which is not something anyone types on purpose.
      * `confirm` is a second, differently-shaped assertion: when `mode` is
        sent explicitly as live it must equal `route`, so one flipped field
        cannot by itself start a real registration.
    """

    model_config = ConfigDict(extra="forbid")

    route: Optional[str] = Field(
        default=None,
        description="Route id, e.g. AE-DEU. Passed to the job as --route.",
        examples=["AE-DEU"],
    )
    registrant: Optional[str] = Field(
        default=None,
        description=("Single client id. Passed to the job as --registrant. "
                     "REQUIRED for a live run."),
        examples=["client_001"],
    )
    combo: Optional[str] = Field(
        default=None,
        description="Restrict to one centre/category combination.",
    )
    mode: Optional[WaitlistRunMode] = Field(
        default=None,
        description=("'rehearse' (default) simulates and submits nothing; "
                     "'live' submits real registrations. When sent as 'live', "
                     "`confirm` must equal `route`."),
    )
    dry_run: Optional[StrictBool] = Field(
        default=None,
        description=("DEPRECATED — use `mode`. Strictly boolean: a string such "
                     "as \"false\" is rejected rather than read as a live run. "
                     "true maps to mode=rehearse, false to mode=live."),
    )
    confirm: Optional[str] = Field(
        default=None,
        max_length=16,
        description=("Must equal `route` when `mode` is sent as 'live'. A "
                     "second assertion of intent, shaped differently from the "
                     "first so one wrong field cannot register anyone."),
    )
    reason: Optional[str] = Field(
        default=None,
        max_length=280,
        description="Free-text note recorded with the job. Not passed to argv.",
    )

    @model_validator(mode="after")
    def _resolve_mode(self) -> "TriggerRequest":
        """Settle mode vs the deprecated dry_run, then gate a live run.

        Order matters: the mode is resolved first so the gates below cannot be
        reasoned about separately from it.
        """
        if self.mode is not None and self.dry_run is not None:
            implied = (WaitlistRunMode.REHEARSE if self.dry_run
                       else WaitlistRunMode.LIVE)
            if implied is not self.mode:
                raise ValueError(
                    f"mode={self.mode.value} and dry_run={self.dry_run} "
                    "contradict each other. Send one, not both — prefer mode.")
        elif self.mode is None:
            # No mode: fall back to the alias, and to REHEARSE when neither was
            # sent. The default must never be the one that spends a VFS place.
            object.__setattr__(
                self, "mode",
                WaitlistRunMode.REHEARSE if (self.dry_run is None or self.dry_run)
                else WaitlistRunMode.LIVE)

        if self.mode is WaitlistRunMode.LIVE:
            if not self.registrant:
                raise ValueError(
                    "a live run must name a registrant. Without one it would "
                    "register every armed client, which is never intended.")
            # Only demanded on the explicit `mode` path. Requiring it of the
            # deprecated `dry_run` path would break the console, which does not
            # send it — and that path already proves intent by other means
            # (an operator-typed confirmation and a server-side kill switch).
            if self.dry_run is None:
                if not self.confirm:
                    raise ValueError(
                        "a live run must send confirm=<route> as a second "
                        "assertion of intent.")
                if not self.route:
                    raise ValueError(
                        "a live run must name the route it confirms.")
                if self.confirm.strip().upper() != self.route:
                    raise ValueError(
                        f"confirm must equal route ({self.route}).")
        return self

    @property
    def is_live(self) -> bool:
        """The single place anything asks "does this submit for real?"."""
        return self.mode is WaitlistRunMode.LIVE

    @field_validator("route")
    @classmethod
    def _validate_route(cls, v: Optional[str]) -> Optional[str]:
        """Route must look like AE-DEU or AE-MT (destination is 2-4 letters)."""
        if v is None:
            return None
        v = v.strip().upper()
        if not _ROUTE_RE.match(v):
            raise ValueError(
                "route must look like 'AE-DEU' — 2 letters, dash, 2-4 letters."
            )
        return v

    @field_validator("registrant")
    @classmethod
    def _validate_registrant(cls, v: Optional[str]) -> Optional[str]:
        """Registrant id is a filename stem, so it must be a strict slug."""
        if v is None:
            return None
        v = v.strip().lower()
        if not _ID_RE.match(v):
            raise ValueError(
                "registrant must be 1-64 characters of lowercase letters, "
                "digits, underscore or hyphen, starting with a letter or digit."
            )
        return v

    @field_validator("combo")
    @classmethod
    def _validate_combo(cls, v: Optional[str]) -> Optional[str]:
        """Combo is a human label like 'Dubai - SCHENGEN', not a slug.

        Internal whitespace is collapsed to match how the waitlist journal and
        registrant loader normalise labels, so 'Dubai  -  SCHENGEN' and
        'Dubai - SCHENGEN' are not treated as two different combos.
        """
        if v is None:
            return None
        v = " ".join(v.split())
        if not _COMBO_RE.match(v):
            raise ValueError(
                "combo must be a label like 'Dubai - SCHENGEN' (letters, digits, "
                "spaces, and - / . & ( ) only; max 80 characters)."
            )
        return v

    def to_cli_args(self) -> List[str]:
        """Render this request as argv entries appended to the job command.

        Only validated fields reach this list, and it is passed to
        `create_subprocess_exec` as a list — never joined into a shell string.
        """
        args: List[str] = []
        if self.route:
            args += ["--route", self.route]
        if self.registrant:
            args += ["--registrant", self.registrant]
        if self.combo:
            args += ["--combo", self.combo]
        # The waitlist CLI takes --dry-run / --live as a mutually exclusive
        # pair. Read from `is_live` — the resolved mode — so there is exactly
        # one place that decides this, and no field can imply it accidentally.
        args.append("--live" if self.is_live else "--dry-run")
        if self.is_live:
            args.append("--yes")  # non-interactive: skip the confirmation prompt
        return args
class DanglingEntry(BaseModel):
    """A journal row needing a human decision.

    'pending' = a submit went out and we never saw the outcome.
    'unknown' = it was submitted but the confirmation could not be read.
    Both BLOCK the client from re-registering, which is correct (a retry could
    duplicate a real appointment) but means an unresolved row parks that client.
    """

    route: str
    combo: str
    registrant_id: str
    status: str
    reason: str = ""
    started_at: str = ""
class StatusResponse(BaseModel):
    """One call answering "is this thing working, and is anything stuck?"."""

    posture: str = Field(
        description="Plain-language summary of what would happen right now if a "
                    "waitlist opened."
    )
    switches: SwitchState
    routes: List[Dict[str, Any]] = Field(default_factory=list)
    clients_total: int = 0
    dangling: List[DanglingEntry] = Field(default_factory=list)
    needs_attention: bool = False
    webhook_configured: bool = False
    undelivered_webhooks: int = 0
    degraded: List[str] = Field(
        default_factory=list,
        description="Things this response could NOT determine. A non-empty list "
                    "means some fields above are placeholders rather than "
                    "measurements — most importantly, an unreadable journal "
                    "makes an empty `dangling` list mean 'unknown', not 'none'.",
    )
class ResolveRequest(BaseModel):
    """Record what a human found on the VFS portal for a dangling entry."""

    model_config = ConfigDict(extra="forbid")

    route: str
    combo: str
    registrant_id: str
    status: str = Field(
        description="'success' if the registration IS on the portal, 'failed' if "
                    "it is not. Check the portal before answering — marking a "
                    "real registration 'failed' lets the bot duplicate it."
    )
    reason: Optional[str] = Field(default=None, max_length=280)
    reference: Optional[str] = Field(
        default=None,
        max_length=80,
        description=(
            "The VFS reference the operator read off the portal, for a "
            "'success'. Stored as data on the journal row rather than only in "
            "`reason`, because the duplicate gates, the per-client rollup and "
            "the reconcile backfill all read the structured field — folding it "
            "into prose left every human-resolved registration with an empty "
            "reference. Ignored for 'failed': a row that did not register "
            "cannot carry a reference."
        ),
    )

    @field_validator("status")
    @classmethod
    def _only_definite_states(cls, v: str) -> str:
        """Only definite outcomes: the point is to replace ambiguity with fact."""
        v = v.strip().lower()
        if v not in ("success", "failed"):
            raise ValueError("status must be 'success' or 'failed' — resolving "
                             "replaces an ambiguous state with a checked fact.")
        return v
class ReconcileProposal(BaseModel):
    """One journal row a confirmation email could settle."""

    registrant_id: str
    route: str
    combo: str
    reference: Optional[str] = None
    new_status: str = ""
    action: str = ""
    blocked_reason: str = ""
    will_apply: bool = False
class ReconcileRequest(BaseModel):
    """Body of POST /inbox/reconcile."""

    model_config = ConfigDict(extra="forbid")

    dry_run: bool = Field(
        default=True,
        description="True (default) shows what WOULD be settled without "
                    "writing anything. Send false to apply.",
    )
class ReconcileResponse(BaseModel):
    """Result of an inbox reconcile pass."""

    mailboxes_checked: int = 0
    mailboxes_failed: List[str] = Field(default_factory=list)
    messages_seen: int = 0
    proposals: List[ReconcileProposal] = Field(default_factory=list)
    applied: int = 0
    dry_run: bool = True
