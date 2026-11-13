"""Replays a macro once or in a loop."""

from __future__ import annotations

import math
import threading
import time
from contextlib import ExitStack
from typing import Callable

from pynput import mouse

from .recording import Event, Recording

# Presses closer than this in time and distance count as a double (or triple) click.
DOUBLE_CLICK_SECONDS = 0.5
DOUBLE_CLICK_PIXELS = 5
# Clicks and scrolls that follow a pointer jump wait this long (seconds) so the system
# has applied the move first; pynput reads the pointer position back when it clicks.
SETTLE_SECONDS = 0.01
# A player that can be paused looks at its pause flag this often (seconds).
PAUSE_TICK = 0.05


def parse_loop_settings(times_text: str, delay_text: str, unit: str) -> tuple[int | None, float]:
    """Validate the loop fields of the window.

    Returns (times, delay_seconds) where times is None for "repeat forever".
    Raises ValueError with a message that can be shown to the user.
    """
    times_text = times_text.strip()
    times = None
    if times_text:
        try:
            times = int(times_text)
        except ValueError:
            raise ValueError("Times must be a whole number, or empty to repeat forever.") from None
        if times < 1:
            raise ValueError("Times must be at least 1, or empty to repeat forever.")
    try:
        delay = float(delay_text.strip().replace(",", "."))
    except ValueError:
        raise ValueError("The delay must be a number.") from None
    if math.isnan(delay) or delay < 0:
        raise ValueError("The delay must be 0 or more.")
    seconds = delay * (60 if unit == "minutes" else 1)  # 1e308 minutes overflows to infinity
    if math.isinf(seconds):
        raise ValueError("The delay is too long.")
    return times, seconds


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} s"
    return f"{seconds // 60} min {seconds % 60:02d} s"


def _continues_click_series(previous: Event | None, event: Event) -> bool:
    return (
        previous is not None
        and previous.button == event.button
        and event.t - previous.t <= DOUBLE_CLICK_SECONDS
        and abs(event.x - previous.x) <= DOUBLE_CLICK_PIXELS
        and abs(event.y - previous.y) <= DOUBLE_CLICK_PIXELS
    )


class Player:
    def __init__(self, controller=None, clock: Callable[[], float] = time.perf_counter):
        self._mouse = controller if controller is not None else mouse.Controller()
        self._clock = clock
        self._buttons = {
            "left": mouse.Button.left,
            "middle": mouse.Button.middle,
            "right": mouse.Button.right,
        }

    def play_once(
        self,
        macro: Recording,
        stop: threading.Event,
        pause: threading.Event | None = None,
        on_pause: Callable[[bool], None] = lambda paused: None,
    ) -> bool:
        """Replay the macro once. Returns False if `stop` interrupted it.

        While `pause` is set the replay holds still, and it carries on from the same point once
        `pause` is cleared. A pause waits for a pressed mouse button to be released first, so the
        button is never left down while the user has the mouse back. on_pause(True) is called when
        the replay has really paused and on_pause(False) when it carries on.
        """
        start = self._clock()
        held: set[str] = set()
        last_press: Event | None = None
        pointer = None  # where this run last put the pointer
        # pynput groups the presses made inside a `with controller:` block into one
        # click series, which is what makes macOS see a double click instead of two clicks.
        series = ExitStack()
        try:
            for event in macro.events:
                while True:
                    if pause is not None and pause.is_set() and not held:
                        series.close()  # whatever is clicked after the pause is a new click
                        last_press = None
                        on_pause(True)
                        paused_for = self._hold(pause, stop)
                        if paused_for is None:
                            return False
                        start += paused_for  # the rest of the macro keeps its timing
                        pointer = None  # the user may have moved the mouse meanwhile
                        on_pause(False)
                    delay = start + event.t - self._clock()
                    if delay > 0:
                        stop.wait(delay if pause is None else min(delay, PAUSE_TICK))
                    if stop.is_set():
                        return False
                    if pause is None or delay <= 0:
                        break
                target = (event.x, event.y)
                self._mouse.position = target
                if event.kind != "move" and target != pointer and stop.wait(SETTLE_SECONDS):
                    return False
                pointer = target
                if event.kind == "click":
                    button = self._buttons[event.button]
                    if event.pressed:
                        if not _continues_click_series(last_press, event):
                            series.close()
                            series.enter_context(self._mouse)
                        last_press = event
                        held.add(event.button)
                        self._mouse.press(button)
                    else:
                        held.discard(event.button)
                        self._mouse.release(button)
                elif event.kind == "scroll":
                    self._mouse.scroll(event.dx, event.dy)
            return True
        finally:
            for name in held:  # never leave a button stuck down after stopping mid-click
                self._mouse.release(self._buttons[name])
            series.close()

    def run(
        self,
        macro: Recording,
        stop: threading.Event,
        times: int | None = 1,
        delay: float = 0.0,
        on_status: Callable[[str, str], None] = lambda headline, detail: None,
        pause: threading.Event | None = None,
    ) -> int:
        """Replay the macro `times` times (forever if None), waiting `delay` seconds between runs.

        The first run starts immediately and nothing waits after the last one.
        on_status(headline, detail) is told what is going on, e.g. ("Playing", "Run 2 of 10")
        or ("Waiting", "Run 3 of 10 starts in 25 s"). Returns how many runs were completed.

        While `pause` is set, the replay or the countdown to the next run stands still
        (on_status reports "Paused") and carries on where it was once `pause` is cleared.
        """
        completed = 0
        while times is None or completed < times:
            progress = f"Run {completed + 1}" + (f" of {times}" if times else "")
            if completed and not self._wait(delay, progress, stop, on_status, pause):
                break
            detail = "" if times == 1 else progress
            on_status("Playing", detail)
            if not self.play_once(
                macro, stop, pause, lambda paused: on_status("Paused" if paused else "Playing", detail)
            ):
                break
            completed += 1
        return completed

    def _hold(self, pause: threading.Event, stop: threading.Event) -> float | None:
        """Block while `pause` is set. Returns how long that took, or None if `stop` ended it."""
        began = self._clock()
        while pause.is_set():
            if stop.wait(PAUSE_TICK):
                return None
        return self._clock() - began

    def _wait(
        self,
        seconds: float,
        progress: str,
        stop: threading.Event,
        on_status: Callable[[str, str], None],
        pause: threading.Event | None = None,
    ) -> bool:
        """Sleep between runs, reporting the time left. Returns False if `stop` interrupted it.

        The time a pause lasts is not counted.
        """
        end = self._clock() + seconds
        reported = None
        while True:
            left = end - self._clock()
            if left <= 0:
                return not stop.is_set()
            message = f"{progress} starts in {format_duration(math.ceil(left))}"
            if pause is not None and pause.is_set():
                on_status("Paused", message)
                paused_for = self._hold(pause, stop)
                if paused_for is None:
                    return False
                end += paused_for
                reported = None
                continue
            if message != reported:
                on_status("Waiting", message)
                reported = message
            if stop.wait(min(left, 1.0 if pause is None else PAUSE_TICK)):
                return False
