"""Route (country) and combo catalog models."""

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

from src.api.core.schemas import ProblemModel


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
