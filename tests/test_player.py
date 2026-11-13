import threading
import unittest

from macro_recorder.player import (
    PAUSE_TICK,
    SETTLE_SECONDS,
    Player,
    format_duration,
    parse_loop_settings,
)
from macro_recorder.recording import Event, Recording

from .fakes import FakeClock, FakeMouse, FakeStop


def press(t, x=10, y=10, button="left"):
    return Event(t, "click", x, y, button=button, pressed=True)


def release(t, x=10, y=10, button="left"):
    return Event(t, "click", x, y, button=button, pressed=False)


class PlayerTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.mouse = FakeMouse(self.clock)
        self.player = Player(self.mouse, clock=self.clock)

    def stop(self, stop_at=None):
        return FakeStop(self.clock, stop_at)


class PlayOnceTests(PlayerTestCase):
    def test_replays_every_kind_of_event_in_order(self):
        macro = Recording(
            [
                Event(0.0, "move", 10, 10),
                press(0.5),
                release(0.6),
                Event(1.0, "scroll", 10, 10, dx=1, dy=-2),
                Event(1.5, "click", 30, 40, button="right", pressed=True),
                Event(1.6, "click", 30, 40, button="middle", pressed=False),
            ]
        )
        self.assertTrue(self.player.play_once(macro, self.stop()))
        self.assertEqual(
            self.mouse.actions(),
            [
                ("move", 10, 10),
                ("move", 10, 10),
                ("press", "left", 1),
                ("move", 10, 10),
                ("release", "left", 1),
                ("move", 10, 10),
                ("scroll", 1, -2),
                ("move", 30, 40),
                ("press", "right", 1),
                ("move", 30, 40),
                ("release", "middle", 1),
                ("release", "right", 1),  # the right button was still held when the macro ended
            ],
        )

    def test_events_keep_their_original_timing(self):
        macro = Recording([Event(0.0, "move", 1, 1), Event(0.5, "move", 2, 2), Event(2.0, "move", 3, 3)])
        self.player.play_once(macro, self.stop())
        times = [entry[0] for entry in self.mouse.log]
        self.assertEqual(times, [0.0, 0.5, 2.0])

    def test_scheduling_does_not_drift_when_the_system_is_slow(self):
        class SlowMouse(FakeMouse):
            @property
            def position(self):
                return self._position

            @position.setter
            def position(self, value):
                self.clock.now += 0.3  # every action takes 300 ms
                FakeMouse.position.fset(self, value)

        slow = SlowMouse(self.clock)
        stop = self.stop()
        macro = Recording([Event(0.0, "move", 1, 1), Event(1.0, "move", 2, 2), Event(2.0, "move", 3, 3)])
        Player(slow, clock=self.clock).play_once(macro, stop)
        # Each event is still due at its own time even though the previous one finished late.
        self.assertEqual([round(entry[0], 6) for entry in slow.log], [0.3, 1.3, 2.3])
        self.assertEqual([round(wait, 6) for wait in stop.waits], [0.7, 0.7])

    def test_a_jump_before_a_click_waits_for_the_pointer_to_arrive(self):
        macro = Recording([press(0.0, 50, 60), release(0.0, 50, 60), press(0.0, 70, 80)])
        stop = self.stop()
        self.player.play_once(macro, stop)
        self.assertEqual(stop.waits, [SETTLE_SECONDS, SETTLE_SECONDS])
        self.assertEqual(
            [entry for entry in self.mouse.actions() if entry[0] != "move"],
            [("press", "left", 1), ("release", "left", 1), ("press", "left", 1), ("release", "left", 1)],
        )

    def test_moves_never_wait(self):
        macro = Recording([Event(0.0, "move", 1, 1), Event(0.0, "move", 500, 500)])
        stop = self.stop()
        self.player.play_once(macro, stop)
        self.assertEqual(stop.waits, [])

    def test_stop_before_the_first_event(self):
        stop = self.stop()
        stop.set()
        self.assertFalse(self.player.play_once(Recording([press(0.0)]), stop))
        self.assertEqual(self.mouse.log, [])

    def test_stop_while_waiting_for_an_event_releases_held_buttons(self):
        macro = Recording([press(0.0), release(10.0)])
        self.assertFalse(self.player.play_once(macro, self.stop(stop_at=5)))
        actions = self.mouse.actions()
        self.assertEqual(actions[-1], ("release", "left", 1))
        self.assertEqual(actions.count(("press", "left", 1)), 1)
        self.assertEqual(self.mouse.entered, self.mouse.exited)

    def test_stop_during_the_settle_wait_releases_nothing_that_was_not_pressed(self):
        macro = Recording([press(0.0, 5, 5)])
        self.assertFalse(self.player.play_once(macro, self.stop(stop_at=0.005)))
        self.assertNotIn("press", [entry[0] for entry in self.mouse.actions()])
        self.assertNotIn("release", [entry[0] for entry in self.mouse.actions()])

    def test_every_held_button_is_released_when_stopped(self):
        macro = Recording([press(0.0, button="left"), press(0.1, button="right"), release(9.0)])
        self.player.play_once(macro, self.stop(stop_at=1))
        released = {entry[1] for entry in self.mouse.actions() if entry[0] == "release"}
        self.assertEqual(released, {"left", "right"})

    def test_a_release_without_a_press_does_not_break_the_macro(self):
        macro = Recording([release(0.0), Event(0.1, "move", 3, 3)])
        self.assertTrue(self.player.play_once(macro, self.stop()))


class ClickSeriesTests(PlayerTestCase):
    def click_states(self, events):
        self.player.play_once(Recording(events), self.stop())
        return [entry[2] for entry in self.mouse.actions() if entry[0] == "press"]

    def test_quick_repeated_clicks_become_a_double_and_triple_click(self):
        events = []
        for index in range(3):
            events += [press(index * 0.2), release(index * 0.2 + 0.05)]
        self.assertEqual(self.click_states(events), [1, 2, 3])
        self.assertEqual(self.mouse.entered, 1)
        self.assertEqual(self.mouse.exited, 1)

    def test_slow_clicks_are_separate_clicks(self):
        events = [press(0.0), release(0.05), press(2.0), release(2.05)]
        self.assertEqual(self.click_states(events), [1, 1])
        self.assertEqual(self.mouse.entered, 2)

    def test_clicks_in_different_places_are_separate_clicks(self):
        events = [press(0.0, 10, 10), release(0.05, 10, 10), press(0.2, 200, 10), release(0.25, 200, 10)]
        self.assertEqual(self.click_states(events), [1, 1])

    def test_clicks_a_few_pixels_apart_still_count_as_a_double_click(self):
        events = [press(0.0, 10, 10), release(0.05, 10, 10), press(0.2, 13, 12), release(0.25, 13, 12)]
        self.assertEqual(self.click_states(events), [1, 2])

    def test_different_buttons_are_separate_clicks(self):
        events = [press(0.0, button="left"), release(0.05, button="left"), press(0.2, button="right")]
        self.assertEqual(self.click_states(events), [1, 1])

    def test_the_series_is_closed_after_each_run(self):
        self.click_states([press(0.0), release(0.05)])
        self.assertEqual(self.mouse.entered, self.mouse.exited)


class RunTests(PlayerTestCase):
    def setUp(self):
        super().setUp()
        self.statuses = []
        self.macro = Recording([press(0.0), release(1.0)])  # lasts one second

    def run_macro(self, times, delay, stop_at=None):
        stop = self.stop(stop_at)
        done = self.player.run(self.macro, stop, times, delay, on_status=lambda *status: self.statuses.append(status))
        return done, stop

    def run_starts(self):
        return [entry[0] for entry in self.mouse.log if entry[1] == "press"]

    def test_play_once(self):
        done, stop = self.run_macro(1, 0)
        self.assertEqual(done, 1)
        self.assertEqual(self.statuses, [("Playing", "")])

    def test_ten_runs_with_a_25_second_delay(self):
        done, stop = self.run_macro(10, 25)
        self.assertEqual(done, 10)
        starts = self.run_starts()
        self.assertEqual(len(starts), 10)
        # The first run starts at once; each later one starts 25 s after the previous run ended.
        self.assertEqual(starts[0], SETTLE_SECONDS)
        gaps = [later - earlier for earlier, later in zip(starts, starts[1:])]
        for gap in gaps:
            self.assertAlmostEqual(gap, 1.0 + 25.0)
        waiting = [status for status in self.statuses if status[0] == "Waiting"]
        self.assertEqual(sum(status[1].startswith("Run 2 of 10") for status in waiting), 25)
        self.assertEqual(waiting[0], ("Waiting", "Run 2 of 10 starts in 25 s"))
        self.assertEqual(len(waiting), 9 * 25)
        self.assertEqual(self.statuses[0], ("Playing", "Run 1 of 10"))
        self.assertEqual(self.statuses[-1], ("Playing", "Run 10 of 10"))  # nothing to wait for after the last run

    def test_no_waiting_before_the_first_or_after_the_last_run(self):
        self.run_macro(2, 7)
        kinds = [status[0] for status in self.statuses]
        self.assertEqual(kinds[0], "Playing")
        self.assertEqual(kinds[-1], "Playing")
        self.assertEqual(kinds.count("Playing"), 2)
        self.assertEqual(kinds.count("Waiting"), 7)

    def test_waiting_counts_down_in_whole_seconds(self):
        self.run_macro(2, 3)
        waiting = [status[1] for status in self.statuses if status[0] == "Waiting"]
        self.assertEqual(waiting, ["Run 2 of 2 starts in 3 s", "Run 2 of 2 starts in 2 s", "Run 2 of 2 starts in 1 s"])

    def test_fractional_delays_are_honoured(self):
        done, stop = self.run_macro(2, 0.25)
        starts = self.run_starts()
        self.assertAlmostEqual(starts[1] - starts[0], 1.25)
        waiting = [status[1] for status in self.statuses if status[0] == "Waiting"]
        self.assertEqual(waiting, ["Run 2 of 2 starts in 1 s"])

    def test_long_delays_are_shown_in_minutes(self):
        self.run_macro(2, 90)
        waiting = [status[1] for status in self.statuses if status[0] == "Waiting"]
        self.assertEqual(waiting[0], "Run 2 of 2 starts in 1 min 30 s")
        self.assertEqual(waiting[-1], "Run 2 of 2 starts in 1 s")

    def test_no_delay_means_no_waiting(self):
        done, stop = self.run_macro(3, 0)
        self.assertEqual(done, 3)
        self.assertEqual([status[0] for status in self.statuses], ["Playing"] * 3)

    def test_without_a_count_it_runs_until_stopped(self):
        done, stop = self.run_macro(None, 5, stop_at=100)
        self.assertGreater(done, 10)
        self.assertTrue(stop.is_set())
        self.assertEqual(self.statuses[0], ("Playing", "Run 1"))
        self.assertTrue(any(status == ("Waiting", "Run 3 starts in 5 s") for status in self.statuses))

    def test_stopping_during_the_delay_does_not_start_another_run(self):
        done, stop = self.run_macro(10, 25, stop_at=10)
        self.assertEqual(done, 1)
        self.assertEqual(len(self.run_starts()), 1)
        self.assertEqual(self.statuses[-1][0], "Waiting")

    def test_stopping_during_a_run_does_not_count_it(self):
        macro = Recording([press(0.0), release(10.0)])
        stop = self.stop(stop_at=3)
        done = self.player.run(macro, stop, 5, 1)
        self.assertEqual(done, 0)

    def test_an_enormous_delay_is_just_a_long_wait(self):
        done, stop = self.run_macro(2, 1e300, stop_at=5)
        self.assertEqual(done, 1)
        self.assertEqual(self.statuses[-1][0], "Waiting")

    def test_a_stop_requested_before_playing_plays_nothing(self):
        stop = self.stop()
        stop.set()
        self.assertEqual(self.player.run(self.macro, stop, 3, 1), 0)
        self.assertEqual(self.mouse.log, [])


class PauseTests(PlayerTestCase):
    """The pause flag is set and cleared from another thread; the fake stop does it at a chosen time.

    A paused player looks at the flag every PAUSE_TICK (0.05 s), so the times below are the ticks
    that follow the times the flag was changed at (0.22 -> 0.25, 5.02 -> 5.05, ...).
    """

    def setUp(self):
        super().setUp()
        self.pause = threading.Event()
        self.changes = []  # (paused, time) as the replay reports it

    def keys(self, pause_at, resume_at=None, stop_at=None):
        stop = self.stop(stop_at)
        stop.schedule(pause_at, self.pause.set)
        if resume_at is not None:
            stop.schedule(resume_at, self.pause.clear)
        return stop

    def play_once(self, macro, stop):
        return self.player.play_once(
            macro, stop, self.pause, lambda paused: self.changes.append((paused, self.clock.now))
        )

    def times_of(self, kind):
        return [entry[0] for entry in self.mouse.log if entry[1] == kind]

    def test_the_tick_is_short_enough_to_feel_immediate(self):
        self.assertLessEqual(PAUSE_TICK, 0.1)

    def test_pausing_holds_the_macro_still_and_the_rest_keeps_its_timing(self):
        macro = Recording([Event(0.0, "move", 10, 10), press(0.5), release(0.6)])
        self.assertTrue(self.play_once(macro, self.keys(pause_at=0.22, resume_at=5.02)))
        (paused, paused_at), (resumed, resumed_at) = self.changes
        self.assertEqual((paused, resumed), (True, False))
        self.assertAlmostEqual(paused_at, 0.25)
        self.assertAlmostEqual(resumed_at, 5.05)
        self.assertEqual([entry for entry in self.mouse.log if paused_at < entry[0] < resumed_at], [])
        # 0.25 s of the macro were left when it was paused, and the pointer is placed again first.
        (press_time,) = self.times_of("press")
        self.assertAlmostEqual(press_time, resumed_at + 0.25 + SETTLE_SECONDS)
        (release_time,) = self.times_of("release")
        self.assertAlmostEqual(release_time, resumed_at + 0.35)

    def test_a_pause_waits_until_no_button_is_held(self):
        macro = Recording([press(0.0), Event(0.5, "move", 20, 20), release(1.0, 20, 20), Event(1.5, "move", 30, 30)])
        self.assertTrue(self.play_once(macro, self.keys(pause_at=0.22, resume_at=10.02)))
        (release_time,) = self.times_of("release")
        self.assertAlmostEqual(release_time, 1.0)  # on time: the button is not held down through the pause
        (_, paused_at), (_, resumed_at) = self.changes
        self.assertAlmostEqual(paused_at, 1.0)
        self.assertEqual([entry for entry in self.mouse.log if paused_at < entry[0] < resumed_at], [])
        self.assertAlmostEqual(self.times_of("move")[-1], resumed_at + 0.5)

    def test_a_pause_requested_before_the_start_holds_the_first_event_too(self):
        self.pause.set()
        stop = self.stop()
        stop.schedule(2.02, self.pause.clear)
        macro = Recording([Event(0.0, "move", 10, 10), Event(0.1, "move", 20, 20)])
        self.assertTrue(self.play_once(macro, stop))
        first, second = self.times_of("move")
        self.assertAlmostEqual(first, 2.05)
        self.assertAlmostEqual(second, 2.15)

    def test_stopping_while_paused_ends_the_replay_without_carrying_on(self):
        macro = Recording([Event(0.0, "move", 10, 10), press(1.0), release(2.0)])
        self.assertFalse(self.play_once(macro, self.keys(pause_at=0.22, stop_at=3.0)))
        self.assertEqual(self.mouse.actions(), [("move", 10, 10)])
        self.assertEqual([paused for paused, _ in self.changes], [True])

    def test_a_click_after_a_pause_is_not_a_double_click_with_the_one_before_it(self):
        macro = Recording([press(0.0), release(0.05), press(0.3), release(0.35)])
        self.assertTrue(self.play_once(macro, self.keys(pause_at=0.12, resume_at=2.02)))
        self.assertEqual([entry[2] for entry in self.mouse.actions() if entry[0] == "press"], [1, 1])
        self.assertEqual((self.mouse.entered, self.mouse.exited), (2, 2))

    def test_a_pause_flag_that_is_never_set_changes_nothing(self):
        macro = Recording(
            [Event(0.0, "move", 10, 10), press(0.5), release(0.6), Event(1.0, "scroll", 10, 10, dx=1, dy=-2)]
        )
        self.assertTrue(self.play_once(macro, self.stop()))
        clock = FakeClock()
        plain = FakeMouse(clock)
        Player(plain, clock=clock).play_once(macro, FakeStop(clock))
        self.assertEqual(self.mouse.actions(), plain.actions())
        self.assertAlmostEqual(self.clock.now, clock.now)
        self.assertEqual(self.changes, [])

    def test_a_stop_requested_before_playing_plays_nothing_even_when_it_can_be_paused(self):
        stop = self.stop()
        stop.set()
        self.assertFalse(self.play_once(Recording([press(0.0)]), stop))
        self.assertEqual(self.mouse.log, [])


class RunPauseTests(PlayerTestCase):
    def setUp(self):
        super().setUp()
        self.pause = threading.Event()
        self.statuses = []
        self.macro = Recording([press(0.0), release(1.0)])  # lasts one second

    def run_macro(self, times, delay, pause_at=None, resume_at=None, stop_at=None, macro=None):
        stop = self.stop(stop_at)
        if pause_at is not None:
            stop.schedule(pause_at, self.pause.set)
        if resume_at is not None:
            stop.schedule(resume_at, self.pause.clear)
        return self.player.run(
            macro or self.macro,
            stop,
            times,
            delay,
            on_status=lambda *status: self.statuses.append(status),
            pause=self.pause,
        )

    def press_times(self):
        return [entry[0] for entry in self.mouse.log if entry[1] == "press"]

    def test_a_pause_between_runs_freezes_the_countdown(self):
        self.assertEqual(self.run_macro(2, 10, pause_at=4.22, resume_at=100.02), 2)
        frozen = ("Paused", "Run 2 of 2 starts in 7 s")  # 6.75 s were left
        self.assertEqual(self.statuses.count(frozen), 1)
        after = self.statuses[self.statuses.index(frozen) + 1]
        self.assertEqual(after, ("Waiting", "Run 2 of 2 starts in 7 s"))
        first, second = self.press_times()
        self.assertAlmostEqual(first, SETTLE_SECONDS)
        # The macro (1 s), the delay (10 s) and the time spent paused (from the tick at 4.25 to the one at 100.05).
        self.assertAlmostEqual(second, 1.0 + 10 + (100.05 - 4.25) + SETTLE_SECONDS)

    def test_a_pause_during_a_run_reports_paused_and_then_playing_again(self):
        macro = Recording([Event(0.0, "move", 10, 10), Event(1.0, "move", 20, 20)])
        self.assertEqual(self.run_macro(3, 5, pause_at=0.22, resume_at=3.02, macro=macro), 3)
        self.assertEqual(
            self.statuses[:3], [("Playing", "Run 1 of 3"), ("Paused", "Run 1 of 3"), ("Playing", "Run 1 of 3")]
        )

    def test_a_single_replay_can_be_paused_too(self):
        macro = Recording([Event(0.0, "move", 10, 10), Event(1.0, "move", 20, 20)])
        self.assertEqual(self.run_macro(1, 0, pause_at=0.22, resume_at=3.02, macro=macro), 1)
        self.assertEqual(self.statuses, [("Playing", ""), ("Paused", ""), ("Playing", "")])

    def test_a_pause_requested_while_a_button_is_down_takes_effect_once_it_is_released(self):
        self.assertEqual(self.run_macro(2, 5, pause_at=0.22, resume_at=20.02), 2)
        self.assertEqual(self.statuses[:2], [("Playing", "Run 1 of 2"), ("Paused", "Run 2 of 2 starts in 5 s")])
        release_time = next(entry[0] for entry in self.mouse.log if entry[1] == "release")
        self.assertAlmostEqual(release_time, 1.0)

    def test_stopping_while_paused_ends_the_loop(self):
        self.assertEqual(self.run_macro(10, 25, pause_at=4.22, stop_at=50), 1)
        self.assertEqual(self.statuses[-1], ("Paused", "Run 2 of 10 starts in 22 s"))
        self.assertEqual(len(self.press_times()), 1)

    def test_a_pause_flag_that_is_never_set_reports_what_a_plain_run_does(self):
        clock = FakeClock()
        plain, plain_statuses = FakeMouse(clock), []
        Player(plain, clock=clock).run(
            self.macro, FakeStop(clock), 3, 7, on_status=lambda *status: plain_statuses.append(status)
        )
        self.assertEqual(self.run_macro(3, 7), 3)
        self.assertEqual(self.statuses, plain_statuses)
        self.assertEqual(self.mouse.actions(), plain.actions())


class ParseLoopSettingsTests(unittest.TestCase):
    def test_a_count_and_a_delay_in_seconds(self):
        self.assertEqual(parse_loop_settings("10", "25", "seconds"), (10, 25.0))

    def test_the_default_is_five_seconds_forever(self):
        self.assertEqual(parse_loop_settings("", "5", "seconds"), (None, 5.0))

    def test_minutes_are_converted_to_seconds(self):
        self.assertEqual(parse_loop_settings("3", "2", "minutes"), (3, 120.0))
        self.assertEqual(parse_loop_settings("3", "0.5", "minutes"), (3, 30.0))

    def test_blanks_and_decimal_commas_are_accepted(self):
        self.assertEqual(parse_loop_settings("  7 ", " 1,5 ", "seconds"), (7, 1.5))
        self.assertEqual(parse_loop_settings("   ", "0", "seconds"), (None, 0.0))

    def test_invalid_counts(self):
        for text in ("0", "-1", "abc", "2.5", "1e3", "ten"):
            with self.subTest(text):
                with self.assertRaises(ValueError) as caught:
                    parse_loop_settings(text, "5", "seconds")
                self.assertIn("Times", str(caught.exception))

    def test_invalid_delays(self):
        for text in ("", "abc", "-1", "nan", "inf", "1..2"):
            with self.subTest(text):
                with self.assertRaises(ValueError) as caught:
                    parse_loop_settings("3", text, "seconds")
                self.assertIn("delay", str(caught.exception))

    def test_a_delay_that_overflows_is_rejected(self):
        for text, unit in (("inf", "seconds"), ("1e308", "minutes"), ("1e307", "minutes")):
            with self.subTest(text=text, unit=unit):
                with self.assertRaises(ValueError) as caught:
                    parse_loop_settings("3", text, unit)
                self.assertIn("too long", str(caught.exception))

    def test_a_huge_delay_that_still_fits_is_accepted(self):
        self.assertEqual(parse_loop_settings("3", "1e300", "minutes"), (3, 6e301))


class FormatDurationTests(unittest.TestCase):
    def test_seconds_and_minutes(self):
        self.assertEqual(format_duration(0), "0 s")
        self.assertEqual(format_duration(59), "59 s")
        self.assertEqual(format_duration(60), "1 min 00 s")
        self.assertEqual(format_duration(125), "2 min 05 s")


if __name__ == "__main__":
    unittest.main()
