"""Settings for the assistant. One frozen object, passed in by the host project.

Nothing here reads the host's config files: the host decides where the API key
and limits come from and hands them over. That is what keeps this package
copyable — it has no idea which project it lives in.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Optional, Tuple


@dataclass(frozen=True)
class AssistantConfig:
    #: OpenAI API key. Never logged, never sent anywhere but `base_url`.
    api_key: str = field(repr=False)
    model: str = "gpt-4o-mini"
    base_url: str = "https://api.openai.com/v1"

    #: A paragraph describing the host project, prepended to the system prompt.
    #: Say what the system does and what its vocabulary means; the tools carry
    #: the facts.
    system_context: str = ""
    #: Shown in the panel header.
    title: str = "Assistant"
    #: Starter questions shown as chips in an empty conversation.
    suggestions: Tuple[str, ...] = ()

    # ---- conversation limits ------------------------------------------------
    max_question_chars: int = 2_000
    max_history_turns: int = 6           # previous user+assistant pairs kept
    max_history_chars: int = 16_000
    max_tool_rounds: int = 8             # model<->tools round trips per answer
    max_tool_calls_per_round: int = 6
    max_tool_output_chars: int = 14_000  # per tool result, after redaction
    max_output_tokens: int = 1_800
    temperature: float = 0.2

    # ---- time ---------------------------------------------------------------
    request_timeout_s: float = 60.0      # one HTTP call to the model
    total_timeout_s: float = 150.0       # one whole answer, tools included
    tool_timeout_s: float = 20.0         # one tool call

    # ---- spend + abuse ------------------------------------------------------
    #: Tokens (prompt + completion) per local calendar day. 0 = unlimited.
    daily_token_budget: int = 1_500_000
    rate_limit_per_minute: int = 12
    max_concurrent: int = 2

    # ---- scope (see scope.py) ----------------------------------------------
    #: One or two sentences: what the assistant is FOR. Drives the scope gate;
    #: empty switches the gate off (grounding still applies).
    scope_description: str = ""
    #: Cheap classifier call before the real one; OUT questions never reach it.
    scope_check: bool = True
    #: Discard any first answer that read no data (no tool call).
    require_grounding: bool = True
    #: Off-topic questions inside the window that trigger a cooldown. 0 = never.
    off_topic_limit: int = 3
    off_topic_window_s: float = 600.0
    off_topic_cooldown_s: float = 600.0
    #: Shown instead of an answer. Empty = built from `title`.
    refusal_message: str = ""

    # ---- privacy ------------------------------------------------------------
    #: Replace e-mail addresses with stable pseudonyms before anything reaches
    #: the model; the server swaps them back in the answer the browser sees.
    pseudonymise_emails: bool = True

    # ---- files (None = feature off) ----------------------------------------
    #: JSONL, one line per question: tools used, tokens, latency. No answers.
    audit_path: Optional[str] = None
    #: Persists the daily token count across restarts.
    usage_path: Optional[str] = None

    # ---- HTTP ---------------------------------------------------------------
    route_prefix: str = "/assistant"
    #: Host-header allow-list (DNS-rebinding guard).
    allowed_hosts: Tuple[str, ...] = ("127.0.0.1", "localhost")
    #: Only serve loopback clients unless the host project opts out.
    loopback_only: bool = True

    def __post_init__(self):
        if not self.api_key or not self.api_key.strip():
            raise ValueError("AssistantConfig.api_key is empty")
        if not self.route_prefix.startswith("/") or self.route_prefix.endswith("/"):
            raise ValueError("route_prefix must start with '/' and not end with one")
        if self.max_tool_rounds < 1:
            raise ValueError("max_tool_rounds must be >= 1")

    def with_(self, **changes) -> "AssistantConfig":
        return replace(self, **changes)

    @classmethod
    def from_env(cls, **overrides) -> "AssistantConfig":
        """OPENAI_API_KEY / ASSISTANT_MODEL from the environment, then overrides."""
        kw = {"api_key": os.environ.get("OPENAI_API_KEY", "")}
        if os.environ.get("ASSISTANT_MODEL"):
            kw["model"] = os.environ["ASSISTANT_MODEL"]
        kw.update(overrides)
        return cls(**kw)
