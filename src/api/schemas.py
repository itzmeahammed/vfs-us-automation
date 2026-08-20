"""Request and response models for the webhook API.

Every endpoint is typed on both sides. Beyond documentation, this is a security
control: `extra="forbid"` on the request model means an unexpected field is a
422, not a silently ignored one, so a caller cannot smuggle in a key hoping a
future version starts honouring it.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Route ids look like "AE-DEU" / "AE-CHE" / "AE-MT". The destination is 2-4
# letters, NOT always 3 — config/routes/AE-MT.json is real. Kept identical to
# src/waitlist/registrant._ROUTE_RE so the API can never accept a route the
# waitlist loader would then reject.
_ROUTE_RE = re.compile(r"^[A-Z]{2}-[A-Z]{2,4}$")

# Registrant ids: the filename stem under config/registrants/, so a strict slug
# — no dots, no slashes, nothing that could read as a path or a flag. Matches
# registrant._ID_RE.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Combo LABELS are human strings from config/routes/<ROUTE>.json and genuinely
# contain spaces and dashes ("Dubai - SCHENGEN"), so a slug charset would reject
# every real value. We allow letters, digits, space, dash, slash, dot, ampersand
# and parentheses — enough for real labels, while still excluding the shell
# metacharacters and control bytes. Note this is defence in depth only: the
# value reaches the child as a single argv entry via create_subprocess_exec,
# with no shell to interpret it.
_COMBO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-/.&()]{0,79}$")


class TriggerRequest(BaseModel):
    """Body of POST /trigger/waitlist. Every field is optional."""

    model_config = ConfigDict(extra="forbid")

    route: Optional[str] = Field(
        default=None,
        description="Route id, e.g. AE-DEU. Passed to the job as --route.",
        examples=["AE-DEU"],
    )
    registrant: Optional[str] = Field(
        default=None,
        description="Single client id. Passed to the job as --registrant.",
        examples=["client_001"],
    )
    combo: Optional[str] = Field(
        default=None,
        description="Restrict to one centre/category combination.",
    )
    dry_run: bool = Field(
        default=True,
        description=(
            "True (default) adds --dry-run: the job simulates without "
            "submitting. Send false explicitly to run for real."
        ),
    )
    reason: Optional[str] = Field(
        default=None,
        max_length=280,
        description="Free-text note recorded with the job. Not passed to argv.",
    )

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
        # The waitlist CLI takes --dry-run / --live as a mutually exclusive pair.
        args.append("--dry-run" if self.dry_run else "--live")
        if not self.dry_run:
            args.append("--yes")  # non-interactive: skip the confirmation prompt
        return args


class JobResponse(BaseModel):
    """Status of a single job."""

    job_id: str
    status: str = Field(
        description="running | succeeded | failed | timed_out | cancelled | "
                    "slots_available. Note 'slots_available' is a BETTER "
                    "outcome than success: a bookable slot exists, so the run "
                    "stopped and nothing was waitlisted — go book it."
    )
    command: List[str]
    pid: Optional[int] = None
    started_at: str
    finished_at: Optional[str] = None
    exit_code: Optional[int] = None
    log_file: Optional[str] = None
    detail: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    outcome: Optional[str] = Field(
        default=None,
        description="Run-level verdict parsed from the job: 'completed' or "
                    "'slots_available'. Null if the job produced no block.",
    )
    results: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Per-client outcomes: route, combo, registrant_id, status "
                    "(skipped|dry_run|pending|success|unknown|failed), reason, "
                    "vfs_reference. Empty until the job finishes.",
    )
    needs_attention: bool = Field(
        default=False,
        description="True when a submit is pending/unknown. A HUMAN must verify "
                    "on the VFS account — never retry automatically, that risks "
                    "a duplicate registration.",
    )


class TriggerResponse(BaseModel):
    """202 Accepted body — the job was spawned, not completed."""

    accepted: bool = True
    message: str
    job: JobResponse


class JobListResponse(BaseModel):
    """Recent job history."""

    count: int
    active_job_id: Optional[str] = None
    jobs: List[JobResponse]


class HealthResponse(BaseModel):
    """Unauthenticated liveness probe. Deliberately says almost nothing."""

    status: str = "ok"
    version: str
    server_time: str


class ErrorResponse(BaseModel):
    """Uniform error envelope for every non-2xx response."""

    error: str = Field(description="Short machine-readable error class.")
    detail: str = Field(description="Human-readable explanation.")
    status_code: int


# --------------------------------------------------------------------------- #
# Client management (Phase 1)                                                  #
# --------------------------------------------------------------------------- #


class ProblemModel(BaseModel):
    """One validation finding, addressable to a form field."""

    field: str = Field(description="Payload key the problem belongs to, or ''.")
    message: str
    severity: str = "error"
    hint: str = ""


class ClientCreateRequest(BaseModel):
    """A client as the web app sends it.

    Unlike TriggerRequest this permits EXTRA keys: form fields are free-form by
    design (each route's page asks for different things), so the API cannot
    enumerate them. They are validated by shape and against the route's actual
    {{placeholder}} requirements rather than by an allow-list.

    Secrets in, never out: `account_password` is accepted here and persisted,
    but no response model ever returns it.
    """

    model_config = ConfigDict(extra="allow")

    client_id: str = Field(
        description="Becomes the filename under config/registrants/. Lowercase "
                    "slug; suggest '<appuserid>-<route>' e.g. 'u10432-che'.",
        examples=["u10432-che"],
    )
    route: str = Field(description="Route id, e.g. AE-CHE.", examples=["AE-CHE"])
    combos: List[str] = Field(
        description="Combination label(s) exactly as in config/routes/<ROUTE>.json.",
        examples=[["Dubai - SCHENGEN"]],
    )
    enabled: bool = Field(
        default=False,
        description="Created PARKED by default. Send true to arm immediately, "
                    "or call POST /clients/{id}/enable later.",
    )
    account: Optional[str] = Field(
        default=None, description="VFS account email this client registers under."
    )
    account_password: Optional[str] = Field(
        default=None,
        description="Password for `account`. All-or-nothing with it. Stored "
                    "0600 and never returned by any endpoint.",
    )

    @field_validator("client_id")
    @classmethod
    def _validate_client_id(cls, v: str) -> str:
        """The id becomes a filename, so it must be a strict slug."""
        v = v.strip().lower()
        if not _ID_RE.match(v):
            raise ValueError(
                "client_id must be 1-64 characters of lowercase letters, digits, "
                "underscore or hyphen, starting with a letter or digit."
            )
        return v

    @field_validator("route")
    @classmethod
    def _validate_route_id(cls, v: str) -> str:
        v = v.strip().upper()
        if not _ROUTE_RE.match(v):
            raise ValueError("route must look like 'AE-CHE' — 2 letters, dash, 2-4 letters.")
        return v

    @field_validator("combos")
    @classmethod
    def _validate_combos(cls, v: List[str]) -> List[str]:
        """Combos are human labels; normalise whitespace, reject empties."""
        if not v:
            raise ValueError("combos must list at least one combination label.")
        out = []
        for combo in v:
            label = " ".join(str(combo).split())
            if not label:
                raise ValueError("combos entries must be non-empty labels.")
            if not _COMBO_RE.match(label):
                raise ValueError(
                    f"combo {combo!r} must be a label like 'Dubai - SCHENGEN'."
                )
            out.append(label)
        return out

    def to_client_data(self) -> Dict[str, Any]:
        """Render as the dict persisted to config/registrants/<id>.json.

        `client_id` is dropped — it is the filename, not file content — and any
        None-valued optional key is omitted so the file stays clean (an absent
        "account" means "use the shared one", which is not the same as null).
        """
        data = self.model_dump(exclude_none=True)
        data.pop("client_id", None)
        return data


class ClientSummary(BaseModel):
    """One row in a client listing."""

    client_id: str
    route: str
    combos: List[str] = Field(default_factory=list)
    enabled: bool = False


class ClientListResponse(BaseModel):
    count: int
    clients: List[ClientSummary] = Field(default_factory=list)


class ClientWriteResponse(BaseModel):
    """Result of a create/update/enable/disable."""

    client_id: str
    created: bool
    enabled: bool
    message: str
    client: Dict[str, Any] = Field(
        default_factory=dict,
        description="Redacted view — secrets removed, PII masked.",
    )


class ClientDetailResponse(BaseModel):
    """One client, plus whether it would actually run right now."""

    client_id: str
    client: Dict[str, Any]
    runnable: bool = Field(description="True when the pre-flight found no problems.")
    problems: List[ProblemModel] = Field(default_factory=list)


class RouteReadinessResponse(BaseModel):
    """Whether a route accepts registrations, and its valid combos."""

    route: str
    ready: bool
    combos: List[str] = Field(
        default_factory=list,
        description="Valid combination labels — use these to populate a form.",
    )
    problems: List[ProblemModel] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Operations / status (Phase 5)                                                #
# --------------------------------------------------------------------------- #


class SwitchState(BaseModel):
    """Every gate between "a waitlist opened" and "a client is registered".

    Reported together because they compose — all must be right for an automatic
    live registration, and the usual confusion is exactly one of them being off.
    """

    register_enabled: bool = Field(description="Master switch. False = nothing registers.")
    dry_run: bool = Field(description="Global dry-run: walk the flow, never submit.")
    auto_trigger_enabled: bool = Field(description="Slot checker may fire waitlist runs.")
    auto_trigger_dry_run: bool = Field(description="Auto-triggered runs stop before submitting.")
    max_per_run: int
    max_per_day: int


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

    @field_validator("status")
    @classmethod
    def _only_definite_states(cls, v: str) -> str:
        """Only definite outcomes: the point is to replace ambiguity with fact."""
        v = v.strip().lower()
        if v not in ("success", "failed"):
            raise ValueError("status must be 'success' or 'failed' — resolving "
                             "replaces an ambiguous state with a checked fact.")
        return v
