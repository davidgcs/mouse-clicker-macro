"""Saves the chosen keyboard shortcuts between runs."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .hotkeys import is_valid_name

DEFAULT_HOTKEYS = {"record": "f1", "stop": "f2"}


def config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "MouseMacroRecorder"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MouseMacroRecorder"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "mouse-macro-recorder"


def settings_path() -> Path:
    return config_dir() / "settings.json"


def load_hotkeys(path: Path | None = None) -> dict[str, str]:
    """The saved shortcuts, or the defaults when nothing usable has been saved."""
    try:
        data = json.loads((path or settings_path()).read_text(encoding="utf-8"))
        saved = {action: data["hotkeys"][action] for action in DEFAULT_HOTKEYS}
    except (OSError, ValueError, RecursionError, KeyError, TypeError):
        return dict(DEFAULT_HOTKEYS)
    if not all(is_valid_name(name) for name in saved.values()) or len(set(saved.values())) < len(saved):
        return dict(DEFAULT_HOTKEYS)
    return saved


def save_hotkeys(keys: dict[str, str], path: Path | None = None) -> None:
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hotkeys": keys}, indent=2) + "\n", encoding="utf-8")
