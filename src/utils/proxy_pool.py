"""Per (account, route) proxy (IP) selection from config/proxylist.txt.

Each (account, ROUTE) pairing is pinned to its OWN dedicated residential IP, so:
  * one IP = one account on one route (stable across runs), and
  * consecutive routes in a run egress from DIFFERENT IPs (route 1 -> IP a,
    route 2 -> IP b, ...), even when the same account serves both.

The line index in proxylist.txt is  account_index*ROUTE_STRIDE + route_index,
so distinct accounts and distinct routes land on distinct lines.

proxylist.txt lines are `user:pass@host:port` (proxy-seller residential). Because
Chrome ignores proxy credentials, ChromeProcess runs each through a tiny local
forwarder (src/utils/proxy_forwarder.py) that adds the auth.

Some proxy ports transiently return '503 No exit node', so selection PROBES the
assigned line (quick request through a temp forwarder) and, only if it has no
exit, scans the next few lines before giving up (direct connection).

Optional overrides (config/proxies.local.ini):
  [proxy-routes] <ROUTE> = <proxy>   — pin a route to one proxy (used as-is)
"""

import logging
import os
import urllib.request

from src.utils.config_reader import get_config_section, get_config_value

PROXYLIST_FILE = os.path.join("config", "proxylist.txt")
# Lines reserved per account for its routes — account i's routes occupy lines
# [i*STRIDE .. i*STRIDE+STRIDE-1]. Must be >= the max number of routes.
_ROUTE_STRIDE = 8
_PROBE_URL = "https://api.ipify.org"


def _proxylist() -> list:
    """Raw `user:pass@host:port` lines from proxylist.txt ([] if absent)."""
    try:
        with open(PROXYLIST_FILE, encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    except OSError:
        return []


def _as_url(line: str) -> str:
    """Normalize a proxylist line / proxy value to a full URL (default http://)."""
    line = line.strip()
    return line if "://" in line else "http://" + line


def parse(url: str):
    """(host, port, user, password) from a proxy URL; user/password may be ''."""
    from urllib.parse import urlparse
    p = urlparse(_as_url(url))
    return p.hostname, p.port, (p.username or ""), (p.password or "")


def _mask(url: str) -> str:
    """Hide credentials when logging a proxy URL."""
    if "@" in url:
        scheme, _, rest = url.partition("://")
        return f"{scheme}://***@{rest.rsplit('@', 1)[-1]}" if scheme else "***@" + rest.rsplit("@", 1)[-1]
    return url


def label(url: str) -> str:
    """Short host:port label (no scheme/creds) for logs."""
    if not url:
        return ""
    h, p, _, _ = parse(url)
    return f"{h}:{p}" if h else _mask(url)


def _route_pin(route: str) -> str:
    if not route:
        return ""
    return (get_config_value("proxy-routes", route.strip().upper(), "") or "").strip()


def _account_index(email: str) -> int:
    """Stable index for `email` among the configured accounts (file order)."""
    from src.utils import credentials
    emails = [c[0] for c in credentials._load_pool()]
    if email in emails:
        return emails.index(email)
    # Fallback: stable hash (not process-random) so it's consistent across runs.
    import hashlib
    return int(hashlib.md5((email or "").encode()).hexdigest(), 16)


def _route_index(route: str) -> int:
    """Stable index for `route` among configured [vfs-url] routes (config order)."""
    keys = [k.upper() for k in (get_config_section("vfs-url") or {})]
    r = (route or "").upper()
    if r in keys:
        return keys.index(r) % _ROUTE_STRIDE
    import hashlib
    return int(hashlib.md5(r.encode()).hexdigest(), 16) % _ROUTE_STRIDE


def _line_for(email: str, route: str) -> int:
    """The proxylist.txt line index dedicated to this (account, route) pairing."""
    lines = _proxylist()
    slot = _account_index(email) * _ROUTE_STRIDE + _route_index(route)
    return slot % len(lines)


def account_proxy(email: str, route: str) -> str:
    """The proxy URL dedicated to this (account, route), or '' if no list."""
    lines = _proxylist()
    if not lines:
        return ""
    return _as_url(lines[_line_for(email, route)])


def probe(proxy_url: str, timeout: int = 15) -> str:
    """
    Returns the exit IP if `proxy_url` currently has a working exit node, else ''.
    Runs the request through a short-lived local forwarder (handles auth).
    """
    from src.utils.proxy_forwarder import ProxyForwarder
    host, port, user, pw = parse(proxy_url)
    if not host:
        return ""
    fwd = ProxyForwarder(host, port, user, pw)
    try:
        lp = fwd.start()
        local = f"http://127.0.0.1:{lp}"
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": local, "https": local})
        )
        return opener.open(_PROBE_URL, timeout=timeout).read().decode().strip()
    except Exception as e:
        logging.debug(f"proxy probe failed for {label(proxy_url)}: {e}")
        return ""
    finally:
        fwd.stop()


def pick_for_run(route: str, email: str = None, scan: int = 8):
    """
    Choose a WORKING proxy for this (account, route) run. Returns (proxy_url,
    exit_ip), or (None, None) for a direct connection.

    Order: [proxy-routes] pin (used as-is) > this (account, route)'s dedicated
    line (probed) > a short scan of the next lines if that exit is empty > direct.
    """
    pin = _route_pin(route)
    if pin:
        logging.info(f"{route}: using pinned proxy {_mask(_as_url(pin))}")
        return _as_url(pin), ""

    lines = _proxylist()
    if not lines or not email:
        return None, None

    start = _line_for(email, route)
    who = email.split("@")[0]
    for k in range(scan + 1):
        proxy = _as_url(lines[(start + k) % len(lines)])
        ip = probe(proxy)
        if ip:
            _h, _p, _, _ = parse(proxy)
            logging.info(
                f"proxyseller ip used here : {ip}:{_p}  "
                f"({who} on {route}{', fell back +' + str(k) if k else ''})"
            )
            return proxy, ip
        logging.warning(f"{route}: {who} proxy {label(proxy)} no exit node — trying next.")

    logging.error(f"{route}: no working proxy exit for {who} — falling back to DIRECT.")
    return None, None


def is_configured() -> bool:
    """True if a proxylist or a route pin is configured."""
    return bool(_proxylist())
