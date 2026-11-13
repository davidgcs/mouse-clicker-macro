"""Mouse Macro Recorder: record mouse movement and clicks, then replay or loop them."""

import sys

if sys.platform == "win32":
    # Makes pynput report real pixels on scaled displays so recorded positions match
    # replayed ones. It must run before pynput or Tk are imported, hence it lives here.
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI aware
    except (AttributeError, OSError):
        pass
