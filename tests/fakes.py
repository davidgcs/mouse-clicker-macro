"""Stand-ins for the clock, mouse, keyboard and listeners, so tests never touch the real ones."""

import threading


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeStop(threading.Event):
    """An Event whose wait() moves a FakeClock forward instead of sleeping.

    It sets itself once the clock reaches `stop_at`, like a user pressing the stop key.
    `schedule(at, callback)` does the same for any other key, e.g. `pause.set` or `pause.clear`.
    """

    def __init__(self, clock, stop_at=None):
        super().__init__()
        self.clock = clock
        self.stop_at = stop_at
        self.waits = []
        self.scheduled = []

    def schedule(self, at, callback):
        self.scheduled.append((at, callback))

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.clock.now += timeout
        due = sorted((entry for entry in self.scheduled if self.clock.now >= entry[0]), key=lambda entry: entry[0])
        for entry in due:
            self.scheduled.remove(entry)
            entry[1]()
        if self.stop_at is not None and self.clock.now >= self.stop_at:
            self.set()
        return self.is_set()


class FakeMouse:
    """Logs what a Player does. Mimics pynput's macOS click-state counting inside `with`."""

    def __init__(self, clock=None):
        self.clock = clock
        self.log = []
        self.entered = 0
        self.exited = 0
        self._click = None
        self._position = (0, 0)

    def _record(self, *entry):
        self.log.append((self.clock.now, *entry) if self.clock else entry)

    @property
    def position(self):
        return self._position

    @position.setter
    def position(self, value):
        self._position = value
        self._record("move", *value)

    def press(self, button):
        if self._click is not None:
            self._click += 1
        self._record("press", button.name, self._click)

    def release(self, button):
        self._record("release", button.name, self._click)

    def scroll(self, dx, dy):
        self._record("scroll", dx, dy)

    def __enter__(self):
        self.entered += 1
        self._click = 0
        return self

    def __exit__(self, *exc_info):
        self.exited += 1
        self._click = None

    def actions(self):
        """The log without timestamps."""
        return [entry[1:] if self.clock else entry for entry in self.log]


class FakeListener:
    """Stands in for a pynput listener; tests call its callbacks by hand."""

    created = []

    def __init__(self, **callbacks):
        self.callbacks = callbacks
        self.started = 0
        self.stopped = 0
        FakeListener.created.append(self)

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    @property
    def running(self):
        return self.started > 0 and self.stopped == 0


class FakeHotkeys:
    def __init__(self, keys, on_action):
        self.keys = dict(keys)
        self.on_action = on_action
        self.started = 0
        self.stopped = 0
        self.capture = None
        self.cancelled = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def set_keys(self, keys):
        self.keys = dict(keys)

    def capture_next(self, callback):
        self.capture = callback

    def cancel_capture(self):
        self.capture = None
        self.cancelled += 1


class FakeRecorder:
    def __init__(self, events=()):
        self.events = list(events)
        self.starts = 0
        self.stops = 0
        self.start_error = None

    @property
    def event_count(self):
        return len(self.events)

    def start(self):
        if self.start_error:
            raise self.start_error
        self.starts += 1

    def stop(self):
        self.stops += 1
        return list(self.events)


class FakePlayer:
    def __init__(self, completed=None, statuses=(), error=None, block=False):
        self.calls = []
        self.completed = completed
        self.statuses = statuses
        self.error = error
        self.block = block  # wait for the stop signal, like a long macro
        self.pause = None  # the pause flag the window handed to the last run
        self.reported = threading.Event()  # set once every status has been reported

    def run(self, macro, stop, times=1, delay=0.0, on_status=lambda headline, detail: None, pause=None):
        self.calls.append((macro, times, delay))
        self.pause = pause
        for status in self.statuses:
            on_status(*status)
        self.reported.set()
        if self.error:
            raise self.error
        if self.block:
            stop.wait(5)
        return self.completed if self.completed is not None else (times or 0)
