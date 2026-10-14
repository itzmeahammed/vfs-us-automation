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
_FLOW_DESCRIPTION = (
    "The flow the bot picked for this country: send it as `flow` in "
    "POST /clients. 'live' books an open slot inside a date range; "
    "'waitlist' registers and books when VFS emails an invitation. When a "
    "country is ready for both, this is 'live'. null means neither is ready."
)
_AVAILABLE_DESCRIPTION = (
    "Every flow this country is ready for, preferred first. Two entries "
    "means the caller may choose either."
)
_READY_DESCRIPTION = "True when at least one flow is ready, i.e. `flow` is set."


class FlowPause(BaseModel):
    """An operator pause in force (PATCH /routes/{route}/flows/{flow})."""

    reason: str = ""
    by: str = Field(default="", description="X-Actor of whoever paused it.")
    at: str = Field(default="", description="When, ISO 8601 UTC.")


class FlowReadiness(BaseModel):
    """One flow on one route: usable or not, and why not."""

    ready: bool
    paused: Optional[FlowPause] = Field(
        default=None,
        description="Set when someone paused this flow for this country.")
    problems: List[ProblemModel] = Field(
        default_factory=list,
        description=(
            "severity 'error' is why this flow is not ready. 'warning' leaves "
            "it ready: a master switch is off, so clients are accepted but "
            "nothing runs for them yet."
        ),
    )
    fields: List[RouteFieldModel] = Field(
        default_factory=list,
        description=(
            "What a client of THIS flow must send in POST /clients, whether "
            "or not the flow is the one picked. Empty when the route has no "
            "form for this flow."
        ),
    )


class RouteFlowsModel(BaseModel):
    waitlist: FlowReadiness
    live: FlowReadiness


class RouteReadinessResponse(BaseModel):
    """Which flow a route runs on, its combos, and its form fields."""

    route: str
    flow: Optional[Literal["live", "waitlist"]] = Field(
        default=None, description=_FLOW_DESCRIPTION)
    available_flows: List[str] = Field(
        default_factory=list, description=_AVAILABLE_DESCRIPTION)
    flows: RouteFlowsModel
    ready: bool = Field(description=_READY_DESCRIPTION)
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
            "Send them as top-level keys in the POST /clients body. These are "
            "the fields of the picked `flow` (the waitlist's when none is "
            "picked); `flows.<flow>.fields` has each flow's own list. A live "
            "client's list opens with date_from and date_to and ends with the "
            "payment page's billing fields. Not listed, because they are not "
            "form data: client_id, flow, route, the combination (`combos`, a "
            "list, for waitlist; `combo`, one label, for live) and the "
            "optional account / account_password."
        ),
    )
    problems: List[ProblemModel] = Field(
        default_factory=list,
        description=(
            "Problems of the picked flow. When no flow is ready, both flows' "
            "problems, so the caller sees everything standing in the way."
        ),
    )
class RouteFlowSummary(BaseModel):
    ready: bool
    paused: Optional[FlowPause] = None
    problems: List[str] = Field(default_factory=list)
class RouteFlowsSummary(BaseModel):
    waitlist: RouteFlowSummary
    live: RouteFlowSummary
class RouteSummary(BaseModel):
    """One route in the routes listing."""

    route: str
    flow: Optional[Literal["live", "waitlist"]] = Field(
        default=None, description=_FLOW_DESCRIPTION)
    available_flows: List[str] = Field(
        default_factory=list, description=_AVAILABLE_DESCRIPTION)
    flows: Optional[RouteFlowsSummary] = Field(
        default=None,
        description="Per-flow readiness. null only if the check itself failed.")
    ready: bool = Field(description=_READY_DESCRIPTION)
    combos: List[str] = Field(default_factory=list)
    clients: int = 0
    problems: List[str] = Field(default_factory=list)
class RoutesListResponse(BaseModel):
    """All configured routes."""

    count: int
    routes: List[RouteSummary] = Field(default_factory=list)
class RouteFlowPatch(BaseModel):
    """Turn one flow off, or back on, for one country."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(
        description="false pauses the flow for this country. true lifts the "
                    "pause; it cannot enable a flow the route's config does "
                    "not have ready.")
    reason: str = Field(
        default="", max_length=300,
        description="Why. Required when pausing: it is shown to everyone who "
                    "reads this route's readiness.")

    @model_validator(mode="after")
    def _reason_when_pausing(self) -> "RouteFlowPatch":
        if not self.enabled and not self.reason.strip():
            raise ValueError("\"reason\" is required when enabled is false.")
        return self
class RouteFlowPatchResponse(BaseModel):
    route: str
    flow_changed: str = Field(description="The flow this call acted on.")
    changed: bool = Field(
        description="False when the call was a no-op: resuming a flow that "
                    "was not paused.")
    message: str
    readiness: RouteSummary = Field(
        description="This route as GET /routes now reports it.")
