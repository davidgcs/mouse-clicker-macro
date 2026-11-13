# Mouse Macro Recorder

A small desktop app for macOS and Windows that records your mouse movements, clicks and scrolling as a
macro, saves it to a file, and replays it once or in a loop.

## Features

- **Record** with the Record button (3, 2, 1 countdown) or with a global shortcut (**F1** by default).
- **Stop** with the Stop button or a shortcut (**F2** by default). The macro is saved automatically.
- **Repeat** the macro once, **import** a macro file saved earlier, or **loop** it.
- A loop runs a set number of times (leave it empty to loop forever) with a pause in seconds or minutes
  between runs (5 seconds by default).
- Both shortcuts can be changed in the app, and the choice is remembered.

## Install

You need Python 3.9 or newer with Tk (the python.org installers include it; with Homebrew run
`brew install python-tk`).

```
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

If the install fails on macOS with an old Python, run `python -m pip install --upgrade pip` first.

## Run

```
python main.py
```

## Using it

**Record.** Click **Record**; the window counts down 3, 2, 1 and recording starts. Do your mouse
actions, then click **Stop** or press the stop shortcut. The macro is saved to
`~/MouseMacros/macro-<date>-<time>.json` and is ready to replay. Pressing the record shortcut starts
recording **immediately**, with no countdown, even when another app is in front. If you press Stop with
the button, the final click on the Stop button is left out of the macro.

**Repeat.** Click **Repeat** to replay the loaded macro once.

**Import.** Click **Import…** and pick a macro file saved earlier.

**Loop.** Set how many times to run it (empty means forever) and how long to wait between runs, in
seconds or minutes, then click **Loop**. The first run starts immediately and nothing waits after the last
one. For example, 10 times with 25 seconds runs the macro, waits 25 seconds, runs it again, and so on for
10 runs in total (9 waits). While a loop is running, the window shows the current run and the time left
until the next one.

**Stop playback.** Click **Stop** or press the stop shortcut at any time, even during a wait.

**Change a shortcut.** Click **Change…** next to it and press the new key (a single key, no combinations).
Shortcuts work while other apps are in front, and the key is still passed on to that app.

## Permissions and platform notes

**macOS.** The system blocks mouse access until you allow the app you start Python from (Terminal,
iTerm, PyCharm, VS Code...) in **System Settings > Privacy & Security**:

- **Input Monitoring** to record and for the shortcuts,
- **Accessibility** to replay.

Quit and reopen that app after allowing it. This program shows a red notice with a shortcut to the
settings while a permission is missing. On Mac laptops F1 and F2 are media keys unless you hold **fn**,
turn on "Use F1, F2, etc. keys as standard function keys" in System Settings > Keyboard, or choose other
shortcuts.

**Windows.** F1 usually opens Help in the app in front, because shortcuts are not swallowed; pick another
key if that bothers you. Windows apps running as administrator ignore input from a normal program, so run
this one as administrator to record or replay on them.

## Macro files

Macros are plain JSON, with one event per line and times in seconds from the first event:

```json
{
  "format": "mouse-macro",
  "version": 1,
  "created": "2025-01-31T10:15:00",
  "events": [
    {"t": 0.0, "type": "move", "x": 640, "y": 400},
    {"t": 0.35, "type": "click", "x": 640, "y": 400, "button": "left", "pressed": true},
    {"t": 0.42, "type": "click", "x": 640, "y": 400, "button": "left", "pressed": false},
    {"t": 0.9, "type": "scroll", "x": 640, "y": 400, "dx": 0, "dy": -3}
  ]
}
```

If you edit a file by hand, keep times to at most 1,000,000 seconds and positions and scroll amounts to
within 1,000,000 of zero; the app refuses to import a macro that goes beyond that.

The shortcuts are saved in `settings.json` in `~/Library/Application Support/MouseMacroRecorder` on
macOS, or `%APPDATA%\MouseMacroRecorder` on Windows.

## Good to know

- Positions are absolute screen coordinates, so replay on the same screen layout and resolution you
  recorded on.
- Only the mouse is recorded, not the keyboard.
- Playback clicks whatever is under the pointer, including this window if it is in the way.
- Only import macros you trust, because a macro can click anything on your screen.

## Development

```
python -m unittest discover -v
```

The tests use fake mouse and keyboard devices, so they never move or click the real pointer. The window
tests need Tk and a display, and are skipped when Tk is unavailable.

| File | Purpose |
| --- | --- |
| `main.py` | Starts the app |
| `macro_recorder/app.py` | Window and the record, play and shortcut state machine |
| `macro_recorder/recording.py` | Macro model and file format |
| `macro_recorder/recorder.py` | Captures mouse events |
| `macro_recorder/player.py` | Replays macros and runs loops |
| `macro_recorder/hotkeys.py` | Global shortcuts |
| `macro_recorder/settings.py` | Saved shortcuts |
| `macro_recorder/permissions.py` | macOS permission checks |
