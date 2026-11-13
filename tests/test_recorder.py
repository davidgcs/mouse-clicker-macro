import unittest

from pynput import mouse

from macro_recorder.recorder import Recorder
from macro_recorder.recording import Event

from .fakes import FakeClock, FakeListener


class RecorderTests(unittest.TestCase):
    def setUp(self):
        FakeListener.created = []
        self.clock = FakeClock(100.0)
        self.recorder = Recorder(clock=self.clock, listener_factory=FakeListener)

    def start(self):
        self.recorder.start()
        return FakeListener.created[-1].callbacks

    def test_start_begins_listening(self):
        callbacks = self.start()
        listener = FakeListener.created[-1]
        self.assertEqual(listener.started, 1)
        self.assertEqual(set(callbacks), {"on_move", "on_click", "on_scroll"})

    def test_events_are_timed_from_the_start(self):
        callbacks = self.start()
        self.clock.now = 100.5
        callbacks["on_move"](10.4, 20.6)
        self.clock.now = 101.25
        callbacks["on_click"](10, 20, mouse.Button.left, True)
        self.clock.now = 101.5
        callbacks["on_click"](10, 20, mouse.Button.left, False)
        self.clock.now = 102.0
        callbacks["on_scroll"](10, 20, 0, -2)
        self.assertEqual(
            self.recorder.stop(),
            [
                Event(0.5, "move", 10, 21),
                Event(1.25, "click", 10, 20, button="left", pressed=True),
                Event(1.5, "click", 10, 20, button="left", pressed=False),
                Event(2.0, "scroll", 10, 20, dx=0, dy=-2),
            ],
        )

    def test_moves_closer_together_than_the_minimum_interval_are_skipped(self):
        interval = Recorder.MIN_MOVE_INTERVAL
        callbacks = self.start()
        for factor in (0, 0.4, 1.2, 1.5, 2.4):
            self.clock.now = 100 + factor * interval
            callbacks["on_move"](0, 0)
        kept = [event.t for event in self.recorder.stop()]
        self.assertEqual(len(kept), 3)
        for actual, factor in zip(kept, (0, 1.2, 2.4)):
            self.assertAlmostEqual(actual, factor * interval)

    def test_clicks_and_scrolls_are_never_skipped(self):
        callbacks = self.start()
        callbacks["on_move"](1, 1)
        callbacks["on_click"](1, 1, mouse.Button.left, True)
        callbacks["on_click"](1, 1, mouse.Button.left, False)
        callbacks["on_scroll"](1, 1, 0, 1)
        self.assertEqual(len(self.recorder.stop()), 4)

    def test_all_three_buttons_are_supported(self):
        callbacks = self.start()
        for button in (mouse.Button.left, mouse.Button.middle, mouse.Button.right):
            callbacks["on_click"](0, 0, button, True)
        self.assertEqual([event.button for event in self.recorder.stop()], ["left", "middle", "right"])

    def test_other_buttons_are_ignored(self):
        callbacks = self.start()
        callbacks["on_click"](0, 0, mouse.Button.unknown, True)
        callbacks["on_click"](0, 0, object(), True)
        self.assertEqual(self.recorder.stop(), [])

    def test_stop_stops_the_listener_and_is_repeatable(self):
        self.start()
        listener = FakeListener.created[-1]
        self.recorder.stop()
        self.assertEqual(listener.stopped, 1)
        self.assertEqual(self.recorder.stop(), self.recorder.stop())
        self.assertEqual(listener.stopped, 1)

    def test_stop_without_start_is_harmless(self):
        self.assertEqual(Recorder(clock=self.clock, listener_factory=FakeListener).stop(), [])

    def test_start_waits_until_the_listener_is_running(self):
        polls = []

        class SlowStarter(FakeListener):
            @property
            def running(self):
                polls.append(1)
                return len(polls) > 3

        Recorder(clock=self.clock, listener_factory=SlowStarter).start()
        self.assertEqual(len(polls), 4)

    def test_start_gives_up_on_a_listener_that_never_runs(self):
        class NeverRuns(FakeListener):
            running = False

        recorder = Recorder(clock=self.clock, listener_factory=NeverRuns)
        recorder.STARTUP_TIMEOUT = 0.05
        recorder.start()
        self.assertEqual(recorder.stop(), [])

    def test_events_that_arrive_after_stop_are_not_returned(self):
        callbacks = self.start()
        callbacks["on_click"](0, 0, mouse.Button.left, True)
        result = self.recorder.stop()
        callbacks["on_click"](0, 0, mouse.Button.left, False)
        self.assertEqual(len(result), 1)

    def test_starting_again_forgets_the_previous_recording(self):
        callbacks = self.start()
        callbacks["on_click"](0, 0, mouse.Button.left, True)
        self.recorder.stop()
        self.clock.now = 200.0
        callbacks = self.start()
        self.assertEqual(self.recorder.event_count, 0)
        self.clock.now = 200.5
        callbacks["on_move"](1, 2)
        self.assertEqual(self.recorder.stop(), [Event(0.5, "move", 1, 2)])

    def test_event_count_grows_while_recording(self):
        callbacks = self.start()
        callbacks["on_click"](0, 0, mouse.Button.left, True)
        callbacks["on_scroll"](0, 0, 0, 1)
        self.assertEqual(self.recorder.event_count, 2)


if __name__ == "__main__":
    unittest.main()
