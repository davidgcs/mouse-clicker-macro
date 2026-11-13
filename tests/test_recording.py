import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from macro_recorder.recording import (
    MAX_DISTANCE,
    MAX_SECONDS,
    Event,
    Recording,
    RecordingError,
    new_macro_path,
    trim_tail,
)


def sample_events():
    return [
        Event(0.0, "move", 10, 20),
        Event(0.25, "click", 10, 20, button="left", pressed=True),
        Event(0.3, "click", 10, 20, button="left", pressed=False),
        Event(0.5, "scroll", 11, 21, dx=0, dy=-3),
        Event(0.75, "click", 40, 50, button="right", pressed=True),
        Event(0.8, "click", 40, 50, button="right", pressed=False),
    ]


def macro_json(events, **overrides):
    data = {"format": "mouse-macro", "version": 1, "events": events}
    data.update(overrides)
    return json.dumps(data)


class RecordingFileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def test_save_and_load_round_trip(self):
        macro = Recording(sample_events(), created="2026-01-02T03:04:05")
        path = self.directory / "nested" / "macro.json"
        macro.save(path)
        loaded = Recording.load(path)
        self.assertEqual(loaded.events, macro.events)
        self.assertEqual(loaded.created, "2026-01-02T03:04:05")

    def test_file_is_json_with_one_event_per_line(self):
        text = Recording(sample_events()).to_json()
        data = json.loads(text)
        self.assertEqual(data["format"], "mouse-macro")
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["events"]), 6)
        event_lines = [line for line in text.splitlines() if line.startswith("    {")]
        self.assertEqual(len(event_lines), 6)
        click_down = {"t": 0.25, "type": "click", "x": 10, "y": 20, "button": "left", "pressed": True}
        self.assertEqual(data["events"][1], click_down)
        self.assertEqual(data["events"][3], {"t": 0.5, "type": "scroll", "x": 11, "y": 21, "dx": 0, "dy": -3})
        self.assertEqual(data["events"][0], {"t": 0.0, "type": "move", "x": 10, "y": 20})

    def test_duration_is_the_time_of_the_last_event(self):
        self.assertEqual(Recording(sample_events()).duration, 0.8)
        self.assertEqual(Recording([]).duration, 0.0)

    def test_events_are_sorted_by_time_when_loading(self):
        events = [
            {"t": 2, "type": "move", "x": 1, "y": 1},
            {"t": 1, "type": "move", "x": 2, "y": 2},
        ]
        loaded = Recording.from_json(macro_json(events))
        self.assertEqual([event.t for event in loaded.events], [1, 2])

    def test_coordinates_are_rounded_to_whole_pixels(self):
        loaded = Recording.from_json(macro_json([{"t": 0, "type": "move", "x": 10.6, "y": 3.2}]))
        self.assertEqual((loaded.events[0].x, loaded.events[0].y), (11, 3))

    def assertRejected(self, text, *fragments):
        with self.assertRaises(RecordingError) as caught:
            Recording.from_json(text)
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))

    def test_rejects_things_that_are_not_macros(self):
        self.assertRejected("not json at all", "not valid JSON")
        self.assertRejected("[1, 2, 3]", "not a mouse macro")
        self.assertRejected(json.dumps({"format": "other", "events": []}), "not a mouse macro")
        self.assertRejected("[" * 100000, "not valid JSON")

    def test_rejects_newer_versions(self):
        self.assertRejected(macro_json([{"t": 0, "type": "move", "x": 0, "y": 0}], version=2), "newer version")

    def test_rejects_missing_or_empty_events(self):
        self.assertRejected(json.dumps({"format": "mouse-macro", "version": 1}), "list of events")
        self.assertRejected(macro_json("nope"), "list of events")
        self.assertRejected(macro_json([]), "no events")

    def test_rejects_bad_events_and_says_which(self):
        good = {"t": 0, "type": "move", "x": 0, "y": 0}
        bad_events = {
            "unknown type": {"t": 0, "type": "teleport", "x": 0, "y": 0},
            "negative time": {"t": -1, "type": "move", "x": 0, "y": 0},
            "missing time": {"type": "move", "x": 0, "y": 0},
            "text coordinate": {"t": 0, "type": "move", "x": "5", "y": 0},
            "boolean coordinate": {"t": 0, "type": "move", "x": True, "y": 0},
            "missing coordinate": {"t": 0, "type": "move", "x": 1},
            "unknown button": {"t": 0, "type": "click", "x": 0, "y": 0, "button": "side", "pressed": True},
            "pressed is not boolean": {"t": 0, "type": "click", "x": 0, "y": 0, "button": "left", "pressed": 1},
            "scroll without amounts": {"t": 0, "type": "scroll", "x": 0, "y": 0},
            "not an object": [1, 2],
        }
        for name, event in bad_events.items():
            with self.subTest(name):
                self.assertRejected(macro_json([good, event]), "event 2")

    def test_rejects_non_finite_numbers(self):
        text = '{"format": "mouse-macro", "version": 1, "events": [{"t": NaN, "type": "move", "x": 0, "y": 0}]}'
        self.assertRejected(text, "event 1")
        text = '{"format": "mouse-macro", "version": 1, "events": [{"t": 0, "type": "move", "x": Infinity, "y": 0}]}'
        self.assertRejected(text, "event 1")

    def test_rejects_numbers_too_big_to_play(self):
        good = {"t": 0, "type": "move", "x": 0, "y": 0}
        beyond_a_float = 10**400
        bad_events = {
            "far in the future": {"t": MAX_SECONDS + 1, "type": "move", "x": 0, "y": 0},
            "time beyond a float": {"t": beyond_a_float, "type": "move", "x": 0, "y": 0},
            "x too far": {"t": 0, "type": "move", "x": MAX_DISTANCE + 1, "y": 0},
            "y too far negative": {"t": 0, "type": "move", "x": 0, "y": -MAX_DISTANCE - 1},
            "x beyond a float": {"t": 0, "type": "move", "x": beyond_a_float, "y": 0},
            "scroll too far": {"t": 0, "type": "scroll", "x": 0, "y": 0, "dx": 0, "dy": MAX_DISTANCE + 1},
        }
        for name, event in bad_events.items():
            with self.subTest(name):
                self.assertRejected(macro_json([good, event]), "event 2", "out of range")

    def test_accepts_values_up_to_the_limits(self):
        events = [
            {"t": MAX_SECONDS, "type": "move", "x": -MAX_DISTANCE, "y": MAX_DISTANCE},
            {"t": 0, "type": "scroll", "x": 0, "y": 0, "dx": -MAX_DISTANCE, "dy": MAX_DISTANCE},
        ]
        loaded = Recording.from_json(macro_json(events))
        self.assertEqual([event.t for event in loaded.events], [0, MAX_SECONDS])

    def test_load_errors_name_the_file(self):
        path = self.directory / "broken.json"
        path.write_text("{}", encoding="utf-8")
        with self.assertRaises(RecordingError) as caught:
            Recording.load(path)
        self.assertIn("broken.json", str(caught.exception))
        with self.assertRaises(RecordingError) as caught:
            Recording.load(self.directory / "missing.json")
        self.assertIn("missing.json", str(caught.exception))

    def test_load_rejects_binary_files(self):
        path = self.directory / "binary.json"
        path.write_bytes(b"\xff\xfe\x00\x01")
        with self.assertRaises(RecordingError):
            Recording.load(path)


class FromCaptureTests(unittest.TestCase):
    def test_time_restarts_at_the_first_event(self):
        raw = [Event(2.5, "move", 1, 1), Event(3.0, "move", 2, 2), Event(4.25, "move", 3, 3)]
        macro = Recording.from_capture(raw)
        self.assertEqual([event.t for event in macro.events], [0.0, 0.5, 1.75])
        self.assertEqual(macro.duration, 1.75)

    def test_nothing_captured_gives_an_empty_macro(self):
        self.assertEqual(Recording.from_capture([]).events, [])
        self.assertEqual(Recording.from_capture([], (0, 0, 10, 10)).events, [])

    def test_trailing_events_inside_the_area_are_dropped(self):
        raw = [
            Event(1.0, "move", 500, 500),
            Event(1.5, "click", 500, 500, button="left", pressed=True),
            Event(1.6, "click", 500, 500, button="left", pressed=False),
            Event(2.0, "move", 50, 50),
            Event(2.1, "click", 50, 50, button="left", pressed=True),
            Event(2.2, "click", 50, 50, button="left", pressed=False),
        ]
        macro = Recording.from_capture(raw, (0, 0, 100, 100))
        self.assertEqual(len(macro.events), 3)
        self.assertEqual(macro.events[-1].t, 0.6)
        self.assertEqual(macro.events[0].t, 0.0)

    def test_only_trailing_events_are_dropped(self):
        raw = [
            Event(0.0, "move", 50, 50),  # inside the area, but not at the end
            Event(1.0, "move", 500, 500),
            Event(2.0, "move", 60, 60),
        ]
        macro = Recording.from_capture(raw, (0, 0, 100, 100))
        self.assertEqual([(event.x, event.y) for event in macro.events], [(50, 50), (500, 500)])

    def test_area_edges_are_inclusive(self):
        raw = [Event(0.0, "move", 500, 500), Event(1.0, "move", 0, 100), Event(2.0, "move", 100, 0)]
        self.assertEqual(len(trim_tail(raw, (0, 0, 100, 100))), 1)

    def test_everything_inside_the_area_leaves_nothing(self):
        raw = [Event(0.0, "move", 5, 5), Event(1.0, "move", 6, 6)]
        self.assertEqual(Recording.from_capture(raw, (0, 0, 100, 100)).events, [])


class NewMacroPathTests(unittest.TestCase):
    def test_names_are_timestamped_and_never_overwrite(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            now = datetime(2026, 7, 8, 9, 10, 11)
            first = new_macro_path(directory, now)
            self.assertEqual(first.name, "macro-20260708-091011.json")
            first.write_text("x")
            second = new_macro_path(directory, now)
            self.assertEqual(second.name, "macro-20260708-091011-2.json")
            second.write_text("x")
            self.assertEqual(new_macro_path(directory, now).name, "macro-20260708-091011-3.json")


if __name__ == "__main__":
    unittest.main()
