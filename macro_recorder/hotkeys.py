"""Global keyboard shortcuts that work while any other application has the focus."""

from __future__ import annotations

import time
from typing import Callable

from pynput import keyboard

#: A key pressed again within this many seconds of its last press (and not released in
#: between) is the keyboard auto-repeating, not the user pressing it again.
REPEAT_WINDOW = 1.0


def key_name(key) -> str | None:
    """A stable name for a pynput key that can be saved: "f1", "esc", "a" or "vk:96"."""
    if isinstance(key, keyboard.Key):
        return key.name
    char = getattr(key, "char", None)
    if char and char.isprintable():
        return char.lower()
    vk = getattr(key, "vk", None)
    return f"vk:{vk}" if vk is not None else None


def is_valid_name(name) -> bool:
    """True if name is something key_name() can produce on this platform."""
    if not isinstance(name, str):
        return False
    if name.startswith("vk:"):
        return name[3:].isdigit()
    return name in keyboard.Key.__members__ or (len(name) == 1 and name.isprintable())


def key_label(name: str) -> str:
    """How a key name is shown to the user: "F1", "Esc", "Page Down", "A"."""
    if name.startswith("vk:"):
        return f"Key {name[3:]}"
    if len(name) == 1:
        return name.upper()
    return name.replace("_", " ").title()


class Hotkeys:
    """Calls on_action(action) when the key assigned to an action is pressed.

    `keys` maps action names to key names, e.g. {"record": "f1", "stop": "f2"}. The
    keys are not consumed, other applications still receive them. on_action runs on
    pynput's listener thread, so it must only hand the action over to another thread.
    """

    def __init__(
        self,
        keys: dict[str, str],
        on_action: Callable[[str], None],
        listener_factory=keyboard.Listener,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._actions = self._by_key(keys)
        self._on_action = on_action
        self._listener_factory = listener_factory
        self._clock = clock
        self._listener = None
        self._capture: Callable[[str], None] | None = None
        self._last_press: dict[str, float] = {}

    @staticmethod
    def _by_key(keys: dict[str, str]) -> dict[str, str]:
        return {name: action for action, name in keys.items()}

    def set_keys(self, keys: dict[str, str]) -> None:
        self._actions = self._by_key(keys)

    def capture_next(self, callback: Callable[[str], None]) -> None:
        """Hand the name of the next key pressed to callback instead of triggering an action."""
        self._capture = callback

    def cancel_capture(self) -> None:
        self._capture = None

    def start(self) -> None:
        if self._listener is None:
            self._listener = self._listener_factory(on_press=self._on_press, on_release=self._on_release)
            self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    def _on_press(self, key) -> None:
        name = key_name(key)
        if name is None:
            return
        now = self._clock()
        repeated = now - self._last_press.get(name, float("-inf")) < REPEAT_WINDOW
        self._last_press[name] = now
        if repeated:
            return
        capture = self._capture
        if capture is not None:
            self._capture = None
            capture(name)
            return
        action = self._actions.get(name)
        if action is not None:
            self._on_action(action)

    def _on_release(self, key) -> None:
        self._last_press.pop(key_name(key), None)
