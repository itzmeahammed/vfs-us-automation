"""Load a processor's selector config from config/payment/<NAME>.json."""

from __future__ import annotations

import json
import os
from typing import Any, Dict

CONFIG_DIR = os.path.join("config", "payment")
DEFAULT_PROCESSOR = "CYBERSOURCE"


class PaymentConfigError(Exception):
    """The processor config is missing, malformed, or disabled."""


def path_for(processor: str = "") -> str:
    name = (processor or DEFAULT_PROCESSOR).strip().upper()
    return os.path.join(CONFIG_DIR, f"{name}.json")


def load(processor: str = "") -> Dict[str, Any]:
    """The processor's spec. Raises unless it exists AND is enabled.

    The enabled check lives here rather than in the caller so that every path
    into the payment code goes through it — this is the only config in the
    repository that can spend money, and "someone forgot to check" must not be
    a way to spend it.
    """
    config_path = path_for(processor)
    if not os.path.isfile(config_path):
        raise PaymentConfigError(
            f"No payment config at {config_path}. Payment processors are "
            "configured per file, like booking routes.")

    with open(config_path, encoding="utf-8") as fh:
        spec = json.load(fh)

    if not spec.get("enabled"):
        raise PaymentConfigError(
            f"{config_path} ships disabled. Enabling it lets a run charge a "
            "real card unattended — set \"enabled\": true deliberately.")
    return spec
