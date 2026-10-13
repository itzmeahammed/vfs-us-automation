import os
import re
from configparser import ConfigParser
from typing import Dict, Optional, Set, Tuple

_config: ConfigParser = None
# The directory the config was loaded from, remembered so helpers that need the
# RAW files (e.g. commented_section_keys) can re-scan them.
_config_dir: str = "config"
# Values set with set_config_value(). They exist only in memory, so a reload
# from disk has to put them back or the bot loses the Chrome it attached to.
_runtime_overrides: Dict[Tuple[str, str], str] = {}
# What the files looked like when they were last read: (path, mtime, size) per
# file. reload_if_changed() compares against it.
_loaded_signature: Optional[Tuple] = None


def _config_files(config_dir: str) -> list:
    """Every file the config is built from, in the order it is read."""
    names = [
        e.name for e in os.scandir(config_dir)
        if e.is_file() and e.name.endswith(".ini")
    ]
    # Read base configs first, then *.local.ini overrides LAST so their values
    # win (real secrets live in config.local.ini; config.ini holds blanks).
    names.sort(key=lambda n: (n.endswith(".local.ini"), n))
    paths = [os.path.join(config_dir, name) for name in names]
    # Read user defined config file
    user_config_path = os.environ.get("VFS_BOT_CONFIG_PATH")
    if user_config_path:
        paths.append(user_config_path)
    return paths


def _signature(paths: list) -> Tuple:
    out = []
    for path in paths:
        try:
            st = os.stat(path)
            out.append((path, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((path, None, None))
    return tuple(out)


def _load(config_dir: str) -> None:
    """Build the config from disk and swap it in.

    The new parser is complete before it replaces the old one, so another
    thread never reads a half-loaded config, and a file that does not parse
    leaves the previous config in place.
    """
    global _config, _loaded_signature
    paths = _config_files(config_dir)
    # Recorded BEFORE parsing: a file that fails to parse is then reported once,
    # not on every reload_if_changed() until someone fixes it.
    _loaded_signature = _signature(paths)
    fresh = ConfigParser()
    for path in paths:
        fresh.read(path)
    for (section, key), value in _runtime_overrides.items():
        if not fresh.has_section(section):
            fresh.add_section(section)
        fresh[section][key] = value
    _config = fresh


def initialize_config(config_dir="config"):
    """
    Reads all INI configuration files in a directory and caches the result.
    Also reads user config from `VFS_BOT_CONFIG_PATH` env var (if set)

    Idempotent: once loaded, later calls change nothing, so a run keeps the
    values it started with. To pick up an edited file use reload_config().

    Args:
        config_dir: The directory containing configuration files (default: "config").
    """
    global _config_dir
    _config_dir = config_dir
    if _config is None:
        _load(config_dir)


def reload_config() -> None:
    """Re-read every config file from disk, replacing the cached values.

    For long-lived processes (the API) after a file changed. Values set with
    set_config_value() survive. Raises if a file does not parse; the previous
    config stays in use.
    """
    _load(_config_dir)


def reload_if_changed() -> bool:
    """reload_config() when a config file was edited, added or removed since
    the last load. Returns True when it reloaded."""
    if _config is None:
        return False
    if _signature(_config_files(_config_dir)) == _loaded_signature:
        return False
    reload_config()
    return True


def get_config_section(section: str, default: Dict = None) -> Dict:
    """
    Get a configuration section as a dictionary.

    Args:
        section: The name of the section to retrieve.
        default: A dictionary containing default values for the section (optional).

    Returns:
        A dictionary containing the configuration for the specified section,
        or the provided default dictionary if the section is not found.
    """
    if _config.has_section(section):
        return dict(_config[section])
    else:
        return default or {}


_SECTION_HEADER_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
_COMMENTED_KEY_RE = re.compile(r"^\s*[;#]\s*(?P<key>[A-Za-z0-9._-]+)\s*[=:]")


def commented_section_keys(section: str) -> Set[str]:
    """Return the keys that are COMMENTED OUT under [section], across every .ini
    file in the config dir (keys upper-cased).

    ConfigParser silently drops comment lines, so a deliberately-disabled entry
    like '; AE-FRA = ...' is invisible to get_config_section. This recovers those
    keys from the raw files so callers can tell 'intentionally disabled' from
    'absent / typo'. Best-effort: returns an empty set on any read error.
    """
    keys: Set[str] = set()
    target = section.strip().lower()
    try:
        names = [e.name for e in os.scandir(_config_dir)
                 if e.is_file() and e.name.endswith(".ini")]
    except OSError:
        return keys
    for name in names:
        current = None
        try:
            with open(os.path.join(_config_dir, name), encoding="utf-8") as f:
                for line in f:
                    header = _SECTION_HEADER_RE.match(line)
                    if header:
                        current = header.group("name").strip().lower()
                        continue
                    if current != target:
                        continue
                    ck = _COMMENTED_KEY_RE.match(line)
                    if ck:
                        keys.add(ck.group("key").upper())
        except OSError:
            continue
    return keys


def get_config_value(section: str, key: str, default: str = None) -> str:
    """
    Get a specific configuration value.

    Args:
        section: The name of the section containing the value.
        key: The name of the key to retrieve.
        default: The default value to return if the section or key is not found (optional).

    Returns:
        The value associated with the given key within the specified section,
        or the provided default value if the section or key does not exist.
    """
    if _config.has_section(section) and _config.has_option(section, key):
        return _config[section][key]
    else:
        return default


def set_config_value(section: str, key: str, value: str) -> None:
    """
    Sets a configuration value at runtime (in the in-memory config only).

    Used by the supervisor to point the bot at the Chrome it just launched —
    e.g. set_config_value("browser", "cdp_url", "http://127.0.0.1:9222") — so the
    bot attaches to the supervisor-owned browser without editing any files.
    """
    _runtime_overrides[(section, key)] = value
    if not _config.has_section(section):
        _config.add_section(section)
    _config[section][key] = value
