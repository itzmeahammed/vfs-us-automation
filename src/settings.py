"""Typed, validated application settings.

A single source of truth for every *tunable* knob in the bot. Values are read,
type-checked and range-checked once at startup; a bad value fails fast with a
clear pydantic error instead of the old scattered `int(str(...))` try/except
fallbacks that silently swallowed typos.

Sources, HIGHEST priority first:
  1. constructor kwargs        (used by tests)
  2. VFSCFG_* environment vars  (e.g. VFSCFG_TIMEOUTS__PAGE_LOAD_MS=45000)
  3. the merged config/*.ini    (via config_reader — your existing .ini files)
  4. the defaults below         (each EQUALS the value previously hardcoded in
                                 code, so behaviour is unchanged until you tune it)

Deliberately NOT here (they stay on config_reader.get_config_value):
  * secrets — Telegram tokens, OpenAI key (no need to type/validate a secret)
  * browser.cdp_url — injected at runtime by the supervisor, not user config
The VFSCFG_ env prefix is chosen so it can never collide with the existing
VFS_PROXY / LOG_LEVEL / BROWSER_ACTIVITY_LOG one-shot env overrides.
"""

from typing import Any, Dict, Optional, Tuple, Type

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from src.utils.config_reader import get_config_section, initialize_config


class _Section(BaseModel):
    # Extra keys in an INI section (e.g. the runtime-injected browser.cdp_url)
    # are ignored rather than rejected, so unknown keys never crash startup.
    model_config = ConfigDict(extra="ignore")


class Timeouts(_Section):
    """Page-level waits (milliseconds). Defaults = the old inline values."""

    page_load_ms: int = 60000       # page.goto on the login URL
    login_wait_ms: int = 15000      # wait for the login form (poll loop) to appear —
                                    # short so a stuck/blank load fails fast and the
                                    # supervisor retries fresh (block/'Session
                                    # Expired' pages are caught in ~1.5s regardless)
    relogin_wait_ms: int = 45000    # wait for the form to REappear after a reload
                                    # (Turnstile/session refresh) — shorter than the
                                    # cold login_wait_ms: a reload that hasn't
                                    # rendered the form in ~45s is stuck, so bail
    dashboard_ms: int = 90000       # await dashboard while handling captcha
    dashboard_frozen_ms: int = 30000  # B: if the post-login loading spinner stays
                                    # stuck this long with no dashboard (a "solved
                                    # but frozen" session), bail early instead of
                                    # grinding the full dashboard_ms — saves ~60s
                                    # and lets a fresh IP/browser retry
    signin_settle_ms: int = 8000    # poll ceiling for the Sign In outcome (a 403
                                    # captured / URL moved / OTP field rendered /
                                    # captcha popped). Replaces a flat 2s sleep
                                    # that a proxied login-403 routinely beat
    login_bounce_grace_ms: int = 6000  # how long the login form may stay on
                                    # screen after Sign In before it counts as a
                                    # bounce rather than a submission in flight
    slot_read_ms: int = 12000       # read the 'Earliest available slot' banner


class Retry(_Section):
    """Attempt/backoff knobs shared by the supervisor and the bot.

    (in-run relaunch count lives in AccountSafety.max_attempts, its historical home.)
    """

    backoff_seconds: int = 15             # pause between relaunch attempts
    max_ip_tries: int = 3                 # fresh IPs to rotate through on a block /
                                          # Turnstile fail. Capped by max_attempts
                                          # too (rotation consumes an attempt), so
                                          # keep the two equal. Churn-safe: a failed
                                          # login isn't recorded as an account IP.
    turnstile_refresh_attempts: int = 1   # page reloads to unstick Turnstile (A:
                                          # a flagged IP won't un-flag on reload —
                                          # 1 retry then rotate, saves re-downloads)
    turnstile_signin_retries: int = 0     # same-IP re-login on a rejected post-Sign
                                          # -In token. 0 = rotate IP immediately —
                                          # each same-IP re-login ESCALATES CF's
                                          # challenge difficulty on that IP.
    session_refresh_attempts: int = 2     # recovery passes on a 'Session Expired
                                          # or Invalid' page before failing fast.
                                          # Raised from 1 now that each pass does
                                          # something DIFFERENT (clear the session
                                          # cookies, then also drop cf_clearance)
                                          # rather than re-requesting the same URL
                                          # with the same cookies that caused it
    dashboard_captcha_cycles: int = 3     # times to re-solve the post-Sign-In captcha
                                          # dialog before declaring a re-challenge loop
    max_page_loads_per_attempt: int = 6   # E: hard cap on navigations (goto/reload)
                                          # within ONE browser attempt — a login/
                                          # Turnstile loop can't spiral into a dozen
                                          # metered page loads; bail to relaunch fresh
    cdp_port: int = 9222                  # Chrome remote-debugging port


class Browser(_Section):
    type: str = "chromium"
    headless: bool = True
    screenshots_enabled: bool = False     # per-step screenshots (final one always taken)


class Turnstile(_Section):
    manual_wait_seconds: int = 0          # local headed debugging only (0 in prod)


class AccountSafety(_Section):
    hard_cooldown_hours: int = 24
    soft_cooldown_hours: int = 2
    fail_threshold: int = 3
    max_attempts: int = 3                 # in-run browser relaunches; also caps how
                                          # many fresh IPs a route rotates through
                                          # (rotation consumes one) — keep == max_ip_tries


class Schedule(_Section):
    runs_per_hour: int = 2
    start_hour: int = 9
    end_hour: int = 19


class Otp(_Section):
    imap_host: str = ""
    imap_port: int = 993
    search_text: str = ""
    timeout_seconds: int = 120
    poll_seconds: int = 5
    otp_length: int = 6
    read_attempts: int = 2   # OpenAI image-read retries to obtain a valid code
    # How many times to submit an OTP to VFS. On each rejection ("Please enter a
    # valid one time password") the image is re-read for a DIFFERENT code and
    # re-submitted. Keep small — each wrong submit counts toward VFS lockout.
    submit_attempts: int = 3


class Proxy(_Section):
    enabled: bool = True


class Logging(_Section):
    level: str = "INFO"
    browser_activity: bool = False


class Waitlist(_Section):
    """Waitlist detection (always on) and registration (opt-in, gated).

    cooldown_hours only affects the read-only NOTIFICATION. Everything else
    gates the mutating registration flow, and every one of those defaults to
    OFF/minimum — registering is never something that starts happening by
    accident after a config edit.
    """

    cooldown_hours: float = 2.0        # per-country notification rate limit

    # --- registration (mutates the VFS account) ---
    register_enabled: bool = False     # MASTER kill switch
    dry_run: bool = True               # walk the flow, stop before submitting
    max_per_run: int = Field(default=1, ge=0)
    max_per_day: int = Field(default=5, ge=0)
    # Telegram for REGISTRATION outcomes. OFF by default: this bot runs on
    # demand with you watching the terminal, so a message is redundant noise in
    # a chat whose value is that it only pings when the hourly checker finds
    # something. (The read-only 'waitlist available' notice is unaffected.)
    telegram_enabled: bool = False

    # --- which VFS account waitlist entries are created under ---
    # A waitlist entry belongs to the account that created it, so the account is
    # a deliberate choice, never the hourly slot-check rotation. See
    # src/waitlist/accounts.py. Secrets stay on config_reader (not typed here).
    #
    # One account may serve several clients — VFS's real limit is undocumented,
    # so both knobs below are configurable rather than assumed.
    # --- client identity documents (passport bio pages) ---
    # Retention, in days, for anything left behind by a crashed or abandoned
    # run. Documents are normally deleted the moment a registration is confirmed
    # (journal.update_status); this sweep is the backstop that makes "we delete
    # after use" true rather than aspirational. 0 disables it.
    # documents_root itself stays on config_reader — it is a path, not a tunable.
    document_retention_days: int = Field(default=30, ge=0)

    max_clients_per_account: int = Field(default=0, ge=0)   # 0 = unlimited
    # Whether an account may hold TWO entries for the SAME combination. Default
    # False = warn and proceed: we do not yet know it is a problem, and blocking
    # wrongly is worse than a warning. Flip to True once VFS's behaviour is known.
    one_client_per_account_combo: bool = False


class Bandwidth(_Section):
    """Metered-proxy savings. Every byte the browser fetches is billed by the
    proxy, so trim what a slot-check doesn't need. Defaults are safe for the
    Cloudflare Turnstile flow: only image/media/font are blocked (JS + CSS are
    KEPT — CSS drives the Turnstile checkbox position)."""

    log_usage: bool = True            # passive: log "Proxy traffic this route: X MB"
    # daily_cap_mb: ACTIVE ceiling on billed proxy traffic per calendar day.
    # Once today's total reaches it, the supervisor pauses the remaining routes
    # (status PAUSED — not a failure, no account is struck) and every later run
    # today exits immediately. Checked BETWEEN routes, never mid-route, so the
    # day can end one route over. 0 disables the cap. See bandwidth_budget.py.
    daily_cap_mb: int = 800
    # warn_at_percent: send ONE Telegram warning per day the first time usage
    # crosses this share of the cap — the point is to hear about a regression at
    # lunchtime, not from the invoice. 0 disables the warning.
    warn_at_percent: int = 60
    # mute_chrome defaults ON: it only silences Chrome's OWN background phone-home
    # (Optimization Guide model ~35 MB/launch, component/safebrowsing updates,
    # sync, telemetry) — NONE of which touch page rendering or the Turnstile
    # widget, and which otherwise dominate the proxy bill. Turn OFF only to debug.
    mute_chrome: bool = True           # disable Chrome's background/phone-home traffic
    # block_resource_types defaults EMPTY (OFF) and should STAY that way unless a
    # measurement says otherwise. Two costs, not one:
    #   1. It is the only setting that installs a Playwright route, and request
    #      interception makes Chromium bypass its HTTP disk cache for the whole
    #      run — measured at ~4.7 MB per route-attempt of VFS bundles that
    #      persist_cache would otherwise serve for free. Blocking a few hundred
    #      KB of images does not come close to paying that back.
    #   2. Blocking image/media/font risks the Turnstile solve (shifts the
    #      coordinate-clicked checkbox + trips CF heuristics).
    block_resource_types: str = ""    # Playwright resource types to abort (comma list)
    # block_hosts: third-party HOSTS to refuse outright (analytics / marketing /
    # telemetry the slot-check never needs). Enforced in the PROXY FORWARDER
    # (src/utils/proxy_forwarder.py), not in the browser: it drops the request
    # before the metered upstream is dialled, so those bytes are never billed —
    # and it costs no HTTP cache, unlike block_resource_types above. Matched by
    # exact host or dotted-suffix (so 'facebook.net' also blocks
    # 'connect.facebook.net' but never 'notfacebook.net').
    # Applies to PROXIED runs only; on a direct connection nothing is metered.
    # NEVER add challenges.cloudflare.com / *.vfsglobal.com here (login + app).
    block_hosts: str = (
        "www.googletagmanager.com,connect.facebook.net,www.facebook.com,"
        "js-cdn.dynatrace.com,passwordsleakcheck-pa.googleapis.com,"
        "csp-reporting.cloudflare.com,static.cloudflareinsights.com,"
        "sctauditing-pa.googleapis.com,www.clarity.ms,"
        "googleads.g.doubleclick.net,analytics.google.com,"
        "www.google-analytics.com,gemini.gstatic.com"
    )
    # Persist a PER-ACCOUNT browser profile across runs so static JS/CSS/fonts (and
    # that account's cf_clearance) are served from disk cache instead of re-fetched
    # through the metered proxy. Off by default — verify Turnstile with a live run
    # before enabling on the scheduler. Cache is per account (never shared: cf_clearance
    # is IP-bound and cookies would cross-contaminate accounts).
    persist_cache: bool = False
    cache_size_mb: int = 128          # disk-cache cap PER account profile

    @property
    def blocked_types(self) -> set:
        return {
            t.strip().lower()
            for t in self.block_resource_types.split(",")
            if t.strip()
        }

    @property
    def blocked_hosts(self) -> set:
        return {
            h.strip().lower()
            for h in self.block_hosts.split(",")
            if h.strip()
        }


# INI section name -> Settings field name (identical today, explicit for safety).
_INI_SECTIONS = {
    "timeouts": "timeouts",
    "retry": "retry",
    "browser": "browser",
    "turnstile": "turnstile",
    "account_safety": "account_safety",
    "schedule": "schedule",
    "otp": "otp",
    "proxy": "proxy",
    "logging": "logging",
    "bandwidth": "bandwidth",
    "waitlist": "waitlist",
}


class _IniSource(PydanticBaseSettingsSource):
    """A pydantic-settings source that reads the merged config/*.ini.

    It reuses config_reader (which layers config.ini + *.local.ini +
    VFS_BOT_CONFIG_PATH), so all existing config files keep working unchanged.
    """

    def get_field_value(self, field, field_name) -> Tuple[Any, str, bool]:
        # Whole sections are supplied via __call__, so per-field lookup is unused.
        return None, field_name, False

    def __call__(self) -> Dict[str, Any]:
        initialize_config()  # idempotent — guarantees the INI is loaded
        data: Dict[str, Any] = {}
        for ini_name, field_name in _INI_SECTIONS.items():
            section = get_config_section(ini_name)
            if not section:
                continue
            # Drop blank values so an empty INI key can't clobber a real default.
            cleaned = {k: v for k, v in section.items() if str(v) != ""}
            if cleaned:
                data[field_name] = cleaned
        return data


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VFSCFG_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    timeouts: Timeouts = Field(default_factory=Timeouts)
    retry: Retry = Field(default_factory=Retry)
    browser: Browser = Field(default_factory=Browser)
    turnstile: Turnstile = Field(default_factory=Turnstile)
    account_safety: AccountSafety = Field(default_factory=AccountSafety)
    schedule: Schedule = Field(default_factory=Schedule)
    otp: Otp = Field(default_factory=Otp)
    proxy: Proxy = Field(default_factory=Proxy)
    logging: Logging = Field(default_factory=Logging)
    bandwidth: Bandwidth = Field(default_factory=Bandwidth)
    waitlist: Waitlist = Field(default_factory=Waitlist)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> Tuple[PydanticBaseSettingsSource, ...]:
        # Priority: init kwargs > VFSCFG_ env > merged INI > field defaults.
        return (init_settings, env_settings, _IniSource(settings_cls), file_secret_settings)


_cached: Optional[Settings] = None


def settings() -> Settings:
    """Return the process-wide settings, building (and caching) them on first use."""
    global _cached
    if _cached is None:
        _cached = Settings()
    return _cached


def reload_settings() -> Settings:
    """Rebuild settings from scratch (e.g. after tests mutate config/env)."""
    global _cached
    _cached = None
    return settings()
