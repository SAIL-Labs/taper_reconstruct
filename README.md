# Taper Tools

Record and reconstruct photonic lantern taper images from diameter scan videos.

## Components

- **Taper Recorder** — System tray app that records a selected screen region to MP4 during taper diameter scans, with integrated one-click reconstruction. Each recording is tagged with its scan speed, outlined on screen while it runs, and logged.
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

### Standalone executable (Windows)

Download `TaperRecorder.exe` from the [latest release](../../releases/latest). No Python installation required.

## Usage

### Taper Recorder (GUI)

```bash
taper-recorder
```

1. A system tray icon (blue "T") appears in the taskbar
2. Right-click → **Select Region...** to draw a capture area
3. Right-click → **Show Region Overlay** to confirm the region is on the right spot
4. Right-click → **Start Recording...** — asks for the scan speed, an optional
   sample label, and the output folder
5. Right-click → **Stop Recording** when the scan is complete
6. Right-click → **Reconstruct Last Recording...** to generate the composite image

Settings (scan speed, frame rate, compression, etc.) are available via the tray menu and persist between sessions.

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

While the overlay is on, the captured region is outlined on screen (cyan when
idle, red while recording) with a status panel showing elapsed time, frame
count, measured frame rate, scan speed, and a live thumbnail of what is being
captured. Both the outline and the panel are drawn *outside* the captured
region, so neither appears in the recording. If the region fills the screen and
the panel has nowhere to go, only the outline is drawn.

#### Files written per recording

| File | Contents |
|------|----------|
| `<name>.mp4` | The recording |
| `<name>.meta.json` | Scan speed, label, region, target vs. measured frame rate, frame timestamps, reconstruction settings |
| `<name>.log` | Capture log: environment, frame size, progress, capture statistics |
| `<name>.reconstruct.log` | Written when that video is reconstructed — inputs, alignment diagnostics, and the traceback if it fails |
| `<name>.reconstructed.png` | The composite image |

Reconstruction reads `<name>.meta.json` for the scan speed and the frame rate
actually achieved during capture, so a recording that ran slower than the
requested rate still gets the right pixel scale.

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
pip install pyinstaller
pyinstaller --onefile --windowed \
    --name TaperRecorder \
    --hidden-import=taper_tools.reconstruct \
    --hidden-import=scipy.stats \
    taper_tools/recorder.py
```

The executable will be in `dist/TaperRecorder.exe`.

### Via GitHub Actions

Push a version tag to trigger an automated build:

```bash
git tag v0.1.0
git push --tags
```

This runs the workflow in `.github/workflows/build-executables.yml`, which builds the Windows executable and creates a GitHub Release with the `.exe`.

You can also trigger a build manually from the Actions tab using "workflow_dispatch".

## Dependencies

- **numpy** — array operations
- **opencv-python-headless** — video I/O and phase correlation
- **PyQt6** — system tray GUI
- **mss** — screen capture
- **tqdm** — progress bars
- **scipy** — KDE for shift peak detection (optional, falls back to histogram)

## License

MIT
