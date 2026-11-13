"""The application window: ties the recorder, the player and the shortcuts to a small Tk interface."""

from __future__ import annotations

import contextlib
import importlib
import math
import platform
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from enum import Enum, auto
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from . import filemanager, icons, permissions, settings
from .hotkeys import Hotkeys, is_valid_name, key_label
from .player import Player, parse_loop_settings
from .recorder import Recorder
from .recording import Recording, RecordingError, new_macro_path
from .tooltip import Tooltip

APP_NAME = "Mouse Macro Recorder"
COUNTDOWN_SECONDS = 3
POLL_MS = 50
MACROS_DIR = Path.home() / "MouseMacros"
CONTENT_WIDTH = 420  # pixels
FOLDER_BUTTON_SPACE = 56  # pixels the folder button takes next to the path of the macro
TITLE_BAR_HEIGHT = 40  # pixels above the window content that may belong to its title bar
LOADED_HINT = "Press Repeat to replay it, or Loop to repeat it."
MACRO_FILETYPES = [("Macro files", "*.json"), ("All files", "*.*")]
ACTION_NAMES = {
    "record": "starting a recording",
    "stop": "stopping",
    "loop": "starting or resuming the loop",
    "pause": "pausing the loop",
}
SHORTCUT_ROWS = (
    ("record", "Start recording now"),
    ("stop", "Stop"),
    ("loop", "Start / resume loop"),
    ("pause", "Pause loop"),
)
SELF_TEST_FLAG = "--self-test"
SELF_TEST_MS = 1000  # how long the self-test keeps the window open
# The app imports these only when it needs them and carries on without them, so a packaged
# build that lacked them would still start. The self-test makes sure they are there.
SELF_TEST_IMPORTS = {"darwin": ("Foundation", "Quartz")}


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


def permission_holder() -> str:
    """Who macOS grants the mouse permissions to: the packaged app itself, or whatever started Python."""
    if getattr(sys, "frozen", False):
        return APP_NAME
    return "the app that runs this program (Terminal, PyCharm...)"


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
        self._dialogs_up = 0  # dialogs showing now; while there is one, whatever is pressed must not start anything
        self.stop_requested = threading.Event()
        self.pause_requested = threading.Event()
        self.playing_status: tuple[str, str] = ("Playing", "")  # what the player last said, shown again on resume
        self.messages: queue.Queue = queue.Queue()
        self._status_lock = threading.Lock()
        self._newest_status: tuple[str, str] | None = None
        self.worker: threading.Thread | None = None
        self.closed = False
        self._poll_job: str | None = None
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
        root.title(APP_NAME)
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
        info = ttk.Frame(macro)
        info.pack(fill="x")
        info.columnconfigure(0, weight=1)
        ttk.Label(
            info, textvariable=self.macro_var, justify="left", wraplength=CONTENT_WIDTH - 30 - FOLDER_BUTTON_SPACE
        ).grid(row=0, column=0, sticky="w")
        # Kept on self: Tk shows nothing for an image that Python has already let go of.
        self.folder_icon, self.folder_icon_disabled = icons.folder_icons(root)
        self.folder_button = ttk.Button(info, image=self.folder_icon, command=self.show_macro_folder)
        self.folder_button.grid(row=0, column=1, sticky="ne", padx=(8, 0))
        Tooltip(self.folder_button, f"Show in {filemanager.file_manager_name()}")
        row = ttk.Frame(macro)
        row.pack(fill="x", pady=(8, 0))
        row.columnconfigure((0, 1, 2), weight=1, uniform="macro")
        self.import_button = ttk.Button(row, text="Import…", command=self.import_macro)
        self.save_as_button = ttk.Button(row, text="Save as…", command=self.save_macro_as)
        self.repeat_button = ttk.Button(row, text="Repeat", command=self.repeat)
        self.import_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.save_as_button.grid(row=0, column=1, sticky="ew", padx=4)
        self.repeat_button.grid(row=0, column=2, sticky="ew", padx=(4, 0))

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
        loop_buttons = ttk.Frame(loop)
        loop_buttons.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        loop_buttons.columnconfigure((0, 1), weight=1, uniform="loop")
        self.loop_button = ttk.Button(loop_buttons, text="Loop", command=self.loop_or_resume)
        self.pause_button = ttk.Button(loop_buttons, text="Pause", command=self.pause)
        self.loop_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.pause_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        shortcuts = ttk.LabelFrame(main, text="Shortcuts", padding=8)
        shortcuts.pack(fill="x")
        shortcuts.columnconfigure(0, weight=1)
        self.change_buttons = {}
        for row_number, (action, text) in enumerate(SHORTCUT_ROWS):
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
            f"macOS is blocking mouse access. Allow {' and '.join(missing)} for {permission_holder()} "
            "in System Settings > Privacy & Security, then restart it."
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

    def _playing_hint(self) -> str:
        pause, stop = (key_label(self.keys[action]) for action in ("pause", "stop"))
        return f"Press {pause} to pause, {stop} to stop."

    def _paused_hint(self) -> str:
        loop, stop = (key_label(self.keys[action]) for action in ("loop", "stop"))
        return f"Press {loop} to resume, {stop} to stop."

    def _show_playback(self, headline: str, detail: str) -> None:
        hint = self._paused_hint() if self.pause_requested.is_set() else self._playing_hint()
        self._show(headline, f"{detail}\n{hint}" if detail else hint)

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
        playing = self.state is State.PLAYING
        # A stopped macro still ends on its own, a moment later: nothing can be paused or resumed then.
        running = playing and not self.stop_requested.is_set()
        paused = running and self.pause_requested.is_set()
        for widget, enabled in (
            (self.record_button, idle),
            (self.stop_button, self.state is not State.IDLE),
            (self.import_button, idle),
            (self.save_as_button, idle and have_macro),
            (self.repeat_button, idle and have_macro),
            (self.loop_button, (idle and have_macro) or paused),
            (self.pause_button, running and not paused),
            (self.times_entry, idle),
            (self.delay_entry, idle),
            (self.unit_box, idle),
        ):
            widget.state(["!disabled" if enabled else "disabled"])
        self.loop_button.configure(text="Resume" if paused else "Loop")
        can_show_folder = idle and self.macro_path is not None
        self.folder_button.state(["!disabled" if can_show_folder else "disabled"])
        self.folder_button.configure(image=self.folder_icon if can_show_folder else self.folder_icon_disabled)
        for action, button in self.change_buttons.items():
            usable = self.state is State.IDLE and self.capturing in (None, action)
            button.state(["!disabled" if usable else "disabled"])
            button.configure(text="Cancel" if self.capturing == action else "Change…")

    def _set_state(self, state: State) -> None:
        self.state = state
        self._update_controls()
        if state is State.IDLE:
            record, stop, loop = (key_label(self.keys[action]) for action in ("record", "stop", "loop"))
            hint = f"Press {record} to record right away, {stop} to stop, {loop} to start the loop."
            self._show("Ready", hint)
        elif state is State.COUNTDOWN:
            self._show(str(COUNTDOWN_SECONDS), "Recording starts at 0. Press Stop to cancel.")
        elif state is State.RECORDING:
            self._show("Recording", "")
        else:
            self._show("Playing", self._playing_hint())

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
        elif action != "pause" and self.dialog_open:
            # Dropped here, not when it is handled: a window that does not poll until the dialog has closed
            # would start it then, long after the key was pressed.
            return
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
                self._poll_job = self.root.after(POLL_MS, self._poll)

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
        if action == "stop":
            self._stop(via_button=False)
        elif action == "pause":
            self.pause()
        elif self.dialog_open:
            return  # nothing may start behind a dialog, not even a key that was pressed just before it opened
        elif action == "record":
            if self.state in (State.IDLE, State.COUNTDOWN) and self.capturing is None:
                self._begin_recording()
        elif action == "loop":
            self.loop_or_resume()

    def _handle_captured(self, action: str, name: str) -> None:
        if self.capturing != action:
            return  # cancelled before the key arrived
        taken_by = next((other for other, key in self.keys.items() if other != action and key == name), None)
        problem = None
        if not is_valid_name(name):
            problem = "That key cannot be used as a shortcut."
        elif taken_by is not None:
            problem = f"{key_label(name)} is already the shortcut for {ACTION_NAMES[taken_by]}. Choose another key."
        if problem is None:
            self.keys[action] = name
            self.hotkeys.set_keys(self.keys)
        self._end_capture()
        if problem is not None:
            self._warning("Shortcut not changed", problem)
            return
        self._set_state(State.IDLE)
        try:
            settings.save_hotkeys(self.keys)
        except OSError as exc:
            self._warning("Shortcut not saved", f"The new shortcut works for now but could not be saved:\n{exc}")

    def _handle_newest_status(self) -> None:
        with self._status_lock:
            status, self._newest_status = self._newest_status, None
        if status is not None:
            self._handle_status(*status)

    def _handle_status(self, headline: str, detail: str) -> None:
        if self.state is not State.PLAYING or self.stop_requested.is_set():
            return
        paused = self.pause_requested.is_set()
        if headline == "Paused":
            if paused:  # otherwise it was resumed before the news got here
                self._show_playback(headline, detail)
            return
        self.playing_status = (headline, detail)
        if not paused:  # while paused, the window keeps saying so
            self._show_playback(headline, detail)

    def _handle_finished(self, done: int, times: int | None, error: Exception | None) -> None:
        self.worker = None
        if self.state is not State.PLAYING:
            return
        stopped = self.stop_requested.is_set()
        self.pause_requested.clear()
        self._set_state(State.IDLE)
        if error is not None:
            self._error("Playback failed", str(error))
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
            self._error("Could not start recording", str(exc))
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
            self._update_controls()

    def _finish_recording(self, via_button: bool) -> None:
        events = self.recorder.stop()
        self.root.bell()
        # The click on our own Stop button must not become part of the macro.
        macro = Recording.from_capture(events, self._window_area() if via_button else None)
        if not macro.events:
            self._set_state(State.IDLE)
            message = "No mouse movement or clicks were recorded."
            if permissions.missing_permissions():
                message += (
                    f"\n\nAllow Input Monitoring for {permission_holder()} in System Settings > Privacy & Security, "
                    "then restart it."
                )
            self._warning("Nothing recorded", message)
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
            self._error(
                "Could not save the macro", f"You can still replay it, but it was not saved to a file:\n{failure}"
            )

    # ---- dialogs, files and playback --------------------------------------------------

    @contextlib.contextmanager
    def _dialogs(self):
        """While a dialog is up, the shortcuts that would start something are ignored.

        Every dialog goes through here, either directly or through `_error`, `_warning` and `_confirm`.
        """
        self._dialogs_up += 1
        try:
            yield
        finally:
            self._dialogs_up -= 1

    @property
    def dialog_open(self) -> bool:
        return self._dialogs_up > 0

    def _error(self, title: str, message: str) -> None:
        with self._dialogs():
            messagebox.showerror(title, message, parent=self.root)

    def _warning(self, title: str, message: str) -> None:
        with self._dialogs():
            messagebox.showwarning(title, message, parent=self.root)

    def _confirm(self, title: str, message: str) -> bool:
        with self._dialogs():
            return messagebox.askyesno(title, message, parent=self.root)

    def import_macro(self) -> None:
        if self.state is not State.IDLE or self.capturing is not None:
            return
        with self._dialogs():
            path = filedialog.askopenfilename(
                parent=self.root,
                title="Import macro",
                initialdir=str(self.macros_dir if self.macros_dir.is_dir() else Path.home()),
                filetypes=MACRO_FILETYPES,
            )
        if not path:
            return
        try:
            macro = Recording.load(Path(path))
        except RecordingError as exc:
            self._error("Could not import the macro", str(exc))
            return
        self.macro, self.macro_path = macro, Path(path)
        self._refresh_macro_info()
        self._show("Imported", LOADED_HINT)

    def _save_suggestion(self) -> tuple[Path, str]:
        """The folder and the file name the Save as dialog starts with."""
        if self.macro_path is not None:
            folder, name = self.macro_path.parent, self.macro_path.name
        else:
            folder, name = self.macros_dir, new_macro_path(self.macros_dir).name
        if not folder.is_dir():
            folder = self.macros_dir if self.macros_dir.is_dir() else Path.home()
        return folder, name

    def save_macro_as(self) -> None:
        if self.state is not State.IDLE or self.capturing is not None or self.macro is None:
            return
        folder, name = self._save_suggestion()
        with self._dialogs():
            chosen = filedialog.asksaveasfilename(
                parent=self.root,
                title="Save macro as",
                initialdir=str(folder),
                initialfile=name,
                defaultextension=".json",
                filetypes=MACRO_FILETYPES,
            )
        if not chosen:
            return
        path = Path(chosen)
        if not path.suffix:
            # Not every system adds the extension, and one that does not cannot warn about replacing a file.
            path = path.with_suffix(".json")
            if path.exists() and not self._confirm(
                "Replace file?", f"{path.name} already exists. Do you want to replace it?"
            ):
                return
        try:
            self.macro.save(path)
        except OSError as exc:
            self._error("Could not save the macro", str(exc))
            return
        self.macro_path = path
        self._refresh_macro_info()
        self._show("Saved", LOADED_HINT)

    def show_macro_folder(self) -> None:
        """Opens the folder of the macro in Finder or File Explorer, with the macro selected."""
        if self.state is not State.IDLE or self.capturing is not None or self.macro_path is None:
            return
        try:
            filemanager.show_in_file_manager(self.macro_path)
        except OSError as exc:
            self._error(f"Could not open {filemanager.file_manager_name()}", str(exc))

    def repeat(self) -> None:
        self._play(times=1, delay=0.0)

    def loop(self) -> None:
        try:
            times, delay = parse_loop_settings(self.times_var.get(), self.delay_var.get(), self.unit_var.get())
        except ValueError as exc:
            self._error("Loop settings", str(exc))
            return
        self._play(times, delay)

    def loop_or_resume(self) -> None:
        """What the Loop button and its shortcut do: start the loop, or carry on with a paused one."""
        if self.state is State.PLAYING:
            self.resume()
        elif self.state is State.IDLE and self.capturing is None:
            if self.macro is None:  # the button is disabled then, so this is the shortcut
                self._show("Nothing to loop", "Record a macro or import one first.")
            else:
                self.loop()

    def pause(self) -> None:
        if self.state is not State.PLAYING or self.stop_requested.is_set() or self.pause_requested.is_set():
            return
        self.pause_requested.set()
        # The player says "Paused" once it really stands still, which a held mouse button can delay.
        self._show("Pausing", self._paused_hint())
        self._update_controls()

    def resume(self) -> None:
        if self.state is not State.PLAYING or self.stop_requested.is_set() or not self.pause_requested.is_set():
            return
        self.pause_requested.clear()
        self._show_playback(*self.playing_status)
        self._update_controls()

    def _play(self, times: int | None, delay: float) -> None:
        if self.state is not State.IDLE or self.macro is None or self.capturing is not None:
            return
        self.stop_requested.clear()
        self.pause_requested.clear()
        self.playing_status = ("Playing", "")
        self._set_state(State.PLAYING)
        self.worker = threading.Thread(target=self._play_worker, args=(self.macro, times, delay), daemon=True)
        self.worker.start()

    def _play_worker(self, macro: Recording, times: int | None, delay: float) -> None:
        """Runs on its own thread so that the window stays responsive."""
        done, error = 0, None
        try:
            done = self.player.run(
                macro, self.stop_requested, times, delay, on_status=self._post_status, pause=self.pause_requested
            )
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
            if not self._confirm("Recording in progress", question):
                return
        self.closed = True
        self.stop_requested.set()
        self.hotkeys.stop()
        self.recorder.stop()
        if self.worker is not None:
            self.worker.join(timeout=1.0)  # lets a macro release any button it is holding
        if self._poll_job is not None:
            self.root.after_cancel(self._poll_job)  # it would fire into a window that is gone
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    """Runs the app and returns its exit code. `--self-test [report]` checks a packaged build instead."""
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == [SELF_TEST_FLAG]:
        return self_test(Path(args[1]) if len(args) > 1 else None)
    permissions.prevent_app_nap()
    root = tk.Tk()
    App(root)
    # Raised once so the window is not left behind the terminal that started it.
    root.lift()
    root.attributes("-topmost", True)
    root.after(300, lambda: root.attributes("-topmost", False))
    root.mainloop()
    return 0


def self_test(report: Path | None = None) -> int:
    """Opens the window and closes it again, to prove that a packaged build has all it needs.

    Returns the exit code, 0 when all went well. The outcome is also written to `report`,
    because a windowed Windows build has nowhere to print.
    """
    problems: list[str] = []
    outcome = ""
    root = None
    try:
        for name in SELF_TEST_IMPORTS.get(sys.platform, ()):
            importlib.import_module(name)
        permissions.prevent_app_nap()
        root = tk.Tk()
        root.report_callback_exception = lambda *error: problems.append("".join(traceback.format_exception(*error)))
        app = App(root)
        outcome = f"Python {platform.python_version()} on {platform.machine()}, Tk {root.tk.call('info', 'patchlevel')}"

        def finish() -> None:
            try:
                app.close()
            finally:
                with contextlib.suppress(tk.TclError):
                    root.destroy()  # also when closing went wrong, so that the self-test cannot hang

        root.after(SELF_TEST_MS, finish)
        root.mainloop()
    except Exception:
        problems.append(traceback.format_exc())
        if root is not None:
            with contextlib.suppress(tk.TclError):
                root.destroy()
    if report is not None:
        report.write_text("".join(problems) if problems else f"ok: {outcome}\n", encoding="utf-8")
    return 1 if problems else 0
