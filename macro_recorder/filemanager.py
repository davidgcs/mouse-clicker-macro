"""Shows a file in the system's file manager: Finder on macOS, File Explorer on Windows."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Finder answers as soon as it has been asked; this only guards against a command that hangs.
COMMAND_TIMEOUT = 10


def file_manager_name() -> str:
    if sys.platform == "darwin":
        return "Finder"
    if sys.platform == "win32":
        return "File Explorer"
    return "file manager"


def show_in_file_manager(path: str | os.PathLike[str]) -> None:
    """Opens the folder that holds `path` with the file selected in it.

    When the file is gone but its folder is not, the folder is opened without a selection.
    Raises OSError, with a message that can be shown to the user, when that is not possible.
    """
    target = Path(path).absolute()
    folder = target.parent
    select = target.exists()
    if not select and not folder.is_dir():
        raise OSError(f"The folder {folder} does not exist any more.")
    if sys.platform == "win32":
        if select:
            # Explorer takes the whole option as one string: /select,"C:\some folder\file.json"
            subprocess.Popen(
                f'explorer /select,"{target}"',
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )  # its exit code says nothing: Explorer reports 1 even when it worked
        else:
            os.startfile(folder)
    elif sys.platform == "darwin":
        _run(["open", "-R", str(target)] if select else ["open", str(folder)])
    else:
        _run(["xdg-open", str(folder)])


def _run(command: list[str]) -> None:
    # All three streams are given: a windowed app has none of its own to pass on.
    try:
        done = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise OSError(f"{file_manager_name()} did not answer.") from None
    if done.returncode:
        raise OSError((done.stderr or "").strip() or f"{command[0]} failed with exit code {done.returncode}.")
