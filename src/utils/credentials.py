"""Multiple VFS credentials with hour-based rotation.

You can run the bot under several VFS accounts and rotate them by the clock hour,
so a different account is used each hour (spreading load / avoiding rate limits).

Accounts live in a dedicated, gitignored file `config/credentials.local.ini`:

    [cred1]
    email = a@example.com
    password = your-password-1

    [cred2]
    email = b@example.com
    password = your-password-2
    ; ... add as many [credN] sections as you like, in order

Rotation: the active account is chosen by the clock hour, offset so the FIRST
account is used at the first run hour of the day (06:00):

    index = (hour - START_HOUR) % number_of_accounts

so cred1 -> 06:00, cred2 -> 07:00, ... wrapping after N hours. Both runs within
an hour (e.g. :29 and :59) use the same account.

If the credentials file is absent or empty, this falls back to the single
[vfs-credential] account in config.ini / config.local.ini — so existing setups
keep working unchanged.
"""

import configparser
import logging
import os

from src.utils.config_reader import get_config_value

# The hour (local time) of the first scheduled run of the day. cred1 maps here.
START_HOUR = 6

CREDENTIALS_FILE = os.path.join("config", "credentials.local.ini")


def _mask(email: str) -> str:
    """Masks an email for logging: 'melicent@web.net' -> 'me***@web.net'."""
    if not email or "@" not in email:
        return "***"
    name, domain = email.split("@", 1)
    head = name[:2] if len(name) > 2 else name[:1]
    return f"{head}***@{domain}"


def _load_pool() -> list:
    """
    Reads all [credN] sections from the credentials file, in file order.

    Returns a list of (email, password) tuples, or [] if the file is missing,
    unreadable, or has no usable credentials.
    """
    if not os.path.isfile(CREDENTIALS_FILE):
        return []
    parser = configparser.ConfigParser()
    try:
        parser.read(CREDENTIALS_FILE)
    except configparser.Error as e:
        logging.warning(f"Could not parse {CREDENTIALS_FILE}: {e}")
        return []

    pool = []
    for section in parser.sections():
        email = parser.get(section, "email", fallback="").strip()
        pwd = parser.get(section, "password", fallback="").strip()
        if email and pwd:
            pool.append((email, pwd))
    return pool


def get_credential(hour: int) -> tuple:
    """
    Returns the (email, password) to use for the given clock `hour` (0-23).

    Rotates through the credentials pool by hour. Falls back to the single
    [vfs-credential] account when no pool file is configured. Also logs which
    account is active (email masked).

    Returns:
        (email, password). Either may be None if nothing is configured at all.
    """
    pool = _load_pool()

    if pool:
        idx = (hour - START_HOUR) % len(pool)
        email, pwd = pool[idx]
        logging.info(
            f"Using credential #{idx + 1}/{len(pool)} for hour {hour:02d}: {_mask(email)}"
        )
        return email, pwd

    # Fallback: the original single account.
    email = get_config_value("vfs-credential", "email")
    pwd = get_config_value("vfs-credential", "password")
    logging.info(f"Using single [vfs-credential] account: {_mask(email or '')}")
    return email, pwd


def active_account(hour: int) -> str:
    """
    Returns a masked, human-friendly label for the account active at `hour`, for
    display in the run summary — e.g. 'pa***@travnook.com (cred 7/10)', or just
    'me***@web.net' when falling back to the single [vfs-credential] account.
    """
    pool = _load_pool()
    if pool:
        idx = (hour - START_HOUR) % len(pool)
        return f"{_mask(pool[idx][0])} (cred {idx + 1}/{len(pool)})"
    email = get_config_value("vfs-credential", "email") or ""
    return _mask(email)


def rotation_schedule() -> list:
    """
    Returns a preview of which account is used each active hour (06:00-23:00),
    as a list of (hour, index, masked_email). For verifying the rotation.
    """
    pool = _load_pool()
    out = []
    for hour in range(START_HOUR, 24):
        if pool:
            idx = (hour - START_HOUR) % len(pool)
            out.append((hour, idx + 1, _mask(pool[idx][0])))
        else:
            email = get_config_value("vfs-credential", "email") or ""
            out.append((hour, 1, _mask(email)))
    return out
