# <img src="assets/icon.svg" width=64 height=64> CZEditor
a scene-based video/audio timeline editor made with PyQT6

## note
some parts of this editor (mostly the compositor) are ai-asssisted, so umm yeah

## features
- **scene-based compositing** - build scenes out of image, video, sound, and
  shape placements on a multi-track timeline, with position, rotation, cropping/masking, and
  size all adjustable per placement
- **reusable symbols** - group placements into named, nested symbols that can
  be dropped back into any scene, with optional recursive nesting
- **timeline editing** - cut, copy, paste, split at the playhead, undo/redo,
  and snap-to-playhead, with frame and second-stepped playback scrubbing
- **live preview** - a real-time preview panel with dedicated pan, crop, and
  mask interaction modes alongside normal editing
- **placement animations** - built-in fade, pop, aero, and slide u/d/l/f in/out animations
  with adjustable duration
- **video & audio export** - render the timeline to video using multiple
  worker processes in parallel, with an offline audio mixdown of every
  sound/video placement muxed in via ffmpeg. png frame export is also
  supported
- **sample-accurate audio engine** - a numpy/`sounddevice`-based
  mixer for realtime playback, automatically falling back to a
  qtmultimedia-based engine if those packages or a usable output device
  aren't available
- **asset library** - drag and drop images, video, and audio into scenes
  directly from a library panel, with thumbnail previews and waveform
  display for audio
- **project management** - new/open/save/save-as, per-project settings,
  autosave, and a configurable default project folder
- **customizable keybinds** - every major action's shortcut can be
  rebound from preferences
- **localization** - ui strings are loaded from json locale files, with
  english included and more installable by dropping in a new locale file
- **cross-platform** - runs anywhere PyQT6 does (linux, macos, windows)

## running from source
```bash
pip install -r requirements.txt
python run.py
```

the `numpy` / `sounddevice` / `soundfile` / `opencv-python-headless` packages
in `requirements.txt` are optional (see the comment there) but recommended
for the sample-accurate audio engine and video preview. exporting video also
needs `ffmpeg` on your `PATH`.

## downloads
every push to `main` builds standalone binaries for Linux, macOS, and Windows
and publishes them as a GitHub Release - see the [releases page](../../releases) for the latest build:

| platform | format |
| --- | --- |
| linux | `.AppImage` and `.tar.gz` |
| macOS | `.app` (zipped) |
| windows | `.exe` |

these are unsigned builds, so macOS Gatekeeper and Windows SmartScreen will
get pissed af on first launch

## building locally
binaries are built with [PyInstaller](https://pyinstaller.org/), using
`assets/icon.svg` as the app icon (rasterized to `.ico`/`.icns`/`.png` by
`scripts/generate_icons.py`, which also sets the in-app window icon at
runtime). see `.github/workflows/build-release.yml` for the exact commands
used in CI; roughly:

```bash
pip install -r requirements.txt pyinstaller
python scripts/generate_icons.py
pyinstaller --onefile --name CZEditor \
  --icon build_assets/icon.ico \
  --add-data "assets:assets" --add-data "locale:locale" \
  run.py
```

(use `;` instead of `:` in `--add-data` on Windows, drop `--icon` on Linux,
and drop `--onefile` in favor of `--windowed` on macOS to get a proper
`.app` bundle)
