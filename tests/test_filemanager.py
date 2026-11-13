import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from macro_recorder import filemanager


class FileManagerTestCase(unittest.TestCase):
    """Nothing is ever started: the commands that would be run are recorded instead."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name) / "my macros"  # with a space, like many real folders
        self.folder.mkdir()
        self.file = self.folder / "macro 1.json"
        self.file.write_text("{}", encoding="utf-8")
        self.run = self.patch(filemanager.subprocess, "run", return_value=mock.Mock(returncode=0, stderr=""))
        self.popen = self.patch(filemanager.subprocess, "Popen")
        self.startfile = self.patch(filemanager.os, "startfile", create=True)

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def on(self, platform):
        self.patch(sys, "platform", new=platform)

    def started(self):
        return self.run.called or self.popen.called or self.startfile.called


class NameTests(unittest.TestCase):
    def test_each_system_calls_it_by_its_own_name(self):
        for platform, name in (("darwin", "Finder"), ("win32", "File Explorer"), ("linux", "file manager")):
            with self.subTest(platform), mock.patch.object(sys, "platform", platform):
                self.assertEqual(filemanager.file_manager_name(), name)


class MacTests(FileManagerTestCase):
    def setUp(self):
        super().setUp()
        self.on("darwin")

    def test_the_file_is_selected_in_finder(self):
        filemanager.show_in_file_manager(self.file)
        self.assertEqual(self.run.call_args.args[0], ["open", "-R", str(self.file)])
        self.popen.assert_not_called()

    def test_a_path_given_as_text_works_too(self):
        filemanager.show_in_file_manager(str(self.file))
        self.assertEqual(self.run.call_args.args[0], ["open", "-R", str(self.file)])

    def test_a_relative_path_is_made_absolute(self):
        with mock.patch.object(Path, "absolute", return_value=self.file):
            filemanager.show_in_file_manager("macro 1.json")
        self.assertEqual(self.run.call_args.args[0], ["open", "-R", str(self.file)])

    def test_a_missing_file_opens_its_folder_instead(self):
        filemanager.show_in_file_manager(self.folder / "gone.json")
        self.assertEqual(self.run.call_args.args[0], ["open", str(self.folder)])

    def test_nothing_is_started_when_the_folder_is_gone_too(self):
        with self.assertRaisesRegex(OSError, "does not exist any more"):
            filemanager.show_in_file_manager(self.folder / "nope" / "macro.json")
        self.assertFalse(self.started())

    def test_the_streams_are_not_inherited_from_a_windowed_app(self):
        filemanager.show_in_file_manager(self.file)
        kwargs = self.run.call_args.kwargs
        self.assertEqual((kwargs["stdin"], kwargs["stdout"]), (subprocess.DEVNULL, subprocess.DEVNULL))

    def test_a_failure_is_reported_with_what_the_command_said(self):
        self.run.return_value = mock.Mock(returncode=1, stderr="The file /x does not exist.\n")
        with self.assertRaisesRegex(OSError, r"^The file /x does not exist\.$"):
            filemanager.show_in_file_manager(self.file)

    def test_a_failure_without_a_message_is_reported_too(self):
        self.run.return_value = mock.Mock(returncode=3, stderr=None)
        with self.assertRaisesRegex(OSError, "open failed with exit code 3"):
            filemanager.show_in_file_manager(self.file)

    def test_a_command_that_hangs_is_reported(self):
        self.run.side_effect = subprocess.TimeoutExpired("open", filemanager.COMMAND_TIMEOUT)
        with self.assertRaisesRegex(OSError, "Finder did not answer"):
            filemanager.show_in_file_manager(self.file)

    def test_a_missing_command_is_reported(self):
        self.run.side_effect = FileNotFoundError(2, "No such file or directory", "open")
        with self.assertRaises(OSError):
            filemanager.show_in_file_manager(self.file)


class WindowsTests(FileManagerTestCase):
    def setUp(self):
        super().setUp()
        self.on("win32")

    def test_the_file_is_selected_in_explorer(self):
        filemanager.show_in_file_manager(self.file)
        # One string, so that the quotes around a path with spaces reach Explorer untouched.
        self.assertEqual(self.popen.call_args.args[0], f'explorer /select,"{self.file}"')
        self.run.assert_not_called()  # it does not wait for Explorer: its exit code is no use
        self.startfile.assert_not_called()

    def test_the_streams_are_not_inherited_from_a_windowed_app(self):
        filemanager.show_in_file_manager(self.file)
        kwargs = self.popen.call_args.kwargs
        self.assertEqual(
            (kwargs["stdin"], kwargs["stdout"], kwargs["stderr"]),
            (subprocess.DEVNULL, subprocess.DEVNULL, subprocess.DEVNULL),
        )

    def test_a_missing_file_opens_its_folder_instead(self):
        filemanager.show_in_file_manager(self.folder / "gone.json")
        self.startfile.assert_called_once_with(self.folder)
        self.popen.assert_not_called()

    def test_nothing_is_started_when_the_folder_is_gone_too(self):
        with self.assertRaisesRegex(OSError, "does not exist any more"):
            filemanager.show_in_file_manager(self.folder / "nope" / "macro.json")
        self.assertFalse(self.started())

    def test_an_explorer_that_cannot_start_is_reported(self):
        self.popen.side_effect = FileNotFoundError(2, "The system cannot find the file specified")
        with self.assertRaises(OSError):
            filemanager.show_in_file_manager(self.file)


class OtherSystemsTests(FileManagerTestCase):
    def test_the_folder_is_opened(self):
        self.on("linux")
        filemanager.show_in_file_manager(self.file)
        self.assertEqual(self.run.call_args.args[0], ["xdg-open", str(self.folder)])


if __name__ == "__main__":
    unittest.main()
