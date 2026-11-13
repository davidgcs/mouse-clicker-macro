import tkinter as tk
import unittest
from tkinter import ttk
from unittest import mock

from macro_recorder import tooltip
from macro_recorder.tooltip import Tooltip


def position(window: tk.Toplevel) -> tuple[int, int]:
    """Where a hint window was put; a window that is never shown still reports it."""
    _size, x, y = window.wm_geometry().split("+", 2)
    return int(x), int(y)


class TooltipTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.button = ttk.Button(self.root, text="Folder")
        self.button.pack()
        self.root.update_idletasks()
        # The hint window is built and measured, but never put on a real screen.
        patcher = mock.patch.object(tk.Toplevel, "deiconify")
        self.deiconify = patcher.start()
        self.addCleanup(patcher.stop)
        self.tip = Tooltip(self.button, "Show in Finder")

    def hints(self):
        return [child for child in self.button.winfo_children() if isinstance(child, tk.Toplevel)]

    def test_nothing_shows_until_the_pointer_has_rested_on_the_widget(self):
        self.button.event_generate("<Enter>")
        self.assertEqual(self.hints(), [])
        self.assertIsNotNone(self.tip._job)

    def test_the_hint_waits_for_the_hover_delay(self):
        with mock.patch.object(self.button, "after", return_value="job") as after:
            self.button.event_generate("<Enter>")
        after.assert_called_once_with(tooltip.HOVER_MS, self.tip._show)

    def test_the_hint_shows_its_text(self):
        self.tip._show()
        (window,) = self.hints()
        (label,) = window.winfo_children()
        self.assertEqual(label.cget("text"), "Show in Finder")
        self.deiconify.assert_called_once()

    def test_the_hint_is_a_window_without_a_title_bar(self):
        self.tip._show()
        (window,) = self.hints()
        self.assertTrue(window.wm_overrideredirect())

    def test_the_hint_is_centred_below_the_widget(self):
        self.tip._show()
        (window,) = self.hints()
        centre = self.button.winfo_rootx() + self.button.winfo_width() // 2
        self.assertEqual(
            position(window),
            (
                centre - window.winfo_reqwidth() // 2,
                self.button.winfo_rooty() + self.button.winfo_height() + tooltip.GAP,
            ),
        )

    def test_the_hint_stays_with_the_widget_on_any_screen(self):
        # Positions are on the whole desktop: another screen starts beyond the width of the primary one, or before 0.
        for rootx in (3000, -1500):
            with self.subTest(rootx=rootx):
                self.tip.hide()
                with (
                    mock.patch.object(self.button, "winfo_rootx", return_value=rootx),
                    mock.patch.object(self.button, "winfo_screenwidth", return_value=1024),
                ):
                    self.tip._show()
                (window,) = self.hints()
                centre = rootx + self.button.winfo_width() // 2
                self.assertEqual(position(window)[0], centre - window.winfo_reqwidth() // 2)

    def test_showing_it_twice_makes_one_window(self):
        self.tip._show()
        self.tip._show()
        self.assertEqual(len(self.hints()), 1)

    def test_leaving_the_widget_hides_the_hint(self):
        self.tip._show()
        self.button.event_generate("<Leave>")
        self.assertEqual(self.hints(), [])
        self.assertIsNone(self.tip._window)

    def test_leaving_before_the_delay_cancels_it(self):
        with mock.patch.object(self.button, "after", return_value="job"):
            self.button.event_generate("<Enter>")
        with mock.patch.object(self.button, "after_cancel") as after_cancel:
            self.button.event_generate("<Leave>")
        after_cancel.assert_called_once_with("job")
        self.assertIsNone(self.tip._job)

    def test_clicking_the_widget_hides_the_hint(self):
        self.tip._show()
        self.button.event_generate("<ButtonPress-1>")
        self.assertEqual(self.hints(), [])

    def test_clicking_the_widget_cancels_a_hint_that_is_still_to_come(self):
        self.button.event_generate("<Enter>")
        self.button.event_generate("<ButtonPress-1>")
        self.assertIsNone(self.tip._job)

    def test_entering_again_does_not_leave_two_timers(self):
        with mock.patch.object(self.button, "after", side_effect=["first", "second"]):
            with mock.patch.object(self.button, "after_cancel") as after_cancel:
                self.button.event_generate("<Enter>")
                self.button.event_generate("<Enter>")
        after_cancel.assert_called_once_with("first")

    def test_hiding_when_there_is_nothing_to_hide_is_fine(self):
        self.tip.hide()
        self.tip.hide()

    def test_destroying_the_widget_takes_the_hint_with_it(self):
        self.tip._show()
        self.button.event_generate("<Enter>")
        self.button.destroy()
        self.assertIsNone(self.tip._window)
        self.assertIsNone(self.tip._job)

    def test_bindings_the_widget_already_had_keep_working(self):
        seen = []
        button = ttk.Button(self.root, text="Other")
        button.bind("<Enter>", lambda event: seen.append("mine"))
        tip = Tooltip(button, "Hint")
        button.event_generate("<Enter>")
        self.assertEqual(seen, ["mine"])
        self.assertIsNotNone(tip._job)


if __name__ == "__main__":
    unittest.main()
