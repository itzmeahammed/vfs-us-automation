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
import time
import urllib.request

from src.utils.config_reader import get_config_value

PROXYLIST_FILE = os.path.join("config", "proxylist.txt")
_PROBE_URL = "https://api.ipify.org"
# Geo endpoint: one call returns BOTH the exit IP and its country, so a proxy can
# be region-checked (UAE?) at the same time it's liveness-checked. Free, no key.
_GEO_URL = "http://ip-api.com/json/?fields=status,countryCode,query"

# In-process cache of geo probes so the SAME proxy isn't re-probed for every route
# in one run (each scheduled run is a fresh process, so the cache is naturally
# short-lived). Maps proxy_url -> (ip, country_code, expires_epoch).
_geo_cache = {}
_GEO_TTL_S = 300


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


def _open_via(local_port: int, url: str, timeout: int) -> str:
    """GET `url` through the local forwarder port; returns the response body."""
    local = f"http://127.0.0.1:{local_port}"
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": local, "https": local})
    )
    return opener.open(url, timeout=timeout).read().decode().strip()


def probe_geo(proxy_url: str, timeout: int = 15):
    """
    Health-check an exit: returns (exit_ip, country_code) through `proxy_url`, or
    (None, None) if the exit is dead. `country_code` is '' when the exit works but
    its region couldn't be determined (geo service unreachable) — a working IP is
    never discarded just because the geo lookup failed.
    """
    import json as _json
    from urllib.parse import urlparse

    from src.utils.proxy_forwarder import ProxyForwarder
    host, port, user, pw = parse(proxy_url)
    if not host:
        return None, None
    scheme = urlparse(_as_url(proxy_url)).scheme or "http"
    fwd = ProxyForwarder(host, port, user, pw, scheme=scheme)
    try:
        lp = fwd.start()
        # Prefer the geo endpoint — one round-trip yields exit IP AND country.
        try:
            data = _json.loads(_open_via(lp, _GEO_URL, timeout))
            if str(data.get("status", "")).lower() == "success":
                return ((data.get("query") or "").strip(),
                        (data.get("countryCode") or "").strip().upper())
        except Exception as e:
            logging.debug(f"geo lookup failed for {label(proxy_url)}: {e}")
        # Geo service unreachable: fall back to a plain liveness probe so a
        # working exit with an unknown region stays usable (region '').
        try:
            return _open_via(lp, _PROBE_URL, timeout), ""
        except Exception as e:
            logging.debug(f"proxy probe failed for {label(proxy_url)}: {e}")
            return None, None
    finally:
        fwd.stop()


def probe(proxy_url: str, timeout: int = 15) -> str:
    """Exit IP if `proxy_url` has a working exit right now, else '' (region ignored)."""
    ip, _cc = probe_geo(proxy_url, timeout)
    return ip or ""


def _require_country() -> str:
    """ISO 3166 alpha-2 the proxy exit MUST be in (default 'AE' — UAE, the only
    region VFS accepts here). Set [proxy] require_country = '' to disable the check."""
    return (get_config_value("proxy", "require_country", "AE") or "").strip().upper()


def _geo_cached(proxy_url: str):
    """probe_geo() with a short in-process cache (avoids re-probing the same exit
    for every route in one run)."""
    now = time.time()
    hit = _geo_cache.get(proxy_url)
    if hit and hit[2] > now:
        return hit[0], hit[1]
    ip, cc = probe_geo(proxy_url)
    _geo_cache[proxy_url] = (ip, cc, now + _GEO_TTL_S)
    return ip, cc


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
    require_cc = _require_country()
    base = _account_index(email) % len(p)
    for k in range(len(p)):
        proxy = p[(base + k) % len(p)]
        if proxy in exclude:
            continue
        # Health-check the exit BEFORE handing it to Chrome: a dead exit would
        # surface later as WinError 10054 mid-run; a non-UAE exit as a 403203
        # geo-block. Skipping both here attacks those failures at the source.
        ip, cc = _geo_cached(proxy)
        if not ip:
            logging.warning(f"{route}: {who} proxy {label(proxy)} no exit — trying next.")
            continue
        if require_cc and cc and cc != require_cc:
            logging.warning(f"{route}: {who} proxy {label(proxy)} exits in {cc}, "
                            f"need {require_cc} — skipping (geo-block risk).")
            continue
        _h, _pt, _, _ = parse(proxy)
        logging.info(f"proxyseller ip used here : {ip}:{_pt}  [{cc or '??'}]  "
                     f"({who} on {route}{', pool+' + str(k) if k else ''})")
        return proxy, ip
    logging.error(f"{route}: no working {require_cc or 'proxy'} exit for {who} — "
                  "falling back to DIRECT.")
    return None, None
