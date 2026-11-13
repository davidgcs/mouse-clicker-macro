import contextlib
import io
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from release import smoke_test

# Stands in for a packaged build: fake_app.py <exit code> <report> --self-test <report path>
FAKE_APP = textwrap.dedent(
    """
    import sys, time
    code, text, flag, report = sys.argv[1:]
    assert flag == "--self-test"
    if text == "hang":
        time.sleep(60)
    if text != "none":
        with open(report, "w", encoding="utf-8") as file:
            file.write(text + "\\n")
    sys.exit(int(code))
    """
)


class SmokeTestTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.fake_app = self.tmp / "fake_app.py"
        self.fake_app.write_text(FAKE_APP, encoding="utf-8")

    def check(self, *command):
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            passed = smoke_test.smoke_test(list(command))
        return passed, shown.getvalue()

    def check_build(self, code, text):
        return self.check(sys.executable, str(self.fake_app), str(code), text)

    def test_a_build_that_reports_ok_passes(self):
        passed, shown = self.check_build(0, "ok: Python 3.14.3 on arm64, Tk 8.6.17")
        self.assertTrue(passed)
        self.assertEqual(shown, "ok: Python 3.14.3 on arm64, Tk 8.6.17\nExit code: 0\n")

    def test_a_build_that_reports_a_problem_fails_and_shows_it(self):
        passed, shown = self.check_build(1, "ZeroDivisionError: division by zero")
        self.assertFalse(passed)
        self.assertEqual(shown, "ZeroDivisionError: division by zero\nExit code: 1\n")

    def test_the_exit_code_and_the_report_must_both_say_it_went_well(self):
        self.assertFalse(self.check_build(1, "ok: but the exit code says otherwise")[0])
        self.assertFalse(self.check_build(0, "something else")[0])

    def test_a_build_that_writes_no_report_fails(self):
        passed, shown = self.check_build(0, "none")
        self.assertFalse(passed)
        self.assertIn("It did not write a report.", shown)

    def test_a_build_that_never_finishes_fails(self):
        with mock.patch.object(smoke_test, "TIMEOUT", 0.5):
            passed, shown = self.check_build(0, "hang")
        self.assertFalse(passed)
        self.assertEqual(shown, "It was still running after 0.5 seconds.\n")

    def test_a_build_that_cannot_start_fails(self):
        passed, shown = self.check(str(self.tmp / "missing"))
        self.assertFalse(passed)
        self.assertIn("It could not be started", shown)

    def test_main_explains_itself_without_a_command(self):
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            self.assertEqual(smoke_test.main([]), 2)
        self.assertIn("python release/smoke_test.py", shown.getvalue())

    def test_main_turns_the_verdict_into_an_exit_code(self):
        with mock.patch.object(smoke_test, "smoke_test", side_effect=[True, False]) as check:
            self.assertEqual(smoke_test.main(["arch", "-x86_64", "app"]), 0)
            self.assertEqual(smoke_test.main(["app"]), 1)
        self.assertEqual(check.call_args_list, [mock.call(["arch", "-x86_64", "app"]), mock.call(["app"])])


if __name__ == "__main__":
    unittest.main()
