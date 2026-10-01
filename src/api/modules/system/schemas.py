"""Switches, overview and config models."""

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

    # --- the four master switches ([switches]) -----------------------------
    waitlist: bool = Field(default=False, description="MASTER — Flow 1: register "
                           "clients on VFS waitlists.")
    invite_booking: bool = Field(default=False, description="MASTER — Flow 2: a VFS "
                                 "invitation email books that client.")
    live_booking: bool = Field(default=False, description="MASTER — Flow 3: a live "
                               "slot inside a booking request's window is booked "
                               "and PAID.")
    test_booking: bool = Field(default=False, description="MASTER — test_mode "
                               "requests may run (stop before the payment click).")
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
    waitlist: Optional[bool] = Field(
        default=None, description="MASTER — Flow 1 (waitlist registration).")
    invite_booking: Optional[bool] = Field(
        default=None, description="MASTER — Flow 2 (invitation email -> booking).")
    live_booking: Optional[bool] = Field(
        default=None, description="MASTER — Flow 3 (live slot -> booking AND PAYMENT).")
    test_booking: Optional[bool] = Field(
        default=None, description="MASTER — test_mode booking requests may run.")
class SwitchesUpdateResponse(BaseModel):
    """Result of toggling switches."""

    updated: List[str] = Field(
        default_factory=list,
        description="Which switches were changed.",
    )
    switches: SwitchState
    message: str = ""
