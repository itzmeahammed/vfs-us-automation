"""Is this machine online?

A fast, dependency-free reachability check used to skip a WHOLE run when there
is no internet. Without it, an offline machine makes every route fail with
connection-refused: the proxy forwarder can't reach any upstream IP (WinError
10061), the DIRECT fallback hits net::ERR_CONNECTION_REFUSED, accounts get
struck for a problem that isn't theirs, and every Telegram send fails too —
dozens of noise lines per run, ×N routes. The supervisor calls this once at
startup and bails cleanly if we're offline (next scheduled run retries).
"""

import logging
import socket

# Well-known, always-on hosts. TCP-connect by IP (no DNS dependence), so the
# check still works when DNS itself is down. A few of them, so one host being
# unreachable can't cause a false "offline".
_PROBES = (
    ("1.1.1.1", 443),   # Cloudflare
    ("8.8.8.8", 53),    # Google DNS
    ("9.9.9.9", 53),    # Quad9
)


def internet_available(timeout: float = 4.0, probes=_PROBES) -> bool:
    """True if ANY probe host is reachable within `timeout` seconds.

    Returns on the first success (typically well under `timeout`); only a total
    failure to reach every probe returns False.
    """
    for host, port in probes:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def require_internet_or_log() -> bool:
    """Convenience gate: True if online; else logs ONE clear line and returns
    False (the caller stops the run). No exception, no stack trace — an offline
    machine is an environment condition, not a bug."""
    if internet_available():
        return True
    logging.error(
        "No internet connectivity detected — skipping this run. No routes "
        "attempted, no accounts struck, no Telegram sent. The next scheduled "
        "run will retry once the connection is back."
    )
    return False
