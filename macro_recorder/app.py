"""The application window: ties the recorder, the player and the shortcuts to a small Tk interface."""

from __future__ import annotations

import math
import queue
import threading
import time
import tkinter as tk
from enum import Enum, auto
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from . import permissions, settings
from .hotkeys import Hotkeys, is_valid_name, key_label
from .player import Player, parse_loop_settings
from .recorder import Recorder
from .recording import Recording, RecordingError, new_macro_path

COUNTDOWN_SECONDS = 3
POLL_MS = 50
MACROS_DIR = Path.home() / "MouseMacros"
CONTENT_WIDTH = 420  # pixels
TITLE_BAR_HEIGHT = 40  # pixels above the window content that may belong to its title bar
LOADED_HINT = "Press Repeat to replay it, or Loop to repeat it."
ACTION_NAMES = {"record": "starting a recording", "stop": "stopping"}


class State(Enum):
    IDLE = auto()
    COUNTDOWN = auto()
    RECORDING = auto()
    PLAYING = auto()


def describe_events(count: int) -> str:
    return f"{count} event{'' if count == 1 else 's'}"


def describe_runs(done: int, times: int | None) -> str:
    """"3 of 10 runs completed", or nothing for a macro that was only played once."""
    if times == 1:
        return ""
    total = f" of {times}" if times else ""
    return f"{done}{total} run{'' if (times or done) == 1 else 's'} completed"


class App:
    """Everything the user sees. All Tk calls happen on the main thread.

    The keyboard listener and the playback worker run on other threads and only talk
    to the window through `self.messages`, which `_poll` empties every few milliseconds.
    """

    def __init__(self, root: tk.Tk, *, hotkeys_factory=Hotkeys, recorder=None, player=None, macros_dir=MACROS_DIR):
        self.root = root
        self.keys = settings.load_hotkeys()
        self.recorder = recorder if recorder is not None else Recorder()
        self.player = player if player is not None else Player()
        self.macros_dir = Path(macros_dir)
        self.clock = time.monotonic
        self.state = State.IDLE
        self.macro: Recording | None = None
        self.macro_path: Path | None = None
        self.capturing: str | None = None  # the action whose shortcut is being chosen
        self.stop_requested = threading.Event()
        self.messages: queue.Queue = queue.Queue()
        self._status_lock = threading.Lock()
        self._newest_status: tuple[str, str] | None = None
        self.worker: threading.Thread | None = None
        self.closed = False
        self.countdown_end = 0.0
        self.record_start = 0.0

        self._build_ui()
        self.hotkeys = hotkeys_factory(self.keys, self._hotkey_pressed)
        self.hotkeys.start()
        self._refresh_macro_info()
        self._set_state(State.IDLE)
        self._poll()

    # ---- window -----------------------------------------------------------------

    def _build_ui(self) -> None:
        root = self.root
        root.title("Mouse Macro Recorder")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.createcommand("tk::mac::Quit", self.close)  # Cmd+Q on macOS

        self.status_var = tk.StringVar(root)
        self.detail_var = tk.StringVar(root)
        self.macro_var = tk.StringVar(root)
        self.times_var = tk.StringVar(root, "")
        self.delay_var = tk.StringVar(root, "5")
        self.unit_var = tk.StringVar(root, "seconds")
        self.key_vars = {action: tk.StringVar(root, key_label(name)) for action, name in self.keys.items()}
        self.big_font = tkfont.nametofont("TkDefaultFont").copy()
        self.big_font.configure(size=24, weight="bold")

        main = ttk.Frame(root, padding=14)
        main.pack()

        missing = permissions.missing_permissions()
        if missing:
            self._build_permission_banner(main, missing)

        ttk.Label(main, textvariable=self.status_var, font=self.big_font, anchor="center").pack(fill="x")
        # Reserve room for two lines so the window does not change size as the text does.
        line_height = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        detail = ttk.Frame(main, width=CONTENT_WIDTH, height=2 * line_height + 6)
        detail.pack_propagate(False)
        detail.pack(fill="x")
        ttk.Label(detail, textvariable=self.detail_var, anchor="n", justify="center", wraplength=CONTENT_WIDTH).pack(
            fill="both", expand=True
        )

        buttons = ttk.Frame(main)
        buttons.pack(fill="x", pady=(4, 12))
        buttons.columnconfigure((0, 1), weight=1, uniform="buttons")
        self.record_button = ttk.Button(buttons, text="Record", command=self.start_countdown)
        self.stop_button = ttk.Button(buttons, text="Stop", command=self.stop_from_button)
        self.record_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        macro = ttk.LabelFrame(main, text="Macro", padding=8)
        macro.pack(fill="x", pady=(0, 10))
        ttk.Label(macro, textvariable=self.macro_var, justify="left", wraplength=CONTENT_WIDTH - 30).pack(anchor="w")
        row = ttk.Frame(macro)
        row.pack(fill="x", pady=(8, 0))
        row.columnconfigure((0, 1), weight=1, uniform="macro")
        self.import_button = ttk.Button(row, text="Import…", command=self.import_macro)
        self.repeat_button = ttk.Button(row, text="Repeat", command=self.repeat)
        self.import_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.repeat_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        loop = ttk.LabelFrame(main, text="Loop", padding=8)
        loop.pack(fill="x", pady=(0, 10))
        loop.columnconfigure(3, weight=1)
        ttk.Label(loop, text="Repeat").grid(row=0, column=0, sticky="w")
        self.times_entry = ttk.Entry(loop, textvariable=self.times_var, width=7, justify="right")
        self.times_entry.grid(row=0, column=1, padx=6, pady=2)
        ttk.Label(loop, text="times (empty = forever)").grid(row=0, column=2, columnspan=2, sticky="w")
        ttk.Label(loop, text="Wait").grid(row=1, column=0, sticky="w")
        self.delay_entry = ttk.Entry(loop, textvariable=self.delay_var, width=7, justify="right")
        self.delay_entry.grid(row=1, column=1, padx=6, pady=2)
        self.unit_box = ttk.Combobox(
            loop, textvariable=self.unit_var, values=("seconds", "minutes"), state="readonly", width=8
        )
        self.unit_box.grid(row=1, column=2, sticky="w")
        ttk.Label(loop, text="between runs").grid(row=1, column=3, sticky="w", padx=6)
        self.loop_button = ttk.Button(loop, text="Loop", command=self.loop)
        self.loop_button.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(8, 0))

        shortcuts = ttk.LabelFrame(main, text="Shortcuts", padding=8)
        shortcuts.pack(fill="x")
        shortcuts.columnconfigure(0, weight=1)
        self.change_buttons = {}
        for row_number, (action, text) in enumerate((("record", "Start recording now"), ("stop", "Stop"))):
            ttk.Label(shortcuts, text=text).grid(row=row_number, column=0, sticky="w", pady=2)
            ttk.Label(shortcuts, textvariable=self.key_vars[action], width=14, anchor="center", relief="sunken").grid(
                row=row_number, column=1, padx=6
            )
            button = ttk.Button(shortcuts, text="Change…", width=9, command=lambda a=action: self.change_shortcut(a))
            button.grid(row=row_number, column=2)
            self.change_buttons[action] = button

    def _build_permission_banner(self, parent: ttk.Frame, missing: list[str]) -> None:
        banner = ttk.Frame(parent)
        banner.pack(fill="x", pady=(0, 10))
        text = (
            f"macOS is blocking mouse access. Allow {' and '.join(missing)} for the app that runs this "
            "program (Terminal, PyCharm...) in System Settings > Privacy & Security, then restart it."
        )
        ttk.Label(banner, text=text, foreground="#c62828", wraplength=CONTENT_WIDTH, justify="left").pack(anchor="w")
        ttk.Button(
            banner, text="Open System Settings", command=lambda: permissions.open_privacy_settings(missing[0])
        ).pack(anchor="w", pady=(6, 0))

    def _show(self, headline: str, detail: str = "") -> None:
        self.status_var.set(headline)
        if self.detail_var.get() != detail:
            self.detail_var.set(detail)

    def _stop_hint(self) -> str:
        return f"Press {key_label(self.keys['stop'])} or click Stop to stop."

    def _refresh_macro_info(self) -> None:
        if self.macro is None:
            self.macro_var.set("No macro yet. Record one, or import a file.")
        else:
            where = str(self.macro_path) if self.macro_path else "Not saved to a file"
            self.macro_var.set(f"{where}\n{self.macro.duration:.1f} s · {describe_events(len(self.macro.events))}")
        self._update_controls()

    def _update_controls(self) -> None:
        idle = self.state is State.IDLE and self.capturing is None
        have_macro = self.macro is not None
        for widget, enabled in (
            (self.record_button, idle),
            (self.stop_button, self.state is not State.IDLE),
            (self.import_button, idle),
            (self.repeat_button, idle and have_macro),
            (self.loop_button, idle and have_macro),
            (self.times_entry, idle),
            (self.delay_entry, idle),
            (self.unit_box, idle),
        ):
            widget.state(["!disabled" if enabled else "disabled"])
        for action, button in self.change_buttons.items():
            usable = self.state is State.IDLE and self.capturing in (None, action)
            button.state(["!disabled" if usable else "disabled"])
            button.configure(text="Cancel" if self.capturing == action else "Change…")

    def _set_state(self, state: State) -> None:
        self.state = state
        self._update_controls()
        if state is State.IDLE:
            record, stop = (key_label(self.keys[action]) for action in ("record", "stop"))
            self._show("Ready", f"Press {record} to start recording right away, {stop} to stop.")
        elif state is State.COUNTDOWN:
            self._show(str(COUNTDOWN_SECONDS), "Recording starts at 0. Press Stop to cancel.")
        elif state is State.RECORDING:
            self._show("Recording", "")
        else:
            self._show("Playing", self._stop_hint())

    def _window_area(self) -> tuple[int, int, int, int]:
        """The screen rectangle covered by this window, including its title bar."""
        self.root.update_idletasks()
        left, top = self.root.winfo_rootx(), self.root.winfo_rooty()
        title_bar = max(TITLE_BAR_HEIGHT, top - self.root.winfo_y())
        return left, top - title_bar, left + self.root.winfo_width(), top + self.root.winfo_height()

    # ---- messages from other threads ------------------------------------------------

    def _hotkey_pressed(self, action: str) -> None:
        """Runs on the keyboard listener thread."""
        if action == "stop":
            self.stop_requested.set()  # a running macro stops at once, not on the next poll
        self.messages.put(("hotkey", action))

    def _post_status(self, headline: str, detail: str) -> None:
        """Runs on the playback thread.

        Only the newest status is worth showing, so one message stands for however many arrive
        before the next poll. A macro that replays in microseconds then cannot outrun the window.
        A message is queued exactly when `_newest_status` was empty, so none is ever missing.
        """
        with self._status_lock:
            queued = self._newest_status is not None
            self._newest_status = (headline, detail)
        if not queued:
            self.messages.put(("newest_status",))

    def _poll(self) -> None:
        try:
            self._drain()
            self._tick()
        finally:
            if not self.closed:
                self.root.after(POLL_MS, self._poll)

    def _drain(self) -> None:
        """Handles the messages that are waiting now; any that arrive meanwhile wait for the next poll.

        Looping until the queue is empty would never return to Tk while a thread keeps filling it.
        """
        for _ in range(self.messages.qsize()):
            try:
                kind, *arguments = self.messages.get_nowait()
            except queue.Empty:
                return
            getattr(self, f"_handle_{kind}")(*arguments)

    def _tick(self) -> None:
        """Advances the countdown and the recording timer."""
        if self.state is State.COUNTDOWN:
            remaining = math.ceil(self.countdown_end - self.clock())
            if remaining > 0:
                self.status_var.set(str(remaining))
            else:
                self._begin_recording()
        elif self.state is State.RECORDING:
            elapsed = int(self.clock() - self.record_start)
            counts = f"{elapsed // 60:02d}:{elapsed % 60:02d} · {describe_events(self.recorder.event_count)}"
            self._show("Recording", f"{counts}\n{self._stop_hint()}")

    def _handle_hotkey(self, action: str) -> None:
        if action == "record":
            if self.state in (State.IDLE, State.COUNTDOWN) and self.capturing is None:
                self._begin_recording()
        elif action == "stop":
            self._stop(via_button=False)

    def _handle_captured(self, action: str, name: str) -> None:
        if self.capturing != action:
            return  # cancelled before the key arrived
        other = next(candidate for candidate in self.keys if candidate != action)
        problem = None
        if not is_valid_name(name):
            problem = "That key cannot be used as a shortcut."
        elif name == self.keys[other]:
            problem = f"{key_label(name)} is already the shortcut for {ACTION_NAMES[other]}. Choose another key."
        if problem is None:
            self.keys[action] = name
            self.hotkeys.set_keys(self.keys)
        self._end_capture()
        if problem is not None:
            messagebox.showwarning("Shortcut not changed", problem, parent=self.root)
            return
        self._set_state(State.IDLE)
        try:
            settings.save_hotkeys(self.keys)
        except OSError as exc:
            messagebox.showwarning(
                "Shortcut not saved", f"The new shortcut works for now but could not be saved:\n{exc}", parent=self.root
            )

    def _handle_newest_status(self) -> None:
        with self._status_lock:
            status, self._newest_status = self._newest_status, None
        if status is not None:
            self._handle_status(*status)

    def _handle_status(self, headline: str, detail: str) -> None:
        if self.state is State.PLAYING:
            self._show(headline, f"{detail}\n{self._stop_hint()}" if detail else self._stop_hint())

    def _handle_finished(self, done: int, times: int | None, error: Exception | None) -> None:
        self.worker = None
        if self.state is not State.PLAYING:
            return
        stopped = self.stop_requested.is_set()
        self._set_state(State.IDLE)
        if error is not None:
            messagebox.showerror("Playback failed", str(error), parent=self.root)
            return
        self._show("Stopped" if stopped else "Finished", describe_runs(done, times))

    # ---- recording -----------------------------------------------------------------

    def start_countdown(self) -> None:
        if self.state is not State.IDLE or self.capturing is not None:
            return
        self.countdown_end = self.clock() + COUNTDOWN_SECONDS
        self._set_state(State.COUNTDOWN)

    def _begin_recording(self) -> None:
        try:
            self.recorder.start()
        except Exception as exc:  # the system refused to let us watch the mouse
            self._set_state(State.IDLE)
            messagebox.showerror("Could not start recording", str(exc), parent=self.root)
            return
        self.record_start = self.clock()
        self._set_state(State.RECORDING)
        self.root.bell()

    def stop_from_button(self) -> None:
        self._stop(via_button=True)

    def _stop(self, via_button: bool) -> None:
        if self.state is State.COUNTDOWN:
            self._set_state(State.IDLE)
        elif self.state is State.RECORDING:
            self._finish_recording(via_button)
        elif self.state is State.PLAYING:
            self.stop_requested.set()
            self._show("Stopping", "")

    def _finish_recording(self, via_button: bool) -> None:
        events = self.recorder.stop()
        self.root.bell()
        # The click on our own Stop button must not become part of the macro.
        macro = Recording.from_capture(events, self._window_area() if via_button else None)
        if not macro.events:
            self._set_state(State.IDLE)
            message = "No mouse movement or clicks were recorded."
            if permissions.missing_permissions():
                message += "\n\nOn macOS, allow Input Monitoring for this app in System Settings > Privacy & Security."
            messagebox.showwarning("Nothing recorded", message, parent=self.root)
            return
        self.macro, self.macro_path = macro, None
        failure = None
        try:
            path = new_macro_path(self.macros_dir)
            macro.save(path)
            self.macro_path = path
        except OSError as exc:
            failure = exc
        self._refresh_macro_info()
        self._set_state(State.IDLE)
        self._show("Recorded", LOADED_HINT)
        if failure is not None:
            messagebox.showerror(
                "Could not save the macro",
                f"You can still replay it, but it was not saved to a file:\n{failure}",
                parent=self.root,
            )

    # ---- files and playback ------------------------------------------------------------

    def import_macro(self) -> None:
        if self.state is not State.IDLE or self.capturing is not None:
            return
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Import macro",
            initialdir=str(self.macros_dir if self.macros_dir.is_dir() else Path.home()),
            filetypes=[("Macro files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            macro = Recording.load(Path(path))
        except RecordingError as exc:
            messagebox.showerror("Could not import the macro", str(exc), parent=self.root)
            return
        self.macro, self.macro_path = macro, Path(path)
        self._refresh_macro_info()
        self._show("Imported", LOADED_HINT)

    def repeat(self) -> None:
        self._play(times=1, delay=0.0)

    def loop(self) -> None:
        try:
            times, delay = parse_loop_settings(self.times_var.get(), self.delay_var.get(), self.unit_var.get())
        except ValueError as exc:
            messagebox.showerror("Loop settings", str(exc), parent=self.root)
            return
        self._play(times, delay)

    def _play(self, times: int | None, delay: float) -> None:
        if self.state is not State.IDLE or self.macro is None or self.capturing is not None:
            return
        self.stop_requested.clear()
        self._set_state(State.PLAYING)
        self.worker = threading.Thread(target=self._play_worker, args=(self.macro, times, delay), daemon=True)
        self.worker.start()

    def _play_worker(self, macro: Recording, times: int | None, delay: float) -> None:
        """Runs on its own thread so that the window stays responsive."""
        done, error = 0, None
        try:
            done = self.player.run(macro, self.stop_requested, times, delay, on_status=self._post_status)
        except Exception as exc:  # reported in the window instead of vanishing with the thread
            error = exc
        self.messages.put(("finished", done, times, error))

    # ---- shortcuts ---------------------------------------------------------------------

    def change_shortcut(self, action: str) -> None:
        if self.state is not State.IDLE:
            return
        if self.capturing == action:
            self._end_capture()
            return
        self.capturing = action
        self.key_vars[action].set("Press a key...")
        self.hotkeys.capture_next(lambda name: self.messages.put(("captured", action, name)))
        self._update_controls()

    def _end_capture(self) -> None:
        action, self.capturing = self.capturing, None
        self.hotkeys.cancel_capture()
        if action is not None:
            self.key_vars[action].set(key_label(self.keys[action]))
        self._update_controls()

    # ---- shutting down -----------------------------------------------------------------

    def close(self) -> None:
        if self.state is State.RECORDING and self.recorder.event_count:
            question = "Close the app and discard the recording?"
            if not messagebox.askyesno("Recording in progress", question, parent=self.root):
                return
        self.closed = True
        self.stop_requested.set()
        self.hotkeys.stop()
        self.recorder.stop()
        if self.worker is not None:
            self.worker.join(timeout=1.0)  # lets a macro release any button it is holding
        self.root.destroy()


def main() -> None:
    permissions.prevent_app_nap()
    root = tk.Tk()
    App(root)
    # Raised once so the window is not left behind the terminal that started it.
    root.lift()
    root.attributes("-topmost", True)
    root.after(300, lambda: root.attributes("-topmost", False))
    root.mainloop()
