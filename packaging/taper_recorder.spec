# PyInstaller spec: one-folder, windowed build of taper_tools.recorder:main.
# Run from the repo root:  pyinstaller packaging/taper_recorder.spec
#
# Output: dist/TaperRecorder/ holding TaperRecorder.exe and _internal/. That folder is what
# Velopack packs into the installer (vpk pack --packDir dist/TaperRecorder) and what the
# portable zip holds. One-folder starts faster than one-file and keeps updates small.
#
# Nothing editable lives in this folder (updates replace all of it): settings are in
# ~/.taper_recorder.json and logs in ~/.taper_tools/logs.
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
NAME = "TaperRecorder"

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    # velopack and reconstruct are imported lazily
    hiddenimports=[
        "velopack",
        "taper_tools.reconstruct",
        "scipy.stats",
        "scipy.stats._kde",
    ],
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)
