"""Launch and own a real Chrome process with remote debugging (CDP).

The slot-check flow attaches to a real, headed Chrome over CDP (Cloudflare blocks
headless/automation browsers). On EC2 the bot must OWN that Chrome's lifecycle so
that every hourly run launches a fresh browser and — crucially — KILLS it on the
way out, success or failure. Leaking Chrome processes on an hourly cron quickly
exhausts memory on a small instance.

`ChromeProcess` is a context manager:

    with ChromeProcess(port=9222, url=VFS_URL) as chrome:
        ...  # attach to chrome.cdp_url via Playwright
    # Chrome (and its whole process tree) is guaranteed dead here.

It is cross-platform (Windows for local dev, Linux for EC2) so the same code runs
in both places; on EC2 it runs under Xvfb (a virtual display) so "headed" Chrome
has somewhere to render.
"""

import logging
import os
import platform
import re
import shutil
import subprocess
import time
import urllib.request


# Every profile dir we create starts with this prefix (under TEMP / tmp). Used
# to find & clean up leftovers from previous runs WITHOUT touching the user's
# normal Chrome, which uses a different user-data-dir.
PROFILE_PREFIX = "vfs-chrome-profile-"


def _profile_base() -> str:
    return os.environ.get("TEMP") or "/tmp"


def _port_is_free(port: int) -> bool:
    """True if nothing is listening on `port` on loopback."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def resolve_port(preferred: int) -> int:
    """The CDP port this run should use: `preferred` if free, else a free one.

    A FIXED port is what stopped a slot check and a waitlist run from existing at
    the same time — both read [retry] cdp_port (9222) and the second Chrome could
    not bind it. Falling back to an OS-assigned free port makes concurrent runs
    possible while keeping 9222 for the common single-run case, so an operator
    attaching a debugger by hand still finds it where the config says.

    There is a small window between testing a port and Chrome binding it. Losing
    that race costs one failed launch, which the supervisor already retries — it
    is not a correctness problem, and it is far rarer than the guaranteed clash
    a hard-coded port produced.
    """
    import socket

    if preferred and _port_is_free(preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))          # 0 = let the OS pick a free one
        chosen = s.getsockname()[1]
    logging.info(
        f"CDP port {preferred} is busy (another run?) — using {chosen} instead."
    )
    return chosen


def _live_bot_profile_dirs() -> list:
    """user-data-dirs of bot Chrome processes that are RUNNING right now.

    Used to avoid deleting a profile another concurrent run is using.
    Best-effort: on any failure it returns [] and the caller falls back to its
    previous behaviour.
    """
    dirs = []
    try:
        if platform.system() == "Windows":
            ps = (
                "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                "ForEach-Object { $_.CommandLine }"
            )
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=30,
            ).stdout
        else:
            out = subprocess.run(
                ["ps", "-eo", "args"], capture_output=True, text=True, timeout=30,
            ).stdout
        for line in (out or "").splitlines():
            m = re.search(r"--user-data-dir=([^\s\"]+)", line)
            if m and PROFILE_PREFIX in m.group(1):
                dirs.append(os.path.normpath(m.group(1)))
    except Exception as e:
        logging.debug(f"_live_bot_profile_dirs (ignored): {e}")
    return dirs


def _mask_egress(egress: str) -> str:
    """Human label for an egress identity (host:port or 'local') without leaking
    the proxy's user:pass credentials into the log."""
    if not egress or egress == "local":
        return egress or "?"
    try:
        from urllib.parse import urlparse
        u = urlparse(egress if "://" in egress else "http://" + egress)
        return f"{u.hostname}:{u.port}" if u.hostname else "proxy"
    except Exception:
        return "proxy"


def _split_proxy(url: str):
    """(scheme, host, port, user, password) from a proxy URL; parts may be ''."""
    from urllib.parse import urlparse
    if url and "://" not in url:
        url = "http://" + url
    p = urlparse(url or "")
    return (p.scheme or "http"), p.hostname, p.port, (p.username or ""), (p.password or "")


def kill_stale_bot_chrome(profile_dir: str = None) -> None:
    """
    Best-effort: kill Chrome left over from a PREVIOUS bot run.

    Matches ONLY Chrome processes launched with our PROFILE_PREFIX user-data-dir,
    so the user's normal browsing Chrome is never touched. Never raises — this is
    cleanup, not the critical path.

    SCOPE. With `profile_dir`, only Chrome using THAT profile is killed. Every
    launch calls this, so the unscoped form meant starting a waitlist run killed
    the slot bot's live Chrome mid-check (and the reverse) — the two could not
    run at the same time no matter what the locking said. Scoping it to one
    profile keeps the real purpose (a crashed run holding a lock on the profile
    we are about to reuse) while leaving other runs alone.

    The unscoped form remains for explicit "clean up everything" callers.
    """
    try:
        if profile_dir:
            target = os.path.normpath(profile_dir)
            match_win = target.replace("'", "''")
            pattern = target
        else:
            match_win = PROFILE_PREFIX
            pattern = PROFILE_PREFIX

        if platform.system() == "Windows":
            # CIM query: chrome.exe whose command line references the profile dir.
            ps = (
                "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                f"Where-Object {{ $_.CommandLine -like '*{match_win}*' }} | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                "-ErrorAction SilentlyContinue }"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
            )
        else:
            # -f matches against the full command line; our profile prefix is
            # distinctive enough not to hit the user's Chrome.
            subprocess.run(
                ["pkill", "-f", pattern],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
            )
    except Exception as e:
        logging.debug(f"kill_stale_bot_chrome (ignored): {e}")


def remove_stale_profiles(keep: str = None) -> None:
    """
    Best-effort: delete leftover bot profile dirs (PROFILE_PREFIX*) under TEMP so
    disk isn't slowly filled by crashed runs. Never raises.

    Profiles belonging to a Chrome that is RUNNING are skipped, as is `keep` (the
    caller's own dir). Without that, a starting run deleted the profile directory
    of a concurrent run — Chrome does not tolerate its user-data-dir vanishing
    underneath it, so the other run died in a way that looked like a VFS problem.
    On Windows an in-use dir would usually survive via ignore_errors, but on
    Linux (EC2) the delete succeeds and takes the live run down with it.
    """
    base = _profile_base()
    protected = {os.path.normpath(p) for p in _live_bot_profile_dirs()}
    if keep:
        protected.add(os.path.normpath(keep))
    try:
        for name in os.listdir(base):
            if not name.startswith(PROFILE_PREFIX):
                continue
            path = os.path.join(base, name)
            if os.path.normpath(path) in protected:
                logging.debug(f"Keeping in-use bot profile: {name}")
                continue
            shutil.rmtree(path, ignore_errors=True)
    except Exception as e:
        logging.debug(f"remove_stale_profiles (ignored): {e}")


def _find_chrome() -> str:
    """
    Locates a Google Chrome / Chromium executable for the current OS.

    Returns the path, or raises FileNotFoundError if none is found. Honors the
    CHROME_PATH env var first so deployments can pin an exact binary.
    """
    env_path = os.environ.get("CHROME_PATH")
    if env_path and os.path.exists(env_path):
        return env_path

    candidates = []
    system = platform.system()
    if system == "Windows":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    else:  # Linux (EC2) / macOS
        # Prefer names on PATH, then common absolute locations.
        for name in (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
        ):
            found = shutil.which(name)
            if found:
                return found
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]

    for path in candidates:
        if path and os.path.exists(path):
            return path

    raise FileNotFoundError(
        "Chrome/Chromium not found. Install Google Chrome, or set CHROME_PATH to "
        "its executable."
    )


class ChromeProcess:
    """
    Launches a real Chrome with `--remote-debugging-port` and owns its lifecycle.

    Args:
        port: CDP port to expose (default 9222).
        url: initial URL to open (the VFS login page).
        profile_dir: dedicated user-data-dir; isolated from your normal Chrome and
            required for the debugging port to be honored. Defaults to a temp dir.
        startup_timeout_s: how long to wait for the CDP endpoint to come up.
    """

    def __init__(self, port=9222, url=None, profile_dir=None, startup_timeout_s=30,
                 proxy=None, profile_key=None):
        # `port` is a PREFERENCE, not a reservation: if another run already holds
        # it, start() takes a free one instead of failing to launch. This is what
        # lets a slot check and a waitlist registration run at the same time.
        #
        # Resolved in start(), NOT here. Resolving at construction time meant two
        # objects built before either launched both saw 9222 free and both chose
        # it — the second Chrome then could not bind. Deciding immediately before
        # the exec shrinks that window to milliseconds, and start() retries on a
        # fresh port if we still lose the race.
        self._preferred_port = port
        self.port = port
        self.url = url
        self.startup_timeout_s = startup_timeout_s
        self.proxy = proxy  # full URL; user:pass runs through a local forwarder
        self._proc = None
        self._forwarder = None  # local auth-injecting forwarder, if the proxy needs it
        # Per-route proxy-traffic report lines, collected at close() and emitted by
        # the caller AFTER the route's outcome is logged (so bandwidth accounting
        # never appears above the pass/fail line it belongs to).
        self.traffic_lines = []
        # Set by _mark_egress(): True when a persistent profile is reused from a
        # DIFFERENT egress IP than last time (so its IP-bound cf_clearance must go).
        self.egress_changed = False

        # Bandwidth: opt-in persistent cache (settings [bandwidth] persist_cache).
        try:
            from src.settings import settings
            bw = settings().bandwidth
            persist = bool(bw.persist_cache)
            self._cache_mb = int(bw.cache_size_mb)
        except Exception:
            persist, self._cache_mb = False, 128
        # Persist only when a per-ACCOUNT key is supplied — never share ONE profile
        # across accounts: cf_clearance is IP-bound (each account has its own pinned
        # IP) and cookies would cross-contaminate accounts.
        self._persist = persist and bool(profile_key)

        if profile_dir:
            # Caller owns it — don't delete on close.
            self.profile_dir = profile_dir
            self._owns_profile = False
        elif self._persist:
            # Per-account profile KEPT across runs, so that account's static assets
            # (JS/CSS/fonts) and its own cf_clearance are served from disk cache
            # instead of re-fetched through the metered proxy. VFS *session* cookies
            # are still cleared each run (src.vfs_bot.session.clear_site_session),
            # which keeps cf_clearance but avoids the stale "Session Expired" page.
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(profile_key))[:64]
            self.profile_dir = os.path.join(_profile_base(), f"{PROFILE_PREFIX}acct-{safe}")
            self._owns_profile = False  # keep it — this is the whole point
        else:
            # Default: throwaway dir, DELETED on close. Rationale: on a limited-disk
            # PC, leaving Chrome profiles in TEMP across many runs is unwanted
            # residue; on a residential IP Turnstile auto-passes each run so
            # persisting cf_clearance isn't required.
            #
            # Named by PID, not by port. The port is no longer known here (start()
            # picks it), and tying the two together meant two concurrent runs that
            # wanted the same port also wanted the same profile directory.
            self.profile_dir = os.path.join(
                _profile_base(), f"{PROFILE_PREFIX}pid{os.getpid()}")
            self._owns_profile = True  # ours — delete on close (no residue)

    @property
    def cdp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _mark_egress(self) -> None:
        """Detect whether this persistent profile is being reused from a DIFFERENT
        egress IP than last time, and record the current egress for next time.

        If it changed (e.g. a cache warmed on the local IP is now run through a
        proxy), sets self.egress_changed so the bot drops the IP-bound cf_clearance
        while keeping the HTTP asset cache. No-op for throwaway profiles.
        """
        self.egress_changed = False
        if not self._persist:
            return
        marker = os.path.join(self.profile_dir, ".vfs_egress")
        current = self.proxy or "local"
        try:
            os.makedirs(self.profile_dir, exist_ok=True)
            prev = None
            if os.path.isfile(marker):
                with open(marker, "r", encoding="utf-8") as f:
                    prev = f.read().strip()
            if prev and prev != current:
                self.egress_changed = True
                logging.info(
                    f"Profile egress changed ({_mask_egress(prev)} -> "
                    f"{_mask_egress(current)}) — cf_clearance will be dropped, "
                    "HTTP cache kept."
                )
            with open(marker, "w", encoding="utf-8") as f:
                f.write(current)
        except Exception as e:
            logging.debug(f"egress marker check failed (ignored): {e}")

    def start(self) -> "ChromeProcess":
        # Clean slate before launching, but SCOPED TO THIS RUN. Kill only Chrome
        # holding OUR profile dir (a crashed previous run using the same account
        # would still own its lock), and never delete a profile another live run
        # is using. The unscoped versions killed and deleted across every bot
        # Chrome on the machine, which made two concurrent runs impossible.
        # Only WIPE profile dirs in throwaway mode — in persist mode the
        # per-account dirs are the cache we want to keep.
        kill_stale_bot_chrome(self.profile_dir)
        if not self._persist:
            remove_stale_profiles(keep=self.profile_dir)

        # Note the egress IP for this (persistent) profile so the bot can drop a
        # stale, IP-bound cf_clearance if the profile was last used from another IP.
        self._mark_egress()

        chrome = _find_chrome()
        args = [
            chrome,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            # Needed when running as root / in many EC2 setups.
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ]

        if self._persist:
            # Cap the on-disk HTTP cache so a kept profile can't grow without bound.
            args.append(f"--disk-cache-size={max(1, self._cache_mb) * 1024 * 1024}")

        # Bandwidth: silence Chrome's OWN background/phone-home traffic — which the
        # per-host proxy breakdown proved is ~90% of the bill (Chrome downloading
        # from *.googleapis.com / googleusercontent.com, NOT from VFS). The worst
        # offender is the Optimization Guide ML model from
        # optimizationguide-pa.googleapis.com (tens of MB) — it never finishes
        # before we kill Chrome, so it re-downloads on EVERY launch. The
        # --disable-features list below stops it (and the other phone-homes) at the
        # source. None of these affect page rendering or the Turnstile widget.
        try:
            from src.settings import settings
            bw = settings().bandwidth
        except Exception:
            bw = None
        if bw and bw.mute_chrome:
            muted_features = (
                "OptimizationHints,OptimizationGuideModelDownloading,"
                "OptimizationTargetPrediction,OptimizationHintsFetching,"
                "Translate,MediaRouter,InterestFeedContentSuggestions,"
                "DownloadBubble,PasswordLeakDetection,AutofillServerCommunication,"
                "CalculateNativeWinOcclusion"
            )
            args += [
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-domain-reliability",
                "--disable-sync",
                "--disable-client-side-phishing-detection",
                "--safebrowsing-disable-auto-update",
                "--disable-features=" + muted_features,
                "--no-pings",
                "--metrics-recording-only",
                "--disable-breakpad",
            ]
            logging.debug("Bandwidth: mute_chrome ON — Google background traffic "
                          "disabled (incl. Optimization Guide model download).")

        if self.proxy:
            # Route ALL of Chrome's traffic through this proxy so VFS sees the
            # residential IP. Chrome's --proxy-server IGNORES user:pass, so if the
            # proxy carries credentials we run it through a tiny LOCAL forwarder
            # (proxy_forwarder) that injects the auth, and point Chrome at that
            # auth-less local port instead.
            proxy_arg = self.proxy
            scheme, host, port_, user, pw = _split_proxy(self.proxy)
            if user and pw and host and port_:
                from src.utils.proxy_forwarder import ProxyForwarder
                # Host blocking is enforced HERE, not in the browser: the
                # forwarder refuses a denylisted host before the metered
                # upstream is dialled, and unlike a Playwright route it costs no
                # HTTP cache. Denylisted bytes are never billed at all.
                deny = bw.blocked_hosts if bw else None
                self._forwarder = ProxyForwarder(host, port_, user, pw,
                                                 scheme=scheme, blocked_hosts=deny)
                local_port = self._forwarder.start()
                proxy_arg = f"http://127.0.0.1:{local_port}"
                logging.debug(
                    f"Chrome proxy via local forwarder :{local_port} -> "
                    f"{scheme}://{host}:{port_} "
                    f"({len(deny or ())} host(s) denylisted)"
                )
            else:
                logging.debug(f"Chrome routing through proxy: {host}:{port_}")
            args.append(f"--proxy-server={proxy_arg}")

        # With in-browser resource blocking enabled, open a BLANK tab so the first
        # real page load is the bot's own (intercepted) navigation — nothing loads
        # through the metered proxy before Playwright attaches its request filter.
        if bw and bw.blocked_types:
            args.append("about:blank")
        elif self.url:
            args.append(self.url)

        # Own a process group so we can kill the whole tree (renderers, GPU proc).
        popen_kwargs = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if platform.system() == "Windows":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True  # setsid → own process group

        # Pick the port HERE, as late as possible, and retry on a different one if
        # another run took it in between. Two concurrent runs racing for 9222 is
        # the normal case now, not an exotic one, so losing the race must cost a
        # relaunch rather than the whole run.
        port_flag = next(
            i for i, a in enumerate(args)
            if str(a).startswith("--remote-debugging-port=")
        )
        last_error = None
        for attempt in range(1, 4):
            # Attempt 1 prefers the configured port; later ones take any free port.
            self.port = resolve_port(self._preferred_port if attempt == 1 else 0)
            args[port_flag] = f"--remote-debugging-port={self.port}"
            logging.debug(f"Launching Chrome (CDP :{self.port}) — {chrome}")
            self._proc = subprocess.Popen(args, **popen_kwargs)
            try:
                self._wait_for_cdp()
                return self
            except RuntimeError as e:
                last_error = e
                try:
                    self._proc.kill()
                except Exception:
                    pass
                if attempt == 3:
                    break
                logging.warning(
                    f"Chrome did not come up on port {self.port} "
                    f"(attempt {attempt}/3) — retrying on another port: {e}"
                )
        raise RuntimeError(
            f"Chrome would not start after 3 attempts: {last_error}"
        )

    def _wait_for_cdp(self) -> None:
        """Polls the CDP /json/version endpoint until it answers or we time out."""
        deadline = time.time() + self.startup_timeout_s
        last_err = None
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"Chrome exited early (code {self._proc.returncode}) before CDP came up."
                )
            try:
                with urllib.request.urlopen(
                    f"{self.cdp_url}/json/version", timeout=2
                ) as resp:
                    if resp.status == 200:
                        logging.debug(f"Chrome CDP ready on {self.cdp_url}")
                        return
            except Exception as e:
                last_err = e
            time.sleep(0.5)
        raise RuntimeError(
            f"Chrome CDP endpoint never came up on {self.cdp_url} "
            f"within {self.startup_timeout_s}s (last error: {last_err})"
        )

    def close(self) -> None:
        """
        Kills Chrome and its entire process tree. Safe to call more than once.
        Also stops the local proxy forwarder, if one was started.
        """
        if self._forwarder:
            # Capture the billed byte count + per-host breakdown BEFORE stopping,
            # then report this route's proxy usage (only the real browser run logs
            # this — the short-lived IP-probe forwarder stays silent).
            fwd = self._forwarder
            fwd_mb = getattr(fwd, "mb", 0.0)
            fwd_blocked = getattr(fwd, "blocked_requests", 0)
            try:
                hosts = fwd.top_hosts(6)
            except Exception:
                hosts = []
            try:
                fwd.stop()
            except Exception:
                pass
            self._forwarder = None
            try:
                from src.settings import settings
                if settings().bandwidth.log_usage and fwd_mb:
                    # Collect (don't log yet) — the caller emits these AFTER the
                    # route's pass/fail line so traffic never sits above it.
                    self.traffic_lines.append(f"Proxy traffic this route: {fwd_mb:.1f} MB")
                    # Where the bytes went — reveals Chrome background (google/
                    # gstatic/safebrowsing) vs Cloudflare vs VFS.
                    for host, nbytes in hosts:
                        self.traffic_lines.append(
                            f"    {nbytes / (1024 * 1024):6.1f} MB  {host}")
                    if fwd_blocked:
                        # Refused at the tunnel, so these never reached the
                        # upstream and are absent from the MB above.
                        self.traffic_lines.append(
                            f"Denylisted hosts refused this route: {fwd_blocked} "
                            "request(s) — never dialled upstream.")
            except Exception:
                pass
        if not self._proc:
            return
        if self._proc.poll() is not None:
            self._proc = None
            return

        logging.debug("Closing Chrome (killing process tree)...")
        try:
            if platform.system() == "Windows":
                # /T kills the whole tree, /F forces it.
                subprocess.run(
                    ["taskkill", "/PID", str(self._proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                import signal

                # Kill the whole process group (we created one via setsid).
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                # Give it a moment, then SIGKILL anything left.
                try:
                    self._proc.wait(timeout=8)
                except Exception:
                    try:
                        os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        except Exception as e:
            logging.warning(f"Error while killing Chrome: {e}")
        finally:
            self._proc = None
            # Remove the throwaway profile so no state survives to the next run
            # (and /tmp doesn't fill up over many hourly runs).
            if getattr(self, "_owns_profile", False):
                shutil.rmtree(self.profile_dir, ignore_errors=True)
            logging.debug("Chrome closed.")

    def __enter__(self) -> "ChromeProcess":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
