"""Shared API models and input patterns used by more than one module."""

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
class ProblemModel(BaseModel):
    """One validation finding, addressable to a form field."""

    field: str = Field(description="Payload key the problem belongs to, or ''.")
    message: str
    severity: str = "error"
    hint: str = ""
