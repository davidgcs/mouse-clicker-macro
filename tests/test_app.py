"""Drives the real window without a display server session being touched: a hidden Tk root,
fake recorder, player and shortcuts, and mocked dialogs. The event loop is never run; tests call
the same methods the loop would."""

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

CONTROLS = ("record", "stop", "import", "repeat", "loop", "times", "delay", "unit", "change_record", "change_stop")
IDLE_WITHOUT_MACRO = ("record", "import", "times", "delay", "unit", "change_record", "change_stop")
IDLE_WITH_MACRO = IDLE_WITHOUT_MACRO + ("repeat", "loop")


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
        self.patch(app_module.settings, "load_hotkeys", side_effect=lambda: {"record": "f1", "stop": "f2"})
        self.missing_permissions = self.patch(app_module.permissions, "missing_permissions", return_value=[])
        self.open_settings = self.patch(app_module.permissions, "open_privacy_settings")
        self.messagebox = self.patch(app_module, "messagebox")
        self.filedialog = self.patch(app_module, "filedialog")
        self.filedialog.askopenfilename.return_value = ""  # the dialog is cancelled unless a test picks a file
        self.bell = mock.Mock()
        self._app = None
        self._hotkeys = None

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

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
            "repeat": app.repeat_button,
            "loop": app.loop_button,
            "times": app.times_entry,
            "delay": app.delay_entry,
            "unit": app.unit_box,
            "change_record": app.change_buttons["record"],
            "change_stop": app.change_buttons["stop"],
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
        self.assertIn("F1", detail)
        self.assertIn("F2", detail)
        shown_keys = {action: var.get() for action, var in self.app.key_vars.items()}
        self.assertEqual(shown_keys, {"record": "F1", "stop": "F2"})

    def test_the_shortcuts_are_listened_for_from_the_start(self):
        self.assertEqual(self.hotkeys.keys, {"record": "f1", "stop": "f2"})
        self.assertEqual(self.hotkeys.started, 1)

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
            app_module.main()
        prevent_nap.assert_called_once_with()
        app_class.assert_called_once_with(root)
        root.mainloop.assert_called_once_with()


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
        self.assertIn("Input Monitoring", self.messagebox.showwarning.call_args.args[1])

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
        self.assertEqual(self.enabled(), controls(*IDLE_WITH_MACRO))

    def test_a_second_recording_gets_its_own_file(self):
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
        self.assertEqual(self.shown(), ("Playing", "Press F2 or click Stop to stop."))
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
            self.assertEqual(self.shown(), (headline, f"{detail}\nPress F2 or click Stop to stop."))

    def test_a_player_reporting_faster_than_the_window_polls_cannot_flood_the_queue(self):
        self.player.statuses = [("Playing", f"Run {number}") for number in range(1, 5001)]
        self.player.block = True
        self.app.loop()
        self.assertTrue(self.player.reported.wait(5))
        self.assertEqual(self.app.messages.qsize(), 1)
        self.app._drain()
        self.assertEqual(self.shown(), ("Playing", "Run 5000\nPress F2 or click Stop to stop."))
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

    def test_statuses_are_shown_with_the_stop_hint(self):
        self.player.block = True
        self.app.loop()
        self.app._handle_status("Waiting", "Run 2 of 10 starts in 25 s")
        self.assertEqual(self.shown(), ("Waiting", "Run 2 of 10 starts in 25 s\nPress F2 or click Stop to stop."))
        self.app._handle_status("Playing", "")
        self.assertEqual(self.shown(), ("Playing", "Press F2 or click Stop to stop."))

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
        self.assertEqual(self.enabled(), controls("stop"))

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


class ControlsTests(AppTestCase):
    def test_only_stop_is_usable_while_busy(self):
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
        self.assertEqual(self.enabled(), controls("stop"))

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
        self.assertEqual(app.keys, {"record": "f5", "stop": "f2"})
        self.assertEqual(self.hotkeys.keys, {"record": "f5", "stop": "f2"})
        self.save_hotkeys.assert_called_once_with({"record": "f5", "stop": "f2"})
        self.assertEqual(app.key_vars["record"].get(), "F5")
        self.assertIn("F5", app.detail_var.get())
        self.assertEqual(str(app.change_buttons["record"].cget("text")), "Change…")
        self.assertEqual(self.enabled(), controls(*IDLE_WITHOUT_MACRO))
        self.assertNoDialog()

    def test_the_stop_shortcut_can_be_changed_too(self):
        self.app.change_shortcut("stop")
        self.capture("esc")
        self.assertEqual(self.app.keys, {"record": "f1", "stop": "esc"})
        self.assertIn("Esc", self.app.detail_var.get())

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
        self.assertEqual(app.keys, {"record": "f1", "stop": "f2"})
        self.assertNoDialog()
        self.save_hotkeys.assert_not_called()

    def test_a_key_already_used_by_the_other_action_is_refused(self):
        app = self.app
        app.change_shortcut("record")
        self.capture("f2")
        title, message = self.messagebox.showwarning.call_args.args[:2]
        self.assertEqual(title, "Shortcut not changed")
        self.assertIn("F2", message)
        self.assertIn("stopping", message)
        self.assertEqual(app.keys, {"record": "f1", "stop": "f2"})
        self.assertEqual(self.hotkeys.keys, {"record": "f1", "stop": "f2"})
        self.assertEqual(app.key_vars["record"].get(), "F1")
        self.assertIsNone(app.capturing)
        self.save_hotkeys.assert_not_called()

    def test_the_stop_shortcut_cannot_take_the_record_key_either(self):
        self.app.change_shortcut("stop")
        self.capture("f1")
        self.assertIn("starting a recording", self.messagebox.showwarning.call_args.args[1])
        self.assertEqual(self.app.keys["stop"], "f2")

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


class CloseTests(AppTestCase):
    def test_closing_while_idle(self):
        self.app.close()
        self.assertTrue(self.app.closed)
        self.assertEqual(self.hotkeys.stopped, 1)
        self.assertEqual(self.recorder.stops, 1)
        self.assertDestroyed()
        self.messagebox.askyesno.assert_not_called()

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
        self.assertTrue(any("Input Monitoring and Accessibility" in text for text in self.labels()))
        button.invoke()
        self.open_settings.assert_called_once_with("Input Monitoring")


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
        self.assertEqual(len(set(sizes)), 1, sizes)

    def test_the_status_text_fits_the_space_reserved_for_it(self):
        app = self.app
        label = detail_label(app)
        room = label.master.winfo_reqheight()
        app.keys.update(record="page_down", stop="page_up")  # the widest labels
        texts = []
        for state in (State.IDLE, State.COUNTDOWN, State.RECORDING, State.PLAYING):
            app._set_state(state)
            if state is State.RECORDING:
                app._tick()
            texts.append(app.detail_var.get())
        for detail in ("Run 12 of 100", "Run 12 of 100 starts in 59 min 59 s"):
            app._handle_status("Waiting", detail)  # the state is PLAYING
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
