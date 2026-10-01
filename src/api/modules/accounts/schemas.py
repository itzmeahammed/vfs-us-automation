"""VFS account health models."""

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
