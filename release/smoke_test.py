r"""Checks that a packaged build starts: runs it in self-test mode and reads the report it writes.

From the repository root, after building:
    python release/smoke_test.py dist\MouseMacroRecorder.exe
    python release/smoke_test.py "dist/Mouse Macro Recorder.app/Contents/MacOS/MouseMacroRecorder"
    python release/smoke_test.py arch -x86_64 "dist/Mouse Macro Recorder.app/Contents/MacOS/MouseMacroRecorder"
The last one runs the Intel half of a macOS build, through Rosetta.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TIMEOUT = 180  # seconds; a broken windowed build can wait forever on an error dialog nobody will close


def smoke_test(command: list[str]) -> bool:
    """Runs `command --self-test <report>`, prints what happened and says whether the build is fine."""
    tmp = Path(tempfile.mkdtemp())
    try:
        report = tmp / "self-test.txt"
        try:
            code = subprocess.run([*command, "--self-test", str(report)], timeout=TIMEOUT, check=False).returncode
        except subprocess.TimeoutExpired:
            print(f"It was still running after {TIMEOUT} seconds.")
            return False
        except OSError as exc:
            print(f"It could not be started: {exc}")
            return False
        outcome = report.read_text(encoding="utf-8") if report.exists() else "It did not write a report.\n"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(outcome, end="")
    print(f"Exit code: {code}")
    return code == 0 and outcome.startswith("ok: ")


def main(args: list[str]) -> int:
    if not args:
        print(__doc__)
        return 2
    return 0 if smoke_test(args) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
