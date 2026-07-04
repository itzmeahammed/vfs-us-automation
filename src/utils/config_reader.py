import os
from configparser import ConfigParser
from typing import Dict

_config: ConfigParser = None


def initialize_config(config_dir="config"):
    """
    Reads all INI configuration files in a directory and caches the result.
    Also reads user config from `VFS_BOT_CONFIG_PATH` env var (if set)

    Args:
        config_dir: The directory containing configuration files (default: "config").
    """
    global _config
    if not _config:
        _config = ConfigParser()
        names = [
            e.name for e in os.scandir(config_dir)
            if e.is_file() and e.name.endswith(".ini")
        ]
        # Read base configs first, then *.local.ini overrides LAST so their values
        # win (real secrets live in config.local.ini; config.ini holds blanks).
        names.sort(key=lambda n: (n.endswith(".local.ini"), n))
        for name in names:
            _config.read(os.path.join(config_dir, name))

    # Read user defined config file
    user_config_path = os.environ.get("VFS_BOT_CONFIG_PATH")
    if user_config_path:
        _config.read(user_config_path)


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
    if not _config.has_section(section):
        _config.add_section(section)
    _config[section][key] = value
