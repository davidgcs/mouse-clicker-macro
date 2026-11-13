"""Drives the real window without a display server session being touched: a hidden Tk root,
fake recorder, player and shortcuts, and mocked dialogs. The event loop is never run; tests call
the same methods the loop would."""

import sys
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk
from unittest import mock

from macro_recorder import app as app_module
from macro_recorder.app import POLL_MS, App, State, describe_events, describe_runs
from macro_recorder.player import Player
from macro_recorder.recording import Event, Recording

from .fakes import FakeClock, FakeHotkeys, FakeMouse, FakePlayer, FakeRecorder


def click(t, x=400, y=300, pressed=True):
    return Event(t, "click", x, y, button="left", pressed=pressed)


# What the recorder hands over: the first event happens 5 s after recording began.
CAPTURE = [Event(5.0, "move", 400, 300), click(5.5), click(5.6, pressed=False)]
# The same recording once it has been turned into a macro.
SAMPLE = [Event(0.0, "move", 400, 300), click(0.5), click(0.6, pressed=False)]

KEYS = {"record": "f1", "stop": "f2", "loop": "f3", "pause": "f4"}
CONTROLS = (
    "record",
    "stop",
    "import",
    "save_as",
    "folder",
    "repeat",
    "loop",
    "pause",
    "times",
    "delay",
    "unit",
    "change_record",
    "change_stop",
    "change_loop",
    "change_pause",
)
CHANGE_BUTTONS = ("change_record", "change_stop", "change_loop", "change_pause")
IDLE_WITHOUT_MACRO = ("record", "import", "times", "delay", "unit") + CHANGE_BUTTONS
# Every macro the tests load or record has a file, so the folder button works too.
IDLE_WITH_MACRO = IDLE_WITHOUT_MACRO + ("save_as", "folder", "repeat", "loop")
PLAYING_HINT = "Press F4 to pause, F2 to stop."
PAUSED_HINT = "Press F3 to resume, F2 to stop."


def controls(*enabled):
    return {name: name in enabled for name in CONTROLS}


def walk(widget):
    for child in widget.winfo_children():
        yield child
        yield from walk(child)


def find_button(root, text):
    return next((w for w in walk(root) if isinstance(w, ttk.Button) and str(w.cget("text")) == text), None)


def detail_label(app):
    return next(
        w for w in walk(app.root) if isinstance(w, ttk.Label) and str(w.cget("textvariable")) == str(app.detail_var)
    )


class AppTestCase(unittest.TestCase):
    """The window is built on first use, so a test can still change the fakes before that."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.macros_dir = self.tmp / "macros"
        self.clock = FakeClock(1000.0)
        self.recorder = FakeRecorder(CAPTURE)
        self.player = FakePlayer()
        self.save_hotkeys = self.patch(app_module.settings, "save_hotkeys")
        self.patch(app_module.settings, "load_hotkeys", side_effect=lambda: dict(KEYS))
        self.missing_permissions = self.patch(app_module.permissions, "missing_permissions", return_value=[])
        self.open_settings = self.patch(app_module.permissions, "open_privacy_settings")
        self.messagebox = self.patch(app_module, "messagebox")
        self.filedialog = self.patch(app_module, "filedialog")
        self.filedialog.askopenfilename.return_value = ""  # the dialog is cancelled unless a test picks a file
        self.filedialog.asksaveasfilename.return_value = ""
        for dialog in (
            self.messagebox.showerror,
            self.messagebox.showwarning,
            self.messagebox.askyesno,
            self.filedialog.askopenfilename,
            self.filedialog.asksaveasfilename,
        ):
            dialog.side_effect = self.announced
        self.show_in_file_manager = self.patch(app_module.filemanager, "show_in_file_manager")
        self.bell = mock.Mock()
        self._app = None
        self._hotkeys = None

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def announced(self, *args, **kwargs):
        """Runs whenever the window shows a dialog, which it must have announced: see `App._dialogs`."""
        self.assertTrue(self.app.dialog_open, "a dialog was shown without ignoring the shortcuts that start things")
        return mock.DEFAULT  # the mock goes on to return what the test made it return

    @property
    def app(self):
        if self._app is None:
            self._app = self.make_app()
        return self._app

    @property
    def hotkeys(self):
        self.launch()  # the fake is created together with the window
        return self._hotkeys

    def launch(self):
        """Builds the window now rather than on first use, so a test can change fakes afterwards."""
        return self.app

    def make_app(self):
        def hotkeys_factory(keys, on_action):
            self._hotkeys = FakeHotkeys(keys, on_action)
            return self._hotkeys

        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk is not available here: {exc}")
        root.withdraw()
        root.bell = self.bell
        self.addCleanup(self.destroy, root)
        app = App(
            root,
            hotkeys_factory=hotkeys_factory,
            recorder=self.recorder,
            player=self.player,
            macros_dir=self.macros_dir,
        )
        app.clock = self.clock
        self.addCleanup(self.end_playback, app)
        return app

    @staticmethod
    def destroy(root):
        # Timers left behind would fire later, inside the event loop of another test's window.
        for job in root.tk.splitlist(root.tk.call("after", "info")):
            root.after_cancel(job)
        try:
            root.update_idletasks()  # Tk 8.5 complains about idle callbacks still pending when it is destroyed
            root.destroy()
        except tk.TclError:
            pass  # the test closed the window already

    @staticmethod
    def end_playback(app):
        app.stop_requested.set()
        if app.worker is not None:
            app.worker.join(5)

    # ---- driving the window ----------------------------------------------------------

    def press(self, action):
        """The keyboard thread reports a shortcut, then the window polls."""
        self.hotkeys.on_action(action)
        self.app._drain()

    def finish_worker(self):
        """Waits for the playback thread, then lets the window handle what it reported."""
        worker = self.app.worker
        worker.join(5)
        self.assertFalse(worker.is_alive(), "playback did not finish")
        self.app._drain()

    def write_macro(self, name="macro.json", events=SAMPLE):
        path = self.tmp / name
        Recording(list(events)).save(path)
        return path

    def load_macro(self, name="macro.json", events=SAMPLE):
        path = self.write_macro(name, events)
        self.filedialog.askopenfilename.return_value = str(path)
        self.app.import_macro()
        return path

    def enabled(self):
        app = self.app
        widgets = {
            "record": app.record_button,
            "stop": app.stop_button,
            "import": app.import_button,
            "save_as": app.save_as_button,
            "folder": app.folder_button,
            "repeat": app.repeat_button,
            "loop": app.loop_button,
            "pause": app.pause_button,
            "times": app.times_entry,
            "delay": app.delay_entry,
            "unit": app.unit_box,
            **{f"change_{action}": button for action, button in app.change_buttons.items()},
        }
        return {name: not widget.instate(["disabled"]) for name, widget in widgets.items()}

    def shown(self):
        return self.app.status_var.get(), self.app.detail_var.get()

    def assertNoDialog(self):
        self.messagebox.showerror.assert_not_called()
        self.messagebox.showwarning.assert_not_called()
        self.messagebox.askyesno.assert_not_called()

    def assertDestroyed(self):
        with self.assertRaises(tk.TclError):
            self.app.root.winfo_exists()


class StartupTests(AppTestCase):
    def test_it_starts_idle_with_the_default_shortcuts(self):
        self.assertIs(self.app.state, State.IDLE)
        status, detail = self.shown()
        self.assertEqual(status, "Ready")
        self.assertEqual(detail, "Press F1 to record right away, F2 to stop, F3 to start the loop.")
        shown_keys = {action: var.get() for action, var in self.app.key_vars.items()}
        self.assertEqual(shown_keys, {"record": "F1", "stop": "F2", "loop": "F3", "pause": "F4"})

    def test_the_shortcuts_are_listened_for_from_the_start(self):
        self.assertEqual(self.hotkeys.keys, KEYS)
        self.assertEqual(self.hotkeys.started, 1)

    def test_every_shortcut_has_a_row_to_change_it(self):
        self.assertEqual(tuple(self.app.change_buttons), tuple(KEYS))
        self.assertEqual(tuple(self.app.key_vars), tuple(KEYS))

    def test_the_loop_settings_start_with_the_defaults(self):
        self.assertEqual(self.app.times_var.get(), "")
        self.assertEqual(self.app.delay_var.get(), "5")
        self.assertEqual(self.app.unit_var.get(), "seconds")
        self.assertEqual(tuple(self.app.unit_box.cget("values")), ("seconds", "minutes"))

    def test_without_a_macro_only_recording_and_importing_are_possible(self):
        self.assertEqual(self.enabled(), controls(*IDLE_WITHOUT_MACRO))
        self.assertIn("No macro yet", self.app.macro_var.get())

    def test_polling_is_scheduled(self):
        after = mock.Mock()
        self.app.root.after = after
        self.app._poll()
        after.assert_called_once_with(POLL_MS, self.app._poll)

    def test_polling_carries_on_after_an_error(self):
        after = mock.Mock()
        self.app.root.after = after
        with mock.patch.object(self.app, "_drain", side_effect=RuntimeError("boom")), self.assertRaises(RuntimeError):
            self.app._poll()
        after.assert_called_once_with(POLL_MS, self.app._poll)

    def test_polling_stops_once_the_app_is_closed(self):
        after = mock.Mock()
        self.app.root.after = after
        self.app.closed = True
        self.app._poll()
        after.assert_not_called()

    def test_main_runs_the_window(self):
        root = self.app.root
        root.mainloop = mock.Mock()
        with mock.patch.object(app_module.tk, "Tk", return_value=root), mock.patch.object(
            app_module, "App"
        ) as app_class, mock.patch.object(app_module.permissions, "prevent_app_nap") as prevent_nap:
            self.assertEqual(app_module.main([]), 0)
        prevent_nap.assert_called_once_with()
        app_class.assert_called_once_with(root)
        root.mainloop.assert_called_once_with()


class SelfTestTests(AppTestCase):
    """`--self-test` is how the release build proves that a packaged app starts."""

    def setUp(self):
        super().setUp()
        try:
            tk.Tk().destroy()
        except tk.TclError as exc:
            self.skipTest(f"Tk is not available here: {exc}")
        self.report = self.tmp / "report.txt"
        self.patch(app_module, "SELF_TEST_MS", new=10)
        self.prevent_nap = self.patch(app_module.permissions, "prevent_app_nap")
        self.windows = []
        self.real_tk, real_app = tk.Tk, App

        def hidden_root():
            root = self.real_tk()
            root.withdraw()
            self.addCleanup(self.destroy, root)
            return root

        def app_with_fakes(root):
            window = real_app(
                root,
                hotkeys_factory=FakeHotkeys,
                recorder=self.recorder,
                player=self.player,
                macros_dir=self.macros_dir,
            )
            self.windows.append(window)
            return window

        self.patch(app_module.tk, "Tk", side_effect=hidden_root)
        self.app_class = self.patch(app_module, "App", side_effect=app_with_fakes)

    def outcome(self):
        return self.report.read_text(encoding="utf-8")

    def test_it_opens_the_window_and_closes_it_again(self):
        self.assertEqual(app_module.self_test(self.report), 0)
        (window,) = self.windows
        self.assertTrue(window.closed)
        self.assertEqual(window.hotkeys.started, 1)
        self.assertEqual(window.hotkeys.stopped, 1)
        self.prevent_nap.assert_called_once_with()
        self.assertRegex(self.outcome(), r"^ok: Python \S+ on \S+, Tk \d")

    def test_it_fails_when_the_window_cannot_be_built(self):
        self.app_class.side_effect = RuntimeError("no window today")
        self.assertEqual(app_module.self_test(self.report), 1)
        self.assertIn("RuntimeError: no window today", self.outcome())

    def test_it_fails_when_something_goes_wrong_while_the_window_is_open(self):
        build = self.app_class.side_effect

        def app_that_fails_later(root):
            root.after(0, lambda: 1 / 0)
            return build(root)

        self.app_class.side_effect = app_that_fails_later
        self.assertEqual(app_module.self_test(self.report), 1)
        self.assertIn("ZeroDivisionError", self.outcome())
        self.assertTrue(self.windows[0].closed)

    def test_it_fails_rather_than_hangs_when_the_window_does_not_close(self):
        build = self.app_class.side_effect
        rescued = []

        def app_that_cannot_close(root):
            window = build(root)
            window.close = mock.Mock(side_effect=RuntimeError("stuck open"))
            root.after(5000, lambda: (rescued.append("the self-test hung"), root.destroy()))
            return window

        self.app_class.side_effect = app_that_cannot_close
        self.assertEqual(app_module.self_test(self.report), 1)
        self.assertEqual(rescued, [])
        self.assertIn("RuntimeError: stuck open", self.outcome())

    def test_it_fails_when_a_module_the_app_can_do_without_is_missing(self):
        self.patch(app_module, "SELF_TEST_IMPORTS", new={sys.platform: ("no_such_module_for_the_self_test",)})
        self.assertEqual(app_module.self_test(self.report), 1)
        self.assertIn("No module named 'no_such_module_for_the_self_test'", self.outcome())
        self.app_class.assert_not_called()

    def test_the_report_is_optional(self):
        self.assertEqual(app_module.self_test(), 0)
        self.assertFalse(self.report.exists())

    def test_main_runs_it_when_asked_and_returns_its_exit_code(self):
        with mock.patch.object(app_module, "self_test", return_value=1) as self_test, mock.patch.object(
            self.real_tk, "mainloop"
        ):
            self.assertEqual(app_module.main(["--self-test", str(self.report)]), 1)
            self.assertEqual(app_module.main(["--self-test"]), 1)
        self.assertEqual(self_test.call_args_list, [mock.call(self.report), mock.call(None)])
        self.app_class.assert_not_called()

    def test_main_reads_the_command_line_by_default(self):
        with mock.patch.object(app_module, "self_test", return_value=0) as self_test, mock.patch.object(
            app_module.sys, "argv", ["MouseMacroRecorder", "--self-test", str(self.report)]
        ), mock.patch.object(self.real_tk, "mainloop"):
            self.assertEqual(app_module.main(), 0)
        self_test.assert_called_once_with(self.report)

    def test_other_arguments_start_the_app_as_usual(self):
        # macOS used to pass a process serial number to apps opened from the Finder.
        with mock.patch.object(app_module, "self_test") as self_test, mock.patch.object(self.real_tk, "mainloop"):
            self.assertEqual(app_module.main(["-psn_0_12345"]), 0)
        self_test.assert_not_called()
        self.app_class.assert_called_once()


class RecordingTests(AppTestCase):
    def test_the_record_button_counts_down_from_three_and_then_records(self):
        app = self.app
        app.start_countdown()
        self.assertIs(app.state, State.COUNTDOWN)
        self.assertEqual(self.enabled(), controls("stop"))
        shown = []
        for elapsed in (0, 0.9, 1.1, 2.1, 2.9):
            self.clock.now = 1000 + elapsed
            app._tick()
            shown.append(app.status_var.get())
        self.assertEqual(shown, ["3", "3", "2", "1", "1"])
        self.assertEqual(self.recorder.starts, 0)
        self.clock.now = 1003
        app._tick()
        self.assertIs(app.state, State.RECORDING)
        self.assertEqual(self.recorder.starts, 1)
        self.assertEqual(app.status_var.get(), "Recording")
        self.bell.assert_called_once_with()

    def test_the_stop_button_cancels_the_countdown(self):
        self.app.start_countdown()
        self.app.stop_from_button()
        self.assertIs(self.app.state, State.IDLE)
        self.clock.now += 10
        self.app._tick()
        self.assertEqual(self.recorder.starts, 0)
        self.assertEqual(self.enabled(), controls(*IDLE_WITHOUT_MACRO))

    def test_the_stop_shortcut_cancels_the_countdown(self):
        self.app.start_countdown()
        self.press("stop")
        self.assertIs(self.app.state, State.IDLE)
        self.assertEqual(self.recorder.starts, 0)

    def test_the_record_shortcut_starts_recording_at_once(self):
        self.press("record")
        self.assertIs(self.app.state, State.RECORDING)
        self.assertEqual(self.recorder.starts, 1)
        self.assertEqual(self.enabled(), controls("stop"))

    def test_the_record_shortcut_skips_a_running_countdown(self):
        self.app.start_countdown()
        self.press("record")
        self.assertIs(self.app.state, State.RECORDING)
        self.clock.now += 10
        self.app._tick()
        self.assertEqual(self.recorder.starts, 1)

    def test_the_record_shortcut_does_nothing_while_already_recording(self):
        self.press("record")
        self.press("record")
        self.assertEqual(self.recorder.starts, 1)

    def test_the_stop_shortcut_does_nothing_when_idle(self):
        self.press("stop")
        self.assertIs(self.app.state, State.IDLE)
        self.assertEqual(self.shown()[0], "Ready")
        self.assertNoDialog()

    def test_the_shortcut_handler_runs_on_the_keyboard_thread_and_only_queues_the_work(self):
        self.hotkeys.on_action("record")
        self.assertIs(self.app.state, State.IDLE)
        self.assertEqual(self.recorder.starts, 0)
        self.app._drain()
        self.assertIs(self.app.state, State.RECORDING)

    def test_the_timer_and_event_count_are_shown_while_recording(self):
        self.press("record")
        self.assertEqual(self.shown()[0], "Recording")
        self.clock.now += 65
        self.recorder.events = [click(0.0)] * 3
        self.app._tick()
        self.assertEqual(self.shown(), ("Recording", "01:05 · 3 events\nPress F2 or click Stop to stop."))
        self.recorder.events = [click(0.0)]
        self.app._tick()
        self.assertIn("1 event\n", self.shown()[1])

    def test_stopping_with_the_shortcut_saves_the_macro(self):
        self.press("record")
        self.press("stop")
        app = self.app
        self.assertIs(app.state, State.IDLE)
        self.assertEqual(self.recorder.stops, 1)
        files = list(self.macros_dir.glob("macro-*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(app.macro_path, files[0])
        self.assertEqual(app.macro.events, SAMPLE)  # the 5 s of waiting before the first event is gone
        self.assertEqual(Recording.load(files[0]).events, SAMPLE)
        self.assertEqual(self.shown()[0], "Recorded")
        self.assertIn(str(files[0]), app.macro_var.get())
        self.assertIn("0.6 s · 3 events", app.macro_var.get())
        self.assertEqual(self.enabled(), controls(*IDLE_WITH_MACRO))
        self.assertEqual(self.bell.call_count, 2)  # when recording started and when it stopped
        self.assertNoDialog()

    def test_stopping_with_the_button_saves_the_macro_too(self):
        self.app._window_area = lambda: (0, 0, 10, 10)
        self.press("record")
        self.app.stop_from_button()
        self.assertEqual(self.app.macro.events, SAMPLE)
        self.assertEqual(len(list(self.macros_dir.glob("macro-*.json"))), 1)

    def test_the_click_on_the_stop_button_is_not_part_of_the_macro(self):
        self.app._window_area = lambda: (0, 0, 200, 100)
        self.recorder.events = [
            Event(1.0, "move", 400, 300),
            click(1.5),
            click(1.6, pressed=False),
            Event(2.0, "move", 100, 50),
            click(2.2, 100, 50),
            click(2.3, 100, 50, pressed=False),
        ]
        self.press("record")
        self.app.stop_from_button()
        self.assertEqual([(e.x, e.y) for e in self.app.macro.events], [(400, 300)] * 3)

    def test_stopping_with_the_shortcut_keeps_everything(self):
        self.app._window_area = lambda: (0, 0, 200, 100)
        self.recorder.events = [Event(1.0, "move", 400, 300), Event(2.0, "move", 100, 50), click(2.2, 100, 50)]
        self.press("record")
        self.press("stop")
        self.assertEqual(len(self.app.macro.events), 3)

    def test_a_recording_made_only_inside_the_window_is_empty(self):
        self.app._window_area = lambda: (0, 0, 200, 100)
        self.recorder.events = [Event(1.0, "move", 100, 50), click(1.1, 100, 50), click(1.2, 100, 50, pressed=False)]
        self.press("record")
        self.app.stop_from_button()
        self.assertEqual(self.messagebox.showwarning.call_args.args[0], "Nothing recorded")
        self.assertIsNone(self.app.macro)

    def test_an_empty_recording_is_reported_and_not_saved(self):
        self.recorder.events = []
        self.press("record")
        self.press("stop")
        self.messagebox.showwarning.assert_called_once()
        title, message = self.messagebox.showwarning.call_args.args[:2]
        self.assertEqual(title, "Nothing recorded")
        self.assertNotIn("Input Monitoring", message)
        self.assertIs(self.app.state, State.IDLE)
        self.assertIsNone(self.app.macro)
        self.assertFalse(self.macros_dir.exists())

    def test_the_empty_recording_message_mentions_the_macos_permission_when_it_is_missing(self):
        self.recorder.events = []
        self.launch()  # built while the permissions were still granted
        self.missing_permissions.return_value = ["Input Monitoring"]
        self.press("record")
        self.press("stop")
        self.assertIn("Allow Input Monitoring for the app that runs", self.messagebox.showwarning.call_args.args[1])

    def test_an_empty_recording_keeps_the_previous_macro(self):
        self.load_macro()
        previous = self.app.macro
        self.recorder.events = []
        self.press("record")
        self.press("stop")
        self.assertIs(self.app.macro, previous)
        self.assertEqual(self.enabled(), controls(*IDLE_WITH_MACRO))

    def test_a_macro_that_cannot_be_saved_can_still_be_replayed(self):
        with mock.patch.object(Recording, "save", side_effect=OSError("disk full")):
            self.press("record")
            self.press("stop")
        self.messagebox.showerror.assert_called_once()
        title, message = self.messagebox.showerror.call_args.args[:2]
        self.assertEqual(title, "Could not save the macro")
        self.assertIn("disk full", message)
        self.assertEqual(self.app.macro.events, SAMPLE)
        self.assertIsNone(self.app.macro_path)
        self.assertIn("Not saved to a file", self.app.macro_var.get())
        self.assertEqual(self.enabled(), controls(*set(IDLE_WITH_MACRO) - {"folder"}))  # there is no file to show
        for _ in range(2):
            self.press("record")
            self.press("stop")
        self.assertEqual(len(list(self.macros_dir.glob("macro-*.json"))), 2)

    def test_a_recorder_that_cannot_start_is_reported(self):
        self.recorder.start_error = RuntimeError("no access")
        self.press("record")
        self.assertIs(self.app.state, State.IDLE)
        title, message = self.messagebox.showerror.call_args.args[:2]
        self.assertEqual((title, message), ("Could not start recording", "no access"))

    def test_a_recorder_that_cannot_start_after_the_countdown_is_reported(self):
        self.recorder.start_error = RuntimeError("no access")
        self.app.start_countdown()
        self.clock.now += 3
        self.app._tick()
        self.assertIs(self.app.state, State.IDLE)
        self.messagebox.showerror.assert_called_once()


class ImportTests(AppTestCase):
    def test_a_saved_macro_can_be_imported(self):
        path = self.load_macro()
        app = self.app
        self.assertEqual(app.macro.events, SAMPLE)
        self.assertEqual(app.macro_path, path)
        self.assertEqual(self.shown()[0], "Imported")
        self.assertIn(str(path), app.macro_var.get())
        self.assertIn("0.6 s · 3 events", app.macro_var.get())
        self.assertEqual(self.enabled(), controls(*IDLE_WITH_MACRO))

    def test_a_recording_can_be_imported_again_after_it_was_made(self):
        self.press("record")
        self.press("stop")
        saved = self.app.macro_path
        self.load_macro("other.json")
        self.assertNotEqual(self.app.macro_path, saved)
        self.filedialog.askopenfilename.return_value = str(saved)
        self.app.import_macro()
        self.assertEqual(self.app.macro_path, saved)

    def test_cancelling_the_dialog_changes_nothing(self):
        self.app.import_macro()
        self.assertIsNone(self.app.macro)
        self.assertEqual(self.shown()[0], "Ready")
        self.assertNoDialog()

    def test_unusable_files_are_reported_and_the_current_macro_is_kept(self):
        good = self.load_macro("good.json")
        loaded = self.app.macro
        for name, text, reason in (
            ("broken.json", "{not json", "not valid JSON"),
            ("other.json", '{"hello": 1}', "not a mouse macro file"),
            ("empty.json", '{"format": "mouse-macro", "version": 1, "events": []}', "no events"),
        ):
            with self.subTest(name):
                (self.tmp / name).write_text(text, encoding="utf-8")
                self.filedialog.askopenfilename.return_value = str(self.tmp / name)
                self.messagebox.showerror.reset_mock()
                self.app.import_macro()
                title, message = self.messagebox.showerror.call_args.args[:2]
                self.assertEqual(title, "Could not import the macro")
                self.assertIn(name, message)
                self.assertIn(reason, message)
                self.assertIs(self.app.macro, loaded)
                self.assertEqual(self.app.macro_path, good)

    def test_a_missing_file_is_reported(self):
        self.filedialog.askopenfilename.return_value = str(self.tmp / "gone.json")
        self.app.import_macro()
        self.assertIn("gone.json", self.messagebox.showerror.call_args.args[1])
        self.assertIsNone(self.app.macro)

    def test_the_dialog_opens_in_the_macros_folder_once_it_exists(self):
        self.app.import_macro()
        self.assertEqual(self.filedialog.askopenfilename.call_args.kwargs["initialdir"], str(Path.home()))
        self.macros_dir.mkdir()
        self.app.import_macro()
        self.assertEqual(self.filedialog.askopenfilename.call_args.kwargs["initialdir"], str(self.macros_dir))

    def test_importing_is_not_possible_while_busy(self):
        self.press("record")
        self.app.import_macro()
        self.filedialog.askopenfilename.assert_not_called()


class PlaybackTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.load_macro()

    def test_repeat_plays_the_macro_once(self):
        self.app.repeat()
        self.assertIs(self.app.state, State.PLAYING)
        self.assertEqual(self.shown(), ("Playing", PLAYING_HINT))
        self.finish_worker()
        self.assertEqual(self.player.calls, [(self.app.macro, 1, 0.0)])
        self.assertIs(self.app.state, State.IDLE)
        self.assertEqual(self.shown(), ("Finished", ""))
        self.assertEqual(self.enabled(), controls(*IDLE_WITH_MACRO))

    def test_loop_uses_the_values_in_the_window(self):
        cases = (
            ("10", "25", "seconds", (10, 25.0)),
            ("", "5", "seconds", (None, 5.0)),
            ("3", "2", "minutes", (3, 120.0)),
            ("", "1.5", "minutes", (None, 90.0)),
            ("1", "0", "seconds", (1, 0.0)),
        )
        for times, delay, unit, expected in cases:
            with self.subTest(times=times, delay=delay, unit=unit):
                self.player.calls.clear()
                self.app.times_var.set(times)
                self.app.delay_var.set(delay)
                self.app.unit_var.set(unit)
                self.app.loop()
                self.finish_worker()
                self.assertEqual([call[1:] for call in self.player.calls], [expected])

    def test_the_default_loop_repeats_forever_with_five_seconds_between_runs(self):
        self.app.loop()
        self.finish_worker()
        self.assertEqual([call[1:] for call in self.player.calls], [(None, 5.0)])

    def test_a_finished_loop_reports_the_number_of_runs(self):
        self.app.times_var.set("10")
        self.app.loop()
        self.finish_worker()
        self.assertEqual(self.shown(), ("Finished", "10 of 10 runs completed"))

    def test_invalid_loop_settings_are_reported_and_nothing_plays(self):
        for times, delay in (("abc", "5"), ("0", "5"), ("2.5", "5"), ("2", "x"), ("2", "-1"), ("2", "")):
            with self.subTest(times=times, delay=delay):
                self.messagebox.showerror.reset_mock()
                self.app.times_var.set(times)
                self.app.delay_var.set(delay)
                self.app.loop()
                self.messagebox.showerror.assert_called_once()
                self.assertEqual(self.messagebox.showerror.call_args.args[0], "Loop settings")
                self.assertEqual(self.player.calls, [])
                self.assertIs(self.app.state, State.IDLE)

    def test_every_status_is_shown_while_the_window_keeps_up(self):
        self.player.block = True
        self.app.loop()
        for headline, detail in (
            ("Playing", "Run 1 of 2"),
            ("Waiting", "Run 2 of 2 starts in 5 s"),
            ("Playing", "Run 2 of 2"),
        ):
            self.app._post_status(headline, detail)
            self.app._drain()
            self.assertEqual(self.shown(), (headline, f"{detail}\n{PLAYING_HINT}"))

    def test_a_player_reporting_faster_than_the_window_polls_cannot_flood_the_queue(self):
        self.player.statuses = [("Playing", f"Run {number}") for number in range(1, 5001)]
        self.player.block = True
        self.app.loop()
        self.assertTrue(self.player.reported.wait(5))
        self.assertEqual(self.app.messages.qsize(), 1)
        self.app._drain()
        self.assertEqual(self.shown(), ("Playing", f"Run 5000\n{PLAYING_HINT}"))
        self.assertEqual(self.app.messages.qsize(), 0)

    def test_a_drain_ends_even_when_a_thread_keeps_filling_the_queue(self):
        handled = []

        def busy_producer(action):
            handled.append(action)
            if len(handled) > 100:
                raise AssertionError("the drain never returned to the window")
            self.app.messages.put(("hotkey", action))  # a new message for every one that is handled

        self.app.messages.put(("hotkey", "first"))
        with mock.patch.object(self.app, "_handle_hotkey", side_effect=busy_producer):
            self.app._drain()
        self.assertEqual(handled, ["first"])
        self.assertEqual(self.app.messages.qsize(), 1)  # left for the next poll

    def test_statuses_are_shown_with_the_pause_and_stop_hint(self):
        self.player.block = True
        self.app.loop()
        self.app._handle_status("Waiting", "Run 2 of 10 starts in 25 s")
        self.assertEqual(self.shown(), ("Waiting", f"Run 2 of 10 starts in 25 s\n{PLAYING_HINT}"))
        self.app._handle_status("Playing", "")
        self.assertEqual(self.shown(), ("Playing", PLAYING_HINT))

    def test_a_late_status_is_ignored(self):
        before = self.shown()
        self.app._handle_status("Waiting", "Run 2 of 10 starts in 25 s")
        self.assertEqual(self.shown(), before)

    def test_the_stop_button_stops_playback(self):
        self.player.block = True
        self.player.completed = 3
        self.app.times_var.set("10")
        self.app.loop()
        self.app.stop_from_button()
        self.assertTrue(self.app.stop_requested.is_set())
        self.assertEqual(self.shown()[0], "Stopping")
        self.finish_worker()
        self.assertIs(self.app.state, State.IDLE)
        self.assertEqual(self.shown(), ("Stopped", "3 of 10 runs completed"))

    def test_the_stop_shortcut_stops_playback_without_waiting_for_the_next_poll(self):
        self.player.block = True
        self.player.completed = 2
        self.app.loop()
        self.hotkeys.on_action("stop")  # on the keyboard thread
        self.assertTrue(self.app.stop_requested.is_set())
        self.finish_worker()
        self.assertEqual(self.shown(), ("Stopped", "2 runs completed"))

    def test_a_new_run_is_not_stopped_by_an_earlier_stop_request(self):
        self.press("stop")
        self.assertTrue(self.app.stop_requested.is_set())
        self.app.repeat()
        self.assertFalse(self.app.stop_requested.is_set())
        self.finish_worker()
        self.assertEqual(self.shown()[0], "Finished")

    def test_a_playback_error_is_reported(self):
        self.player.error = RuntimeError("boom")
        self.app.loop()
        self.finish_worker()
        self.assertEqual(self.messagebox.showerror.call_args.args[:2], ("Playback failed", "boom"))
        self.assertIs(self.app.state, State.IDLE)
        self.assertEqual(self.enabled(), controls(*IDLE_WITH_MACRO))

    def test_nothing_else_can_start_while_playing(self):
        self.player.block = True
        self.app.loop()
        self.press("record")
        self.app.start_countdown()
        self.app.repeat()
        self.app.loop()
        self.app.import_macro()
        self.assertIs(self.app.state, State.PLAYING)
        self.assertEqual(self.recorder.starts, 0)
        self.assertEqual(len(self.player.calls), 1)
        self.filedialog.askopenfilename.assert_called_once()  # only the import that loaded the macro
        self.assertEqual(self.enabled(), controls("stop", "pause"))

    def test_repeat_and_loop_need_a_macro(self):
        app = self.app
        app.macro = None
        app.repeat()
        app.loop()
        self.assertEqual(self.player.calls, [])
        self.assertIs(app.state, State.IDLE)


class LoopThroughTheRealPlayerTests(AppTestCase):
    """The window with the real Player driving a fake mouse, to check the whole loop is wired up."""

    QUICK_MACRO = [Event(0.0, "move", 400, 300), click(0.01), click(0.02, pressed=False)]

    def presses(self, mouse):
        return sum(action[0] == "press" for action in mouse.actions())

    def test_a_loop_runs_the_macro_the_requested_number_of_times(self):
        mouse = FakeMouse()
        self.player = Player(mouse)
        self.load_macro(events=self.QUICK_MACRO)
        self.app.times_var.set("3")
        self.app.delay_var.set("0.05")
        self.app.loop()
        self.finish_worker()
        actions = mouse.actions()
        self.assertEqual([action for action in actions if action[0] == "press"], [("press", "left", 1)] * 3)
        self.assertEqual([action for action in actions if action[0] == "release"], [("release", "left", 1)] * 3)
        self.assertEqual(self.shown(), ("Finished", "3 of 3 runs completed"))

    def test_a_loop_without_a_count_runs_until_stopped(self):
        mouse = FakeMouse()
        self.player = Player(mouse)
        self.load_macro(events=self.QUICK_MACRO)
        self.app.delay_var.set("0.01")
        self.app.loop()
        give_up = time.monotonic() + 5
        while self.presses(mouse) < 3 and time.monotonic() < give_up:
            time.sleep(0.01)
        # The worker may report it has finished before or after the shortcut message, either way the loop ends.
        self.hotkeys.on_action("stop")
        self.finish_worker()
        self.assertGreaterEqual(self.presses(mouse), 3)
        self.assertEqual(self.shown()[0], "Stopped")
        self.assertEqual(mouse.entered, mouse.exited)

    def wait_until(self, condition, what):
        give_up = time.monotonic() + 5
        while not condition():
            self.assertLess(time.monotonic(), give_up, f"timed out waiting for {what}")
            time.sleep(0.01)
            self.app._drain()

    def test_a_loop_stands_still_while_paused_and_carries_on_when_resumed(self):
        mouse = FakeMouse()
        self.player = Player(mouse)
        self.load_macro(events=self.QUICK_MACRO)
        self.app.delay_var.set("0.01")
        self.app.loop()
        self.wait_until(lambda: self.presses(mouse) >= 2, "the loop to start")
        self.press("pause")
        self.wait_until(lambda: self.app.status_var.get() == "Paused", "the player to stand still")
        self.assertEqual(self.app.detail_var.get().splitlines()[-1], PAUSED_HINT)
        frozen = self.presses(mouse)
        time.sleep(0.3)
        self.app._drain()
        self.assertEqual(self.presses(mouse), frozen)
        self.assertEqual(self.app.status_var.get(), "Paused")
        self.press("loop")
        self.wait_until(lambda: self.presses(mouse) > frozen, "the loop to carry on")
        self.assertIn(self.app.status_var.get(), ("Playing", "Waiting"))
        self.hotkeys.on_action("stop")
        self.finish_worker()
        self.assertEqual(self.shown()[0], "Stopped")
        self.assertEqual(mouse.entered, mouse.exited)

    def test_a_paused_loop_can_be_stopped(self):
        mouse = FakeMouse()
        self.player = Player(mouse)
        self.load_macro(events=self.QUICK_MACRO)
        self.app.delay_var.set("0.01")
        self.app.loop()
        self.wait_until(lambda: self.presses(mouse) >= 1, "the loop to start")
        self.press("pause")
        self.wait_until(lambda: self.app.status_var.get() == "Paused", "the player to stand still")
        self.hotkeys.on_action("stop")
        self.finish_worker()
        self.assertEqual(self.shown()[0], "Stopped")
        self.assertEqual(mouse.entered, mouse.exited)


class ControlsTests(AppTestCase):
    def test_only_stop_and_pause_are_usable_while_busy(self):
        self.load_macro()
        app = self.app
        app.start_countdown()
        self.assertEqual(self.enabled(), controls("stop"))
        app.stop_from_button()
        self.assertEqual(self.enabled(), controls(*IDLE_WITH_MACRO))
        self.press("record")
        self.assertEqual(self.enabled(), controls("stop"))
        self.recorder.events = []
        app.stop_from_button()
        self.player.block = True
        app.loop()
        self.assertEqual(self.enabled(), controls("stop", "pause"))

    def test_changing_a_shortcut_disables_everything_but_its_cancel_button(self):
        self.load_macro()
        self.app.change_shortcut("stop")
        self.assertEqual(self.enabled(), controls("change_stop"))
        self.assertEqual(str(self.app.change_buttons["stop"].cget("text")), "Cancel")
        self.assertEqual(str(self.app.change_buttons["record"].cget("text")), "Change…")


class ShortcutTests(AppTestCase):
    def capture(self, name):
        """The user presses a key while a shortcut is being chosen."""
        self.hotkeys.capture(name)
        self.app._drain()

    def test_choosing_a_new_shortcut(self):
        app = self.app
        app.change_shortcut("record")
        self.assertEqual(app.capturing, "record")
        self.assertEqual(app.key_vars["record"].get(), "Press a key...")
        self.assertIsNotNone(self.hotkeys.capture)
        self.capture("f5")
        self.assertIsNone(app.capturing)
        self.assertEqual(app.keys, {**KEYS, "record": "f5"})
        self.assertEqual(self.hotkeys.keys, {**KEYS, "record": "f5"})
        self.save_hotkeys.assert_called_once_with({**KEYS, "record": "f5"})
        self.assertEqual(app.key_vars["record"].get(), "F5")
        self.assertIn("F5", app.detail_var.get())
        self.assertEqual(str(app.change_buttons["record"].cget("text")), "Change…")
        self.assertEqual(self.enabled(), controls(*IDLE_WITHOUT_MACRO))
        self.assertNoDialog()

    def test_the_stop_shortcut_can_be_changed_too(self):
        self.app.change_shortcut("stop")
        self.capture("esc")
        self.assertEqual(self.app.keys, {**KEYS, "stop": "esc"})
        self.assertIn("Esc", self.app.detail_var.get())

    def test_the_loop_and_pause_shortcuts_can_be_changed_too(self):
        app = self.app
        for action, key, label in (("loop", "f7", "F7"), ("pause", "f8", "F8")):
            with self.subTest(action=action):
                app.change_shortcut(action)
                self.assertEqual(app.capturing, action)
                self.assertEqual(app.key_vars[action].get(), "Press a key...")
                self.capture(key)
                self.assertEqual(app.keys[action], key)
                self.assertEqual(self.hotkeys.keys[action], key)
                self.assertEqual(app.key_vars[action].get(), label)
        self.assertEqual(app.keys, {**KEYS, "loop": "f7", "pause": "f8"})
        self.assertEqual(self.save_hotkeys.call_count, 2)  # every change is saved
        self.assertIn("F7", app.detail_var.get())  # the hint names the loop key

    def test_pressing_the_button_again_cancels(self):
        app = self.app
        app.change_shortcut("record")
        app.change_shortcut("record")
        self.assertIsNone(app.capturing)
        self.assertEqual(self.hotkeys.cancelled, 1)
        self.assertEqual(app.key_vars["record"].get(), "F1")
        self.assertEqual(self.enabled(), controls(*IDLE_WITHOUT_MACRO))
        self.save_hotkeys.assert_not_called()

    def test_a_key_that_arrives_after_cancelling_is_ignored(self):
        app = self.app
        app.change_shortcut("record")
        callback = self.hotkeys.capture
        app.change_shortcut("record")
        callback("f5")
        app._drain()
        self.assertEqual(app.keys, KEYS)
        self.assertNoDialog()
        self.save_hotkeys.assert_not_called()

    def test_a_key_already_used_by_another_action_is_refused(self):
        app = self.app
        app.change_shortcut("record")
        self.capture("f2")
        title, message = self.messagebox.showwarning.call_args.args[:2]
        self.assertEqual(title, "Shortcut not changed")
        self.assertIn("F2", message)
        self.assertIn("stopping", message)
        self.assertEqual(app.keys, KEYS)
        self.assertEqual(self.hotkeys.keys, KEYS)
        self.assertEqual(app.key_vars["record"].get(), "F1")
        self.assertIsNone(app.capturing)
        self.save_hotkeys.assert_not_called()

    def test_no_action_can_take_the_key_of_another(self):
        names = {
            "record": "starting a recording",
            "stop": "stopping",
            "loop": "starting or resuming the loop",
            "pause": "pausing the loop",
        }
        for action in KEYS:
            for other, key in KEYS.items():
                if other == action:
                    continue
                with self.subTest(action=action, key=key):
                    self.messagebox.showwarning.reset_mock()
                    self.app.change_shortcut(action)
                    self.capture(key)
                    self.assertIn(names[other], self.messagebox.showwarning.call_args.args[1])
                    self.assertEqual(self.app.keys, KEYS)
        self.save_hotkeys.assert_not_called()

    def test_choosing_the_key_it_already_has_is_fine(self):
        self.app.change_shortcut("record")
        self.capture("f1")
        self.assertNoDialog()
        self.assertEqual(self.app.keys["record"], "f1")

    def test_a_key_that_cannot_be_used_is_refused(self):
        self.app.change_shortcut("record")
        self.capture("not a key")
        self.assertIn("cannot be used", self.messagebox.showwarning.call_args.args[1])
        self.assertEqual(self.app.keys["record"], "f1")

    def test_a_shortcut_that_cannot_be_saved_still_works_for_this_session(self):
        self.save_hotkeys.side_effect = OSError("read-only")
        self.app.change_shortcut("record")
        self.capture("f6")
        self.assertEqual(self.app.keys["record"], "f6")
        self.assertEqual(self.hotkeys.keys["record"], "f6")
        title, message = self.messagebox.showwarning.call_args.args[:2]
        self.assertEqual(title, "Shortcut not saved")
        self.assertIn("read-only", message)

    def test_shortcuts_cannot_be_changed_while_busy(self):
        self.press("record")
        self.app.change_shortcut("record")
        self.assertIsNone(self.app.capturing)
        self.assertIsNone(self.hotkeys.capture)

    def test_nothing_starts_while_a_shortcut_is_being_chosen(self):
        app = self.app
        app.change_shortcut("stop")
        self.press("record")
        app.start_countdown()
        app.import_macro()
        self.assertIs(app.state, State.IDLE)
        self.assertEqual(self.recorder.starts, 0)
        self.filedialog.askopenfilename.assert_not_called()

    def test_playing_cannot_start_while_a_shortcut_is_being_chosen(self):
        self.load_macro()
        self.app.change_shortcut("stop")
        self.app.repeat()
        self.app.loop()
        self.assertEqual(self.player.calls, [])


class LoopShortcutTests(AppTestCase):
    """The loop shortcut does what the Loop button does."""

    def test_it_starts_the_loop_with_the_values_in_the_window(self):
        self.load_macro()
        self.app.times_var.set("3")
        self.app.delay_var.set("2")
        self.press("loop")
        self.assertIs(self.app.state, State.PLAYING)
        self.finish_worker()
        self.assertEqual(self.player.calls, [(self.app.macro, 3, 2.0)])
        self.assertEqual(self.shown(), ("Finished", "3 of 3 runs completed"))

    def test_without_a_macro_it_says_so_and_nothing_starts(self):
        self.press("loop")
        self.assertEqual(self.shown(), ("Nothing to loop", "Record a macro or import one first."))
        self.assertIs(self.app.state, State.IDLE)
        self.assertEqual(self.player.calls, [])
        self.assertNoDialog()

    def test_invalid_loop_settings_are_reported_like_the_button_does(self):
        self.load_macro()
        self.app.times_var.set("abc")
        self.press("loop")
        self.assertEqual(self.messagebox.showerror.call_args.args[0], "Loop settings")
        self.assertEqual(self.player.calls, [])
        self.assertIs(self.app.state, State.IDLE)

    def test_it_does_nothing_while_counting_down_recording_or_choosing_a_key(self):
        self.load_macro()
        app = self.app
        app.start_countdown()
        self.press("loop")
        self.assertIs(app.state, State.COUNTDOWN)
        app.stop_from_button()
        self.press("record")
        self.press("loop")
        self.assertIs(app.state, State.RECORDING)
        app.stop_from_button()
        app.change_shortcut("stop")
        self.press("loop")
        self.assertIs(app.state, State.IDLE)
        self.assertEqual(self.player.calls, [])

    def test_it_does_not_start_a_second_loop_while_one_is_running(self):
        self.load_macro()
        self.player.block = True
        self.press("loop")
        self.press("loop")
        self.assertEqual(len(self.player.calls), 1)
        self.assertEqual(self.shown(), ("Playing", PLAYING_HINT))


class PausedPlaybackTests(AppTestCase):
    """The pause shortcut holds a running macro, the loop shortcut (or the Loop button) carries on."""

    def setUp(self):
        super().setUp()
        self.load_macro()
        self.player.block = True

    def start(self, times="10"):
        self.app.times_var.set(times)
        self.app.loop()

    def test_the_player_is_given_the_pause_flag(self):
        self.start()
        self.assertTrue(self.player.reported.wait(5))
        self.assertIs(self.player.pause, self.app.pause_requested)

    def test_the_pause_shortcut_pauses(self):
        self.start()
        self.press("pause")
        self.assertTrue(self.app.pause_requested.is_set())
        self.assertEqual(self.shown(), ("Pausing", PAUSED_HINT))
        self.assertEqual(self.enabled(), controls("stop", "loop"))
        self.assertEqual(str(self.app.loop_button.cget("text")), "Resume")

    def test_the_pause_button_pauses(self):
        self.start()
        self.app.pause_button.invoke()
        self.assertTrue(self.app.pause_requested.is_set())
        self.assertEqual(self.shown(), ("Pausing", PAUSED_HINT))

    def test_a_single_repeat_can_be_paused_too(self):
        self.app.repeat()
        self.press("pause")
        self.assertEqual(self.shown(), ("Pausing", PAUSED_HINT))
        self.press("loop")
        self.assertEqual(self.shown(), ("Playing", PLAYING_HINT))
        self.assertEqual(len(self.player.calls), 1)

    def test_the_window_says_paused_once_the_player_stands_still(self):
        self.start()
        self.press("pause")
        self.app._handle_status("Paused", "Run 3 of 10")
        self.assertEqual(self.shown(), ("Paused", f"Run 3 of 10\n{PAUSED_HINT}"))

    def test_a_report_of_a_pause_that_is_already_over_is_ignored(self):
        self.start()
        self.press("pause")
        self.press("loop")
        self.app._handle_status("Paused", "Run 3 of 10")
        self.assertEqual(self.shown(), ("Playing", PLAYING_HINT))

    def test_the_loop_shortcut_resumes_and_shows_what_the_player_last_said(self):
        self.start()
        self.app._handle_status("Waiting", "Run 4 of 10 starts in 5 s")
        self.press("pause")
        self.app._handle_status("Paused", "Run 4 of 10 starts in 5 s")
        self.press("loop")
        self.assertFalse(self.app.pause_requested.is_set())
        self.assertEqual(self.shown(), ("Waiting", f"Run 4 of 10 starts in 5 s\n{PLAYING_HINT}"))
        self.assertEqual(self.enabled(), controls("stop", "pause"))
        self.assertEqual(str(self.app.loop_button.cget("text")), "Loop")
        self.assertEqual(len(self.player.calls), 1)  # it carried on, it did not start another loop

    def test_the_loop_button_says_resume_and_resumes(self):
        self.start()
        self.press("pause")
        self.assertEqual(str(self.app.loop_button.cget("text")), "Resume")
        self.app.loop_button.invoke()
        self.assertFalse(self.app.pause_requested.is_set())
        self.assertEqual(self.shown(), ("Playing", PLAYING_HINT))
        self.assertEqual(len(self.player.calls), 1)

    def test_what_the_player_says_while_paused_is_shown_after_resuming(self):
        self.start()
        self.press("pause")
        self.app._handle_status("Paused", "Run 3 of 10")
        self.app._handle_status("Waiting", "Run 4 of 10 starts in 5 s")
        self.assertEqual(self.shown(), ("Paused", f"Run 3 of 10\n{PAUSED_HINT}"))
        self.press("loop")
        self.assertEqual(self.shown(), ("Waiting", f"Run 4 of 10 starts in 5 s\n{PLAYING_HINT}"))

    def test_pausing_twice_or_resuming_a_running_macro_changes_nothing(self):
        self.start()
        running = self.shown()
        self.press("loop")
        self.assertEqual(self.shown(), running)
        self.assertEqual(len(self.player.calls), 1)
        self.press("pause")
        paused = self.shown()
        self.press("pause")
        self.assertEqual(self.shown(), paused)

    def test_a_paused_macro_can_be_stopped(self):
        self.player.completed = 2
        self.start()
        self.press("pause")
        self.app._handle_status("Paused", "Run 3 of 10")
        self.press("stop")
        self.assertTrue(self.app.stop_requested.is_set())
        self.assertEqual(self.shown(), ("Stopping", ""))
        self.assertEqual(self.enabled(), controls("stop"))
        self.assertEqual(str(self.app.loop_button.cget("text")), "Loop")
        self.app._handle_status("Playing", "Run 3 of 10")  # news that is out of date by now
        self.assertEqual(self.shown(), ("Stopping", ""))
        self.finish_worker()
        self.assertEqual(self.shown(), ("Stopped", "2 of 10 runs completed"))
        self.assertEqual(self.enabled(), controls(*IDLE_WITH_MACRO))
        self.assertFalse(self.app.pause_requested.is_set())

    def test_nothing_can_be_resumed_once_it_is_stopping(self):
        self.start()
        self.press("pause")
        self.app.stop_from_button()  # the worker may be gone before the next poll
        self.app.resume()
        self.assertTrue(self.app.pause_requested.is_set())  # still held until the worker has gone
        self.assertEqual(self.shown(), ("Stopping", ""))
        self.finish_worker()
        self.assertFalse(self.app.pause_requested.is_set())

    def test_nothing_can_be_paused_once_it_is_stopping(self):
        self.start()
        self.app.stop_from_button()
        self.app.pause()
        self.assertFalse(self.app.pause_requested.is_set())
        self.assertEqual(self.shown(), ("Stopping", ""))
        self.assertEqual(self.enabled(), controls("stop"))
        self.finish_worker()

    def test_a_new_playback_does_not_start_paused(self):
        self.start()
        self.press("pause")
        self.press("stop")
        self.finish_worker()
        self.player.block = False
        self.app.repeat()
        self.assertFalse(self.app.pause_requested.is_set())
        self.assertEqual(self.shown(), ("Playing", PLAYING_HINT))
        self.finish_worker()
        self.assertEqual(self.shown(), ("Finished", ""))

    def test_a_macro_that_ends_while_pausing_ends_normally(self):
        self.start("1")
        self.press("pause")
        self.app._handle_finished(1, 1, None)  # the player was done before it saw the request
        self.assertIs(self.app.state, State.IDLE)
        self.assertFalse(self.app.pause_requested.is_set())
        self.assertEqual(self.shown(), ("Finished", ""))
        self.assertEqual(self.enabled(), controls(*IDLE_WITH_MACRO))
        self.assertEqual(str(self.app.loop_button.cget("text")), "Loop")

    def test_the_hints_name_the_chosen_keys(self):
        self.app.keys.update(pause="f8", loop="f7", stop="esc")
        self.assertEqual(self.app._playing_hint(), "Press F8 to pause, Esc to stop.")
        self.assertEqual(self.app._paused_hint(), "Press F7 to resume, Esc to stop.")


class PauseOutsidePlaybackTests(AppTestCase):
    def test_the_pause_shortcut_and_button_do_nothing_when_nothing_plays(self):
        self.load_macro()
        before = self.shown()
        self.press("pause")
        self.app.pause()
        self.assertEqual(self.shown(), before)
        self.assertFalse(self.app.pause_requested.is_set())
        self.assertEqual(self.enabled(), controls(*IDLE_WITH_MACRO))

    def test_it_does_nothing_while_counting_down_or_recording(self):
        self.app.start_countdown()
        self.press("pause")
        self.assertIs(self.app.state, State.COUNTDOWN)
        self.app.stop_from_button()
        self.press("record")
        self.press("pause")
        self.assertIs(self.app.state, State.RECORDING)
        self.assertFalse(self.app.pause_requested.is_set())
        self.assertEqual(self.shown()[0], "Recording")


class SaveAsTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.path = self.load_macro()

    def choose(self, path):
        self.filedialog.asksaveasfilename.return_value = str(path)

    def test_the_macro_is_written_to_the_chosen_file_which_becomes_the_current_one(self):
        chosen = self.tmp / "elsewhere" / "my macro.json"
        chosen.parent.mkdir()
        self.choose(chosen)
        self.app.save_macro_as()
        self.assertEqual(Recording.load(chosen).events, SAMPLE)
        self.assertEqual(self.app.macro_path, chosen)
        self.assertIn(str(chosen), self.app.macro_var.get())
        self.assertEqual(self.shown(), ("Saved", app_module.LOADED_HINT))
        self.assertEqual(self.enabled(), controls(*IDLE_WITH_MACRO))
        self.assertTrue(self.path.exists())  # the original is left alone
        self.assertNoDialog()

    def test_the_dialog_starts_with_the_folder_and_name_of_the_current_macro(self):
        self.app.save_macro_as()
        self.filedialog.asksaveasfilename.assert_called_once_with(
            parent=self.app.root,
            title="Save macro as",
            initialdir=str(self.tmp),
            initialfile="macro.json",
            defaultextension=".json",
            filetypes=app_module.MACRO_FILETYPES,
        )

    def test_a_macro_without_a_file_starts_with_a_new_name_in_the_macros_folder(self):
        self.macros_dir.mkdir()
        self.app.macro_path = None
        self.app.save_macro_as()
        kwargs = self.filedialog.asksaveasfilename.call_args.kwargs
        self.assertEqual(kwargs["initialdir"], str(self.macros_dir))
        self.assertRegex(kwargs["initialfile"], r"^macro-\d{8}-\d{6}\.json$")

    def test_a_folder_that_is_gone_falls_back_to_one_that_exists(self):
        self.app.macro_path = self.tmp / "gone" / "macro.json"
        self.app.save_macro_as()
        self.assertEqual(self.filedialog.asksaveasfilename.call_args.kwargs["initialdir"], str(Path.home()))
        self.macros_dir.mkdir()
        self.app.save_macro_as()
        self.assertEqual(self.filedialog.asksaveasfilename.call_args.kwargs["initialdir"], str(self.macros_dir))
        self.assertEqual(self.filedialog.asksaveasfilename.call_args.kwargs["initialfile"], "macro.json")

    def test_cancelling_changes_nothing(self):
        before = self.shown(), self.app.macro_var.get()
        self.filedialog.asksaveasfilename.return_value = ""
        self.app.save_macro_as()
        self.assertEqual((self.shown(), self.app.macro_var.get()), before)
        self.assertEqual(self.app.macro_path, self.path)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["macro.json"])
        self.assertNoDialog()

    def test_a_name_without_an_extension_gets_json(self):
        self.choose(self.tmp / "mine")
        self.app.save_macro_as()
        self.assertEqual(self.app.macro_path, self.tmp / "mine.json")
        self.assertEqual(Recording.load(self.tmp / "mine.json").events, SAMPLE)
        self.assertFalse((self.tmp / "mine").exists())
        self.messagebox.askyesno.assert_not_called()

    def test_a_name_without_an_extension_asks_before_replacing_a_file(self):
        existing = self.write_macro("mine.json", [Event(0.0, "move", 1, 2)])
        self.choose(self.tmp / "mine")
        self.messagebox.askyesno.return_value = False
        self.app.save_macro_as()
        self.messagebox.askyesno.assert_called_once()
        self.assertIn("mine.json", self.messagebox.askyesno.call_args.args[1])
        self.assertEqual(Recording.load(existing).events, [Event(0.0, "move", 1, 2)])
        self.assertEqual(self.app.macro_path, self.path)
        self.messagebox.askyesno.return_value = True
        self.app.save_macro_as()
        self.assertEqual(Recording.load(existing).events, SAMPLE)
        self.assertEqual(self.app.macro_path, existing)

    def test_another_extension_is_kept_and_left_to_the_dialog_to_confirm(self):
        self.choose(self.tmp / "mine.macro")
        self.app.save_macro_as()
        self.assertEqual(self.app.macro_path, self.tmp / "mine.macro")
        self.assertEqual(Recording.load(self.tmp / "mine.macro").events, SAMPLE)
        self.messagebox.askyesno.assert_not_called()

    def test_saving_over_the_current_file_is_allowed(self):
        self.choose(self.path)
        self.app.save_macro_as()
        self.assertEqual(self.app.macro_path, self.path)
        self.assertEqual(Recording.load(self.path).events, SAMPLE)
        self.assertNoDialog()

    def test_a_macro_that_was_never_saved_can_be_saved(self):
        self.app.macro_path = None
        self.choose(self.tmp / "first.json")
        self.app.save_macro_as()
        self.assertEqual(self.app.macro_path, self.tmp / "first.json")
        self.assertNotIn("Not saved", self.app.macro_var.get())

    def test_a_failure_is_reported_and_the_current_file_stays(self):
        self.choose(self.tmp / "mine.json")
        with mock.patch.object(Recording, "save", side_effect=OSError("read-only")):
            self.app.save_macro_as()
        title, message = self.messagebox.showerror.call_args.args[:2]
        self.assertEqual((title, message), ("Could not save the macro", "read-only"))
        self.assertEqual(self.app.macro_path, self.path)
        self.assertIsNotNone(self.app.macro)
        self.assertEqual(self.enabled(), controls(*IDLE_WITH_MACRO))

    def test_it_needs_a_macro(self):
        self.app.macro = None
        self.app.save_macro_as()
        self.filedialog.asksaveasfilename.assert_not_called()

    def test_it_is_not_offered_while_busy_or_choosing_a_shortcut(self):
        app = self.app
        app.change_shortcut("stop")
        app.save_macro_as()
        app.change_shortcut("stop")  # cancels
        app.start_countdown()
        app.save_macro_as()
        app.stop_from_button()
        self.player.block = True
        app.loop()
        app.save_macro_as()
        self.filedialog.asksaveasfilename.assert_not_called()

    def test_the_save_as_button_opens_the_dialog(self):
        self.app.save_as_button.invoke()
        self.filedialog.asksaveasfilename.assert_called_once()

    def test_the_shortcuts_work_again_when_the_dialog_fails(self):
        self.filedialog.asksaveasfilename.side_effect = RuntimeError("no dialog")
        with self.assertRaises(RuntimeError):
            self.app.save_macro_as()
        self.assertFalse(self.app.dialog_open)


class DialogScenarios:
    """While a dialog is up, the shortcuts that would start something are ignored.

    What the window does while a dialog is showing depends on the system and on what opened the dialog: it may go
    on handling messages, or stand still until the dialog closes. The same scenarios run both ways.
    """

    polling = False

    def user_presses_keys(self, *actions):
        """A dialog that the user answers late, after pressing `actions` while it was showing."""

        def dialog(*args, **kwargs):
            self.assertTrue(self.app.dialog_open)
            for action in actions:
                self.hotkeys.on_action(action)  # the keyboard thread reports a key the moment it is pressed
                if self.polling:
                    self.app._drain()
            return ""  # cancelled, or "no"

        return dialog

    def assertNothingStarted(self, recordings=0, loops=0):
        self.app._drain()  # a window that did not handle messages in the dialog does so now that it has closed
        self.assertIs(self.app.state, State.IDLE)
        self.assertEqual(self.recorder.starts, recordings)
        self.assertEqual(len(self.player.calls), loops)
        self.assertFalse(self.app.dialog_open)

    def test_the_import_dialog(self):
        self.load_macro()
        self.filedialog.askopenfilename.reset_mock()  # loading the macro used the dialog already
        self.filedialog.askopenfilename.side_effect = self.user_presses_keys("record", "loop")
        self.app.import_macro()
        self.filedialog.askopenfilename.assert_called_once()
        self.assertNothingStarted()

    def test_the_save_as_dialog(self):
        self.load_macro()
        self.filedialog.asksaveasfilename.side_effect = self.user_presses_keys("record", "loop")
        self.app.save_macro_as()
        self.filedialog.asksaveasfilename.assert_called_once()
        self.assertNothingStarted()

    def test_the_question_about_replacing_a_file(self):
        self.load_macro()
        taken = self.write_macro("taken.json")
        self.filedialog.asksaveasfilename.return_value = str(taken.with_suffix(""))
        self.messagebox.askyesno.side_effect = self.user_presses_keys("record", "loop")
        self.app.save_macro_as()
        self.messagebox.askyesno.assert_called_once()
        self.assertNothingStarted()

    def test_an_error_about_the_loop_settings(self):
        self.load_macro()
        self.app.times_var.set("abc")
        self.messagebox.showerror.side_effect = self.user_presses_keys("record", "loop")
        self.press("loop")
        self.messagebox.showerror.assert_called_once()
        self.assertNothingStarted()

    def test_a_warning_that_nothing_was_recorded(self):
        self.load_macro()
        self.recorder.events = []
        self.press("record")
        self.messagebox.showwarning.side_effect = self.user_presses_keys("record", "loop")
        self.app.stop_from_button()
        self.messagebox.showwarning.assert_called_once()
        self.assertNothingStarted(recordings=1)

    def test_an_error_about_a_recorder_that_cannot_start(self):
        self.load_macro()
        self.recorder.start_error = RuntimeError("no access")
        self.messagebox.showerror.side_effect = self.user_presses_keys("record", "loop")
        self.press("record")
        self.messagebox.showerror.assert_called_once()
        self.assertNothingStarted()

    def test_an_error_about_a_macro_that_failed(self):
        self.load_macro()
        self.player.error = RuntimeError("boom")
        self.messagebox.showerror.side_effect = self.user_presses_keys("record", "loop")
        self.app.repeat()
        self.finish_worker()
        self.messagebox.showerror.assert_called_once()
        self.assertNothingStarted(loops=1)

    def test_a_warning_about_a_shortcut_that_cannot_be_used(self):
        self.load_macro()
        self.messagebox.showwarning.side_effect = self.user_presses_keys("record", "loop")
        self.app.change_shortcut("loop")
        self.hotkeys.capture("f2")  # the key that stops
        self.app._drain()
        self.messagebox.showwarning.assert_called_once()
        self.assertNothingStarted()

    def test_the_shortcuts_work_again_once_the_dialog_has_closed(self):
        self.load_macro()
        self.filedialog.askopenfilename.side_effect = self.user_presses_keys("record", "loop")
        self.app.import_macro()
        self.press("record")
        self.assertIs(self.app.state, State.RECORDING)


class DialogGuardTests(DialogScenarios, AppTestCase):
    """The window stands still while the dialog is up, as it does when the dialog was opened by a message."""

    def test_a_key_that_starts_something_does_not_even_reach_the_window(self):
        with self.app._dialogs():
            self.hotkeys.on_action("record")
            self.hotkeys.on_action("loop")
        self.assertTrue(self.app.messages.empty())

    def test_a_key_that_was_pressed_just_before_the_dialog_opened_is_ignored_too(self):
        self.load_macro()
        self.hotkeys.on_action("record")  # reported, but the window has not handled it yet

        def handle_messages_in_the_dialog(**kwargs):
            self.app._drain()
            return ""

        self.filedialog.askopenfilename.side_effect = handle_messages_in_the_dialog
        self.app.import_macro()
        self.assertNothingStarted()

    def test_stopping_and_pausing_are_not_ignored(self):
        self.load_macro()
        self.player.block = True
        self.app.loop()
        with self.app._dialogs():
            self.press("pause")
            self.press("stop")
        self.assertTrue(self.app.pause_requested.is_set())
        self.assertTrue(self.app.stop_requested.is_set())
        self.finish_worker()

    def test_a_dialog_inside_a_dialog_does_not_lift_the_guard_when_it_closes(self):
        with self.app._dialogs():
            with self.app._dialogs():
                self.assertTrue(self.app.dialog_open)
            self.assertTrue(self.app.dialog_open)
        self.assertFalse(self.app.dialog_open)


class DialogGuardWhilePollingTests(DialogScenarios, AppTestCase):
    """The window goes on handling messages while the dialog is up, as it does on Windows for a button's dialog."""

    polling = True


class FolderButtonTests(AppTestCase):
    def test_it_shows_the_macro_in_the_file_manager(self):
        path = self.load_macro()
        self.app.folder_button.invoke()
        self.show_in_file_manager.assert_called_once_with(path)
        self.assertNoDialog()

    def test_it_is_for_the_macro_that_is_selected_now(self):
        self.load_macro("first.json")
        second = self.load_macro("second.json")
        self.app.show_macro_folder()
        self.show_in_file_manager.assert_called_once_with(second)

    def test_a_new_recording_can_be_shown_right_away(self):
        self.press("record")
        self.press("stop")
        self.assertTrue(self.enabled()["folder"])
        self.app.show_macro_folder()
        self.show_in_file_manager.assert_called_once_with(self.app.macro_path)
        self.assertEqual(self.app.macro_path.parent, self.macros_dir)

    def test_after_save_as_it_points_to_the_new_file(self):
        self.load_macro()
        chosen = self.tmp / "saved.json"
        self.filedialog.asksaveasfilename.return_value = str(chosen)
        self.app.save_macro_as()
        self.app.show_macro_folder()
        self.show_in_file_manager.assert_called_once_with(chosen)

    def test_it_is_disabled_without_a_file_and_looks_it(self):
        app = self.app

        def icon():
            return str(app.folder_button.cget("image")[0])

        self.assertFalse(self.enabled()["folder"])
        self.assertEqual(icon(), str(app.folder_icon_disabled))
        app.macro = Recording(list(SAMPLE))  # a macro that was never saved
        app._refresh_macro_info()
        self.assertFalse(self.enabled()["folder"])
        app.show_macro_folder()
        self.show_in_file_manager.assert_not_called()
        self.load_macro()
        self.assertTrue(self.enabled()["folder"])
        self.assertEqual(icon(), str(app.folder_icon))

    def test_a_failure_is_reported_with_the_name_of_the_file_manager(self):
        self.load_macro()
        self.show_in_file_manager.side_effect = OSError("it is gone")
        self.app.show_macro_folder()
        title, message = self.messagebox.showerror.call_args.args[:2]
        self.assertEqual(title, f"Could not open {app_module.filemanager.file_manager_name()}")
        self.assertEqual(message, "it is gone")
        self.assertIs(self.app.state, State.IDLE)

    def test_nothing_is_opened_while_busy_or_choosing_a_shortcut(self):
        self.load_macro()
        app = self.app
        app.change_shortcut("stop")
        app.show_macro_folder()
        app.change_shortcut("stop")  # cancels
        app.start_countdown()
        app.show_macro_folder()
        app.stop_from_button()
        self.player.block = True
        app.loop()
        app.show_macro_folder()
        self.show_in_file_manager.assert_not_called()

    def test_the_tooltip_names_the_file_manager(self):
        with mock.patch.object(app_module, "Tooltip") as tooltip:
            self.launch()
        tooltip.assert_called_once_with(
            self.app.folder_button, f"Show in {app_module.filemanager.file_manager_name()}"
        )


class CloseTests(AppTestCase):
    def test_closing_while_idle(self):
        self.app.close()
        self.assertTrue(self.app.closed)
        self.assertEqual(self.hotkeys.stopped, 1)
        self.assertEqual(self.recorder.stops, 1)
        self.assertDestroyed()
        self.messagebox.askyesno.assert_not_called()

    def test_closing_leaves_no_poll_waiting_to_fire(self):
        root = self.app.root

        def waiting():
            return root.tk.splitlist(root.tk.call("after", "info"))

        self.assertIn(self.app._poll_job, waiting())
        self.app.close()
        self.assertEqual(waiting(), ())

    def test_closing_while_recording_asks_first(self):
        self.press("record")
        self.messagebox.askyesno.return_value = False
        self.app.close()
        self.messagebox.askyesno.assert_called_once()
        self.assertFalse(self.app.closed)
        self.assertEqual(self.hotkeys.stopped, 0)
        self.assertEqual(self.app.root.winfo_exists(), 1)
        self.messagebox.askyesno.return_value = True
        self.app.close()
        self.assertTrue(self.app.closed)
        self.assertEqual(self.recorder.stops, 1)
        self.assertDestroyed()

    def test_closing_while_recording_nothing_does_not_ask(self):
        self.recorder.events = []
        self.press("record")
        self.app.close()
        self.messagebox.askyesno.assert_not_called()
        self.assertTrue(self.app.closed)

    def test_closing_while_playing_stops_the_macro_and_waits_for_it(self):
        self.load_macro()
        self.player.block = True
        self.app.loop()
        worker = self.app.worker
        self.app.close()
        self.assertTrue(self.app.stop_requested.is_set())
        self.assertFalse(worker.is_alive())
        self.assertDestroyed()

    def test_closing_while_paused_does_not_hang(self):
        self.load_macro()
        self.player.block = True
        self.app.loop()
        self.press("pause")
        worker = self.app.worker
        self.app.close()
        self.assertTrue(self.app.stop_requested.is_set())
        self.assertFalse(worker.is_alive())
        self.assertDestroyed()

    def test_the_window_close_button_closes_the_app(self):
        self.app.root.tk.call(self.app.root.protocol("WM_DELETE_WINDOW"))
        self.assertTrue(self.app.closed)
        self.assertDestroyed()

    def test_quitting_from_the_macos_menu_closes_the_app(self):
        self.app.root.tk.call("tk::mac::Quit")
        self.assertTrue(self.app.closed)
        self.assertDestroyed()


class PermissionBannerTests(AppTestCase):
    def labels(self):
        return [str(w.cget("text")) for w in walk(self.app.root) if isinstance(w, ttk.Label)]

    def test_there_is_no_banner_when_everything_is_allowed(self):
        self.assertIsNone(find_button(self.app.root, "Open System Settings"))

    def test_the_banner_explains_what_to_allow_and_opens_the_settings(self):
        self.missing_permissions.return_value = ["Input Monitoring", "Accessibility"]
        button = find_button(self.app.root, "Open System Settings")
        self.assertIsNotNone(button)
        self.assertTrue(any("Input Monitoring and Accessibility for the app that runs" in t for t in self.labels()))
        button.invoke()
        self.open_settings.assert_called_once_with("Input Monitoring")

    def test_the_packaged_app_asks_for_the_permissions_itself(self):
        self.missing_permissions.return_value = ["Accessibility"]
        with mock.patch.object(app_module.sys, "frozen", True, create=True):
            self.launch()
        self.assertTrue(any("Allow Accessibility for Mouse Macro Recorder in" in t for t in self.labels()))
        self.assertFalse(any("Terminal" in text for text in self.labels()))


class LayoutTests(AppTestCase):
    def size(self):
        self.app.root.update_idletasks()
        return self.app.root.winfo_reqwidth(), self.app.root.winfo_reqheight()

    def test_the_window_does_not_change_size_as_the_status_changes(self):
        self.load_macro()
        app = self.app
        sizes = [self.size()]
        app.start_countdown()
        for elapsed in (0, 1.1, 2.1):
            self.clock.now = 1000 + elapsed
            app._tick()
            sizes.append(self.size())
        self.press("record")
        self.clock.now += 3725
        self.recorder.events = [click(0.0)] * 12345
        app._tick()
        sizes.append(self.size())
        self.recorder.events = []
        app.stop_from_button()
        sizes.append(self.size())
        self.player.block = True
        app.loop()
        sizes.append(self.size())
        app._handle_status("Waiting", "Run 12 of 100 starts in 59 min 59 s")
        sizes.append(self.size())
        self.press("pause")
        sizes.append(self.size())  # "Pausing", and the Loop button now says "Resume"
        app._handle_status("Paused", "Run 12 of 100 starts in 59 min 59 s")
        sizes.append(self.size())
        self.press("loop")
        sizes.append(self.size())
        self.assertEqual(len(set(sizes)), 1, sizes)

    def test_the_status_text_fits_the_space_reserved_for_it(self):
        app = self.app
        label = detail_label(app)
        room = label.master.winfo_reqheight()
        app.keys.update(record="page_down", stop="page_up", loop="page_down", pause="page_up")  # the widest labels
        texts = []
        for state in (State.IDLE, State.COUNTDOWN, State.RECORDING, State.PLAYING):
            app._set_state(state)
            if state is State.RECORDING:
                app._tick()
            texts.append(app.detail_var.get())
        for detail in ("Run 12 of 100", "Run 12 of 100 starts in 59 min 59 s"):
            app._handle_status("Waiting", detail)  # the state is PLAYING
            texts.append(app.detail_var.get())
        app.pause()
        texts.append(app.detail_var.get())
        app._handle_status("Paused", "Run 12 of 100 starts in 59 min 59 s")
        texts.append(app.detail_var.get())
        for text in texts:
            with self.subTest(text):
                app.detail_var.set(text)
                app.root.update_idletasks()
                self.assertLessEqual(label.winfo_reqheight(), room)

    def test_the_window_area_includes_the_title_bar(self):
        root = self.app.root
        root.winfo_rootx = lambda: 500
        root.winfo_rooty = lambda: 130
        root.winfo_y = lambda: 100
        root.winfo_width = lambda: 420
        root.winfo_height = lambda: 600
        self.assertEqual(self.app._window_area(), (500, 90, 920, 730))  # at least 40 px above the content
        root.winfo_y = lambda: 60
        self.assertEqual(self.app._window_area(), (500, 60, 920, 730))  # a taller title bar is used as it is


class DescribeTests(unittest.TestCase):
    def test_runs(self):
        cases = {
            (1, 1): "",
            (3, 10): "3 of 10 runs completed",
            (1, 10): "1 of 10 runs completed",
            (0, 10): "0 of 10 runs completed",
            (10, 10): "10 of 10 runs completed",
            (1, None): "1 run completed",
            (4, None): "4 runs completed",
            (0, None): "0 runs completed",
        }
        for (done, times), expected in cases.items():
            with self.subTest(done=done, times=times):
                self.assertEqual(describe_runs(done, times), expected)

    def test_events(self):
        self.assertEqual(describe_events(0), "0 events")
        self.assertEqual(describe_events(1), "1 event")
        self.assertEqual(describe_events(2), "2 events")


if __name__ == "__main__":
    unittest.main()
