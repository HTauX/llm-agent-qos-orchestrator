from __future__ import annotations

import configparser
import os
from functools import lru_cache
from typing import Any, Dict


def _default_config_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "config.ini"))


@lru_cache(maxsize=1)
def load_config() -> configparser.ConfigParser:
    """
    Load `qos-system-lg/config.ini` (can be overridden via the `QOS_CONFIG_PATH` environment variable).
    """

    cfg = configparser.ConfigParser(interpolation=None)
    path = os.getenv("QOS_CONFIG_PATH") or _default_config_path()
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        # Resolve relative paths against the qos-system-lg/ directory to avoid CWD dependency
        path = os.path.abspath(os.path.join(os.path.dirname(_default_config_path()), path))

    cfg.read(path, encoding="utf-8")
    return cfg


def get_str(section: str, key: str, *, fallback: str = "") -> str:
    cfg = load_config()
    if cfg.has_option(section, key):
        return (cfg.get(section, key) or "").strip()
    return fallback


def get_bool(section: str, key: str, *, fallback: bool = False) -> bool:
    cfg = load_config()
    if not cfg.has_option(section, key):
        return fallback
    try:
        return cfg.getboolean(section, key, fallback=fallback)
    except Exception:
        raw = (cfg.get(section, key) or "").strip().lower()
        if raw in {"1", "true", "yes", "y", "on"}:
            return True
        if raw in {"0", "false", "no", "n", "off"}:
            return False
        return fallback


def get_prompt(key: str, *, fallback: str) -> str:
    """
    Read a prompt from [prompts]; if not configured, return the fallback.
    """

    v = get_str("prompts", key, fallback="")
    return v if v else fallback


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:  # type: ignore[override]
        return "{" + key + "}"


def format_template(template: str, values: Dict[str, Any]) -> str:
    """
    Apply `str.format` to a string template, keeping missing fields unchanged (to avoid KeyError).
    """

    try:
        return template.format_map(_SafeDict({k: v for k, v in values.items()}))
    except Exception:
        return template
