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


class ProxyForwarder:
    def __init__(self, up_host: str, up_port: int, username: str, password: str,
                 bind_host: str = "127.0.0.1"):
        self.up_host = up_host
        self.up_port = int(up_port)
        self._auth = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.bind_host = bind_host
        self._srv = None
        self._thread = None
        self._stop = threading.Event()
        self.port = None

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

            if method == b"CONNECT":
                # HTTPS: open the tunnel to the upstream proxy WITH auth, then pipe.
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
            else:
                # Plain HTTP proxy request: inject Proxy-Authorization, forward as-is.
                rest = head.split(b"\r\n", 1)[1]
                up.sendall(first_line + b"\r\n"
                           + b"Proxy-Authorization: Basic " + auth + b"\r\n" + rest)
                self._pipe(client, up)
        except Exception as e:
            logging.debug(f"Proxy forwarder connection error: {e}")
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
                dst = b if s is a else a
                try:
                    dst.sendall(data)
                except OSError:
                    return

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._srv:
                self._srv.close()
        except OSError:
            pass
