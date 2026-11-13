"""The macro data model and its JSON file format."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

FORMAT = "mouse-macro"
VERSION = 1
BUTTONS = ("left", "middle", "right")
KINDS = ("move", "click", "scroll")
# Limits that stop a hand-edited file from breaking playback: a wait longer than about 49 days
# overflows what Windows can sleep for, and no screen layout reaches a million pixels.
MAX_SECONDS = 1_000_000
MAX_DISTANCE = 1_000_000  # coordinates in pixels, scroll amounts in steps


class RecordingError(Exception):
    """A macro file could not be read or is not a valid macro."""


def _number(data: dict, key: str, limit: int) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number")
    if not -limit <= value <= limit:  # also false for nan, infinity and integers too big for a float
        raise ValueError(f"{key} is out of range")
    return value


@dataclass(frozen=True)
class Event:
    t: float  # seconds since the macro started
    kind: str  # "move", "click" or "scroll"
    x: int
    y: int
    button: str = ""  # click only: "left", "middle" or "right"
    pressed: bool = False  # click only: True when the button goes down
    dx: int = 0  # scroll only
    dy: int = 0  # scroll only

    def to_dict(self) -> dict:
        data = {"t": self.t, "type": self.kind, "x": self.x, "y": self.y}
        if self.kind == "click":
            data.update(button=self.button, pressed=self.pressed)
        elif self.kind == "scroll":
            data.update(dx=self.dx, dy=self.dy)
        return data

    @classmethod
    def from_dict(cls, data) -> Event:
        """Build an event from parsed JSON; raises ValueError describing what is wrong."""
        if not isinstance(data, dict):
            raise ValueError("must be an object")
        kind = data.get("type")
        if kind not in KINDS:
            raise ValueError(f"unknown type {kind!r}")
        t = _number(data, "t", MAX_SECONDS)
        if t < 0:
            raise ValueError("t must not be negative")
        fields = {
            "t": t,
            "kind": kind,
            "x": round(_number(data, "x", MAX_DISTANCE)),
            "y": round(_number(data, "y", MAX_DISTANCE)),
        }
        if kind == "click":
            button, pressed = data.get("button"), data.get("pressed")
            if button not in BUTTONS:
                raise ValueError(f"unknown button {button!r}")
            if not isinstance(pressed, bool):
                raise ValueError("pressed must be true or false")
            fields.update(button=button, pressed=pressed)
        elif kind == "scroll":
            fields.update(dx=round(_number(data, "dx", MAX_DISTANCE)), dy=round(_number(data, "dy", MAX_DISTANCE)))
        return cls(**fields)


def trim_tail(events: list[Event], area: tuple[int, int, int, int]) -> list[Event]:
    """Drop the trailing events that are inside area, given as (left, top, right, bottom)."""
    left, top, right, bottom = area
    end = len(events)
    while end and left <= events[end - 1].x <= right and top <= events[end - 1].y <= bottom:
        end -= 1
    return events[:end]


@dataclass
class Recording:
    events: list[Event]
    created: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def duration(self) -> float:
        return self.events[-1].t if self.events else 0.0

    @classmethod
    def from_capture(cls, events: list[Event], ignore_area: tuple[int, int, int, int] | None = None) -> Recording:
        """Turn raw recorder output into a macro.

        Trailing events inside ignore_area (the app's own window, when recording was
        stopped with its button) are dropped, and time restarts at the first event so
        a macro never begins or ends with dead time.
        """
        if ignore_area:
            events = trim_tail(events, ignore_area)
        if events:
            start = events[0].t
            events = [replace(event, t=round(event.t - start, 4)) for event in events]
        return cls(events)

    def to_json(self) -> str:
        header = {"format": FORMAT, "version": VERSION, "created": self.created}
        head = json.dumps(header, indent=2)[:-2]  # without the closing "\n}"
        rows = ",\n".join(f"    {json.dumps(event.to_dict())}" for event in self.events)
        return f'{head},\n  "events": [\n{rows}\n  ]\n}}\n'

    @classmethod
    def from_json(cls, text: str) -> Recording:
        try:
            data = json.loads(text)
        except (ValueError, RecursionError) as exc:
            raise RecordingError(f"not valid JSON ({exc})") from exc
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise RecordingError("this is not a mouse macro file")
        version = data.get("version")
        if isinstance(version, int) and version > VERSION:
            raise RecordingError("it was created by a newer version of this app")
        raw_events = data.get("events")
        if not isinstance(raw_events, list):
            raise RecordingError("the file has no list of events")
        events = []
        for number, raw in enumerate(raw_events, start=1):
            try:
                events.append(Event.from_dict(raw))
            except ValueError as exc:
                raise RecordingError(f"event {number}: {exc}") from exc
        if not events:
            raise RecordingError("the macro contains no events")
        events.sort(key=lambda event: event.t)
        created = data.get("created")
        return cls(events, created if isinstance(created, str) else "")

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Recording:
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RecordingError(f"Could not read {path.name}: {exc}") from exc
        try:
            return cls.from_json(text)
        except RecordingError as exc:
            raise RecordingError(f"{path.name}: {exc}") from exc


def new_macro_path(directory: Path, now: datetime | None = None) -> Path:
    """A timestamped file name inside directory that does not exist yet."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    path = Path(directory) / f"macro-{stamp}.json"
    counter = 2
    while path.exists():
        path = Path(directory) / f"macro-{stamp}-{counter}.json"
        counter += 1
    return path
