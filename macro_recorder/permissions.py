"""macOS privacy permissions: without them the system silently hides or ignores mouse input."""

from __future__ import annotations

import subprocess
import sys

_SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?"
_PANES = {"Input Monitoring": "Privacy_ListenEvent", "Accessibility": "Privacy_Accessibility"}

_activity = None  # keeps the App Nap opt-out alive for the lifetime of the process


def missing_permissions() -> list[str]:
    """Permissions this process still lacks. Empty on other systems, or when it cannot tell."""
    if sys.platform != "darwin":
        return []
    try:
        import Quartz

        missing = []
        if not Quartz.CGPreflightListenEventAccess():  # needed to record
            missing.append("Input Monitoring")
        if not Quartz.CGPreflightPostEventAccess():  # needed to replay
            missing.append("Accessibility")
        return missing
    except (ImportError, AttributeError):
        return []


def open_privacy_settings(permission: str | None = None) -> None:
    """Open System Settings on the pane where `permission` can be granted."""
    try:
        subprocess.run(["open", _SETTINGS_URL + _PANES.get(permission, "Privacy")], check=False)
    except OSError:
        pass


def prevent_app_nap() -> None:
    """Stop macOS from throttling this app's timers while its window is hidden behind others.

    Throttling would distort the timing of recorded and replayed events.
    """
    global _activity
    if sys.platform != "darwin" or _activity is not None:
        return
    try:
        from Foundation import NSActivityUserInitiatedAllowingIdleSystemSleep, NSProcessInfo

        _activity = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
            NSActivityUserInitiatedAllowingIdleSystemSleep, "Recording and replaying mouse input"
        )
    except (ImportError, AttributeError):
        pass
