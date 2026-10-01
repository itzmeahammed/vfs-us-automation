"""Client models: the waitlist client shape and its journal."""

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

from src.api.core.schemas import _ROUTE_RE, _ID_RE, _COMBO_RE
from src.api.core.schemas import ProblemModel
from src.api.modules.waitlist.schemas import TriggerRequest


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
