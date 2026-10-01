"""Waitlist (Flow 1) request and response models."""

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
from src.api.modules.system.schemas import SwitchState


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
