#!/usr/bin/env python3
"""
Clipper - open a video or audio file, seek around, mark a start/end, and
export the marked range to many formats (mp4, webm, mkv, mov, mp3, wav,
ogg, flac, m4a, opus).

Requirements:
    pip install PySide6
    ffmpeg installed and on your PATH  (https://ffmpeg.org/download.html)

Run:
    python clipper.py [optional_media_file]

Shortcuts:
    Space       play / pause
    I / O       set start / end marker at current position
    Left/Right  seek -1s / +1s   (hold Shift for 5s)
    P           preview the selected range

How it is put together:
    * PySide6 (Qt) provides the window, the video player and the buttons.
      It is only used for *previewing* and picking times.
    * ffmpeg (an external program) does the actual cutting/converting.
      We launch it in the background with QProcess so the UI doesn't freeze.
    * All times inside the program are stored in MILLISECONDS (integers),
      because that is what Qt's media player uses. We only convert to
      seconds when building the ffmpeg command.
"""
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QProcess, QUrl
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QSlider, QVBoxLayout, QWidget,
)

# Extensions we treat as audio-only inputs: they play sound but have no
# frames, so the video pane is hidden when one of these is loaded.
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".oga", ".flac", ".m4a", ".aac",
              ".opus", ".wma"}

# Every format we can export to, and how to produce it:
#   "copy" - stream copy (-c copy) is allowed for this output format
#   "args" - ffmpeg arguments used when re-encoding into this format
# Audio formats include -vn so any video stream in the input is dropped.
EXPORT_FORMATS = {
    "mp4":  {"copy": True,  "args": ["-c:v", "libx264", "-preset", "fast", "-crf", "20",
                                     "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]},
    "webm": {"copy": True,  "args": ["-c:v", "libvpx-vp9", "-row-mt", "1", "-crf", "30",
                                     "-b:v", "0", "-c:a", "libopus", "-b:a", "160k"]},
    "mkv":  {"copy": True,  "args": ["-c:v", "libx264", "-preset", "fast", "-crf", "20",
                                     "-c:a", "aac", "-b:a", "192k"]},
    "mov":  {"copy": True,  "args": ["-c:v", "libx264", "-preset", "fast", "-crf", "20",
                                     "-c:a", "aac", "-b:a", "192k"]},
    "mp3":  {"copy": False, "args": ["-vn", "-c:a", "libmp3lame", "-q:a", "2"]},
    "wav":  {"copy": True,  "args": ["-vn", "-c:a", "pcm_s16le"]},
    "ogg":  {"copy": True,  "args": ["-vn", "-c:a", "libvorbis", "-q:a", "5"]},
    "flac": {"copy": True,  "args": ["-vn", "-c:a", "flac"]},
    "m4a":  {"copy": True,  "args": ["-vn", "-c:a", "aac", "-b:a", "192k"]},
    "opus": {"copy": True,  "args": ["-vn", "-c:a", "libopus", "-b:a", "160k"]},
}

# File-picker filter: a broad list of video and audio inputs.
OPEN_FILTER = (
    "Video files (*.mp4 *.mov *.mkv *.webm *.avi *.m4v *.mpg *.mpeg *.ts "
    "*.wmv *.flv *.3gp);;"
    f"Audio files ({' '.join('*' + e for e in sorted(AUDIO_EXTS))});;"
    "All files (*)"
)


def fmt(ms: int) -> str:
    """Turn milliseconds into a readable HH:MM:SS.mmm string.

    Example: 83250 -> "00:01:23.250"
    """
    ms = max(0, int(ms))  # guard against negative values
    # divmod gives (quotient, remainder), peeling off hours, then minutes, etc.
    h, rem = divmod(ms, 3_600_000)   # 3,600,000 ms in an hour
    m, rem = divmod(rem, 60_000)     # 60,000 ms in a minute
    s, milli = divmod(rem, 1000)     # 1,000 ms in a second
    return f"{h:02d}:{m:02d}:{s:02d}.{milli:03d}"


class Clipper(QWidget):
    """The main (and only) window of the app."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Clipper")
        self.resize(900, 650)

        # ---------- state ----------
        self.path: Path | None = None   # currently loaded media file
        self.start_ms = 0               # clip start marker
        self.end_ms = 0                 # clip end marker (0 = not set yet)
        self.previewing = False         # True while "Preview" is playing the range
        self.proc: QProcess | None = None  # the running ffmpeg process, if any

        # ---------- player ----------
        # Qt splits playback into three objects: the player decodes the file,
        # the audio output plays sound, and the video widget draws frames.
        self.video = QVideoWidget()
        self.audio = QAudioOutput()
        self.player = QMediaPlayer()
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)

        # ---------- widgets ----------
        # Transport row: open file, play/pause, current time display
        self.open_btn = QPushButton("Open media…")
        self.play_btn = QPushButton("Play / Pause")
        self.slider = QSlider(Qt.Horizontal)   # the seek bar
        self.slider.setRange(0, 0)             # real range is set once duration is known
        self.time_lbl = QLabel("00:00:00.000 / 00:00:00.000")

        # Marker row: choose which part of the video becomes the clip
        self.set_start_btn = QPushButton("Set start [I]")
        self.set_end_btn = QPushButton("Set end [O]")
        self.preview_btn = QPushButton("Preview [P]")
        self.range_lbl = QLabel("Start: --    End: --")

        # Export row: output format, fast-copy option, export button
        self.format_box = QComboBox()
        self.format_box.addItems(list(EXPORT_FORMATS))
        self.fast_check = QCheckBox("Fast copy (no re-encode, cuts on keyframes)")
        self.fast_check.setToolTip(
            "Copy the streams as-is instead of re-encoding. Only works when the "
            "source codecs are compatible with the chosen output container.")
        self.export_btn = QPushButton("Export clip…")
        self.status = QLabel("Open a video or audio file to begin.")

        # ---------- layout ----------
        # Each row is a horizontal layout; they're stacked in a vertical one.
        top = QHBoxLayout()
        top.addWidget(self.open_btn)
        top.addWidget(self.play_btn)
        top.addWidget(self.time_lbl, 1, Qt.AlignRight)

        marks = QHBoxLayout()
        marks.addWidget(self.set_start_btn)
        marks.addWidget(self.set_end_btn)
        marks.addWidget(self.preview_btn)
        marks.addWidget(self.range_lbl, 1)

        export = QHBoxLayout()
        export.addWidget(QLabel("Format:"))
        export.addWidget(self.format_box)
        export.addWidget(self.fast_check, 1)
        export.addWidget(self.export_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self.video, 1)   # stretch=1: video takes all spare space
        layout.addWidget(self.slider)
        layout.addLayout(top)
        layout.addLayout(marks)
        layout.addLayout(export)
        layout.addWidget(self.status)

        # ---------- signal/slot wiring ----------
        # Qt "signals" fire on events (a click, a position change...);
        # we connect each one to the method that should handle it.
        self.open_btn.clicked.connect(self.open_dialog)
        self.play_btn.clicked.connect(self.toggle_play)
        self.set_start_btn.clicked.connect(self.set_start)
        self.set_end_btn.clicked.connect(self.set_end)
        self.preview_btn.clicked.connect(self.preview)
        self.export_btn.clicked.connect(self.export)

        # "Fast copy" is only offered for output formats that allow stream copy.
        self.format_box.currentTextChanged.connect(
            lambda t: self.fast_check.setEnabled(
                EXPORT_FORMATS.get(t, {}).get("copy", False)))

        # Dragging the slider seeks the player. Clicking on the bar also
        # jumps there (sliderPressed), not just dragging.
        self.slider.sliderMoved.connect(self.player.setPosition)
        self.slider.sliderPressed.connect(
            lambda: self.player.setPosition(self.slider.value()))

        # The player tells us when the playhead moves / the file length is known.
        self.player.positionChanged.connect(self.on_position)
        self.player.durationChanged.connect(self.on_duration)

        # Keyboard shortcuts (key, handler). Lambdas let us pass an argument.
        for key, fn in [
            ("Space", self.toggle_play),
            ("I", self.set_start),
            ("O", self.set_end),
            ("P", self.preview),
            ("Left", lambda: self.nudge(-1000)),        # back 1 second
            ("Right", lambda: self.nudge(1000)),        # forward 1 second
            ("Shift+Left", lambda: self.nudge(-5000)),  # back 5 seconds
            ("Shift+Right", lambda: self.nudge(5000)),  # forward 5 seconds
        ]:
            QShortcut(QKeySequence(key), self, activated=fn)

        # Optional: a file path passed on the command line is opened immediately.
        if len(sys.argv) > 1:
            self.load(Path(sys.argv[1]))

    # ------------------------------------------------------------------
    # File loading and playback
    # ------------------------------------------------------------------
    def open_dialog(self):
        """Show a file picker and load the chosen media file."""
        f, _ = QFileDialog.getOpenFileName(
            self, "Open media file", "", OPEN_FILTER)
        if f:  # empty string means the user cancelled
            self.load(Path(f))

    def load(self, path: Path):
        """Load a media file into the player and reset the markers."""
        self.path = path
        # Qt wants a URL, not a plain path; resolve() makes it absolute first.
        self.player.setSource(QUrl.fromLocalFile(str(path.resolve())))
        self.start_ms = 0
        self.end_ms = 0  # will be filled in by on_duration once Qt knows the length
        self.player.pause()  # show the first frame without auto-playing
        # Audio-only files have no frames to show, so hide the video pane.
        is_audio = path.suffix.lower() in AUDIO_EXTS
        self.video.setVisible(not is_audio)
        self.setWindowTitle(f"Clipper - {path.name}")
        self.status.setText(
            f"Loaded {path.name}" + (" (audio only)" if is_audio else ""))

    def toggle_play(self):
        """Play if paused, pause if playing."""
        self.previewing = False  # manual control cancels any running preview
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def nudge(self, delta):
        """Jump forward/backward by `delta` ms, staying within the video."""
        new_pos = self.player.position() + delta
        self.player.setPosition(max(0, min(self.player.duration(), new_pos)))

    def on_duration(self, dur):
        """Called when the video length becomes known (after loading)."""
        self.slider.setRange(0, dur)   # slider units are milliseconds
        if self.end_ms == 0:
            self.end_ms = dur          # default selection = whole video
        self.update_range_label()

    def on_position(self, pos):
        """Called continuously during playback as the playhead moves."""
        # Don't fight the user: only move the slider if they aren't dragging it.
        if not self.slider.isSliderDown():
            self.slider.setValue(pos)
        self.time_lbl.setText(f"{fmt(pos)} / {fmt(self.player.duration())}")

        # If we're previewing the selection, stop once we reach the end marker.
        if self.previewing and pos >= self.end_ms:
            self.player.pause()
            self.previewing = False

    # ------------------------------------------------------------------
    # Start / end markers
    # ------------------------------------------------------------------
    def set_start(self):
        """Mark the current playhead position as the clip start."""
        self.start_ms = self.player.position()
        # Keep the range valid: if start passed the end, extend end to the video's end.
        if self.end_ms <= self.start_ms:
            self.end_ms = self.player.duration()
        self.update_range_label()

    def set_end(self):
        """Mark the current playhead position as the clip end."""
        self.end_ms = self.player.position()
        # Keep the range valid: if end is before start, reset start to the beginning.
        if self.start_ms >= self.end_ms:
            self.start_ms = 0
        self.update_range_label()

    def update_range_label(self):
        """Refresh the 'Start / End / length' text under the video."""
        self.range_lbl.setText(
            f"Start: {fmt(self.start_ms)}    End: {fmt(self.end_ms)}    "
            f"({(self.end_ms - self.start_ms) / 1000:.2f}s)")

    def preview(self):
        """Play only the selected range (jump to start, auto-stop at end)."""
        self.player.setPosition(self.start_ms)
        self.previewing = True  # on_position() watches this flag to stop at end_ms
        self.player.play()

    # ------------------------------------------------------------------
    # Export (runs ffmpeg)
    # ------------------------------------------------------------------
    def export(self):
        """Ask where to save, build an ffmpeg command, and run it."""
        # --- sanity checks before doing anything ---
        if not self.path:
            QMessageBox.information(self, "Clipper", "Open a media file first.")
            return
        if not shutil.which("ffmpeg"):  # looks for ffmpeg on the PATH
            QMessageBox.critical(self, "Clipper",
                                 "ffmpeg not found. Install it and make sure it's on your PATH.")
            return
        if self.end_ms <= self.start_ms:
            QMessageBox.warning(self, "Clipper", "End must be after start.")
            return

        # --- choose the output file ---
        ext = self.format_box.currentText()          # e.g. "mp4", "webm", "wav"
        spec = EXPORT_FORMATS[ext]
        default = f"{self.path.stem}_clip.{ext}"     # e.g. holiday_clip.mp4
        out, _ = QFileDialog.getSaveFileName(
            self, "Export clip", str(self.path.with_name(default)),
            f"{ext.upper()} (*.{ext});;All files (*)")
        if not out:
            return  # user cancelled
        if not out.lower().endswith(f".{ext}"):
            out += f".{ext}"  # some platforms don't append the extension for us

        # --- build the ffmpeg command ---
        # ffmpeg takes seconds, so convert from our milliseconds.
        start = f"{self.start_ms / 1000:.3f}"
        dur = f"{(self.end_ms - self.start_ms) / 1000:.3f}"

        # Common part:
        #   -y        overwrite the output file without asking
        #   -ss       start time; placed BEFORE -i so ffmpeg seeks quickly
        #   -i        the input file
        #   -t        how long the clip should be (duration, not end time)
        args = ["-y", "-ss", start, "-i", str(self.path), "-t", dur]

        if self.fast_check.isChecked() and spec["copy"]:
            # -c copy: copy the streams as-is, no re-encoding (very fast, but
            #          can only cut on keyframes so the start may be slightly off).
            # -avoid_negative_ts make_zero: fixes timestamp quirks from the cut.
            args += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
        else:
            # Re-encode into the chosen format; each format lists its own args
            # in EXPORT_FORMATS (e.g. libx264+aac for mp4, vp9+opus for webm,
            # -vn + pcm_s16le for wav). Audio-only input into a video format
            # just yields a container with an audio stream.
            args += spec["args"]
        args.append(out)  # the output path always goes last

        # --- run ffmpeg in the background ---
        self.export_btn.setEnabled(False)  # prevent starting two exports at once
        self.status.setText("Exporting…")
        self.proc = QProcess(self)
        # Merge stderr into stdout so we can show ffmpeg's log if something fails.
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        # Call export_done when the process ends (code 0 = success).
        self.proc.finished.connect(lambda code, _s: self.export_done(code, out))
        self.proc.start("ffmpeg", args)

    def export_done(self, code, out):
        """Called when ffmpeg finishes; report success or show the error log."""
        self.export_btn.setEnabled(True)
        if code == 0:
            self.status.setText(f"Saved: {out}")
        else:
            # Show only the last ~1500 characters; the useful error is at the end.
            log = bytes(self.proc.readAll()).decode(errors="replace")[-1500:]
            self.status.setText("Export failed.")
            QMessageBox.critical(self, "ffmpeg error", log)


# Standard Python entry point: only runs when executed directly,
# not when this file is imported by another script.
if __name__ == "__main__":
    app = QApplication(sys.argv)  # Qt needs one application object per program
    w = Clipper()
    w.show()
    sys.exit(app.exec())          # start the event loop; exit with its return code