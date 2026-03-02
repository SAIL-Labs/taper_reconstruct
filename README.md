# Taper Tools

Record and reconstruct photonic lantern taper images from diameter scan videos.

## Components

- **Taper Recorder** — System tray app that records a selected screen region to MP4 during taper diameter scans, with integrated one-click reconstruction.
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
3. Right-click → **Start Recording** (prompts for output folder)
4. Right-click → **Stop Recording** when the scan is complete
5. Right-click → **Reconstruct Last Recording...** to generate the composite image

Settings (scan speed, frame rate, compression, etc.) are available via the tray menu and persist between sessions.

### Taper Reconstruct (CLI)

```bash
# Auto mode (recommended) — derives pixel scale from scan speed
taper-reconstruct video.mp4 --speed 1000 -o taper.png

# Constant mode — uses known pixel scale
taper-reconstruct video.mp4 --mode constant --speed 1000 --pixel_scale 1.5

# With compression along scan axis
taper-reconstruct video.mp4 --speed 1000 --compress 10

# Full options
taper-reconstruct --help
```

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
