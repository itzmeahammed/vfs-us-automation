"""The run_id: one identifier tying every record of a single run together.

    ═════════════════ WHY THIS EXISTS ═════════════════

Before this module there were three record systems that could not be joined:

    the API job store      logs/api_jobs/jobs.jsonl   keyed by job_id
    the application log    logs/app-YYYY-MM-DD.log    keyed by nothing
    the two journals       state/*.jsonl              keyed by (route, client)

So "what happened on the booking that charged this card?" meant reading three
files by eye and correlating on wall-clock timestamps. That is archaeology, and
it is exactly the work you least want to be doing when a payment is unanswered.

Now every one of those records carries the same `run_id`, and the folder of
screenshots is named after it too. One grep answers the question.

    ═════════════════ WHERE IT COMES FROM ═════════════════

There is exactly one rule: THE RUN ID IS INHERITED IF PRESENT, GENERATED IF NOT.

    API-triggered    the API mints it, exports VFS_RUN_ID to the child, and
                     stores it on the JobRecord. The child inherits it, so the
                     job and the run share one id and `job_id == run_id`.
    manual CLI       no env var, so the CLI mints one and prints it. Same shape,
                     so a hand-run booking is as traceable as an API one.

Inheritance is what makes the chain hold across the process boundary, and it is
why this reads the environment rather than taking a parameter: the child is
several frames deep in argparse by the time anything needs the id, and
threading it through every signature would be a much larger change for the
same result.

The id is short (12 hex characters) because it goes in a directory name and
gets typed into greps by humans. It is not a security token; it is a join key.
"""

from __future__ import annotations

import os
import secrets
import time

#: The environment variable that carries the id across a process boundary.
ENV_VAR = "VFS_RUN_ID"

#: Cached so repeated calls inside one process agree with each other. Without
#: this a manual run would mint a new id per caller and the join key would be
#: useless in the one case it is most often read by hand.
_run_id: str | None = None


def new_run_id() -> str:
    """Mint a fresh id. Time-ordered prefix so a directory listing sorts."""
    return f"{int(time.time()):x}{secrets.token_hex(3)}"


def run_id() -> str:
    """This process's run id, inherited from the parent or minted once.

    Idempotent: the first call decides, every later call agrees. Also exports
    the value back into the environment so any grandchild process this run
    spawns joins the same chain.
    """
    global _run_id
    if _run_id is not None:
        return _run_id

    inherited = (os.environ.get(ENV_VAR) or "").strip()
    # Validated rather than trusted: this value reaches a directory name, and
    # an inherited "../../etc" would escape the runs/ tree. Anything unexpected
    # is replaced rather than sanitised, because a caller passing a strange id
    # has a bug we want to see a fresh id for, not a quietly mangled one.
    if inherited and len(inherited) <= 40 and inherited.isalnum():
        _run_id = inherited
    else:
        _run_id = new_run_id()
        os.environ[ENV_VAR] = _run_id
    return _run_id


def is_inherited() -> bool:
    """True when the parent supplied the id (i.e. this run was API-triggered)."""
    return bool((os.environ.get(ENV_VAR) or "").strip()) and run_id() == os.environ.get(ENV_VAR)


def reset_for_tests() -> None:
    """Forget the cached id. Tests only — never call this in a real run."""
    global _run_id
    _run_id = None
    os.environ.pop(ENV_VAR, None)
