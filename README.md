# Taper Tools

Record and reconstruct photonic lantern taper images from diameter scan videos.

## Components

- **Taper Recorder** — System tray app that records screen regions to MP4: a diameter scan for reconstruction (tagged with its scan speed) or the taper being drawn (a plain video), each from its own region, outlined on screen while it runs, and logged. Includes one-click reconstruction.
- **Taper Reconstruct** — CLI tool that stitches video frames into a single composite image of the taper using phase correlation alignment and linear blending.

## Installation

### From source (recommended for development)

Requires Python ≥ 3.10.

```bash
# Clone and install
git clone https://github.com/your-org/taper_reconstruct.git
cd taper_reconstruct
pip install -e .
```

### Using conda/mamba

```bash
mamba env create -f environment.yml
mamba activate taper_reconstruct
pip install -e .
```

### Windows installer (recommended)

Download `TaperRecorder-win-Setup.exe` from the [latest release](../../releases/latest) and run it. No Python and no admin rights needed: it installs for the current user under `%LOCALAPPDATA%\TaperRecorder`, adds a Start menu shortcut and an entry in Add/Remove Programs.

The installed app keeps itself up to date from this repository's GitHub releases:

- A few seconds after start-up it quietly checks for a newer release and downloads it in the background.
- When one is ready, a tray message says so and the tray menu gains **Restart to Install vX.Y.Z**. Ignore it and the update is installed the next time the app starts. Restarting is refused while a recording or reconstruction is running.
- Tray menu → **Check for Updates...** reports every outcome; **About Taper Recorder...** shows the version.
- Start it with `--no-update-check` to skip the start-up check.

Only one copy runs per user. Starting it again while it's running just shows a tray message from the running copy, so a double-clicked shortcut can't start a second recorder.

Settings (`~/.taper_recorder.json`) and logs (`~/.taper_tools/logs`) live outside the install folder, so updates never touch them.

`TaperRecorder-win-Portable.zip` holds the same app without installing; unzip and run `TaperRecorder.exe`. It updates itself too.

**Upgrading from 0.4.0 or earlier** (the single `TaperRecorder-vX.Y.Z.exe` in a zip): those builds can't update themselves. Install `TaperRecorder-win-Setup.exe` once by hand, then delete the old exe. Your settings carry over.

## Usage

### Taper Recorder (GUI)

```bash
taper-recorder
```

1. A system tray icon (blue "T") appears in the taskbar
2. Right-click → **Select Scan Region...** to draw the capture area for
   diameter scans; **Select Process Region...** for the view of the taper
   being drawn (the two are independent and remembered separately)
3. Right-click → **Show Region Overlays** to confirm both are on the right
   spot (scan in cyan, process in amber)
4. Right-click → **Start Scan Recording...** — asks for the scan speed, scan
   axis, an optional sample label and notes, a time limit, and the output
   folder — or **Start Process Recording...**, which asks only for a label,
   notes, frame rate and time limit
5. **Stop** (or **Pause**) from the buttons beside the overlay, or
   right-click → **Stop … Recording** / **Pause Recording**
6. Right-click → **Reconstruct Last Scan...** — shows the scan speed, axis
   and frame rate it is about to use, and where each came from, then builds
   the composite image

#### Two kinds of recording

| | Scan | Process |
|---|---|---|
| What it captures | The diameter scan sweeping along the taper | The taper being drawn, as it happens |
| Region | Scan region | Process region |
| Asks for | Scan speed, axis, label, notes | Label, notes |
| Default frame rate | 15 fps | 5 fps (a draw runs for minutes) |
| File name | `taper_scan_<time>_<speed>umps[_label].mp4` | `taper_process_<time>[_label].mp4` |
| Afterwards | Reconstructed into one image | Kept as a video |

Only one recording runs at a time. Both write a `.meta.json` sidecar and a
capture log; a process sidecar carries `"kind": "process"`, and if such a
video is ever sent to **Reconstruct from File...** the confirmation dialog
says so in red.

#### Filing a recording whose folder doesn't exist yet

A device usually gets its data folder only once the taper is finished, which
is after its process recording has to start. So the setup dialog has a
checkbox, **Choose the folder when the recording finishes** — on by default
for process recordings, off for scans, remembered per mode. A deferred
recording is written to `<output folder>/unfiled/`, and when it stops a
prompt asks where it goes: **Choose Folder...** opens a picker (create the
device folder right there) and moves the video with its sidecar and logs;
**Later** leaves it in `unfiled/`.

Anything left behind is filed from **File Held Recordings...** in the tray
menu: tick the recordings that belong together — typically the draw and the
scan that followed it — and pick one destination. The video and every file
sharing its name (`.meta.json`, `.log`, `.reconstruct.log`,
`.reconstructed.png`) always move as a set, nothing is ever overwritten, and
a video that is still being reconstructed is held back until that finishes.
The tray mentions how many recordings are waiting when the app starts.

Settings hold the defaults (scan speed, axis, frame rate, compression, etc.)
and persist between sessions; the recording dialog starts from them and
updates them with whatever you last used.

#### Scan speed and file naming

Every recording prompts for the scan speed, which goes straight into the file name:

```
taper_scan_20260822_143000_1000umps_lantern7.mp4
           │               │        └── optional sample label
           │               └── 1000 µm/s
           └── start time
```

Speeds with a fraction use `p` for the decimal point (`12p5umps` = 12.5 µm/s).

#### Capture overlay

While the overlays are on, each configured region is outlined on screen
(cyan for scan and amber for process when idle, red while recording, yellow
while paused) with a status panel showing elapsed time, frame count, measured
frame rate, scan speed, when the time limit will stop it, and a live thumbnail
of what is being captured. During a recording a strip with **Pause**/**Resume**
and **Stop** buttons sits beside the panel. Everything is drawn *outside* the
captured region, so none of it appears in the recording. If the region fills
the screen and the panel has nowhere to go, only the outline is drawn.

The recording overlay comes down with the recording. Whether the idle outlines
stay up afterwards is the **Keep the region overlays on screen after the
recording** setting (off by default), in both Settings and the setup dialog.

#### Pausing and the time limit

Pause stops frames being written; the file stays open and the elapsed clock
freezes, so the measured frame rate and the sidecar's frame timestamps are
unaffected. Each pause is recorded in the sidecar. Pausing a *scan* is allowed
but flagged, since frames either side of the gap will not line up: the capture
log says so, and the reconstruction dialog warns in red.

Every recording has a time limit — **Stop after** in the setup dialog, 5 min by
default for both modes, remembered per mode, 0 for none — as a safety net for
one left running. Paused time does not count. The overlay shows "stops at
MM:SS", switching to a countdown in the last minute, and when it fires the
recording ends exactly as Stop would, with the usual filing prompt if the
folder was deferred.

#### Files written per recording

| File | Contents |
|------|----------|
| `<name>.mp4` | The recording |
| `<name>.meta.json` | Scan speed, label, region, target vs. measured frame rate, frame timestamps, reconstruction settings |
| `<name>.log` | Capture log: environment, frame size, progress, capture statistics |
| `<name>.reconstruct.log` | Written when that video is reconstructed — inputs, alignment diagnostics, and the traceback if it fails |
| `<name>.reconstructed.png` | The composite image |

These always travel together: filing a held recording moves the whole set.

Reconstruction reads `<name>.meta.json` for the scan speed, scan axis and the
frame rate actually achieved during capture, so a recording that ran slower
than the requested rate still gets the right pixel scale.

#### Reconstructing

Choosing **Reconstruct Last Recording...** or **Reconstruct from File...**
opens a confirmation dialog listing the scan speed, scan axis and frame rate,
each tagged with its source: the recording's sidecar, the speed in the file
name, or the video container. A value that had to fall back to a Settings
default — a video from before this version, or one recorded elsewhere — is
flagged in red so it gets checked rather than trusted. Edit anything that is
wrong and press Enter. Automatic reconstruction after a recording skips the
dialog, since those values were just entered.

#### Logs

Everything is also appended to a rolling application log at
`~/.taper_tools/logs/taper_tools.log` (2 MB × 5 files; override the location with
the `TAPER_TOOLS_LOG_DIR` environment variable). Reach it from the tray menu via
**Logs → View Last Log...** or **Logs → Open Log Folder**. When a reconstruction
fails, the error dialog names the log holding the traceback.

### Taper Reconstruct (CLI)

```bash
# Recording made by the taper recorder — speed, frame rate and axis come
# from the .meta.json sidecar (or the speed in the file name)
taper-reconstruct taper_scan_20260822_143000_1000umps.mp4 -o taper.png

# Auto mode (recommended) — derives pixel scale from scan speed
taper-reconstruct video.mp4 --speed 1000 -o taper.png

# Constant mode — uses known pixel scale
taper-reconstruct video.mp4 --mode constant --speed 1000 --pixel_scale 1.5

# With compression along scan axis
taper-reconstruct video.mp4 --speed 1000 --compress 10

# Full options
taper-reconstruct --help
```

Each run writes `<output>.log` with the inputs, the alignment diagnostics and,
if the run fails, the traceback (`--log PATH` to move it, `--no-log` to skip it,
`--verbose` for debug detail on the console). `--ignore-sidecar` disables the
metadata lookup and `--fps` overrides the frame rate used for the pixel scale.

#### Alignment modes

| Mode | Description | Required args |
|------|-------------|---------------|
| `auto` (default) | Phase correlation + outlier replacement using known scan speed | `--speed` |
| `correlation` | Phase correlation only | — |
| `constant` | Fixed shift from known speed and pixel scale | `--speed`, `--pixel_scale` |

## Building the executable

### Locally with PyInstaller

```bash
pip install -e ".[dev]"
pyinstaller packaging/taper_recorder.spec
```

This is a one-folder build: `dist/TaperRecorder/` holds `TaperRecorder.exe` and `_internal/`. Updates only work in a copy installed by the Velopack installer, so a local build doesn't update itself.

### Releasing

The version lives in one place, `taper_tools/_version.py`. To release:

```bash
# bump __version__ in taper_tools/_version.py to 0.5.0, commit, then:
git tag v0.5.0
git push origin main v0.5.0
```

The workflow in `.github/workflows/build-executables.yml` then, on `windows-latest`:

1. installs the package and fails if the tag isn't `v` + `__version__`;
2. runs the tests;
3. builds the one-folder app with PyInstaller;
4. packs it with Velopack (`vpk pack`) into the installer, the portable zip and the update feed;
5. publishes all of `Releases/` to the GitHub release: `TaperRecorder-win-Setup.exe`, `TaperRecorder-win-Portable.zip`, `TaperRecorder-X.Y.Z-full.nupkg`, `releases.win.json`, `assets.win.json` and `RELEASES`. Keep them all: installed copies update from these.

Running the workflow by hand (Actions → Windows build → Run workflow) builds the same files as a workflow artifact, without a release.

The `vpk` CLI version (`VPK_VERSION` in the workflow) and the `velopack` Python package version (pinned in `pyproject.toml`) must stay equal.

## Dependencies

- **numpy** — array operations
- **opencv-python-headless** — video I/O and phase correlation
- **PyQt6** — system tray GUI
- **mss** — screen capture
- **tqdm** — progress bars
- **scipy** — KDE for shift peak detection (optional, falls back to histogram)
- **velopack** — installer hooks and self-update for the Windows build

## License

MIT
