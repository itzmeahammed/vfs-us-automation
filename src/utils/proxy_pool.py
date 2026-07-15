"""Per-account proxy (IP) selection — a small fixed pool of IPs shared by ALL routes.

Each account is PINNED to one IP from the pool (account_index mod N), so it always
egresses from the SAME IP across every route — a stable, consistent identity. With
5 IPs and ~10 accounts, each IP hosts ~2 accounts.

Pool source (first that is set):
  * [proxy-pool] list in config/proxies.local.ini, else
  * config/proxylist.txt — one proxy per line, either
        user:pass@host:port   (or host:port), or the provider CSV
        IP, PORT, LOGIN, PASSWORD          (a header row is ignored)

Chrome ignores proxy credentials, so ChromeProcess runs authenticated proxies
through a tiny local forwarder (proxy_forwarder). Selection PROBES the pinned IP
for a working exit and falls back to the next pool entry if it's down.

Master switch: [proxy] enabled (config.ini); per run: supervisor --proxy / --local.
"""

import logging
import os
import urllib.request

from src.utils.config_reader import get_config_value

PROXYLIST_FILE = os.path.join("config", "proxylist.txt")
_PROBE_URL = "https://api.ipify.org"


def _as_url(s: str) -> str:
    s = s.strip()
    return s if "://" in s else "http://" + s


def parse(url: str):
    """(host, port, user, password) from a proxy URL; user/password may be ''."""
    from urllib.parse import urlparse
    p = urlparse(_as_url(url))
    return p.hostname, p.port, (p.username or ""), (p.password or "")


def _mask(url: str) -> str:
    if "@" in url:
        scheme, _, rest = url.partition("://")
        tail = rest.rsplit("@", 1)[-1]
        return (f"{scheme}://***@{tail}" if scheme else "***@" + tail)
    return url


def label(url: str) -> str:
    """Short host:port label (no scheme/creds)."""
    if not url:
        return ""
    h, p, _, _ = parse(url)
    return f"{h}:{p}" if h else _mask(url)


def _parse_line(line: str) -> str:
    """A proxylist.txt line -> proxy URL, or '' to skip (blank/comment/header)."""
    line = line.strip()
    if not line or line[0] in "#;":
        return ""
    if "@" in line:                         # user:pass@host:port
        return _as_url(line)
    if "," in line:                         # provider CSV: IP, PORT, LOGIN, PASSWORD
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[1].isdigit():
            ip, port = parts[0], parts[1]
            if len(parts) >= 4 and parts[2]:
                return f"http://{parts[2]}:{parts[3]}@{ip}:{port}"
            return f"http://{ip}:{port}"
        return ""                           # header row / malformed
    if ":" in line:
        parts = line.split(":")
        if len(parts) == 4:                 # provider colon format (4 fields)
            a, b, host, d = parts
            if d.isdigit() and not host.isdigit():     # USER:PASS:HOST:PORT
                return f"http://{a}:{b}@{host}:{d}"
            if b.isdigit():                            # HOST:PORT:USER:PASS
                return f"http://{host}:{d}@{a}:{b}"
        return _as_url(line)                # host:port
    return ""


def _proxylist() -> list:
    try:
        with open(PROXYLIST_FILE, encoding="utf-8") as f:
            return [u for u in (_parse_line(ln) for ln in f) if u]
    except OSError:
        return []


def _pool_list() -> list:
    raw = get_config_value("proxy-pool", "list", "") or ""
    return [_as_url(p.strip()) for p in raw.replace(";", ",").split(",") if p.strip()]


def pool() -> list:
    """The IP pool: [proxy-pool] if set, else parsed proxylist.txt."""
    return _pool_list() or _proxylist()


def _route_pin(route: str) -> str:
    if not route:
        return ""
    return (get_config_value("proxy-routes", route.strip().upper(), "") or "").strip()


def _account_index(email: str) -> int:
    """Stable index for `email` among configured accounts (file order)."""
    from src.utils import credentials
    emails = [c[0] for c in credentials._load_pool()]
    if email in emails:
        return emails.index(email)
    import hashlib
    return int(hashlib.md5((email or "").encode()).hexdigest(), 16)


def account_proxy(email: str, route: str = None) -> str:
    """The IP this account is pinned to (route is ignored — same IP everywhere)."""
    p = pool()
    if not p or not email:
        return ""
    return p[_account_index(email) % len(p)]


def probe(proxy_url: str, timeout: int = 15) -> str:
    """Exit IP if `proxy_url` has a working exit right now, else '' (auth handled)."""
    from src.utils.proxy_forwarder import ProxyForwarder
    from urllib.parse import urlparse
    host, port, user, pw = parse(proxy_url)
    if not host:
        return ""
    scheme = urlparse(_as_url(proxy_url)).scheme or "http"
    fwd = ProxyForwarder(host, port, user, pw, scheme=scheme)
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


def is_enabled() -> bool:
    """Master switch: proxy pool (True) or the PC's own IP (False). VFS_PROXY env
    (on/off/proxy/local/1/0) overrides the [proxy] enabled config for one run."""
    ov = os.environ.get("VFS_PROXY")
    if ov is not None:
        return ov.strip().lower() in ("1", "true", "on", "yes", "proxy")
    return str(get_config_value("proxy", "enabled", "true")).strip().lower() \
        in ("1", "true", "on", "yes")


def pick_for_run(route: str, email: str = None, exclude=None):
    """
    Choose a WORKING proxy for this account (pinned IP), shared across all routes.
    Returns (proxy_url, exit_ip), or (None, None) for a direct connection.

    `exclude` is a set of proxy URLs already tried (e.g. one that returned 403201)
    — they are skipped so the caller can rotate to a DIFFERENT IP.

    Order: switch off -> direct > [proxy-routes] pin > account's pinned pool IP
    (probed; falls through the pool if its exit is down/excluded) > direct.
    """
    if not is_enabled():
        logging.info(f"{route}: proxy disabled — using local IP (direct).")
        return None, None

    exclude = set(exclude or [])

    pin = _route_pin(route)
    if pin and _as_url(pin) not in exclude:
        logging.info(f"{route}: using pinned proxy {_mask(_as_url(pin))}")
        return _as_url(pin), ""

    p = pool()
    if not p or not email:
        return None, None
    who = email.split("@")[0]
    base = _account_index(email) % len(p)
    for k in range(len(p)):
        proxy = p[(base + k) % len(p)]
        if proxy in exclude:
            continue
        ip = probe(proxy)
        if ip:
            _h, _pt, _, _ = parse(proxy)
            logging.info(f"proxyseller ip used here : {ip}:{_pt}  "
                         f"({who} on {route}{', pool+' + str(k) if k else ''})")
            return proxy, ip
        logging.warning(f"{route}: {who} proxy {label(proxy)} no exit — trying next.")
    logging.error(f"{route}: no working proxy for {who} — falling back to DIRECT.")
    return None, None
