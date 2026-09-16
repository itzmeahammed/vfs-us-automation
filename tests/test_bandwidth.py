"""Unit tests for the bandwidth-saving machinery.

Two denylists, deliberately enforced in two different places:

  * block_resource_types -> in-browser Playwright route. Costs the HTTP cache
    (interception makes Chromium bypass it), so it must stay OFF unless a
    measurement justifies it. Tested here by faking Playwright's route object.
  * block_hosts -> the proxy forwarder, which refuses the request before the
    metered upstream is dialled and costs no cache. Tested here against the
    real socket server, with a stub upstream.

Run: python -m unittest tests.test_bandwidth
"""

import os
import socket
import threading
import unittest

from src.settings import Bandwidth
from src.vfs_bot.vfs_bot_factory import get_vfs_bot


class _FakeRequest:
    def __init__(self, resource_type, url="https://visa.vfsglobal.com/are/en/hun/x"):
        self.resource_type = resource_type
        self.url = url


class _FakeRoute:
    def __init__(self, resource_type, url="https://visa.vfsglobal.com/are/en/hun/x"):
        self.request = _FakeRequest(resource_type, url)
        self.aborted = False
        self.continued = False

    def abort(self):
        self.aborted = True

    def continue_(self):
        self.continued = True


class _FakeContext:
    """Captures the handler registered via context.route(pattern, handler)."""

    def __init__(self):
        self.handler = None

    def route(self, pattern, handler):
        self.handler = handler


class TestResourceFilter(unittest.TestCase):
    def _handler_for(self, blocked_csv="image,media,font", hosts_csv=""):
        from src.utils.config_reader import initialize_config

        initialize_config()
        bot = get_vfs_bot("AE", "ITA")
        ctx = _FakeContext()
        # Patch the settings the method reads by temporarily building our own.
        import src.settings as s

        original = s._cached
        s._cached = s.Settings(bandwidth=Bandwidth(
            block_resource_types=blocked_csv, block_hosts=hosts_csv))
        try:
            bot._install_resource_blocking(ctx)
        finally:
            s._cached = original
        return bot, ctx.handler

    def test_blocks_heavy_types(self):
        bot, handler = self._handler_for()
        self.assertIsNotNone(handler, "filter was not installed")
        for rtype in ("image", "media", "font"):
            r = _FakeRoute(rtype)
            handler(r)
            self.assertTrue(r.aborted, f"{rtype} should be aborted")
            self.assertFalse(r.continued)

    def test_allows_essential_types(self):
        bot, handler = self._handler_for()
        for rtype in ("script", "stylesheet", "document", "xhr", "fetch"):
            r = _FakeRoute(rtype)
            handler(r)
            self.assertFalse(r.aborted, f"{rtype} must NOT be blocked (breaks flow)")
            self.assertTrue(r.continued)

    def test_no_types_installs_nothing(self):
        # THE regression guard. Interception costs the entire HTTP disk cache
        # (~4.7 MB per route-attempt of refetched VFS bundles), so nothing but an
        # explicit block_resource_types may install a route.
        bot, handler = self._handler_for(blocked_csv="", hosts_csv="")
        self.assertIsNone(handler, "no types -> no interception, so the cache survives")

    def test_hosts_alone_never_install_the_filter(self):
        # In Aug 2026 a non-empty block_hosts default started installing the
        # route and tripled the proxy bill (2.23 -> 6.90 MB/attempt) to save
        # ~12 MB/day. Hosts belong to the forwarder; they must never reach here.
        bot, handler = self._handler_for(blocked_csv="",
                                         hosts_csv=Bandwidth().block_hosts)
        self.assertIsNone(
            handler,
            "block_hosts must NOT install in-browser interception — it is "
            "enforced in the proxy forwarder precisely to keep the HTTP cache")

    def test_filter_ignores_hosts_entirely(self):
        # With types on, the filter is installed — but it must judge ONLY the
        # resource type. A denylisted host arriving as a script still goes
        # through here; the forwarder is what refuses it.
        bot, handler = self._handler_for(blocked_csv="image",
                                         hosts_csv="www.googletagmanager.com")
        r = _FakeRoute("script", "https://www.googletagmanager.com/gtm.js")
        handler(r)
        self.assertFalse(r.aborted, "host matching does not belong in the browser filter")
        self.assertTrue(r.continued)


class TestHostMatching(unittest.TestCase):
    """The denylist matcher and the host parser it is fed by. One implementation
    lives in proxy_forwarder — these are the only rules in the codebase."""

    def test_dest_host_forms(self):
        from src.utils.proxy_forwarder import dest_host

        cases = [
            # (method, target, head, expected)
            (b"CONNECT", b"visa.vfsglobal.com:443", b"", "visa.vfsglobal.com"),
            (b"CONNECT", b"[2606:4700::1]:443", b"", "2606:4700::1"),
            (b"GET", b"http://cdn.example.com/a.js", b"", "cdn.example.com"),
            (b"GET", b"http://cdn.example.com:8080/a.js", b"", "cdn.example.com"),
            # Origin-form: authority only exists in the Host header.
            (b"GET", b"/a.js", b"GET /a.js HTTP/1.1\r\nHost: cdn.example.com\r\n\r\n",
             "cdn.example.com"),
            # Case and the trailing FQDN dot must not defeat a denylist entry.
            (b"CONNECT", b"WWW.GoogleTagManager.COM.:443", b"", "www.googletagmanager.com"),
            (b"GET", b"/a.js", b"", ""),          # undeterminable -> never blocked
        ]
        for method, target, head, expected in cases:
            self.assertEqual(dest_host(method, target, head), expected,
                             f"{method!r} {target!r}")

    def test_exact_and_dotted_suffix_only(self):
        from src.utils.proxy_forwarder import host_blocked

        pats = {"facebook.net"}
        self.assertTrue(host_blocked("facebook.net", pats))
        self.assertTrue(host_blocked("connect.facebook.net", pats))
        # A lookalike must NOT match — that is why the suffix carries the dot.
        self.assertFalse(host_blocked("notfacebook.net", pats))
        self.assertFalse(host_blocked("facebook.net.evil.com", pats))
        self.assertFalse(host_blocked("", pats))
        self.assertFalse(host_blocked("facebook.net", set()))

    def test_default_denylist_spares_vfs_and_cloudflare(self):
        # The whole flow dies if either is ever refused, so prove the shipped
        # list leaves them alone — including the hosts we actually depend on.
        from src.utils.proxy_forwarder import host_blocked

        pats = Bandwidth().blocked_hosts
        for host in ("visa.vfsglobal.com", "lift-api.vfsglobal.com",
                     "liftassets.vfsglobal.com", "challenges.cloudflare.com"):
            self.assertFalse(host_blocked(host, pats), f"{host} must NEVER be refused")
        # ...while still catching what it is for.
        for host in ("www.googletagmanager.com", "connect.facebook.net",
                     "js-cdn.dynatrace.com"):
            self.assertTrue(host_blocked(host, pats), f"{host} should be refused")


class _StubUpstream:
    """Minimal CONNECT-speaking proxy that records how many times it was dialled."""

    def __init__(self):
        self.dials = 0
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        self._srv.settimeout(0.25)
        while not self._stop.is_set():
            try:
                c, _ = self._srv.accept()
            except (socket.timeout, OSError):
                continue
            self.dials += 1
            try:
                c.settimeout(2)
                c.recv(4096)
                c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            except OSError:
                pass
            finally:
                try:
                    c.close()
                except OSError:
                    pass

    def close(self):
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass


class TestForwarderHostBlocking(unittest.TestCase):
    """End to end over real sockets: a denylisted host must be refused BEFORE the
    metered upstream is dialled, which is the entire point of moving host
    blocking out of the browser."""

    def setUp(self):
        from src.utils.proxy_forwarder import ProxyForwarder

        self.up = _StubUpstream()
        self.fwd = ProxyForwarder("127.0.0.1", self.up.port, "u", "p",
                                  blocked_hosts={"js-cdn.dynatrace.com", "facebook.net"})
        self.port = self.fwd.start()
        self.addCleanup(self.up.close)
        self.addCleanup(self.fwd.stop)

    def _connect(self, authority):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            s.settimeout(5)
            s.sendall(f"CONNECT {authority} HTTP/1.1\r\n"
                      f"Host: {authority}\r\n\r\n".encode())
            return s.recv(4096)
        finally:
            s.close()

    def test_denylisted_host_is_refused_without_dialling_upstream(self):
        resp = self._connect("js-cdn.dynatrace.com:443")
        self.assertIn(b"403", resp.split(b"\r\n", 1)[0])
        self.assertEqual(self.up.dials, 0,
                         "the metered upstream must never be dialled for a blocked host")
        self.assertEqual(self.fwd.blocked_requests, 1)
        self.assertEqual(self.fwd.mb, 0.0, "a refused request must bill zero bytes")

    def test_subdomain_of_denylisted_host_is_refused(self):
        self._connect("connect.facebook.net:443")
        self.assertEqual(self.up.dials, 0)
        self.assertEqual(self.fwd.blocked_requests, 1)

    def test_allowed_host_reaches_upstream(self):
        resp = self._connect("visa.vfsglobal.com:443")
        self.assertIn(b"200", resp.split(b"\r\n", 1)[0])
        self.assertEqual(self.up.dials, 1)
        self.assertEqual(self.fwd.blocked_requests, 0)

    def test_lookalike_host_is_not_refused(self):
        self._connect("notfacebook.net:443")
        self.assertEqual(self.up.dials, 1, "only exact/dotted-suffix matches are blocked")
        self.assertEqual(self.fwd.blocked_requests, 0)


class TestForwarderWithoutDenylist(unittest.TestCase):
    """No denylist (the IP-probe forwarder in proxy_pool) must pass everything —
    a probe that cannot reach its geo-check API breaks proxy selection."""

    def test_everything_passes(self):
        from src.utils.proxy_forwarder import ProxyForwarder

        up = _StubUpstream()
        fwd = ProxyForwarder("127.0.0.1", up.port, "u", "p")
        port = fwd.start()
        self.addCleanup(up.close)
        self.addCleanup(fwd.stop)
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            s.settimeout(5)
            s.sendall(b"CONNECT js-cdn.dynatrace.com:443 HTTP/1.1\r\n\r\n")
            resp = s.recv(4096)
        finally:
            s.close()
        self.assertIn(b"200", resp.split(b"\r\n", 1)[0])
        self.assertEqual(up.dials, 1)
        self.assertEqual(fwd.blocked_requests, 0)


class TestPersistProfile(unittest.TestCase):
    """ChromeProcess profile selection: throwaway by default, per-account when
    persist_cache is on, and safe fallback when no account key is given."""

    def _chrome(self, persist, key):
        import src.settings as s
        from src.utils.chrome_launcher import ChromeProcess

        original = s._cached
        s._cached = s.Settings(bandwidth=Bandwidth(persist_cache=persist))
        try:
            return ChromeProcess(port=9222, profile_key=key)
        finally:
            s._cached = original

    def test_default_is_throwaway(self):
        from src.utils.chrome_launcher import PROFILE_PREFIX

        c = self._chrome(persist=False, key="gusal@travnook.com")
        self.assertFalse(c._persist)
        self.assertTrue(c._owns_profile, "throwaway profile must be deleted on close")
        # Named by PID, not by CDP port. This used to assert the dir ended in
        # "9222": the port and the profile name were the same value, so two
        # concurrent runs that both wanted 9222 also wanted the SAME profile
        # directory. The port is now chosen at start() and the profile is keyed
        # to the process, so what matters is that it is ours and distinctive.
        self.assertIn(PROFILE_PREFIX, c.profile_dir)
        self.assertTrue(c.profile_dir.endswith(f"pid{os.getpid()}"))

    def test_persist_is_per_account(self):
        a = self._chrome(persist=True, key="gusal@travnook.com")
        b = self._chrome(persist=True, key="zaid@travnook.com")
        self.assertTrue(a._persist)
        self.assertFalse(a._owns_profile, "persistent profile must NOT be deleted")
        self.assertIn("acct-gusal_travnook.com", a.profile_dir)
        # Different accounts get DIFFERENT dirs (no cf_clearance / cookie mixing).
        self.assertNotEqual(a.profile_dir, b.profile_dir)

    def test_persist_without_key_falls_back(self):
        c = self._chrome(persist=True, key=None)
        self.assertFalse(c._persist, "no account key -> never share one profile")
        self.assertTrue(c._owns_profile)


class TestEgressMarker(unittest.TestCase):
    """A persistent profile reused from a different egress IP must flag the change
    (so the bot drops the IP-bound cf_clearance) — this is what makes 'warm on
    local, run on proxy' safe."""

    def _proc(self, profile_dir, proxy):
        from src.utils.chrome_launcher import ChromeProcess

        c = ChromeProcess(profile_dir=profile_dir)
        c._persist = True          # force persist path for the test
        c.proxy = proxy
        c._mark_egress()
        return c

    def test_egress_change_detected(self):
        import shutil
        import tempfile

        d = os.path.join(tempfile.gettempdir(), "vfs-egress-unittest")
        shutil.rmtree(d, ignore_errors=True)
        try:
            self.assertFalse(self._proc(d, None).egress_changed,
                             "first use has no prior egress -> no change")
            self.assertTrue(self._proc(d, "http://u:p@1.2.3.4:8080").egress_changed,
                            "local -> proxy is a change (drop cf_clearance)")
            self.assertFalse(self._proc(d, "http://u:p@1.2.3.4:8080").egress_changed,
                             "same proxy again -> no change")
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
