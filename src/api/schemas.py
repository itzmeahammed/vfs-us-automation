"""Request and response models for the webhook API.

Every endpoint is typed on both sides. Beyond documentation, this is a security
control: `extra="forbid"` on the request model means an unexpected field is a
422, not a silently ignored one, so a caller cannot smuggle in a key hoping a
future version starts honouring it.
"""

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

# Applicant field KEYS become the left half of `--applicant key=value`, so they
# must not contain '=' (which would re-split into a different field downstream)
# and must be plausible form-field names.
_APPLICANT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


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
    run_id: str = Field(
        default="",
        description="The correlation key for this run. Equal to job_id for "
                    "API-triggered runs. Every log line the run wrote, every "
                    "journal row, and its screenshots folder all carry it — so "
                    "`jq 'select(.run_id==\"...\")' logs/app.jsonl` returns "
                    "the whole run.",
    )
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
    replayed: bool = Field(
        default=False,
        description="True when an Idempotency-Key matched an earlier request "
                    "and the ORIGINAL job is being returned — nothing new was "
                    "started. Treat the job exactly as you would a fresh one.",
    )


class JobLogResponse(BaseModel):
    """Tail of a job's log file.

    Exists because the absolute `log_file` path in JobResponse is meaningless to
    a remote web app (and mildly disclosive). This returns the content instead.
    """

    job_id: str
    lines: List[str] = Field(default_factory=list)
    line_count: int = 0
    truncated: bool = Field(
        default=False,
        description="True when older lines were omitted — this is a TAIL, not "
                    "the whole log.",
    )
    log_available: bool = Field(
        default=True,
        description="False when the log file has been pruned or never existed.",
    )


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


class ClientPatchRequest(BaseModel):
    """A PARTIAL client update: only the keys actually sent are changed.

    Every field is optional, including the ones ClientCreateRequest requires.
    Distinguishing "sent as null" from "not sent" is done with
    `model_fields_set`, not by the value — the endpoint reads that set rather
    than treating None as "remove", because a caller sending `"account": null`
    plausibly means either.

    Use PUT when you want omitted fields REMOVED; use this when you want them
    left alone.
    """

    model_config = ConfigDict(extra="allow")

    route: Optional[str] = Field(default=None, description="Route id, e.g. AE-CHE.")
    combos: Optional[List[str]] = Field(
        default=None, description="Combination label(s). Replaces the whole list."
    )
    enabled: Optional[bool] = Field(
        default=None,
        description="Arm or park the client. Omit to leave the current state.",
    )
    account: Optional[str] = None
    account_password: Optional[str] = None

    @field_validator("route")
    @classmethod
    def _validate_route_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().upper()
        if not _ROUTE_RE.match(v):
            raise ValueError("route must look like 'AE-CHE' — 2 letters, dash, 2-4 letters.")
        return v

    @field_validator("combos")
    @classmethod
    def _validate_combos(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return None
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

    def to_patch(self) -> Dict[str, Any]:
        """Only the keys the caller actually sent, ready to merge.

        Reads `model_fields_set` rather than filtering None, so an explicit
        null is preserved as an intentional value while an omitted field simply
        does not appear.
        """
        sent = self.model_fields_set or set()
        dumped = self.model_dump()
        return {key: value for key, value in dumped.items() if key in sent}


class ClientSummary(BaseModel):
    """One row in a client listing.

    The first four fields are always present and cost nothing but a file read.
    Everything below them is filled only when the caller asks via `?include=`,
    because each extra costs real work per client — see ClientListResponse.
    """

    client_id: str
    route: str
    combos: List[str] = Field(default_factory=list)
    enabled: bool = False

    created_at: str = Field(
        default="",
        description="When the client was first written (UTC, ISO 8601). "
                    "Clients that predate timestamping were backfilled with "
                    "the date of that migration, not their true creation date.",
    )
    updated_at: str = Field(
        default="",
        description="When the client was last written (UTC, ISO 8601).",
    )
    enabled_at: str = Field(
        default="",
        description="When the client was last ARMED (UTC, ISO 8601). Empty "
                    "while parked: it is cleared on disable, so it never reads "
                    "as live when it is not.",
    )

    # --- include=status ---------------------------------------------------
    runnable: Optional[bool] = Field(
        default=None,
        description="Would this client register right now? Null unless "
                    "`include=status` was requested.",
    )
    problem_count: Optional[int] = Field(
        default=None,
        description="How many pre-flight problems this client has. Null unless "
                    "`include=status`. Call GET /clients/{id} for the detail.",
    )

    # --- include=journal --------------------------------------------------
    last_status: Optional[str] = Field(
        default=None,
        description="Outcome of this client's most recent registration attempt "
                    "(success, failed, pending, dry_run). Null unless "
                    "`include=journal`, or if they have never run.",
    )
    last_run_at: Optional[str] = Field(
        default=None,
        description="When that attempt finished (or started, if it is still "
                    "running). Null unless `include=journal`.",
    )
    vfs_reference: Optional[str] = Field(
        default=None,
        description="The VFS booking reference from the most recent SUCCESSFUL "
                    "registration, e.g. SWDB79918334684. Null unless "
                    "`include=journal`, or if none has succeeded.",
    )
    run_count: Optional[int] = Field(
        default=None,
        description="How many registration attempts this client has made. "
                    "Null unless `include=journal`.",
    )


class ClientListResponse(BaseModel):
    """A client listing.

    `include` is opt-in rather than always-on because the extras are not free:
    `status` re-runs each client's full pre-flight (tens of milliseconds each,
    so a large fleet would turn a list call into a multi-second one), and
    `journal` reads the registration history. A dashboard asks for what it
    needs in ONE call; a simple picker keeps the cheap default.
    """

    count: int
    clients: List[ClientSummary] = Field(default_factory=list)
    included: List[str] = Field(
        default_factory=list,
        description="Which optional blocks were actually filled in, echoed "
                    "back so a caller can tell 'not requested' from 'requested "
                    "but empty'.",
    )


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
    warnings: List[ProblemModel] = Field(
        default_factory=list,
        description=(
            "Non-blocking findings. The write SUCCEEDED — these say what is "
            "probably not intended, not what went wrong. A client whose form "
            "email differs from their VFS account is the common case: the "
            "invitation lands in a mailbox the watcher does not read."
        ),
    )


class ClientDetailResponse(BaseModel):
    """One client, plus whether it would actually run right now."""

    client_id: str
    client: Dict[str, Any]
    runnable: bool = Field(description="True when the pre-flight found no problems.")
    problems: List[ProblemModel] = Field(default_factory=list)


class RouteFieldModel(BaseModel):
    """One field this route's VFS form asks for.

    Together these are the signup form a web app should render. They are
    derived from the route's own step definitions — the same config the bot
    fills from — so they cannot drift from what the portal really wants.
    """

    name: str = Field(description="Key to send in the POST /clients body.")
    label: str = Field(description="The portal's own label for this field.")
    kind: str = Field(
        description="Input type to render: text, select, checkbox, file, date."
    )
    required: bool = Field(
        description="False for fields the portal only shows on some renders."
    )
    step: str = Field(description="Which form step asks for it.")
    notes: str = Field(
        default="",
        description="Value constraints, e.g. digits-only or upper-cased.",
    )
    options: List[str] = Field(
        default_factory=list,
        description=(
            "For a dropdown, the exact option strings observed on the portal. "
            "Render the control from these and send back one of them verbatim: "
            "the bot matches the option by this text, so a near-miss "
            "('Lebanese' where the portal says 'Lebanon') cannot be selected. "
            "Empty unless options_status is 'known'."
        ),
    )
    options_status: str = Field(
        default="not_a_choice",
        description=(
            "How far this field's options can be trusted. 'known': options[] "
            "was captured from the live portal — render a dropdown, and note "
            "that POST /clients REJECTS any other value. 'unknown': this is a "
            "dropdown whose options have not been harvested yet — render free "
            "text and warn, as the value cannot be checked until they are. "
            "'not_a_choice': an ordinary text/date/file field; ignore options[]."
        ),
    )
    options_captured_at: str = Field(
        default="",
        description=(
            "When options[] was read off the portal (ISO 8601), or empty if "
            "never. Shows at a glance whether a list is observed or stale."
        ),
    )


class RouteReadinessResponse(BaseModel):
    """Whether a route accepts registrations, its combos, and its form fields."""

    route: str
    ready: bool
    combos: List[str] = Field(
        default_factory=list,
        description="Valid combination labels — use these to populate a form.",
    )
    fields: List[RouteFieldModel] = Field(
        default_factory=list,
        description=(
            "The client-data fields this route needs. Render a form from these "
            "rather than hardcoding a field list: routes genuinely differ (one "
            "needs address lines, another needs a passport scan), and a "
            "hardcoded form silently creates clients that can never register. "
            "Send them as top-level keys in the POST /clients body."
        ),
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

    @field_validator("status")
    @classmethod
    def _only_definite_states(cls, v: str) -> str:
        """Only definite outcomes: the point is to replace ambiguity with fact."""
        v = v.strip().lower()
        if v not in ("success", "failed"):
            raise ValueError("status must be 'success' or 'failed' — resolving "
                             "replaces an ambiguous state with a checked fact.")
        return v


# --------------------------------------------------------------------------- #
# Routes listing                                                               #
# --------------------------------------------------------------------------- #


class RouteSummary(BaseModel):
    """One route in the routes listing."""

    route: str
    ready: bool
    combos: List[str] = Field(default_factory=list)
    clients: int = 0
    problems: List[str] = Field(default_factory=list)


class RoutesListResponse(BaseModel):
    """All configured routes."""

    count: int
    routes: List[RouteSummary] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Pipeline overview                                                            #
# --------------------------------------------------------------------------- #


class PipelineResponse(BaseModel):
    """Full pipeline snapshot — clients, journal, routes, mailboxes, health."""

    generated_at: float
    clients: List[Dict[str, Any]] = Field(default_factory=list)
    waitlist_rows: List[Dict[str, Any]] = Field(default_factory=list)
    waitlist_routes: List[Dict[str, Any]] = Field(default_factory=list)
    booking_routes: List[Dict[str, Any]] = Field(default_factory=list)
    inbox_routes: List[Dict[str, Any]] = Field(default_factory=list)
    mailboxes: List[Dict[str, Any]] = Field(default_factory=list)
    health: List[Dict[str, Any]] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    totals: Dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Read-only config                                                             #
# --------------------------------------------------------------------------- #


class ConfigResponse(BaseModel):
    """Operational settings, secrets stripped."""

    schedule: Dict[str, Any] = Field(default_factory=dict)
    timeouts: Dict[str, Any] = Field(default_factory=dict)
    retry: Dict[str, Any] = Field(default_factory=dict)
    browser: Dict[str, Any] = Field(default_factory=dict)
    account_safety: Dict[str, Any] = Field(default_factory=dict)
    waitlist: Dict[str, Any] = Field(default_factory=dict)
    webhook: Dict[str, Any] = Field(default_factory=dict)
    inbox: Dict[str, Any] = Field(default_factory=dict)
    bandwidth: Dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Client journal                                                               #
# --------------------------------------------------------------------------- #


class JournalRow(BaseModel):
    """One registration attempt from the waitlist journal."""

    route: str = ""
    combo: str = ""
    registrant_id: str = ""
    status: str = ""
    vfs_reference: Optional[str] = None
    account: Optional[str] = None
    reason: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class ClientJournalResponse(BaseModel):
    """Full registration history for one client."""

    client_id: str
    count: int
    rows: List[JournalRow] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Webhook management                                                           #
# --------------------------------------------------------------------------- #


class WebhookTestResponse(BaseModel):
    """Result of a webhook test ping."""

    delivered: bool
    event: str = ""
    attempts: int = 0
    status_code: Optional[int] = None
    error: str = ""
    skipped: bool = False


class WebhookDeadletterResponse(BaseModel):
    """How many webhook deliveries failed."""

    count: int
    configured: bool


# --------------------------------------------------------------------------- #
# Inbox reconcile                                                              #
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Booking status                                                               #
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Switches (operational toggles)                                               #
# --------------------------------------------------------------------------- #


class SwitchesUpdateRequest(BaseModel):
    """Partial update of operational switches. Only supplied fields change."""

    model_config = ConfigDict(extra="forbid")

    register_enabled: Optional[bool] = Field(
        default=None,
        description="Master switch. False = nothing registers.",
    )
    dry_run: Optional[bool] = Field(
        default=None,
        description="Global dry-run: walk the flow, never submit.",
    )
    auto_trigger_enabled: Optional[bool] = Field(
        default=None,
        description="Slot checker may fire waitlist runs.",
    )
    auto_trigger_dry_run: Optional[bool] = Field(
        default=None,
        description="Auto-triggered runs stop before submitting.",
    )
    max_per_run: Optional[int] = Field(
        default=None, ge=1, le=50,
        description="Max registrations per invocation.",
    )
    max_per_day: Optional[int] = Field(
        default=None, ge=1, le=200,
        description="Max registrations per calendar day.",
    )


class SwitchesUpdateResponse(BaseModel):
    """Result of toggling switches."""

    updated: List[str] = Field(
        default_factory=list,
        description="Which switches were changed.",
    )
    switches: SwitchState
    message: str = ""


# --------------------------------------------------------------------------- #
# Account health                                                               #
# --------------------------------------------------------------------------- #


class AccountRouteHealth(BaseModel):
    """One route's health for one account."""

    route: str
    cooldown_until: float = 0
    fails: int = 0
    last_reason: str = ""
    benched: bool = False


class AccountHealth(BaseModel):
    """Health state for one VFS account."""

    email: str
    disabled: bool = False
    disabled_reason: str = ""
    routes: List[AccountRouteHealth] = Field(default_factory=list)


class AccountHealthResponse(BaseModel):
    """All accounts' circuit-breaker state."""

    count: int
    accounts: List[AccountHealth] = Field(default_factory=list)


class AccountClearResponse(BaseModel):
    """Result of clearing an account's health state."""

    email: str
    route: Optional[str] = None
    cleared: bool


class AccountBenchRequest(BaseModel):
    """Bench an account on a route."""

    model_config = ConfigDict(extra="forbid")

    route: str = Field(description="Route to bench on, e.g. AE-CHE.")
    hours: int = Field(default=2, ge=1, le=168, description="Hours to bench.")
    reason: str = Field(default="benched via API", max_length=280)


# --------------------------------------------------------------------------- #
# Booking trigger                                                              #
# --------------------------------------------------------------------------- #


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

        if self.entry == "new" and not self.combo:
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



# --------------------------------------------------------------------------- #
# Payments — the unanswered-payment surface                                    #
# --------------------------------------------------------------------------- #


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
