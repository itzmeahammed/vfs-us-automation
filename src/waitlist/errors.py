"""Typed exceptions for the waitlist-registration flow.

The taxonomy mirrors src/vfs_bot/errors.py, with ONE critical addition that the
slot-check flow never needed: a distinction between failures BEFORE and AFTER
the point of no return.

    WaitlistError            -> before commit; safe to abandon, nothing changed
    WaitlistCommittedError   -> AFTER commit; VFS state MAY have been mutated

WaitlistCommittedError deliberately does NOT subclass RetryableError. The
supervisor retries RetryableError by relaunching Chrome and re-running the whole
flow — which is exactly the wrong response once we have submitted something. A
committed failure needs a human to look at the journal and the account, never an
automatic retry.
"""


class WaitlistError(Exception):
    """
    Base for waitlist failures raised BEFORE anything was submitted.

    Safe to abandon: the page (and the VFS account) are unchanged, so the caller
    can simply skip this registration and carry on with the run.
    """


class WaitlistConfigError(WaitlistError):
    """The route/registrant configuration is missing, malformed or incomplete.

    Raised at LOAD time, before a browser is ever touched — a typo in a JSON file
    should cost a second, not a run.
    """


class WaitlistNotOfferedError(WaitlistError):
    """The waitlist checkbox was not present on the page.

    Normal and expected (the combo may have real slots, or none at all). Not
    worth alerting about.
    """


class WaitlistSkipped(WaitlistError):
    """A guard declined this registration (kill switch off, no target
    configured, cap reached, already registered, dangling journal entry).

    Carries the human-readable reason so the caller can report WHY nothing was
    attempted. Not a failure — a deliberate no-op.
    """


class WaitlistStepError(WaitlistError):
    """A pre-commit step failed: a field could not be filled, a control was
    never found, or a page never loaded. Nothing was submitted."""


class WaitlistCommittedError(Exception):
    """
    A failure AFTER the point of no return — the submit was (or may have been)
    delivered to VFS, so the registration's true state is unknown.

    NOT retryable, and deliberately outside the RetryableError hierarchy so the
    supervisor's relaunch logic can never pick it up. The correct response is:
    journal it as 'unknown', screenshot, alert a human, and stop touching this
    route/registrant until someone confirms what actually happened on the VFS
    account.
    """
