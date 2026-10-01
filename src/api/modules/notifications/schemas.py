"""Webhook models."""

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
