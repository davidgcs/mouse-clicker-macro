"""Saves the chosen keyboard shortcuts between runs."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .hotkeys import is_valid_name

DEFAULT_HOTKEYS = {"record": "f1", "stop": "f2", "loop": "f3", "pause": "f4"}
# Earlier versions saved only these two. A file without the others is an old file, not a broken one.
REQUIRED_HOTKEYS = ("record", "stop")


def config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "MouseMacroRecorder"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MouseMacroRecorder"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "mouse-macro-recorder"


def settings_path() -> Path:
    return config_dir() / "settings.json"


def load_hotkeys(path: Path | None = None) -> dict[str, str]:
    """The saved shortcuts, or the defaults when nothing usable has been saved.

    Shortcuts that were added after the file was written get their default key, or the first
    free function key when the user has already given that key to another action.
    """
    try:
        data = json.loads((path or settings_path()).read_text(encoding="utf-8"))
        stored = data["hotkeys"]
        saved = {action: stored[action] for action in REQUIRED_HOTKEYS}
        saved.update({action: stored[action] for action in DEFAULT_HOTKEYS if action in stored})
    except (OSError, ValueError, RecursionError, KeyError, TypeError):
        return dict(DEFAULT_HOTKEYS)
    if not all(is_valid_name(name) for name in saved.values()) or len(set(saved.values())) < len(saved):
        return dict(DEFAULT_HOTKEYS)
    taken = set(saved.values())
    for action, name in DEFAULT_HOTKEYS.items():
        if action not in saved:
            if name in taken:
                name = next(f"f{number}" for number in range(1, 13) if f"f{number}" not in taken)
            saved[action] = name
            taken.add(name)
    return {action: saved[action] for action in DEFAULT_HOTKEYS}


def save_hotkeys(keys: dict[str, str], path: Path | None = None) -> None:
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hotkeys": keys}, indent=2) + "\n", encoding="utf-8")
