import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pynput import keyboard

from macro_recorder import settings
from macro_recorder.hotkeys import REPEAT_WINDOW, Hotkeys, is_valid_name, key_label, key_name

from .fakes import FakeClock, FakeListener

F1 = keyboard.Key.f1
F2 = keyboard.Key.f2
ESC = keyboard.Key.esc


def char(value):
    return keyboard.KeyCode.from_char(value)


class KeyNameTests(unittest.TestCase):
    def test_special_keys_use_their_pynput_name(self):
        self.assertEqual(key_name(F1), "f1")
        self.assertEqual(key_name(keyboard.Key.page_down), "page_down")

    def test_characters_are_lower_cased(self):
        self.assertEqual(key_name(char("a")), "a")
        self.assertEqual(key_name(char("A")), "a")
        self.assertEqual(key_name(char("7")), "7")

    def test_keys_without_a_printable_character_fall_back_to_the_virtual_key_code(self):
        self.assertEqual(key_name(keyboard.KeyCode.from_vk(96)), "vk:96")
        self.assertEqual(key_name(keyboard.KeyCode(vk=96, char="\x01")), "vk:96")

    def test_unidentifiable_keys_have_no_name(self):
        self.assertIsNone(key_name(char("\x01")))
        self.assertIsNone(key_name(object()))


class IsValidNameTests(unittest.TestCase):
    def test_names_that_key_name_can_produce(self):
        for name in ("f1", "f12", "esc", "page_down", "a", "7", "vk:96"):
            with self.subTest(name):
                self.assertTrue(is_valid_name(name))

    def test_everything_else(self):
        for name in ("", "F1", "nonsense", "ab", "vk:", "vk:x", "vk:-1", 5, None, ["f1"]):
            with self.subTest(name):
                self.assertFalse(is_valid_name(name))


class KeyLabelTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(key_label("f1"), "F1")
        self.assertEqual(key_label("esc"), "Esc")
        self.assertEqual(key_label("page_down"), "Page Down")
        self.assertEqual(key_label("a"), "A")
        self.assertEqual(key_label("vk:96"), "Key 96")


class HotkeysTestCase(unittest.TestCase):
    def setUp(self):
        FakeListener.created = []
        self.clock = FakeClock(1000.0)
        self.actions = []
        self.hotkeys = Hotkeys(
            {"record": "f1", "stop": "f2"}, self.actions.append, listener_factory=FakeListener, clock=self.clock
        )
        self.hotkeys.start()
        self.listener = FakeListener.created[-1]

    def press(self, key, after=0.0):
        self.clock.now += after
        self.listener.callbacks["on_press"](key)

    def release(self, key):
        self.listener.callbacks["on_release"](key)

    def tap(self, key, after=0.0):
        self.press(key, after)
        self.release(key)


class ListenerLifecycleTests(HotkeysTestCase):
    def test_start_creates_and_starts_one_listener(self):
        self.assertEqual(len(FakeListener.created), 1)
        self.assertEqual(self.listener.started, 1)
        self.assertEqual(set(self.listener.callbacks), {"on_press", "on_release"})
        self.hotkeys.start()
        self.assertEqual(len(FakeListener.created), 1)

    def test_stop_stops_the_listener(self):
        self.hotkeys.stop()
        self.assertEqual(self.listener.stopped, 1)
        self.hotkeys.stop()
        self.assertEqual(self.listener.stopped, 1)

    def test_it_can_be_started_again_after_stopping(self):
        self.hotkeys.stop()
        self.hotkeys.start()
        self.assertEqual(len(FakeListener.created), 2)
        self.assertEqual(FakeListener.created[-1].started, 1)

    def test_stop_without_start_is_harmless(self):
        Hotkeys({}, lambda action: None, listener_factory=FakeListener).stop()


class DispatchTests(HotkeysTestCase):
    def test_each_key_triggers_its_action(self):
        self.tap(F1)
        self.tap(F2)
        self.assertEqual(self.actions, ["record", "stop"])

    def test_other_keys_do_nothing(self):
        self.tap(ESC)
        self.tap(char("a"))
        self.tap(keyboard.KeyCode.from_vk(5))
        self.assertEqual(self.actions, [])

    def test_keys_without_a_name_are_ignored(self):
        self.press(object())
        self.release(object())
        self.press(char("\x01"))
        self.assertEqual(self.actions, [])

    def test_character_keys_can_be_shortcuts_regardless_of_case(self):
        self.hotkeys.set_keys({"record": "r", "stop": "vk:96"})
        self.tap(char("r"))
        self.tap(char("R"))
        self.tap(keyboard.KeyCode.from_vk(96))
        self.assertEqual(self.actions, ["record", "record", "stop"])

    def test_set_keys_replaces_the_previous_shortcuts(self):
        self.hotkeys.set_keys({"record": "f5", "stop": "esc"})
        self.tap(F1)
        self.tap(keyboard.Key.f5)
        self.tap(ESC)
        self.assertEqual(self.actions, ["record", "stop"])


class AutoRepeatTests(HotkeysTestCase):
    def test_a_held_key_triggers_once(self):
        for _ in range(10):
            self.press(F1, after=0.03)
        self.assertEqual(self.actions, ["record"])

    def test_a_key_held_for_longer_than_the_window_still_triggers_once(self):
        for _ in range(100):
            self.press(F1, after=0.1)
        self.assertEqual(self.actions, ["record"])

    def test_pressing_again_after_releasing_triggers_again(self):
        self.tap(F1)
        self.tap(F1, after=0.05)
        self.assertEqual(self.actions, ["record", "record"])

    def test_a_missed_release_does_not_lock_the_key_forever(self):
        self.press(F1)
        self.press(F1, after=REPEAT_WINDOW)
        self.assertEqual(self.actions, ["record", "record"])

    def test_keys_are_tracked_separately(self):
        self.press(F1)
        self.press(F2, after=0.01)
        self.press(F1, after=0.01)
        self.press(F2, after=0.01)
        self.assertEqual(self.actions, ["record", "stop"])


class CaptureTests(HotkeysTestCase):
    def setUp(self):
        super().setUp()
        self.captured = []

    def test_the_next_key_goes_to_the_callback_and_triggers_nothing(self):
        self.hotkeys.capture_next(self.captured.append)
        self.tap(F1)
        self.assertEqual(self.captured, ["f1"])
        self.assertEqual(self.actions, [])

    def test_only_one_key_is_captured(self):
        self.hotkeys.capture_next(self.captured.append)
        self.tap(ESC)
        self.tap(char("x"))
        self.tap(F1)
        self.assertEqual(self.captured, ["esc"])
        self.assertEqual(self.actions, ["record"])

    def test_a_held_captured_key_does_not_trigger_its_own_action(self):
        self.hotkeys.capture_next(self.captured.append)
        self.press(F2)
        self.press(F2, after=0.03)
        self.press(F2, after=0.03)
        self.assertEqual(self.captured, ["f2"])
        self.assertEqual(self.actions, [])

    def test_unnameable_keys_do_not_end_the_capture(self):
        self.hotkeys.capture_next(self.captured.append)
        self.press(object())
        self.press(char("\x01"))
        self.assertEqual(self.captured, [])
        self.tap(char("q"))
        self.assertEqual(self.captured, ["q"])

    def test_cancelling_restores_normal_behaviour(self):
        self.hotkeys.capture_next(self.captured.append)
        self.hotkeys.cancel_capture()
        self.tap(F1)
        self.assertEqual(self.captured, [])
        self.assertEqual(self.actions, ["record"])

    def test_the_callback_may_change_the_shortcuts(self):
        def reassign(name):
            self.hotkeys.set_keys({"record": name, "stop": "f2"})

        self.hotkeys.capture_next(reassign)
        self.tap(char("z"))
        self.tap(char("z"), after=0.05)
        self.assertEqual(self.actions, ["record"])


class SettingsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "nested" / "settings.json"

    def write(self, text):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")

    def test_defaults_are_f1_and_f2(self):
        self.assertEqual(settings.DEFAULT_HOTKEYS, {"record": "f1", "stop": "f2"})

    def test_a_missing_file_gives_the_defaults(self):
        self.assertEqual(settings.load_hotkeys(self.path), settings.DEFAULT_HOTKEYS)

    def test_the_defaults_cannot_be_changed_through_the_result(self):
        settings.load_hotkeys(self.path)["record"] = "x"
        self.assertEqual(settings.load_hotkeys(self.path)["record"], "f1")

    def test_round_trip_creates_missing_directories(self):
        settings.save_hotkeys({"record": "f5", "stop": "esc"}, self.path)
        self.assertEqual(settings.load_hotkeys(self.path), {"record": "f5", "stop": "esc"})
        self.assertEqual(json.loads(self.path.read_text()), {"hotkeys": {"record": "f5", "stop": "esc"}})

    def test_unusable_files_give_the_defaults(self):
        bad = {
            "not json": "{",
            "empty": "",
            "not an object": "[]",
            "null": "null",
            "no hotkeys": "{}",
            "hotkeys not an object": '{"hotkeys": []}',
            "missing action": '{"hotkeys": {"record": "f5"}}',
            "invalid name": '{"hotkeys": {"record": "f5", "stop": "nonsense"}}',
            "not a string": '{"hotkeys": {"record": "f5", "stop": 3}}',
            "same key twice": '{"hotkeys": {"record": "f5", "stop": "f5"}}',
            "too deep": "[" * 100000,
        }
        for label, text in bad.items():
            with self.subTest(label):
                self.write(text)
                self.assertEqual(settings.load_hotkeys(self.path), settings.DEFAULT_HOTKEYS)

    def test_a_binary_file_gives_the_defaults(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"\xff\xfe\x00\x01")
        self.assertEqual(settings.load_hotkeys(self.path), settings.DEFAULT_HOTKEYS)

    def test_a_directory_instead_of_a_file_gives_the_defaults(self):
        self.path.mkdir(parents=True)
        self.assertEqual(settings.load_hotkeys(self.path), settings.DEFAULT_HOTKEYS)

    def test_unknown_entries_are_ignored(self):
        self.write('{"hotkeys": {"record": "f5", "stop": "f6", "other": "x"}, "future": 1}')
        self.assertEqual(settings.load_hotkeys(self.path), {"record": "f5", "stop": "f6"})


class ConfigDirTests(unittest.TestCase):
    def test_windows_uses_appdata(self):
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(os.environ, {"APPDATA": "/appdata"}):
            self.assertEqual(settings.config_dir(), Path("/appdata") / "MouseMacroRecorder")

    def test_windows_without_appdata(self):
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(os.environ):
            os.environ.pop("APPDATA", None)
            self.assertEqual(
                settings.config_dir(), Path.home() / "AppData" / "Roaming" / "MouseMacroRecorder"
            )

    def test_macos_uses_application_support(self):
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(
                settings.config_dir(), Path.home() / "Library" / "Application Support" / "MouseMacroRecorder"
            )

    def test_other_systems_follow_xdg(self):
        with mock.patch.object(sys, "platform", "linux"), mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/xdg"}):
            self.assertEqual(settings.config_dir(), Path("/xdg") / "mouse-macro-recorder")
        with mock.patch.object(sys, "platform", "linux"), mock.patch.dict(os.environ):
            os.environ.pop("XDG_CONFIG_HOME", None)
            self.assertEqual(settings.config_dir(), Path.home() / ".config" / "mouse-macro-recorder")

    def test_the_settings_file_lives_in_the_config_dir(self):
        self.assertEqual(settings.settings_path(), settings.config_dir() / "settings.json")


if __name__ == "__main__":
    unittest.main()
