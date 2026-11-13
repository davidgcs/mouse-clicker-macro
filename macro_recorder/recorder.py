"""Captures mouse movement, clicks and scrolling from the whole screen."""

from __future__ import annotations

import time
from typing import Callable

from pynput import mouse

from .recording import BUTTONS, Event


class Recorder:
    #: Moves closer together than this (in seconds) are dropped to keep files small.
    MIN_MOVE_INTERVAL = 0.005
    #: How long start() waits for the listener thread to get going.
    STARTUP_TIMEOUT = 1.0

    def __init__(self, clock: Callable[[], float] = time.perf_counter, listener_factory=mouse.Listener):
        self._clock = clock
        self._listener_factory = listener_factory
        self._listener = None
        self._events: list[Event] = []
        self._started = 0.0
        self._last_move = float("-inf")

    @property
    def event_count(self) -> int:
        return len(self._events)

    def start(self) -> None:
        self._events = []
        self._started = self._clock()
        self._last_move = float("-inf")
        self._listener = self._listener_factory(
            on_move=self._on_move, on_click=self._on_click, on_scroll=self._on_scroll
        )
        self._listener.start()
        # pynput ignores stop() until the listener thread is running. A listener that missed
        # its stop would go on adding its events to the next recording, so wait for it here.
        give_up = time.monotonic() + self.STARTUP_TIMEOUT
        while not self._listener.running and time.monotonic() < give_up:
            time.sleep(0.001)

    def stop(self) -> list[Event]:
        """Stop capturing; returns the raw events, timed from the moment start() was called."""
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        return list(self._events)

    # The callbacks below run on pynput's listener thread.

    def _add(self, now: float, kind: str, x: float, y: float, **fields) -> None:
        self._events.append(Event(now - self._started, kind, round(x), round(y), **fields))

    def _on_move(self, x, y) -> None:
        now = self._clock()
        if now - self._last_move >= self.MIN_MOVE_INTERVAL:
            self._last_move = now
            self._add(now, "move", x, y)

    def _on_click(self, x, y, button, pressed) -> None:
        name = getattr(button, "name", "")
        if name in BUTTONS:  # side buttons and unknown buttons are not supported
            self._add(self._clock(), "click", x, y, button=name, pressed=bool(pressed))

    def _on_scroll(self, x, y, dx, dy) -> None:
        self._add(self._clock(), "scroll", x, y, dx=round(dx), dy=round(dy))
