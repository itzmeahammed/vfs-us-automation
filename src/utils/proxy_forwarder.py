"""A tiny local proxy that adds authentication to an upstream HTTP proxy.

Chrome's --proxy-server flag ignores user:pass credentials, so we cannot point
Chrome straight at an authenticated proxy. This forwarder bridges the gap:

    Chrome  ->  127.0.0.1:<local>  (this forwarder, no auth)  ->  upstream proxy
                                    injects Proxy-Authorization  (user:pass@host:port)

It is transparent: for HTTPS it tunnels the CONNECT to the upstream (adding the
auth header once), then pipes raw bytes — so Cloudflare/Turnstile see a normal
TLS connection egressing from the residential IP. Pure stdlib; no dependencies.

Lifecycle is owned by ChromeProcess: one forwarder per browser, started before
Chrome and stopped when Chrome is killed (routes run one at a time, so there is
only ever one forwarder on one local port).
"""

import base64
import logging
import select
import socket
import threading

_BUF = 65536

# Running total of bytes tunnelled across ALL forwarders this process (i.e. the
# whole supervisor run — every route). Lets run_all_routes report a single
# "proxy traffic this run" figure. Reset per process start (fresh import).
_session_bytes = 0
_session_lock = threading.Lock()


def session_mb() -> float:
    """Total proxy bytes tunnelled so far this process, in MB."""
    return _session_bytes / (1024 * 1024)


def reset_session_bytes() -> None:
    global _session_bytes
    with _session_lock:
        _session_bytes = 0


class ProxyForwarder:
    def __init__(self, up_host: str, up_port: int, username: str, password: str,
                 bind_host: str = "127.0.0.1", scheme: str = "http"):
        self.up_host = up_host
        self.up_port = int(up_port)
        self._user = username or ""
        self._pw = password or ""
        self._auth = base64.b64encode(f"{username}:{password}".encode()).decode()
        # Upstream protocol: 'http' (HTTP CONNECT) or 'socks5'/'socks' (SOCKS5).
        self.scheme = (scheme or "http").lower()
        self.bind_host = bind_host
        self._srv = None
        self._thread = None
        self._stop = threading.Event()
        self.port = None
        self._err_logged = False  # log the first upstream error only (avoid spam)
        self._bytes = 0           # bytes tunnelled through THIS forwarder (billed)
        self._bytes_lock = threading.Lock()

    @staticmethod
    def _recvn(sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            c = sock.recv(n - len(buf))
            if not c:
                break
            buf += c
        return buf

    def _socks5_connect(self, up: socket.socket, target: bytes) -> None:
        """SOCKS5 handshake to the upstream (with username/password auth) for
        CONNECT to `target` (b'host:port'). Raises on failure."""
        host, _, port = target.partition(b":")
        host = host.decode()
        port = int(port or b"443")
        # Greeting: offer no-auth (0) and username/password (2).
        up.sendall(b"\x05\x02\x00\x02")
        r = self._recvn(up, 2)
        if len(r) < 2 or r[0] != 5:
            raise OSError("socks5 greeting failed")
        method = r[1]
        if method == 0x02:                     # username/password (RFC 1929)
            u, p = self._user.encode(), self._pw.encode()
            up.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            a = self._recvn(up, 2)
            if len(a) < 2 or a[1] != 0:
                raise OSError("socks5 auth rejected")
        elif method != 0x00:                   # 0 = no auth needed
            raise OSError(f"socks5 no acceptable auth method ({method})")
        # CONNECT command, address type 3 (domain name).
        hb = host.encode()
        up.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + port.to_bytes(2, "big"))
        rep = self._recvn(up, 4)
        if len(rep) < 4 or rep[1] != 0:
            raise OSError(f"socks5 connect rejected (rep {rep[1] if len(rep) > 1 else '?'})")
        atyp = rep[3]                           # consume the bound-address that follows
        if atyp == 1:
            self._recvn(up, 4 + 2)
        elif atyp == 3:
            ln = self._recvn(up, 1)
            self._recvn(up, (ln[0] if ln else 0) + 2)
        elif atyp == 4:
            self._recvn(up, 16 + 2)

    def start(self) -> int:
        """Bind an ephemeral local port and serve in the background. Returns the port."""
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self.bind_host, 0))
        self._srv.listen(64)
        self.port = self._srv.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        logging.debug(
            f"Proxy forwarder up on {self.bind_host}:{self.port} -> "
            f"{self.up_host}:{self.up_port}"
        )
        return self.port

    def _serve(self) -> None:
        self._srv.settimeout(0.5)
        while not self._stop.is_set():
            try:
                client, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        up = None
        try:
            client.settimeout(60)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(_BUF)
                if not chunk:
                    return
                head += chunk
                if len(head) > 1024 * 1024:
                    return
            first_line = head.split(b"\r\n", 1)[0]
            parts = first_line.split(b" ")
            if len(parts) < 2:
                return
            method, target = parts[0].upper(), parts[1]

            up = socket.create_connection((self.up_host, self.up_port), timeout=30)
            auth = self._auth.encode()
            is_socks = self.scheme.startswith("socks")

            if method == b"CONNECT":
                if is_socks:
                    # SOCKS5: authenticate + CONNECT to the target, then pipe.
                    try:
                        self._socks5_connect(up, target)
                    except Exception as e:
                        logging.debug(f"socks5 connect failed: {e}")
                        client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                        return
                    client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    self._pipe(client, up)
                    return
                # HTTP upstream: open the tunnel WITH auth, then pipe.
                up.sendall(
                    b"CONNECT " + target + b" HTTP/1.1\r\n"
                    b"Host: " + target + b"\r\n"
                    b"Proxy-Authorization: Basic " + auth + b"\r\n"
                    b"Proxy-Connection: Keep-Alive\r\n\r\n"
                )
                resp = b""
                while b"\r\n\r\n" not in resp:
                    c = up.recv(_BUF)
                    if not c:
                        break
                    resp += c
                status_line = resp.split(b"\r\n", 1)[0]
                if b" 200 " in status_line:
                    client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    self._pipe(client, up)
                else:
                    client.sendall(resp)  # forward the upstream error (e.g. 407)
            elif is_socks:
                # Plain HTTP over a SOCKS upstream is uncommon (VFS is all HTTPS);
                # not supported — the browser will just retry over HTTPS.
                client.sendall(b"HTTP/1.1 501 Not Implemented\r\n\r\n")
            else:
                # Plain HTTP proxy request: inject Proxy-Authorization, forward as-is.
                rest = head.split(b"\r\n", 1)[1]
                up.sendall(first_line + b"\r\n"
                           + b"Proxy-Authorization: Basic " + auth + b"\r\n" + rest)
                self._pipe(client, up)
        except Exception as e:
            # Log ONCE per forwarder (Chrome opens many connections; a refused/
            # broken upstream would otherwise spam dozens of identical lines).
            if not self._err_logged:
                self._err_logged = True
                logging.warning(
                    f"Proxy forwarder: upstream {self.scheme}://{self.up_host}:"
                    f"{self.up_port} error: {e} (further errors suppressed)")
        finally:
            for s in (client, up):
                try:
                    if s:
                        s.close()
                except OSError:
                    pass

    def _pipe(self, a: socket.socket, b: socket.socket) -> None:
        a.setblocking(False)
        b.setblocking(False)
        socks = [a, b]
        while not self._stop.is_set():
            try:
                r, _, x = select.select(socks, [], socks, 1)
            except (OSError, ValueError):
                return
            if x:
                return
            for s in r:
                try:
                    data = s.recv(_BUF)
                except BlockingIOError:
                    continue
                except OSError:
                    return
                if not data:
                    return
                # Meter it: every byte tunnelled here is billed by the proxy.
                with self._bytes_lock:
                    self._bytes += len(data)
                dst = b if s is a else a
                try:
                    dst.sendall(data)
                except OSError:
                    return

    @property
    def mb(self) -> float:
        """MB tunnelled through this forwarder (billed proxy traffic)."""
        return self._bytes / (1024 * 1024)

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._srv:
                self._srv.close()
        except OSError:
            pass
        # Roll this forwarder's tally into the process-wide session total. The
        # per-route human-readable log is emitted by ChromeProcess (which knows
        # THIS forwarder is the real browser run, not a short-lived IP probe).
        global _session_bytes
        with _session_lock:
            _session_bytes += self._bytes
