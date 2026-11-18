# Clipper

A small desktop app for cutting clips out of video files. Open a video, scrub
through it, mark a start and end point, and export the selection as an **mp4**
or **mp3**. The original file is never modified.

## Features

- Preview and seek through any video your system can play (mp4, mov, mkv, webm, avi, m4v)
- Mark start/end points with buttons or keyboard shortcuts
- Preview just the selected range before exporting
- Export as **mp4** (video + audio) or **mp3** (audio only)
- Two mp4 modes: frame-accurate re-encode, or near-instant fast copy

## Requirements

- Python 3.10 or newer
- [PySide6](https://pypi.org/project/PySide6/) (used for the window and video preview)
- [ffmpeg](https://ffmpeg.org/download.html) installed and available on your `PATH` (used for the actual export)

## Installation

1. Install the Python dependency:

   ```
   pip install PySide6
   ```

2. Install ffmpeg:

   | OS      | Command                          |
   |---------|----------------------------------|
   | Windows | `winget install ffmpeg`          |
   | macOS   | `brew install ffmpeg`            |
   | Ubuntu/Debian | `sudo apt install ffmpeg`  |

3. Check it worked by running `ffmpeg -version` in a terminal.

## Usage

```
python clipper.py              # then click "Open video…"
python clipper.py myvideo.mp4  # or open a file directly
```

1. Open a video.
2. Seek to where you want the clip to begin and press **Set start** (or `I`).
3. Seek to where it should end and press **Set end** (or `O`).
4. Press **Preview** (or `P`) to check the selection.
5. Choose **mp4** or **mp3**, then click **Export clip…** and pick where to save it.

If you never set a marker, the start defaults to the beginning and the end to
the end of the video.

## Keyboard shortcuts

| Key                | Action                              |
|--------------------|-------------------------------------|
| `Space`            | Play / pause                        |
| `I`                | Set start marker at current position |
| `O`                | Set end marker at current position  |
| `P`                | Preview the selected range          |
| `←` / `→`          | Seek back / forward 1 second        |
| `Shift` + `←` / `→`| Seek back / forward 5 seconds       |

## Export modes

| Format | Option                | Speed   | Accuracy                                  |
|--------|-----------------------|---------|-------------------------------------------|
| mp4    | Re-encode (default)   | Slower  | Frame-accurate cut                        |
| mp4    | **Fast copy** ticked  | Instant | Start snaps to the nearest keyframe (can be off by a second or two) |
| mp3    | n/a                   | Fast    | Accurate; video is dropped                |

Re-encoding uses H.264 video (CRF 20) and AAC audio (192 kbps). MP3 output uses
LAME at quality level 2 (about 190 kbps VBR).

Exported files are named `<original>_clip.<ext>` by default and saved wherever
you choose. An existing file with the same name will be overwritten.

## How it works

- **PySide6 (Qt)** handles the window, video playback and controls. It is used
  only for previewing and choosing times.
- **ffmpeg** does the cutting and conversion. It is launched in the background
  with `QProcess`, so the interface stays responsive during export.
- All times are stored internally in milliseconds, the unit Qt's media player
  uses, and converted to seconds only when the ffmpeg command is built.

The ffmpeg command it runs looks roughly like this (re-encode mode):

```
ffmpeg -y -ss <start> -i input.mp4 -t <duration> \
       -c:v libx264 -preset fast -crf 20 -c:a aac -b:a 192k -movflags +faststart \
       output.mp4
```

## Troubleshooting

- **"ffmpeg not found"**: ffmpeg isn't on your `PATH`. Install it, then restart your terminal and the app.
- **Video won't play or shows a black screen**: Qt uses your system's media codecs. Try a different file, or on Linux install the GStreamer plugins (`sudo apt install gstreamer1.0-libav gstreamer1.0-plugins-good`).
- **Fast-copy clip starts at the wrong spot or has a frozen first frame**: that's the keyframe limitation. Untick *Fast copy* for an exact cut.
- **Export failed**: an error dialog shows the tail of ffmpeg's log. The cause (bad path, unsupported codec, no write permission) is usually in the last few lines.

## Ideas for extending

- Export several clips from one session
- A waveform or thumbnail strip under the seek bar
- Fine-grained start/end time entry (type a timestamp)
- Extra output formats (wav, gif, webm)