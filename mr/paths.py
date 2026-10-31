"""Config and data directories. The environment is read on each call."""
from __future__ import annotations

import os


def _set(name: str) -> str | None:
    """A set environment variable with a leading ~ expanded. Missing or empty is unset."""
    val = os.environ.get(name)
    if val is None or val == "":
        return None
    return os.path.expanduser(val)


def config_dir() -> str:
    """$XDG_CONFIG_HOME/mr, or ~/.config/mr when that variable is unset."""
    base = _set("XDG_CONFIG_HOME")
    if base is None:
        return os.path.join(os.path.expanduser("~"), ".config", "mr")
    return os.path.join(base, "mr")


def data_dir() -> str:
    """$MR_HOME, else $XDG_DATA_HOME/mr, else ~/.local/share/mr.

    A set MR_HOME is returned as given (after ~ expansion) with no extra mr segment.
    """
    home = _set("MR_HOME")
    if home is not None:
        return home
    base = _set("XDG_DATA_HOME")
    if base is None:
        return os.path.join(os.path.expanduser("~"), ".local", "share", "mr")
    return os.path.join(base, "mr")


def default_config_path() -> str:
    """config_dir() / routes.yaml."""
    return os.path.join(config_dir(), "routes.yaml")
