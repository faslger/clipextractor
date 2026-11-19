#!/usr/bin/env python3
"""
Clipper - open a video or audio file, seek around, mark a start/end (the
marked range is highlighted on the seek bar), and export it to many
formats (mp4, webm, mkv, mov, mp3, wav, ogg, flac, m4a, opus).
Marked ranges can be collected into a clip list and exported all at once:
one file per clip, or all joined into a single file ("Join").
The "Options…" button opens a popup with extra ffmpeg settings
(frame rate, resolution, aspect ratio, scaling, quality, bitrates,
speed, raw arguments).

Requirements:
    pip install PySide6
    ffmpeg installed and on your PATH  (https://ffmpeg.org/download.html)

Run:
    python clipper.py [optional_media_file]

Shortcuts:
    Space       play / pause
    I / O       set start / end marker at current position
    A           add the marked range to the clip list
    Left/Right  seek -1s / +1s   (hold Shift for 5s)
    P           preview the selected range

The "Help" button (and the "?" button in the Options popup) open guide
windows explaining how to set and configure everything.
A waveform strip under the seek bar shows where the audio is loud, on an
absolute dB scale (green = quiet, yellow = medium, red = loud).

How it is put together:
    * PySide6 (Qt) provides the window, the video player and the buttons.
      It is only used for *previewing* and picking times.
    * ffmpeg (an external program) does the actual cutting/converting.
      We launch it in the background with QProcess so the UI doesn't freeze.
    * All times inside the program are stored in MILLISECONDS (integers),
      because that is what Qt's media player uses. We only convert to
      seconds when building the ffmpeg command.
"""
import array
import ctypes.util
import math
import os
import subprocess
import shlex
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QRect, QRectF, Qt, QProcess, QUrl, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPalette, QPen, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSlider, QStyle,
    QStyleOptionSlider, QTextBrowser, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

# Verbose/debug mode: pass --verbose or set CLIPPER_DEBUG=1 to print runtime
# diagnostics (PySide6 version, Qt platform plugin, ffmpeg/ffprobe versions)
# and to enable Qt's multimedia debug logging on stderr.
DEBUG = "--verbose" in sys.argv or bool(os.environ.get("CLIPPER_DEBUG"))


def linux_media_hint() -> str | None:
    """Software probe for QtMultimedia's decoding backend (Linux only).

    Qt 6.5+ decodes media with FFmpeg (system libav* libraries, or the copies
    bundled inside the PySide6 wheel); older Qt builds use GStreamer. A bare
    GTK desktop such as Mint/Cinnamon often has neither. Returns a short
    install hint when nothing can be found, or None when a backend looks
    present. This is advisory only: a wrong guess shows an extra dialog,
    it never blocks the app.
    """
    if any(ctypes.util.find_library(n) for n in ("avcodec", "avformat", "avutil")):
        return None  # system FFmpeg libraries found
    if ctypes.util.find_library("gstreamer-1.0"):
        return None  # GStreamer found
    try:  # the PySide6 wheel may bundle its own libs next to the package
        import PySide6
        base = Path(PySide6.__file__).parent
        if any(base.glob("**/libavcodec*")):
            return None
    except Exception:
        pass
    return (
        "No media decoding backend could be found. Preview playback will "
        "likely fail until one is installed.\n\n"
        "Debian / Ubuntu / Mint:\n"
        "  sudo apt install ffmpeg libavcodec-extra gstreamer1.0-plugins-base "
        "gstreamer1.0-plugins-good gstreamer1.0-plugins-bad "
        "gstreamer1.0-plugins-ugly gstreamer1.0-libav\n\n"
        "Fedora: sudo dnf install gstreamer1-plugins-{base,good,bad-free,ugly-free} ffmpeg\n\n"
        "Windows / macOS: reinstall PySide6 (pip install --force-reinstall PySide6)."
    )


def ffprobe_ok(path: Path) -> bool | None:
    """Quick pre-check that a file really contains a media stream.

    Runs ffprobe (when available) before handing the file to Qt, so unreadable
    or non-media files get a clear message instead of a player error deep in
    the backend. Returns True/False, or None when ffprobe is not installed
    (in which case the check is skipped, exactly like before).
    """
    if not shutil.which("ffprobe"):
        return None
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=format_name",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None  # probe problems are not the file's fault; skip the check
    return r.returncode == 0


# Extensions we treat as audio-only inputs: they play sound but have no
# frames, so the video pane is hidden when one of these is loaded.
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".oga", ".flac", ".m4a", ".aac",
              ".opus", ".wma"}

# Every format we can export to, and how to produce it:
#   "copy"  - stream copy (-c copy) is allowed for this output format
#   "audio" - format is audio-only (its args already include -vn)
#   "args"  - ffmpeg arguments used when re-encoding into this format
EXPORT_FORMATS = {
    "mp4":  {"copy": True,  "audio": False, "args": ["-c:v", "libx264", "-preset", "fast", "-crf", "20",
                                                      "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]},
    "webm": {"copy": True,  "audio": False, "args": ["-c:v", "libvpx-vp9", "-row-mt", "1", "-crf", "30",
                                                      "-b:v", "0", "-c:a", "libopus", "-b:a", "160k"]},
    "mkv":  {"copy": True,  "audio": False, "args": ["-c:v", "libx264", "-preset", "fast", "-crf", "20",
                                                      "-c:a", "aac", "-b:a", "192k"]},
    "mov":  {"copy": True,  "audio": False, "args": ["-c:v", "libx264", "-preset", "fast", "-crf", "20",
                                                      "-c:a", "aac", "-b:a", "192k"]},
    "mp3":  {"copy": False, "audio": True,  "args": ["-vn", "-c:a", "libmp3lame", "-q:a", "2"]},
    "wav":  {"copy": True,  "audio": True,  "args": ["-vn", "-c:a", "pcm_s16le"]},
    "ogg":  {"copy": True,  "audio": True,  "args": ["-vn", "-c:a", "libvorbis", "-q:a", "5"]},
    "flac": {"copy": True,  "audio": True,  "args": ["-vn", "-c:a", "flac"]},
    "m4a":  {"copy": True,  "audio": True,  "args": ["-vn", "-c:a", "aac", "-b:a", "192k"]},
    "opus": {"copy": True,  "audio": True,  "args": ["-vn", "-c:a", "libopus", "-b:a", "160k"]},
}

# File-picker filter: a broad list of video and audio inputs.
OPEN_FILTER = (
    "Video files (*.mp4 *.mov *.mkv *.webm *.avi *.m4v *.mpg *.mpeg *.ts "
    "*.wmv *.flv *.3gp);;"
    f"Audio files ({' '.join('*' + e for e in sorted(AUDIO_EXTS))});;"
    "All files (*)"
)

# Waveform analysis: the audio is decoded to mono PCM at WAVE_RATE Hz and
# bucketed into RMS levels every WAVE_BUCKET_MS milliseconds. Painting
# aggregates the buckets down to one bar per pixel column, so the strip is
# as high-resolution as the window is wide. Levels are shown on an absolute
# scale: dB relative to full scale, mapped onto 0..1 over a fixed -60..0 dB
# window (so green/yellow/red keep their meaning in every file, instead of
# rescaling each file to its own peak).
WAVE_RATE = 8000
WAVE_BUCKET_MS = 10
WAVE_FLOOR_DB = -60.0


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


def safe_name(text: str) -> str:
    """Turn a clip name into a safe filename part (no path separators or
    characters the filesystem may reject, capped in length)."""
    cleaned = "".join(c for c in text.strip()
                      if c not in '/\\:*?"<>|').strip().strip(".")
    return cleaned[:60]


class MarkerSlider(QSlider):
    """Seek bar that paints the queued clip ranges and the marked [start, end]
    range on top of it (the marked range gets the stronger highlight)."""

    def __init__(self, *args):
        super().__init__(*args)
        self.start_ms = 0
        self.end_ms = 0
        self.ranges: list[tuple[int, int]] = []  # queued clips (ms)

    def set_marks(self, start_ms: int, end_ms: int):
        """Store the marker positions (milliseconds) and repaint."""
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.update()

    def set_ranges(self, ranges: list[tuple[int, int]]):
        """Store the queued clip ranges (milliseconds) and repaint."""
        self.ranges = list(ranges)
        self.update()

    def _x_for(self, ms: int) -> int:
        """Pixel x where the handle center would sit if the value were `ms`."""
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        opt.sliderPosition = max(self.minimum(), min(self.maximum(), ms))
        opt.sliderValue = opt.sliderPosition
        handle = self.style().subControlRect(
            QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
        return handle.center().x()

    def paintEvent(self, event):
        super().paintEvent(event)  # draw the normal slider first
        dur = self.maximum()
        if dur <= 0:
            return
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(
            QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
        cy = groove.center().y()

        highlight = self.palette().color(QPalette.Highlight)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # Queued clips: soft translucent bands across the groove.
        for s, e in self.ranges:
            if e <= s:
                continue
            x1 = self._x_for(s)
            x2 = self._x_for(min(dur, e))
            band = QColor(highlight)
            band.setAlpha(45)
            p.fillRect(QRect(x1, cy - 3, max(1, x2 - x1), 6), band)
        # Marked range: stronger band across the groove (the playhead handle
        # stays visible through it) plus solid marker lines at start and end.
        if self.end_ms > self.start_ms:
            x1 = self._x_for(self.start_ms)
            x2 = self._x_for(min(dur, self.end_ms))
            band = QColor(highlight)
            band.setAlpha(90)
            p.fillRect(QRect(x1, cy - 3, max(1, x2 - x1), 6), band)
            p.setPen(QPen(highlight, 2))
            r = self.rect()
            p.drawLine(x1, r.top() + 1, x1, r.bottom() - 1)
            p.drawLine(x2, r.top() + 1, x2, r.bottom() - 1)
        p.end()


class WaveformView(QWidget):
    """Strip below the seek bar showing audio activity over time.

    `levels` holds one loudness level (0..1) per WAVE_BUCKET_MS bucket, on an
    absolute dBFS scale (0 = WAVE_FLOOR_DB, 1 = full scale); painting
    aggregates buckets into one bottom-anchored bar per pixel column, colored
    green (quiet), yellow or red (loud). `duration_ms` + `inset` map time to
    pixels exactly like the seek bar's groove, and a white stripe marks the
    current playback position.

    Queued clip ranges (`ranges`) are painted as soft background bands.

    Mouse: click/drag seeks (seekRequested); Shift+click/drag marks the clip
    range (rangeMarked with start/end in ms).
    """

    seekRequested = Signal(int)      # user clicked/dragged: seek to this time
    rangeMarked = Signal(int, int)   # user shift-dragged: set start/end (ms)

    def __init__(self):
        super().__init__()
        self.levels: list[float] = []
        self.placeholder = ""   # text drawn when there are no levels
        self.duration_ms = 0    # total media duration (0 = unknown)
        self.inset = 0.0        # px trimmed each side to match the slider groove
        self.playhead_ms = -1   # current position; < 0 hides the white stripe
        self.ranges: list[tuple[int, int]] = []  # queued clips (ms)
        self._drag_mode = None       # None | "seek" | "mark"
        self._mark_anchor = 0        # where a Shift+drag started (ms)
        self.setMinimumHeight(40)
        self.setMaximumHeight(60)

    def set_levels(self, levels: list[float], placeholder: str = ""):
        self.levels = levels
        self.placeholder = placeholder
        self.update()

    def set_timeline(self, duration_ms: int, inset: float):
        """Set the time->pixel mapping so the strip lines up with the slider."""
        self.duration_ms = duration_ms
        self.inset = inset
        self.update()

    def set_playhead(self, ms: int):
        """Move the white position stripe (ms); negative hides it."""
        self.playhead_ms = ms
        self.update()

    def set_ranges(self, ranges: list[tuple[int, int]]):
        """Store the queued clip ranges (ms) to paint as background bands."""
        self.ranges = list(ranges)
        self.update()

    def _x_for(self, ms: float) -> float:
        """Pixel x for a time in ms, using the same mapping as the slider."""
        span = max(1.0, self.width() - 2 * self.inset)
        return self.inset + (ms / self.duration_ms) * span

    def _ms_for(self, x: float) -> int:
        """Time in ms for a pixel x (inverse of _x_for), clamped to the file."""
        if self.duration_ms <= 0:
            return 0
        span = max(1.0, self.width() - 2 * self.inset)
        return max(0, min(self.duration_ms,
                          int((x - self.inset) * self.duration_ms / span)))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.duration_ms > 0:
            ms = self._ms_for(event.position().x())
            if event.modifiers() & Qt.ShiftModifier:
                self._drag_mode = "mark"      # Shift+drag: mark start..end
                self._mark_anchor = ms
                self.rangeMarked.emit(ms, ms)
            else:
                self._drag_mode = "seek"      # plain click/drag: seek
                self.seekRequested.emit(ms)
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_mode == "seek":
            self.seekRequested.emit(self._ms_for(event.position().x()))
        elif self._drag_mode == "mark":
            cur = self._ms_for(event.position().x())
            self.rangeMarked.emit(min(self._mark_anchor, cur),
                                  max(self._mark_anchor, cur))
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_mode is not None:
            self._drag_mode = None
        else:
            super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), self.palette().color(QPalette.Window))
        w, h = self.width(), self.height()
        # Queued clip ranges: soft background bands behind the bars.
        if self.ranges and self.duration_ms > 0:
            band = QColor(self.palette().color(QPalette.Highlight))
            band.setAlpha(40)
            for s, e in self.ranges:
                if e <= s:
                    continue
                x1 = self._x_for(s)
                x2 = self._x_for(min(self.duration_ms, e))
                p.fillRect(QRectF(x1, 0, max(1.0, x2 - x1), h), band)
        n = len(self.levels)
        if n and self.duration_ms > 0:
            # one bar per pixel column across the groove area; each column
            # shows the peak level of the buckets that fall into it
            x0 = int(self.inset)
            x1 = max(x0 + 1, int(w - self.inset))
            span = max(1.0, w - 2 * self.inset)
            for cx in range(x0, x1):
                i0 = max(0, int((cx - self.inset) * self.duration_ms / span / WAVE_BUCKET_MS))
                i1 = int((cx + 1 - self.inset) * self.duration_ms / span / WAVE_BUCKET_MS) + 1
                i0 = min(i0, n - 1)                    # clamp into range so the
                i1 = max(i0 + 1, min(n, i1))            # slice is never empty
                lv = max(self.levels[i0:i1])
                bh = max(1.0, lv * (h - 4))
                if lv < 0.4:
                    c = QColor(76, 175, 80)    # green: quiet (< -36 dBFS)
                elif lv < 0.75:
                    c = QColor(255, 193, 7)    # yellow: medium (< -15 dBFS)
                else:
                    c = QColor(244, 67, 54)    # red: loud (> -15 dBFS)
                p.fillRect(QRectF(cx, h - bh, 1.0, bh), c)
        elif n:
            # duration not known yet: plain full-width bars
            for cx in range(w):
                i0 = cx * n // w
                i1 = max(i0 + 1, (cx + 1) * n // w)
                lv = max(self.levels[i0:i1])
                bh = max(1.0, lv * (h - 4))
                p.fillRect(QRectF(cx, h - bh, 1.0, bh), QColor(120, 160, 220))
        elif self.placeholder:
            p.setPen(self.palette().color(QPalette.Mid))
            p.drawText(self.rect(), Qt.AlignCenter, self.placeholder)
        # white stripe for the current playback position
        if self.duration_ms > 0 and self.playhead_ms >= 0:
            x = self._x_for(min(self.playhead_ms, self.duration_ms))
            p.fillRect(QRectF(x - 1, 0, 2, h), QColor(255, 255, 255))
        p.end()


class ExportOptionsDialog(QDialog):
    """Popup with extra ffmpeg options applied to the exported clip.

    Each slider is gated by a checkbox, so an option is only sent to ffmpeg
    when the user enabled it. Dropdowns use a None data value for "default".
    The dialog is kept alive by the main window, so settings persist between
    openings.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export options")
        self.resize(500, 620)

        # ---------- video ----------
        vbox = QGroupBox("Video  (ignored for audio-only exports)")
        vform = QFormLayout(vbox)
        self.fps_chk, self.fps_slider, row = self._check_slider(
            "Set frame rate", 1, 120, 30, " fps")
        vform.addRow(self.fps_chk, row)
        self.res_box = QComboBox()
        self.res_box.addItem("Source (no change)", None)
        for r in ["1920x1080", "1280x720", "854x480", "640x360"]:
            self.res_box.addItem(r, r)
        vform.addRow("Resolution:", self.res_box)
        self.aspect_box = QComboBox()
        self.aspect_box.addItem("Source (no change)", None)
        for a in ["16:9", "4:3", "1:1", "9:16", "21:9"]:
            self.aspect_box.addItem(a, a)
        vform.addRow("Aspect ratio:", self.aspect_box)
        self.scale_chk, self.scale_slider, row = self._check_slider(
            "Scale (up/down)", 25, 400, 100, "%")
        vform.addRow(self.scale_chk, row)
        self.crf_chk, self.crf_slider, row = self._check_slider(
            "Override quality (CRF, lower = better)", 0, 51, 20)
        vform.addRow(self.crf_chk, row)
        self.vbr_box = QComboBox()
        self.vbr_box.addItem("Default (CRF)", None)
        for b in ["500k", "1M", "2M", "5M", "8M"]:
            self.vbr_box.addItem(b, b)
        vform.addRow("Video bitrate:", self.vbr_box)
        self.speed_chk, self.speed_slider, row = self._check_slider(
            "Change speed", 50, 200, 100, "%")
        vform.addRow(self.speed_chk, row)

        # ---------- audio ----------
        abox = QGroupBox("Audio")
        aform = QFormLayout(abox)
        self.abr_box = QComboBox()
        self.abr_box.addItem("Default (format's setting)", None)
        for b in ["96k", "128k", "192k", "256k", "320k"]:
            self.abr_box.addItem(b, b)
        aform.addRow("Audio bitrate:", self.abr_box)
        self.ar_box = QComboBox()
        self.ar_box.addItem("Source (no change)", None)
        for hz in [48000, 44100, 22050]:
            self.ar_box.addItem(f"{hz} Hz", hz)
        aform.addRow("Sample rate:", self.ar_box)
        self.ch_box = QComboBox()
        self.ch_box.addItem("Source (no change)", None)
        self.ch_box.addItem("Mono", 1)
        self.ch_box.addItem("Stereo", 2)
        aform.addRow("Channels:", self.ch_box)

        # ---------- raw extra args ----------
        xbox = QGroupBox("Advanced")
        xlayout = QVBoxLayout(xbox)
        xlayout.addWidget(QLabel("Extra ffmpeg arguments (appended as-is):"))
        self.extra_edit = QLineEdit()
        self.extra_edit.setPlaceholderText("e.g. -pix_fmt yuv420p -profile:v high")
        xlayout.addWidget(self.extra_edit)
        note = QLabel("Extra options are ignored when 'Fast copy' is checked, and "
                      "video options when the export is audio-only.")
        note.setWordWrap(True)
        xlayout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.Help)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.helpRequested.connect(self.show_help)

        layout = QVBoxLayout(self)
        layout.addWidget(vbox)
        layout.addWidget(abox)
        layout.addWidget(xbox)
        layout.addWidget(buttons)

    def show_help(self):
        """Pop out a window explaining every option in this dialog."""
        HelpDialog("Export options help", OPTIONS_HELP_HTML, self).exec()

    def _check_slider(self, label, lo, hi, default, suffix=""):
        """A checkbox that enables a slider with a value label.

        Returns (checkbox, slider, row widget); the option is only applied
        when the checkbox is checked.
        """
        chk = QCheckBox(label)
        slider = QSlider(Qt.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(default)
        val = QLabel(f"{default}{suffix}")
        slider.valueChanged.connect(lambda v, l=val, s=suffix: l.setText(f"{v}{s}"))
        chk.toggled.connect(slider.setEnabled)
        chk.toggled.connect(val.setEnabled)
        slider.setEnabled(False)
        val.setEnabled(False)
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(slider, 1)
        h.addWidget(val)
        return chk, slider, row

    def speed_factor(self) -> float | None:
        """Playback speed factor (1.0 = normal) if the speed option is enabled."""
        if self.speed_chk.isChecked():
            return self.speed_slider.value() / 100.0
        return None

    def video_filters(self) -> list[str]:
        """The video filter chain (scale / pad) from the current widgets.

        The speed option's setpts is deliberately not included here; callers
        add it themselves so Join mode can apply it after the concat instead.
        """
        vf: list[str] = []
        scale = self.res_box.currentData()
        if scale:
            # a fixed resolution wins over the scale slider
            w, h = scale.split("x")
            vf.append(f"scale={w}:{h}")
        elif self.scale_chk.isChecked():
            f = self.scale_slider.value() / 100.0
            # keep width/height even (required by many codecs)
            vf.append(f"scale=trunc(iw*{f:.3f}/2)*2:trunc(ih*{f:.3f}/2)*2")
        ar = self.aspect_box.currentData()
        if ar and not scale:
            # Letterbox into the chosen aspect ratio with pad alone: the
            # canvas grows to the ratio (black bars), the content is never
            # cropped or resized. max() keeps the canvas >= the input, since
            # pad cannot shrink. Ignored when a fixed resolution is set
            # (it already defines the canvas).
            a, b = ar.split(":")
            # commas inside max() must be escaped for the filtergraph parser
            vf.append(f"pad=max(iw\\,trunc(ih*{a}/{b}/2)*2):"
                      f"max(ih\\,trunc(iw*{b}/{a}/2)*2):(ow-iw)/2:(oh-ih)/2")
        return vf

    def _video_flags(self) -> list[str]:
        """Plain output flags for video: -r, -crf, -b:v."""
        args: list[str] = []
        if self.fps_chk.isChecked():
            args += ["-r", str(self.fps_slider.value())]
        if self.crf_chk.isChecked():
            args += ["-crf", str(self.crf_slider.value())]
        vbr = self.vbr_box.currentData()
        if vbr:
            args += ["-b:v", vbr]
        return args

    def _audio_flags(self, ext: str) -> list[str]:
        """Plain output flags for audio plus raw extra args (-b:a, -ar, -ac)."""
        args: list[str] = []
        # A bitrate makes no sense for lossless PCM/FLAC, so skip it there.
        abr = self.abr_box.currentData()
        if abr and ext not in ("wav", "flac"):
            args += ["-b:a", abr]
        ar = self.ar_box.currentData()
        if ar:
            args += ["-ar", str(ar)]
        ch = self.ch_box.currentData()
        if ch:
            args += ["-ac", str(ch)]
        raw = self.extra_edit.text().strip()
        if raw:
            args += shlex.split(raw)
        return args

    def ffmpeg_args(self, ext: str, video: bool) -> list[str]:
        """Build the extra ffmpeg arguments from the current widget states.

        `video` is False for audio-only exports, so video filters are skipped.
        """
        args: list[str] = self._video_flags() if video else []
        vf: list[str] = self.video_filters() if video else []
        if self.speed_chk.isChecked():
            k = self.speed_slider.value() / 100.0
            args += ["-af", f"atempo={k:.3f}"]   # audio tempo
            if video:
                vf.append(f"setpts=PTS/{k:.3f}")  # video timestamps
        if vf:
            args += ["-vf", ",".join(vf)]
        args += self._audio_flags(ext)
        return args

    def ffmpeg_output_args(self, ext: str, video: bool) -> list[str]:
        """Extra arguments without any filtergraph flags (no -vf / -af).

        Used by Join mode, where the filters are folded into the single
        filter_complex (concat + post-processing) instead.
        """
        args: list[str] = self._video_flags() if video else []
        args += self._audio_flags(ext)
        return args


class HelpDialog(QDialog):
    """Simple read-only help window that renders an HTML guide."""

    def __init__(self, title: str, html: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(580, 540)
        browser = QTextBrowser()
        browser.setHtml(html)
        browser.setOpenExternalLinks(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(browser)
        layout.addWidget(buttons)


MAIN_HELP_HTML = """
<h2>Using Clipper</h2>
<p><b>Workflow:</b> open a video or audio file, seek to where the clip should
begin and press <b>I</b> (or "Set start"), seek to where it should end and
press <b>O</b> (or "Set end"), pick a format, then press <b>Export…</b>.</p>

<h3>Seek bar</h3>
<p>The highlighted band and the two vertical lines on the seek bar show the
marked start/end range. Dragging the bar seeks; the playhead handle stays
visible inside the band. Clicking the bar jumps straight to that position.</p>
<p>The strip directly below the seek bar is a waveform of the audio, aligned
with the seek bar above it: each pixel column is one slice of time, colored
by absolute loudness — green (quiet, below about -36 dB), yellow (medium) or
red (loud, above about -15 dB, relative to full scale) — and a white stripe
marks the current playback position. <b>Click or drag on it to seek</b>; hold
<b>Shift</b> while
dragging to mark the start/end range directly on the waveform. Files without
an audio track show a "No audio track" placeholder instead.</p>

<h3>Transport &amp; markers</h3>
<ul>
<li><b>Space</b> / Play-Pause &mdash; play or pause.</li>
<li><b>Left / Right</b> &mdash; seek 1 s back/forward (<b>Shift</b> = 5 s).</li>
<li><b>I / O</b> &mdash; set the start/end marker at the playhead.</li>
<li><b>P</b> / Preview &mdash; plays only the marked range and stops at the
end marker. Tick <b>Loop</b> next to it to repeat the range until you pause.</li>
<li><b>A</b> / Add clip &mdash; add the marked range to the clip list.</li>
</ul>

<h3>Clip list (export several clips at once)</h3>
<p>Press <b>A</b> (or "Add clip") to commit the marked range to the clip list
shown on the right, and repeat for every range you want — the new clip's name
editor opens immediately so you can name it. Each clip gets a default name
(<code>clip01</code>, <code>clip02</code>, …); press <b>F2</b> or the
<b>Rename</b> button to change a name (the name is sanitized and used in the
exported filename). All queued clips are painted on the seek bar and the
waveform (softer bands than the current marks). Double-click a list entry to
load it back into the markers; use <b>Remove</b> / <b>Clear</b> to edit the
list. When the list is non-empty, <b>Export…</b> writes one file per clip
(<code>name_clip01.ext</code>, <code>name_myname.ext</code>, …) and runs them
one after another — or, with <b>Join</b> ticked, merges all clips into a
single file (always re-encoded, so the cuts stay exact). With an empty list,
Export… uses the marked range as before.</p>

<h3>Export row</h3>
<ul>
<li><b>Format</b> &mdash; mp4, webm, mkv, mov (video containers) or mp3, wav,
ogg, flac, m4a, opus (audio). The extension is appended automatically.</li>
<li><b>Audio only</b> &mdash; drops the video stream (<code>-vn</code>) even
for video containers like mp4. Disabled for audio formats, which are already
audio-only.</li>
<li><b>Fast copy</b> &mdash; copies the streams without re-encoding: very
fast, but cuts on keyframes so the start may be slightly off, and the source
codecs must be compatible with the chosen container.</li>
<li><b>Join</b> &mdash; with 2 or more clips in the list, merge them into one
output file (concat) instead of one file per clip. Always re-encodes, so
Fast copy is ignored in this mode.</li>
<li><b>Options…</b> &mdash; popup with extra ffmpeg settings (frame rate,
resolution, aspect ratio, scaling, quality, bitrates, speed, raw arguments).
It has its own "?" help button explaining each option.</li>
<li><b>Export…</b> &mdash; asks for a filename and runs ffmpeg in the
background; with clips in the list, one job per clip runs sequentially. The
status line shows progress and the saved path.</li>
</ul>

<h3>Notes</h3>
<ul>
<li>Preview playback depends on your system's media codecs; exporting always
goes through ffmpeg, so every listed format can be exported.</li>
<li>If ffmpeg fails, a popup shows the end of its log with the reason.</li>
</ul>
"""

OPTIONS_HELP_HTML = """
<h2>Export options</h2>
<p>Every slider is gated by a checkbox: an option is only sent to ffmpeg when
its checkbox is enabled. The options are appended after the chosen format's
own settings, so they override them. They are <b>ignored when "Fast copy" is
checked</b> (stream copy cannot be combined with filters/codec options), and
the video options are ignored for <b>audio-only</b> exports.</p>

<h3>Video</h3>
<ul>
<li><b>Set frame rate</b> (slider, 1&ndash;120 fps) &mdash; forces the output
frame rate (<code>-r</code>). Lower fps = smaller file; a very high fps on a
low-fps source just duplicates frames.</li>
<li><b>Resolution</b> &mdash; scales the video to a fixed size
(<code>-vf scale=WxH</code>). A fixed resolution wins over the scale slider
and the aspect-ratio option (it already defines the output canvas).</li>
<li><b>Aspect ratio</b> &mdash; letterboxes the frame into the chosen ratio
(e.g. 16:9, 9:16) using <code>pad</code>: black bars are added so the output
has that ratio; the content itself is never cropped or resized. Ignored when
a fixed resolution is chosen.</li>
<li><b>Scale (up/down)</b> (slider, 25&ndash;400%) &mdash; rescales the video by
a factor (<code>scale=iw*f:ih*f</code>), keeping the aspect ratio and rounding
to even dimensions (required by H.264 and friends). 50% halves the size,
200% doubles it.</li>
<li><b>Override quality (CRF)</b> (slider, 0&ndash;51, lower = better) &mdash;
<code>-crf</code>. Around 18&ndash;23 looks near-lossless for H.264; webm
(VP9) defaults to 30.</li>
<li><b>Video bitrate</b> &mdash; <code>-b:v</code>: fixed bitrate instead of
CRF, useful when a streaming target expects a specific bitrate.</li>
<li><b>Change speed</b> (slider, 50&ndash;200%) &mdash; slows down or speeds
up the clip (<code>setpts</code> for video, <code>atempo</code> for audio).
The clip's length changes accordingly: a 3 s range at 50% becomes ~6 s.</li>
</ul>

<h3>Audio</h3>
<ul>
<li><b>Audio bitrate</b> &mdash; <code>-b:a</code>, 96k&ndash;320k. Skipped for
wav/flac, which are lossless.</li>
<li><b>Sample rate</b> &mdash; <code>-ar</code>: resamples the audio, e.g.
48000 Hz for video, 44100 Hz for music.</li>
<li><b>Channels</b> &mdash; <code>-ac</code>: downmix to mono or upmix to
stereo.</li>
</ul>

<h3>Advanced</h3>
<p><b>Extra ffmpeg arguments</b> &mdash; raw flags appended as-is (split on
whitespace, quotes allowed), e.g.
<code>-pix_fmt yuv420p -profile:v high -metadata title="My clip"</code>.
When several options conflict, the last one on the command line wins. Invalid
arguments make ffmpeg fail &mdash; the error log is shown in a popup.</p>
"""


class Clipper(QWidget):
    """The main (and only) window of the app."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Clipper")
        self.resize(1150, 650)

        # ---------- state ----------
        self.path: Path | None = None   # currently loaded media file
        self.start_ms = 0               # clip start marker
        self.end_ms = 0                 # clip end marker (0 = not set yet)
        self.clips: list[tuple[str, int, int]] = []  # queued clips: (name, start, end) in ms
        self._clip_seq = 0                  # counter for default clip names
        self._clip_refresh = False          # True while the list widget is rebuilt
        self.previewing = False         # True while "Preview" is playing the range
        self.proc: QProcess | None = None  # the running ffmpeg export, if any
        self.queue: list[tuple[str, list[str]]] = []  # pending (out, args) jobs
        self.job_total = 0              # number of jobs in the current export run
        self._last_out = ""             # last saved file (for the status line)
        self.wave_proc: QProcess | None = None  # ffmpeg analysis for the waveform
        self.wave_buf = bytearray()     # leftover bytes of the pcm stream
        self.wave_acc = 0.0             # sum of squares in the current bucket
        self.wave_cnt = 0               # samples in the current bucket

        # ---------- player ----------
        # Qt splits playback into three objects: the player decodes the file,
        # the audio output plays sound, and the video widget draws frames.
        self.video = QVideoWidget()
        self.audio = QAudioOutput()
        self.player = QMediaPlayer()
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)

        # ---------- widgets ----------
        # Seek bar (paints the marked range) with the time readout beside it.
        self.slider = MarkerSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)             # real range is set once duration is known
        self.time_lbl = QLabel("00:00:00.000 / 00:00:00.000")
        # Audio-activity strip shown below the seek bar once a file is analyzed.
        self.wave = WaveformView()
        self.wave.setToolTip("Audio loudness on an absolute scale: green = quiet "
                            "(< -36 dB), yellow = medium, red = loud (> -15 dB). "
                            "Click/drag to seek; Shift+drag to mark the clip range.")
        self.wave.setVisible(False)

        # Buttons; shortcut hints live in tooltips to keep the labels short.
        self.open_btn = QPushButton("Open…")
        self.open_btn.setToolTip("Open a video or audio file")
        self.play_btn = QPushButton("Play / Pause")
        self.play_btn.setToolTip("Play / pause (Space)")
        self.set_start_btn = QPushButton("Set start")
        self.set_start_btn.setToolTip("Set clip start at the playhead (I)")
        self.set_end_btn = QPushButton("Set end")
        self.set_end_btn.setToolTip("Set clip end at the playhead (O)")
        self.preview_btn = QPushButton("Preview")
        self.preview_btn.setToolTip("Play only the marked range (P); tick Loop to repeat it")
        self.loop_check = QCheckBox("Loop")
        self.loop_check.setToolTip("Repeat the marked range while previewing")
        self.range_lbl = QLabel("Start: --    End: --")
        self.help_btn = QPushButton("Help")
        self.help_btn.setToolTip("Show the user guide (shortcuts, markers, export options)")

        # Export controls: output format, audio-only option, fast copy, button
        self.format_box = QComboBox()
        self.format_box.addItems(list(EXPORT_FORMATS))
        self.audio_only_check = QCheckBox("Audio only")
        self.audio_only_check.setToolTip(
            "Drop the video stream and export sound only (ffmpeg -vn), even for "
            "video containers like mp4 or webm.")
        self.fast_check = QCheckBox("Fast copy")
        self.fast_check.setToolTip(
            "Copy the streams as-is instead of re-encoding: very fast, but cuts "
            "on keyframes so the start may be slightly off. Only works when the "
            "source codecs are compatible with the chosen output container.")
        self.join_check = QCheckBox("Join")
        self.join_check.setToolTip(
            "With 2+ clips in the clip list, merge them into a single output file "
            "instead of one file per clip (always re-encoded, so cuts stay exact)")
        self.join_check.setEnabled(False)  # enabled once 2+ clips are queued
        self.opts_btn = QPushButton("Options…")
        self.opts_btn.setToolTip(
            "Extra ffmpeg options for the export: frame rate, resolution, "
            "aspect ratio, scaling, quality, bitrates, speed, raw arguments…")
        self.export_btn = QPushButton("Export…")
        self.export_btn.setToolTip(
            "Export the marked range (or every clip in the list) with ffmpeg")
        self.status = QLabel("Open a video or audio file to begin.")
        # Kept as a member so the popup remembers its settings between openings.
        self.opts_dlg = ExportOptionsDialog(self)

        # Clip-list panel: ranges committed with "Add clip" for multi-clip export.
        # Column 0 is the editable name (used in the exported filename),
        # column 1 shows the range.
        self.clip_total = QLabel("0 clips, 0.00s total")
        self.clip_list = QTreeWidget()
        self.clip_list.setHeaderLabels(["Name", "Range"])
        self.clip_list.setRootIsDecorated(False)
        self.clip_list.setColumnWidth(0, 120)
        self.clip_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.clip_list.setEditTriggers(
            QAbstractItemView.EditTrigger.EditKeyPressed)  # F2 renames
        self.clip_list.setToolTip(
            "Double-click a clip to load it back into the markers; "
            "F2 (or Rename) to change its name — the name is used in the "
            "exported filename")
        self.add_clip_btn = QPushButton("Add clip")
        self.add_clip_btn.setToolTip("Add the marked range to the clip list (A); "
                                     "its name editor opens so you can name it now")
        self.rename_clip_btn = QPushButton("Rename")
        self.rename_clip_btn.setToolTip("Rename the selected clip (F2); the name "
                                        "becomes part of the exported filename")
        self.rm_clip_btn = QPushButton("Remove")
        self.rm_clip_btn.setToolTip("Remove the selected clip(s) from the list")
        self.clear_clip_btn = QPushButton("Clear")
        self.clear_clip_btn.setToolTip("Remove all clips from the list")

        # ---------- layout ----------
        # Seek bar and waveform share a 2-column grid, so the waveform lines
        # up exactly with the seek bar: column 0 stretches (slider / wave),
        # column 1 holds the time readout.
        seek = QGridLayout()
        seek.setColumnStretch(0, 1)
        seek.addWidget(self.slider, 0, 0)
        seek.addWidget(self.time_lbl, 0, 1)
        seek.addWidget(self.wave, 1, 0)

        # Row 2: transport buttons, a separator, then the clip-range buttons;
        # the start/end readout fills the remaining space on the right.
        controls = QHBoxLayout()
        controls.addWidget(self.open_btn)
        controls.addWidget(self.play_btn)
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        controls.addWidget(sep)
        controls.addWidget(self.set_start_btn)
        controls.addWidget(self.set_end_btn)
        controls.addWidget(self.preview_btn)
        controls.addWidget(self.loop_check)
        controls.addWidget(self.range_lbl, 1)
        controls.addWidget(self.help_btn)

        # Row 3: export options on the left, the export button on the right.
        export = QHBoxLayout()
        export.addWidget(QLabel("Format:"))
        export.addWidget(self.format_box)
        export.addWidget(self.audio_only_check)
        export.addWidget(self.fast_check)
        export.addWidget(self.join_check)
        export.addWidget(self.opts_btn)
        export.addStretch(1)
        export.addWidget(self.export_btn)

        layout = QVBoxLayout()
        layout.addWidget(self.video, 1)   # stretch=1: video takes all spare space
        layout.addLayout(seek)
        layout.addLayout(controls)
        layout.addLayout(export)
        layout.addWidget(self.status)

        # Right-hand panel holding the clip list for multi-clip export.
        clips_box = QGroupBox("Clips")
        clips_box.setMaximumWidth(300)
        clips_layout = QVBoxLayout(clips_box)
        clips_layout.addWidget(self.clip_total)
        clips_layout.addWidget(self.clip_list, 1)
        clip_btns = QHBoxLayout()
        clip_btns.addWidget(self.add_clip_btn, 1)
        clip_btns2 = QHBoxLayout()
        clip_btns2.addWidget(self.rename_clip_btn)
        clip_btns2.addWidget(self.rm_clip_btn)
        clip_btns2.addWidget(self.clear_clip_btn)
        clips_layout.addLayout(clip_btns)
        clips_layout.addLayout(clip_btns2)

        main = QHBoxLayout(self)
        main.addLayout(layout, 1)
        main.addWidget(clips_box)

        # ---------- signal/slot wiring ----------
        # Qt "signals" fire on events (a click, a position change...);
        # we connect each one to the method that should handle it.
        self.open_btn.clicked.connect(self.open_dialog)
        self.play_btn.clicked.connect(self.toggle_play)
        self.set_start_btn.clicked.connect(self.set_start)
        self.set_end_btn.clicked.connect(self.set_end)
        self.preview_btn.clicked.connect(self.preview)
        self.opts_btn.clicked.connect(self.open_options)
        self.export_btn.clicked.connect(self.export)
        self.help_btn.clicked.connect(self.open_help)
        self.add_clip_btn.clicked.connect(self.add_clip)
        self.rename_clip_btn.clicked.connect(self.rename_clip)
        self.rm_clip_btn.clicked.connect(self.remove_clips)
        self.clear_clip_btn.clicked.connect(self.clear_clips)
        self.clip_list.itemDoubleClicked.connect(self.load_clip)
        self.clip_list.itemChanged.connect(self._clip_item_changed)

        # Fast copy is only offered for formats that allow stream copy; the
        # "Audio only" box is redundant for formats that already drop video.
        self.format_box.currentTextChanged.connect(self.update_export_options)
        self.update_export_options(self.format_box.currentText())
        self.refresh_clips()  # initial clip-list label / Join checkbox state

        # Dragging the slider seeks the player. Clicking on the bar also
        # jumps there (sliderPressed), not just dragging.
        self.slider.sliderMoved.connect(self.player.setPosition)
        self.slider.sliderPressed.connect(
            lambda: self.player.setPosition(self.slider.value()))

        # The player tells us when the playhead moves / the file length is known.
        self.player.positionChanged.connect(self.on_position)
        self.player.durationChanged.connect(self.on_duration)
        # Decoding problems (bad codec, missing backend) arrive here instead
        # of crashing the process; _player_error shows what went wrong.
        self.player.errorOccurred.connect(self._player_error)

        # Waveform interactions: click/drag seeks, Shift+drag marks the range.
        self.wave.seekRequested.connect(self.wave_seek)
        self.wave.rangeMarked.connect(self.wave_mark_range)

        # Keyboard shortcuts (key, handler). Lambdas let us pass an argument.
        for key, fn in [
            ("Space", self.toggle_play),
            ("I", self.set_start),
            ("O", self.set_end),
            ("A", self.add_clip),
            ("P", self.preview),
            ("Left", lambda: self.nudge(-1000)),        # back 1 second
            ("Right", lambda: self.nudge(1000)),        # forward 1 second
            ("Shift+Left", lambda: self.nudge(-5000)),  # back 5 seconds
            ("Shift+Right", lambda: self.nudge(5000)),  # forward 5 seconds
        ]:
            QShortcut(QKeySequence(key), self, activated=fn)

        # Startup health checks: warn early about a missing ffmpeg or a broken
        # media backend instead of failing later deep inside a codec library.
        self._startup_checks()

        # Optional: a file path passed on the command line is opened immediately.
        if len(sys.argv) > 1:
            self.load(Path(sys.argv[1]))

    def _startup_checks(self):
        """Non-blocking startup diagnostics shown in the status line / dialogs."""
        warnings: list[str] = []
        if not shutil.which("ffmpeg"):
            warnings.append("ffmpeg not found — waveform and export disabled. "
                            "Install ffmpeg and make sure it's on your PATH.")
            self.status.setText("WARNING: ffmpeg not found — exports disabled.")
        if sys.platform.startswith("linux"):
            hint = linux_media_hint()
            if hint is not None:
                QMessageBox.warning(
                    self, "Clipper — media backend", hint)
                warnings.append("media backend not found")
        if DEBUG:
            try:
                import importlib.metadata as md
                ver = md.version("PySide6")
            except Exception:
                ver = "?"
            print(f"[clipper] Python {sys.version.split()[0]}, "
                  f"PySide6 {ver}, platform plugin "
                  f"{QApplication.platformName()!r}", file=sys.stderr)
            for tool in ("ffmpeg", "ffprobe"):
                found = shutil.which(tool)
                print(f"[clipper] {tool}: {found or 'NOT FOUND'}", file=sys.stderr)
            if warnings:
                print(f"[clipper] startup warnings: {warnings}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Playback error handling
    # ------------------------------------------------------------------
    def _player_error(self, err, msg):
        """QMediaPlayer failed (bad codec, unreadable file, broken backend).

        Qt reports the error here instead of crashing, so we surface it as a
        dialog with a troubleshooting hint and reset the UI to a usable state.
        """
        print(f"[clipper] QMediaPlayer error {err!r}: {msg}", file=sys.stderr)
        if err == QMediaPlayer.Error.NoError:
            return  # a stale signal fired with no error attached
        hints = {
            QMediaPlayer.Error.FormatError:
                "The file's container or codecs may not be supported by the "
                "installed media backend. On Linux install ffmpeg and the "
                "GStreamer plugins (see the startup warning); on Windows/macOS "
                "reinstalling PySide6 usually fixes this.",
            QMediaPlayer.Error.ResourceError:
                "The file could not be read. Check that it still exists and "
                "that you have permission to open it.",
            QMediaPlayer.Error.MediaOutOfRange:
                "The requested play position lies outside the file.",
        }
        text = f"Playback failed:\n{msg}"
        if err in hints:
            text += "\n\n" + hints[err]
        QMessageBox.critical(self, "Playback error", text)
        # Reset to a sane, usable state (no half-loaded UI left behind).
        self.previewing = False
        self.status.setText(f"Playback error: {msg}")
        self.slider.setRange(0, 0)
        self.slider.set_marks(0, 0)
        self.wave.set_timeline(0, self._slider_inset())
        self.wave.set_playhead(-1)
        self.wave.set_levels([], "")
        self.time_lbl.setText("00:00:00.000 / 00:00:00.000")

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
        # Sanity checks first: nothing good can come from feeding Qt an
        # unreadable or non-media file.
        try:
            if not path.exists() or path.stat().st_size == 0:
                QMessageBox.warning(self, "Clipper",
                                    f"The file does not exist or is empty:\n{path}")
                return
        except OSError as e:
            QMessageBox.warning(self, "Clipper",
                                f"The file cannot be read:\n{path}\n\n{e}")
            return
        probe = ffprobe_ok(path)
        if probe is False:  # None = ffprobe absent, skip the check silently
            QMessageBox.warning(self, "Clipper",
                                "ffprobe could not find a readable audio or video "
                                f"stream in this file:\n{path}\n\n"
                                "The file may be corrupt or not a media file.")
            return
        self.path = path
        # Qt wants a URL, not a plain path; resolve() makes it absolute first.
        self.player.setSource(QUrl.fromLocalFile(str(path.resolve())))
        self.start_ms = 0
        self.end_ms = 0  # will be filled in by on_duration once Qt knows the length
        self.clips = []  # a new file starts with a fresh clip list
        self._clip_seq = 0
        self.refresh_clips()
        self.slider.set_marks(0, 0)  # clear any old marks on the seek bar
        self.wave.set_timeline(0, self._slider_inset())  # until duration is known
        self.wave.set_playhead(-1)
        self.player.pause()  # show the first frame without auto-playing
        self.start_waveform(path)
        # Audio-only files have no frames to show, so hide the video pane.
        is_audio = path.suffix.lower() in AUDIO_EXTS
        self.video.setVisible(not is_audio)
        self.setWindowTitle(f"Clipper - {path.name}")
        self.status.setText(
            f"Loaded {path.name}" + (" (audio only)" if is_audio else ""))

    # ------------------------------------------------------------------
    # Waveform (audio-activity strip under the seek bar)
    # ------------------------------------------------------------------
    def start_waveform(self, path: Path):
        """Analyze the file's audio with ffmpeg and fill the waveform strip."""
        if not shutil.which("ffmpeg"):
            self.wave.set_levels([], "ffmpeg not found — no waveform")
            self.wave.setVisible(True)
            return
        if self.wave_proc is not None:  # an older analysis is still running
            self.wave_proc.finished.disconnect()
            self.wave_proc.kill()
            self.wave_proc = None
        self.wave.set_levels([], "Analyzing audio…")
        self.wave.setVisible(True)
        self.wave_buf = bytearray()
        self.wave_acc = 0.0
        self.wave_cnt = 0
        self.wave_proc = QProcess(self)
        self.wave_proc.readyReadStandardOutput.connect(self._wave_read)
        self.wave_proc.finished.connect(self._wave_done)
        # Decode to mono float32 PCM at WAVE_RATE Hz on stdout; we bucket the
        # samples into RMS levels ourselves as they stream in.
        self.wave_proc.start("ffmpeg", ["-v", "error", "-i", str(path), "-vn",
                                        "-ac", "1", "-ar", str(WAVE_RATE),
                                        "-f", "f32le", "-"])

    def _wave_read(self):
        """Consume a chunk of PCM samples into per-bucket RMS levels."""
        self.wave_buf += bytes(self.wave_proc.readAllStandardOutput())
        n = len(self.wave_buf) // 4          # float32 = 4 bytes per sample
        if not n:
            return
        samples = array.array("f")
        samples.frombytes(bytes(self.wave_buf[:n * 4]))
        del self.wave_buf[:n * 4]            # keep a partial sample for later
        bucket = WAVE_RATE * WAVE_BUCKET_MS // 1000   # samples per bucket
        levels = self.wave.levels
        for x in samples:
            self.wave_acc += x * x
            self.wave_cnt += 1
            if self.wave_cnt == bucket:
                levels.append(math.sqrt(self.wave_acc / self.wave_cnt))
                self.wave_acc = 0.0
                self.wave_cnt = 0
        self.wave.update()

    def _wave_done(self, code, _status):
        """Drain the pipe, map RMS levels onto 0..1 on a fixed dB scale, show."""
        if self.wave_proc is not None:
            self._wave_read()                # drain whatever is left in the pipe
        if self.wave_cnt:                    # partial last bucket
            self.wave.levels.append(math.sqrt(self.wave_acc / self.wave_cnt))
        levels = self.wave.levels
        if levels and max(levels) > 0:
            # Map each bucket's RMS level to dB relative to full scale and
            # then onto 0..1 over a fixed WAVE_FLOOR_DB..0 dB window. An
            # absolute scale (not peak normalization) keeps the colors
            # meaningful: quiet parts of a loud file still show green.
            floor = WAVE_FLOOR_DB
            self.wave.set_levels([
                min(1.0, max(0.0, (20 * math.log10(max(lv, 1e-9)) - floor)
                             / -floor))
                for lv in levels
            ])
        else:
            # no audio stream (ffmpeg exits non-zero when there is nothing to
            # decode) -> keep the strip visible with a placeholder
            self.wave.set_levels([], "No audio track")
        self.wave.setVisible(True)
        self.wave_proc = None

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

    def _slider_inset(self) -> float:
        """Half the slider handle width: the groove is inset by this on each
        side, so the waveform uses the same inset to line up with it."""
        opt = QStyleOptionSlider()
        self.slider.initStyleOption(opt)
        handle = self.slider.style().subControlRect(
            QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self.slider)
        return handle.width() / 2

    def on_duration(self, dur):
        """Called when the video length becomes known (after loading)."""
        self.slider.setRange(0, dur)   # slider units are milliseconds
        self.wave.set_timeline(dur, self._slider_inset())
        if self.end_ms == 0:
            self.end_ms = dur          # default selection = whole video
        self.update_range_label()

    def on_position(self, pos):
        """Called continuously during playback as the playhead moves."""
        # Don't fight the user: only move the slider if they aren't dragging it.
        if not self.slider.isSliderDown():
            self.slider.setValue(pos)
        self.wave.set_playhead(pos)
        self.time_lbl.setText(f"{fmt(pos)} / {fmt(self.player.duration())}")

        # If we're previewing the selection, stop at the end marker — or, when
        # Loop is ticked, jump back to the start and keep playing.
        if self.previewing and pos >= self.end_ms:
            if self.loop_check.isChecked():
                self.player.setPosition(self.start_ms)
            else:
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
        """Refresh the 'Start / End / length' text and the marks on the seek bar."""
        self.range_lbl.setText(
            f"Start: {fmt(self.start_ms)}    End: {fmt(self.end_ms)}    "
            f"({(self.end_ms - self.start_ms) / 1000:.2f}s)")
        self.slider.set_marks(self.start_ms, self.end_ms)

    def preview(self):
        """Play only the selected range (jump to start, stop at end or loop)."""
        self.player.setPosition(self.start_ms)
        self.previewing = True  # on_position() watches this flag at end_ms
        self.player.play()

    def wave_seek(self, ms: int):
        """Seek to a position clicked/dragged on the waveform."""
        self.previewing = False  # manual control cancels any running preview
        self.player.setPosition(ms)

    def wave_mark_range(self, a: int, b: int):
        """Set the clip range from a Shift+drag on the waveform (ms)."""
        self.start_ms = a
        self.end_ms = b if b > a else a + 1  # keep the range non-empty
        self.update_range_label()

    # ------------------------------------------------------------------
    # Clip list (collect several ranges for multi-clip export)
    # ------------------------------------------------------------------
    def add_clip(self):
        """Add the currently marked range to the clip list under a default
        name, and open that name's editor so it can be renamed right away."""
        if not self.path:
            self.status.setText("Open a media file first.")
            return
        if self.end_ms <= self.start_ms:
            self.status.setText("Set a valid range first (end after start).")
            return
        self._clip_seq += 1
        name = f"clip{self._clip_seq:02d}"
        self.clips.append((name, self.start_ms, self.end_ms))
        self.refresh_clips()
        item = self.clip_list.topLevelItem(len(self.clips) - 1)
        self.clip_list.setCurrentItem(item)
        self.clip_list.editItem(item, 0)  # name editor opens immediately
        self.status.setText(
            f"Added clip '{name}': {fmt(self.start_ms)} - {fmt(self.end_ms)}")

    def rename_clip(self):
        """Open the name editor for the selected clip (F2 does the same)."""
        item = self.clip_list.currentItem()
        if item is not None:
            self.clip_list.editItem(item, 0)

    def _clip_item_changed(self, item, col):
        """A list cell was edited: store a sanitized name (column 0) or restore
        the read-only range text (column 1)."""
        if self._clip_refresh:
            return
        row = self.clip_list.indexOfTopLevelItem(item)
        if not (0 <= row < len(self.clips)):
            return
        _, s, e = self.clips[row]
        if col != 0:
            # only the name column is editable; put the range text back
            self._clip_refresh = True
            try:
                item.setText(1, self._range_text(s, e))
            finally:
                self._clip_refresh = False
            return
        name = safe_name(item.text(0)) or f"clip{row + 1:02d}"
        if name != item.text(0):
            self._clip_refresh = True
            try:
                item.setText(0, name)  # show the sanitized name
            finally:
                self._clip_refresh = False
        self.clips[row] = (name, s, e)

    def remove_clips(self):
        """Remove the selected clip(s) from the list."""
        rows = sorted((self.clip_list.indexOfTopLevelItem(i)
                       for i in self.clip_list.selectedItems()), reverse=True)
        for row in rows:
            del self.clips[row]
        if rows:
            self.refresh_clips()
            self.status.setText(f"{len(self.clips)} clip(s) left.")

    def clear_clips(self):
        """Remove all clips from the list."""
        if self.clips:
            self.clips = []
            self.refresh_clips()
            self.status.setText("Clip list cleared.")

    def load_clip(self, item, _col=0):
        """Double-clicked a clip: load its range back into the markers and seek."""
        s, e = item.data(0, Qt.UserRole)
        self.start_ms, self.end_ms = s, e
        self.update_range_label()
        self.wave_seek(s)

    @staticmethod
    def _range_text(s: int, e: int) -> str:
        return f"{fmt(s)} -> {fmt(e)}  ({(e - s) / 1000:.2f}s)"

    def refresh_clips(self):
        """Rebuild the clip-list widget and repaint the ranges on slider/wave."""
        self._clip_refresh = True
        try:
            self.clip_list.clear()
            total = 0
            for name, s, e in self.clips:
                item = QTreeWidgetItem([name, self._range_text(s, e)])
                # QTreeWidgetItem is not editable by default (unlike list
                # items), which makes editItem()/F2 fail with "editing failed"
                item.setFlags(item.flags() | Qt.ItemIsEditable)
                item.setData(0, Qt.UserRole, (s, e))
                self.clip_list.addTopLevelItem(item)
                total += e - s
            self.clip_total.setText(
                f"{len(self.clips)} clip(s), {total / 1000:.2f}s total")
            ranges = [(s, e) for _n, s, e in self.clips]
            self.slider.set_ranges(ranges)
            self.wave.set_ranges(ranges)
            self.join_check.setEnabled(len(self.clips) >= 2)
        finally:
            self._clip_refresh = False

    # ------------------------------------------------------------------
    # Export (runs ffmpeg)
    # ------------------------------------------------------------------
    def update_export_options(self, text):
        """Enable/disable the export checkboxes for the chosen format."""
        spec = EXPORT_FORMATS.get(text, {})
        self.fast_check.setEnabled(spec.get("copy", False))
        self.audio_only_check.setEnabled(not spec.get("audio", False))

    def open_options(self):
        """Open the advanced ffmpeg-options popup (modal; settings persist)."""
        self.opts_dlg.exec()

    def open_help(self):
        """Pop out the main user-guide window."""
        HelpDialog("Clipper help", MAIN_HELP_HTML, self).exec()

    def export(self):
        """Build ffmpeg job(s) and run them one after another.

        With a non-empty clip list every clip is exported (one file per clip,
        or a single joined file when "Join" is ticked); otherwise the
        currently marked range is exported as a single job.
        """
        # --- sanity checks before doing anything ---
        if not self.path:
            QMessageBox.information(self, "Clipper", "Open a media file first.")
            return
        if not shutil.which("ffmpeg"):  # looks for ffmpeg on the PATH
            QMessageBox.critical(self, "Clipper",
                                 "ffmpeg not found. Install it and make sure it's on your PATH.")
            return

        ext = self.format_box.currentText()          # e.g. "mp4", "webm", "wav"
        spec = EXPORT_FORMATS[ext]
        join = self.join_check.isChecked() and len(self.clips) >= 2
        jobs: list[tuple[str, list[str]]] = []       # (output path, ffmpeg args)

        if self.clips and join:
            # One output file holding all clips back to back.
            default = f"{self.path.stem}_joined.{ext}"
            out, _ = QFileDialog.getSaveFileName(
                self, "Export joined clips", str(self.path.with_name(default)),
                f"{ext.upper()} (*.{ext});;All files (*)")
            if not out:
                return  # user cancelled
            if not out.lower().endswith(f".{ext}"):
                out += f".{ext}"
            jobs.append((out, self._build_join_args(ext, spec, out)))
        elif self.clips:
            # One file per clip: name_clip01.ext, name_clip02.ext, ...
            default = f"{self.path.stem}.{ext}"
            base, _ = QFileDialog.getSaveFileName(
                self, "Export clips (base name)", str(self.path.with_name(default)),
                f"{ext.upper()} (*.{ext});;All files (*)")
            if not base:
                return  # user cancelled
            if not base.lower().endswith(f".{ext}"):
                base += f".{ext}"
            p = Path(base)
            width = max(2, len(str(len(self.clips))))
            for i, (name, s, e) in enumerate(self.clips, 1):
                # the clip's name becomes part of the filename; fall back to
                # the positional name if the name is empty/unusable
                safe = safe_name(name) or f"clip{i:0{width}d}"
                out = str(p.with_name(f"{p.stem}_{safe}{p.suffix}"))
                jobs.append((out, self._build_args(ext, spec, s, e, out)))
        else:
            if self.end_ms <= self.start_ms:
                QMessageBox.warning(self, "Clipper", "End must be after start.")
                return
            default = f"{self.path.stem}_clip.{ext}"     # e.g. holiday_clip.mp4
            out, _ = QFileDialog.getSaveFileName(
                self, "Export clip", str(self.path.with_name(default)),
                f"{ext.upper()} (*.{ext});;All files (*)")
            if not out:
                return  # user cancelled
            if not out.lower().endswith(f".{ext}"):
                out += f".{ext}"  # some platforms don't append the extension for us
            jobs.append((out, self._build_args(ext, spec,
                                                self.start_ms, self.end_ms, out)))

        # --- run the jobs in the background, one at a time ---
        self.queue = jobs
        self.job_total = len(jobs)
        self.export_btn.setEnabled(False)  # prevent starting two exports at once
        self._next_job()

    def _export_has_video(self, spec: dict) -> bool:
        """True when the export keeps a video stream: the source has frames,
        the format is not audio-only and 'Audio only' is not ticked."""
        return (self.path is not None
                and self.path.suffix.lower() not in AUDIO_EXTS
                and not spec["audio"]
                and not self.audio_only_check.isChecked())

    def _build_args(self, ext: str, spec: dict, start_ms: int, end_ms: int,
                    out: str) -> list[str]:
        """ffmpeg arguments cutting [start_ms, end_ms] from the source into `out`."""
        fast = self.fast_check.isChecked() and spec["copy"]
        # ffmpeg takes seconds, so convert from our milliseconds.
        start = f"{start_ms / 1000:.3f}"
        dur = (end_ms - start_ms) / 1000
        k = self.opts_dlg.speed_factor()
        if k and not fast:
            # A speed change stretches the clip's timeline (setpts/atempo),
            # so let the output run longer/shorter to hold the whole range.
            dur /= k
        dur_s = f"{dur:.3f}"

        # Common part:
        #   -y        overwrite the output file without asking
        #   -ss       start time; placed BEFORE -i so ffmpeg seeks quickly
        #   -i        the input file
        #   -t        how long the clip should be (duration, not end time)
        args = ["-y", "-ss", start, "-i", str(self.path), "-t", dur_s]

        if fast:
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
            # Extra options from the "Options…" dialog, appended after the
            # format's own args so they can override them (last option wins).
            # Stream copy cannot be combined with filters/codec options, so
            # they only apply when re-encoding.
            args += self.opts_dlg.ffmpeg_args(ext, video=self._export_has_video(spec))
        if self.audio_only_check.isChecked() and not spec["audio"]:
            # Strip the video stream from a video-container export.
            args += ["-vn"]
        args.append(out)  # the output path always goes last
        return args

    def _build_join_args(self, ext: str, spec: dict, out: str) -> list[str]:
        """One ffmpeg run cutting every queued clip and concatenating them.

        Each clip becomes its own input (-ss/-t before -i, which cuts exactly
        when re-encoding) and a concat filter joins them. Always re-encodes:
        stream copy can't go through a filtergraph, so "Fast copy" is ignored
        in Join mode. The options' video filters and the speed change are
        applied after the concat, inside the same filter_complex.
        """
        clips = self.clips
        n = len(clips)
        video = self._export_has_video(spec)
        args = ["-y"]
        for _name, s, e in clips:
            args += ["-ss", f"{s / 1000:.3f}", "-t", f"{(e - s) / 1000:.3f}",
                     "-i", str(self.path)]
        if video:
            fc = "".join(f"[{i}:v][{i}:a]" for i in range(n))
            fc += f"concat=n={n}:v=1:a=1[v0][a0]"
            vlab, alab = "[v0]", "[a0]"
        else:
            fc = "".join(f"[{i}:a]" for i in range(n))
            fc += f"concat=n={n}:v=0:a=1[a0]"
            vlab, alab = None, "[a0]"
        k = self.opts_dlg.speed_factor()
        if k:
            if vlab:
                fc += f";[v0]setpts=PTS/{k:.3f}[v1]"
                vlab = "[v1]"
            fc += f";{alab}atempo={k:.3f}[a1]"
            alab = "[a1]"
        vf = self.opts_dlg.video_filters() if video else []
        if vf and vlab:
            fc += f";{vlab}" + ",".join(vf) + "[v2]"
            vlab = "[v2]"
        args += ["-filter_complex", fc]
        if vlab:
            args += ["-map", vlab]
        args += ["-map", alab]
        args += spec["args"]
        if self.audio_only_check.isChecked() and not spec["audio"]:
            args += ["-vn"]
        args += self.opts_dlg.ffmpeg_output_args(ext, video=video)
        args.append(out)  # the output path always goes last
        return args

    def _next_job(self):
        """Start the next queued ffmpeg job, or report completion if none left."""
        if not self.queue:
            self.export_btn.setEnabled(True)
            if self.job_total > 1:
                self.status.setText(
                    f"Saved all {self.job_total} clips in {Path(self._last_out).parent}")
            elif self._last_out:
                self.status.setText(f"Saved: {self._last_out}")
            return
        out, args = self.queue.pop(0)
        done = self.job_total - len(self.queue)
        if self.job_total > 1:
            self.status.setText(
                f"Exporting clip {done}/{self.job_total}: {Path(out).name}…")
        else:
            self.status.setText("Exporting…")
        self.proc = QProcess(self)
        # Merge stderr into stdout so we can show ffmpeg's log if something fails.
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        # Call _job_done when the process ends (code 0 = success), passing the
        # exit status too so a crash (OOM kill, segfault) is reported, not hidden.
        self.proc.finished.connect(
            lambda code, status, o=out: self._job_done(code, status, o))
        # Catch spawn failures (ffmpeg vanished from PATH) as their own event.
        self.proc.errorOccurred.connect(
            lambda e, o=out: self._proc_error(e, o))
        self.proc.start("ffmpeg", args)

    def _proc_error(self, error, out):
        """The ffmpeg process could not start or died abnormally."""
        if error == QProcess.ProcessError.FailedToStart:
            self.queue.clear()
            self.export_btn.setEnabled(True)
            self.status.setText("Export failed.")
            QMessageBox.critical(self, "Clipper",
                                 "ffmpeg could not be started. Install it and "
                                 "make sure it's on your PATH.")
        else:
            # Crash / write errors: log it; finished() still fires for
            # crashes with a nonzero code and _job_done shows the message.
            print(f"[clipper] ffmpeg process error {error!r} for {out}",
                  file=sys.stderr)

    def _job_done(self, code, status, out):
        """Called when an ffmpeg job finishes; start the next one or report."""
        self._last_out = out
        if code == 0 and status != QProcess.ExitStatus.CrashExit:
            self._next_job()
            return
        # Show only the last ~1500 characters; the useful error is at the end.
        log = bytes(self.proc.readAll()).decode(errors="replace")[-1500:]
        if not log.strip():  # crash with no output (killed, segfault...)
            log = (f"ffmpeg exited abnormally (code {code}, "
                   f"status {'crashed' if status == QProcess.ExitStatus.CrashExit else 'normal'}).")
        self.queue.clear()               # abort the remaining jobs
        self.export_btn.setEnabled(True)
        self.status.setText("Export failed.")
        QMessageBox.critical(self, "ffmpeg error", log)


# Standard Python entry point: only runs when executed directly,
# not when this file is imported by another script.
if __name__ == "__main__":
    if DEBUG:
        # Turn on Qt's own multimedia/mediaplayer debug logging on stderr so
        # backend problems (this app's most common failure mode) are visible.
        os.environ.setdefault("QT_LOGGING_RULES",
                              "*.debug=false;qt.multimedia.*.debug=true")
    app = QApplication(sys.argv)  # Qt needs one application object per program
    w = Clipper()
    w.show()
    sys.exit(app.exec())          # start the event loop; exit with its return code