"""Open a URL in Chrome through a rotating residential proxy — standalone tool.

Every run picks the NEXT proxy from config/proxylist.txt (index 0,1,2,... wraps
back to 0 after the last one), so consecutive runs egress from different IPs. The
rotation position is remembered between runs in a small state file.

Chrome's --proxy-server ignores user:pass, so this script starts a tiny local
proxy that injects the auth and points Chrome at that (same trick the main bot
uses). The browser stays OPEN until you press Enter in this terminal.

Usage (PowerShell, from the project root):
    & .venv\\Scripts\\python.exe open_with_proxy.py "https://visa.vfsglobal.com/are/en/ita/login"

Options:
    --index N        use proxy N for THIS run (0-based) instead of auto-rotating
    --proxylist PATH proxy file (default: config/proxylist.txt)
    --chrome PATH    chrome.exe location (auto-detected if omitted)
    --no-probe       skip the egress-IP lookup (a bit faster to start)

Proxy file: one per line, either
    LOGIN:PASSWORD:HOST:PORT              (e.g. user:pass:res.proxy-seller.com:10000)
    http://LOGIN:PASSWORD@HOST:PORT
Lines starting with '#' are ignored. Pure stdlib; nothing else required.
"""

import argparse
import base64
import os
import select
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PROXYLIST = os.path.join(HERE, "config", "proxylist.txt")
STATE_FILE = os.path.join(HERE, ".proxy_run.state")
_BUF = 65536


# --------------------------------------------------------------------------- #
# Proxy list + rotation                                                        #
# --------------------------------------------------------------------------- #

def parse_proxy_line(line):
    """(user, password, host, port) from one proxy line, or None if unparseable."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:                                   # http://user:pass@host:port
        creds_host = line.split("://", 1)[1]
        if "@" in creds_host:
            creds, hostport = creds_host.rsplit("@", 1)
            user, _, pw = creds.partition(":")
        else:
            user, pw, hostport = "", "", creds_host
        host, _, port = hostport.partition(":")
        return (user, pw, host, port or "80")
    parts = line.split(":")
    if len(parts) == 4:                                 # LOGIN:PASSWORD:HOST:PORT
        user, pw, host, port = parts
        return (user, pw, host, port)
    if len(parts) == 2:                                 # HOST:PORT (no auth)
        host, port = parts
        return ("", "", host, port)
    return None


def load_proxies(path):
    if not os.path.exists(path):
        sys.exit(f"Proxy list not found: {path}")
    proxies = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            p = parse_proxy_line(line)
            if p:
                proxies.append(p)
    if not proxies:
        sys.exit(f"No usable proxies in {path}")
    return proxies


def next_index(count):
    """Return the next rotation index (0..count-1) and persist it for next time."""
    last = -1
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            last = int(f.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        last = -1
    idx = (last + 1) % count
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(str(idx))
    except OSError:
        pass
    return idx


# --------------------------------------------------------------------------- #
# Local auth-injecting forwarder (HTTP upstream: Chrome -> here -> proxy)       #
# --------------------------------------------------------------------------- #

class Forwarder:
    def __init__(self, host, port, user, pw):
        self.up_host, self.up_port = host, int(port)
        self._auth = base64.b64encode(f"{user}:{pw}".encode()).decode()
        self._srv = None
        self._stop = threading.Event()
        self.port = None

    def start(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(64)
        self.port = self._srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()
        return self.port

    def _serve(self):
        self._srv.settimeout(0.5)
        while not self._stop.is_set():
            try:
                client, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
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
            if method == b"CONNECT":                    # HTTPS tunnel
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
                if b" 200 " in resp.split(b"\r\n", 1)[0]:
                    client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    self._pipe(client, up)
                else:
                    client.sendall(resp)                # forward upstream error (e.g. 407)
            else:                                       # plain HTTP: inject auth, forward
                rest = head.split(b"\r\n", 1)[1]
                up.sendall(first_line + b"\r\n"
                           + b"Proxy-Authorization: Basic " + auth + b"\r\n" + rest)
                self._pipe(client, up)
        except Exception:
            pass
        finally:
            for s in (client, up):
                try:
                    if s:
                        s.close()
                except OSError:
                    pass

    def _pipe(self, a, b):
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
                try:
                    (b if s is a else a).sendall(data)
                except OSError:
                    return

    def stop(self):
        self._stop.set()
        try:
            if self._srv:
                self._srv.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Chrome + egress probe                                                         #
# --------------------------------------------------------------------------- #

def find_chrome(override=None):
    if override:
        return override
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    sys.exit("chrome.exe not found — pass --chrome <path>.")


def probe_egress(user, pw, host, port, timeout=15):
    """Return the exit IP through this proxy, or None. Uses urllib (auth in URL)."""
    proxy_url = f"http://{user}:{pw}@{host}:{port}"
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    )
    try:
        with opener.open("https://api.ipify.org", timeout=timeout) as resp:
            return resp.read().decode().strip()
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="Open a URL in Chrome via a rotating proxy.")
    ap.add_argument("url", help="URL to open in Chrome")
    ap.add_argument("--index", type=int, default=None,
                    help="use proxy N this run (0-based) instead of auto-rotating")
    ap.add_argument("--proxylist", default=DEFAULT_PROXYLIST)
    ap.add_argument("--chrome", default=None, help="path to chrome.exe")
    ap.add_argument("--no-probe", action="store_true", help="skip egress-IP lookup")
    args = ap.parse_args()

    proxies = load_proxies(args.proxylist)
    n = len(proxies)
    idx = (args.index % n) if args.index is not None else next_index(n)
    user, pw, host, port = proxies[idx]

    print(f"Proxy {idx}/{n - 1}:  {host}:{port}")
    if not args.no_probe:
        ip = probe_egress(user, pw, host, port)
        print(f"Egress IP:   {ip or 'PROBE FAILED (proxy may be down)'}")

    fwd = Forwarder(host, port, user, pw)
    local_port = fwd.start()
    print(f"Forwarder:   127.0.0.1:{local_port} -> {host}:{port}")

    profile_dir = tempfile.mkdtemp(prefix="chrome-proxy-run-")
    chrome = find_chrome(args.chrome)
    proc = subprocess.Popen([
        chrome,
        f"--proxy-server=127.0.0.1:{local_port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        args.url,
    ])
    print(f"\nChrome launched (PID {proc.pid}) -> {args.url}")
    print(">>> Press Enter here to close the browser and exit. <<<")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass

    # Close Chrome (whole process tree) and stop the forwarder.
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    except Exception:
        proc.terminate()
    fwd.stop()
    import shutil
    shutil.rmtree(profile_dir, ignore_errors=True)
    print("Closed.")


if __name__ == "__main__":
    main()
