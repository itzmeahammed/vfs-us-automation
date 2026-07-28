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
    slot_read_ms: int = 12000       # read the 'Earliest available slot' banner


class Retry(_Section):
    """Attempt/backoff knobs shared by the supervisor and the bot.

    (in-run relaunch count lives in AccountSafety.max_attempts, its historical home.)
    """

    backoff_seconds: int = 15             # pause between relaunch attempts
    max_ip_tries: int = 2                 # different IPs to try on a 403201 block
    turnstile_refresh_attempts: int = 2   # page reloads to unstick Turnstile
    turnstile_signin_retries: int = 2     # same-IP reload+re-solve on a rejected
                                          # (non-403201) login 403 before rotating IP
    session_refresh_attempts: int = 1     # login-URL refreshes on a 'Session Expired
                                          # or Invalid' page before failing fast
    dashboard_captcha_cycles: int = 3     # times to re-solve the post-Sign-In captcha
                                          # dialog before declaring a re-challenge loop
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
    max_attempts: int = 2                 # mirror of Retry.max_attempts (INI [account_safety])


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


class Bandwidth(_Section):
    """Metered-proxy savings. Every byte the browser fetches is billed by the
    proxy, so trim what a slot-check doesn't need. Defaults are safe for the
    Cloudflare Turnstile flow: only image/media/font are blocked (JS + CSS are
    KEPT — CSS drives the Turnstile checkbox position)."""

    log_usage: bool = True            # passive: log "Proxy traffic this route: X MB"
    # mute_chrome defaults ON: it only silences Chrome's OWN background phone-home
    # (Optimization Guide model ~35 MB/launch, component/safebrowsing updates,
    # sync, telemetry) — NONE of which touch page rendering or the Turnstile
    # widget, and which otherwise dominate the proxy bill. Turn OFF only to debug.
    mute_chrome: bool = True           # disable Chrome's background/phone-home traffic
    # block_resource_types defaults EMPTY (OFF): blocking image/media/font DOES
    # risk the Cloudflare Turnstile solve (shifts the coordinate-clicked checkbox
    # + trips CF heuristics), so opt in via config only after re-verifying it.
    block_resource_types: str = ""    # Playwright resource types to abort (comma list)
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
