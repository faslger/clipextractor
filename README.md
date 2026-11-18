# Clipper

A small desktop app for cutting clips out of video **or audio** files. Open a
file, scrub through it, mark a start and end point (shown as a highlighted
range on the seek bar), and export the selection to many formats. The
original file is never modified.

## Features

- Preview and seek through any media your system can play — video (mp4, mov,
  mkv, webm, avi, m4v, …) and audio (mp3, wav, ogg, flac, m4a, aac, opus, …)
- Mark start/end points with buttons or keyboard shortcuts; the marked range
  is highlighted on the seek bar
- Waveform strip under the seek bar shows audio activity over time
  (green/yellow/red loudness bars; a placeholder is shown if the file has
  no audio track)
- Preview just the selected range before exporting
- Export to **mp4, webm, mkv, mov** (video containers) or **mp3, wav, ogg,
  flac, m4a, opus** (audio)
- **Audio only** option: strip the video even when exporting to a video
  container
- Two export modes: frame-accurate re-encode, or near-instant **fast copy**
- **Options…** popup with extra ffmpeg settings: frame rate, resolution,
  aspect ratio, up/downscaling, quality (CRF), video/audio bitrate, playback
  speed, sample rate, channels, and raw extra arguments
- **Help** button (and a **?** button in the Options popup) opening in-app
  guide windows

## Requirements

- Python 3.10 or newer
- [PySide6](https://pypi.org/project/PySide6/) (used for the window and preview)
- [ffmpeg](https://ffmpeg.org/download.html) installed and available on your `PATH` (used for the actual export)

## Installation

1. Install the Python dependency:

   ```
   pip install -r requirements.txt
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
python clipper.py              # then click "Open…"
python clipper.py myvideo.mp4  # or open a file directly
```

1. Open a video or audio file.
2. Seek to where you want the clip to begin and press **Set start** (or `I`).
3. Seek to where it should end and press **Set end** (or `O`).
4. Press **Preview** (or `P`) to check the selection.
5. Choose a format, then click **Export…** and pick where to save it.

If you never set a marker, the start defaults to the beginning and the end to
the end of the file.

## Keyboard shortcuts

| Key                | Action                              |
|--------------------|-------------------------------------|
| `Space`            | Play / pause                        |
| `I`                | Set start marker at current position |
| `O`                | Set end marker at current position  |
| `P`                | Preview the selected range          |
| `←` / `→`          | Seek back / forward 1 second        |
| `Shift` + `←` / `→`| Seek back / forward 5 seconds       |

## Export row

| Control        | What it does |
|----------------|--------------|
| **Format**     | Output format: mp4, webm, mkv, mov, mp3, wav, ogg, flac, m4a, opus. The extension is appended automatically. |
| **Audio only** | Drops the video stream (`-vn`) even for video containers. Disabled for audio formats. |
| **Fast copy**  | Stream copy without re-encoding: instant, but cuts on keyframes (start can be off) and needs source codecs that fit the container. Disabled for mp3. |
| **Options…**   | Popup with extra ffmpeg settings (see below). Settings persist between openings. |
| **Export…**    | Asks for a filename and runs ffmpeg in the background. |

## Export options (the "Options…" popup)

Each slider is gated by a checkbox — an option is only applied when enabled.
Options are appended after the format's own settings, so they override them.
They are ignored when **Fast copy** is checked, and the video options are
ignored for **audio-only** exports.

| Option | ffmpeg flag | Notes |
|--------|-------------|-------|
| Set frame rate (1–120 fps) | `-r` | High fps on a low-fps source just duplicates frames |
| Resolution | `-vf scale=WxH` | Fixed output size; wins over the scale slider and aspect-ratio options |
| Aspect ratio | `pad` | Letterboxes the frame into 16:9, 4:3, 1:1, 9:16 or 21:9 — black bars added, content never cropped/resized; ignored when a fixed resolution is set |
| Scale (up/down, 25–400%) | `scale=iw*f:ih*f` | Rescales by a factor, keeps aspect ratio, rounds to even dimensions |
| Override quality (CRF 0–51) | `-crf` | Lower = better/larger; ~18–23 is near-lossless for H.264 |
| Video bitrate | `-b:v` | Fixed bitrate instead of CRF (e.g. for streaming targets) |
| Change speed (50–200%) | `setpts` + `atempo` | Clip length changes accordingly (3 s at 50% → ~6 s) |
| Audio bitrate | `-b:a` | Skipped for lossless wav/flac |
| Sample rate | `-ar` | e.g. 48000 Hz for video, 44100 Hz for music |
| Channels | `-ac` | Mono or stereo |
| Extra ffmpeg arguments | appended as-is | Raw flags, e.g. `-pix_fmt yuv420p -metadata title="My clip"` |

## Help

- **Help** (main window): user guide covering the workflow, shortcuts, the
  seek-bar markers, and the export row.
- **?** (inside the Options popup): explains every option in that dialog.

## How it works

- **PySide6 (Qt)** handles the window, playback and controls. It is used
  only for previewing and choosing times.
- **ffmpeg** does the cutting and conversion. It is launched in the
  background with `QProcess`, so the interface stays responsive during
  export.
- All times are stored internally in milliseconds, the unit Qt's media player
  uses, and converted to seconds only when the ffmpeg command is built.

The ffmpeg command it runs looks roughly like this (mp4 re-encode mode):

```
ffmpeg -y -ss <start> -i input.mp4 -t <duration> \
       -c:v libx264 -preset fast -crf 20 -c:a aac -b:a 192k -movflags +faststart \
       output.mp4
```

Export formats live in the `EXPORT_FORMATS` table at the top of
`clipper.py` — each entry lists whether stream copy is allowed and the ffmpeg
arguments used when re-encoding, so adding a format is a one-line change.

## Troubleshooting

- **"ffmpeg not found"**: ffmpeg isn't on your `PATH`. Install it, then restart your terminal and the app.
- **File won't play or shows a black screen**: Qt uses your system's media codecs. Try a different file, or on Linux install the GStreamer plugins (`sudo apt install gstreamer1.0-libav gstreamer1.0-plugins-good`). Exporting still works via ffmpeg even if preview doesn't.
- **Fast-copy clip starts at the wrong spot or has a frozen first frame**: that's the keyframe limitation. Untick *Fast copy* for an exact cut.
- **Export failed**: an error dialog shows the tail of ffmpeg's log. The cause (bad path, unsupported codec, no write permission) is usually in the last few lines.

## Ideas for extending

- Export several clips from one session (a queue)
- A waveform or thumbnail strip under the seek bar
- Fine-grained start/end time entry (type a timestamp)
- Animated GIF output
