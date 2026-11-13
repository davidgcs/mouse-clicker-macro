# PyInstaller recipe for the downloadable app. From the repository root:
#     pip install -r release/requirements.txt
#     pyinstaller --noconfirm --clean release/MouseMacroRecorder.spec
# Windows gets a single file, dist/MouseMacroRecorder.exe. macOS gets "dist/Mouse Macro Recorder.app".
# The APP_VERSION environment variable sets the version the macOS app reports.
import os
import sys
import sysconfig

NAME = "MouseMacroRecorder"
ROOT = os.path.dirname(SPECPATH)

analysis = Analysis([os.path.join(ROOT, "main.py")])
pyz = PYZ(analysis.pure)

if sys.platform == "darwin":
    # A universal2 Python, such as the one from python.org, makes an app for Apple silicon and Intel Macs alike.
    universal = sysconfig.get_platform().endswith("universal2")
    exe = EXE(
        pyz,
        analysis.scripts,
        exclude_binaries=True,
        name=NAME,
        console=False,
        upx=False,
        target_arch="universal2" if universal else None,
    )
    BUNDLE(
        COLLECT(exe, analysis.binaries, analysis.datas, name=NAME, upx=False),
        name="Mouse Macro Recorder.app",
        bundle_identifier="io.github.davidgcs.mouse-macro-recorder",
        version=os.environ.get("APP_VERSION", "0.0.0"),
    )
else:
    EXE(pyz, analysis.scripts, analysis.binaries, analysis.datas, name=NAME, console=False, upx=False)
