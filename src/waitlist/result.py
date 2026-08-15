"""The outcome record for one waitlist registration attempt.

A single, explicit value object shared by register.py (which produces it), the
journal (which persists it), notify.py (which renders it) and the CLI (which
prints it) — so "what happened" is never re-derived from log strings.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


class Status:
    """Terminal states for a registration attempt.

    COMMITTED_STATES are the ones where VFS state may have changed — they are
    what the dedup check and the dangling-entry gate key off.
    """

    SKIPPED = "skipped"        # a guard declined; nothing attempted
    DRY_RUN = "dry_run"        # walked the flow, stopped before submit
    PENDING = "pending"        # submit in flight (write-ahead marker)
    SUCCESS = "success"        # confirmed registered
    UNKNOWN = "unknown"        # submitted, outcome unconfirmed — NEEDS A HUMAN
    FAILED = "failed"          # failed before commit; nothing submitted

    #: States that mean "this (route, combo, registrant) is spoken for".
    #: PENDING counts: an in-flight submit may well have landed.
    COMMITTED_STATES = (PENDING, SUCCESS, UNKNOWN)

    #: States that need a human to look at the VFS account.
    NEEDS_ATTENTION = (PENDING, UNKNOWN)


@dataclass
class WaitlistResult:
    """One registration attempt, start to finish."""

    route: str
    combo: str
    registrant_id: str
    status: str
    account: str = ""
    reason: str = ""                    # why skipped / how it failed
    vfs_reference: Optional[str] = None  # confirmation number, when VFS gives one
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: Optional[str] = None
    screenshots: List[str] = field(default_factory=list)
    steps_completed: List[str] = field(default_factory=list)

    @property
    def committed(self) -> bool:
        """True if VFS state may have been mutated by this attempt."""
        return self.status in Status.COMMITTED_STATES

    @property
    def needs_attention(self) -> bool:
        """True if a human must verify this on the VFS account."""
        return self.status in Status.NEEDS_ATTENTION

    def finish(self, status: str, reason: str = "") -> "WaitlistResult":
        """Stamp the terminal status and finish time. Returns self for chaining."""
        self.status = status
        if reason:
            self.reason = reason
        self.finished_at = datetime.now().isoformat(timespec="seconds")
        return self

    def to_dict(self) -> dict:
        return {
            "route": self.route,
            "combo": self.combo,
            "registrant_id": self.registrant_id,
            "status": self.status,
            "account": self.account,
            "reason": self.reason,
            "vfs_reference": self.vfs_reference,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "screenshots": self.screenshots,
            "steps_completed": self.steps_completed,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WaitlistResult":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    def summary(self) -> str:
        """One-line human summary, used by the CLI and Telegram."""
        head = f"[{self.status.upper()}] {self.route} · {self.combo} · {self.registrant_id}"
        if self.vfs_reference:
            head += f" · ref {self.vfs_reference}"
        if self.reason:
            head += f" — {self.reason}"
        return head
