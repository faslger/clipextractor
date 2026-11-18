#!/usr/bin/env python3
"""
Clipper - open a video or audio file, seek around, mark a start/end (the
marked range is highlighted on the seek bar), and export it to many
formats (mp4, webm, mkv, mov, mp3, wav, ogg, flac, m4a, opus).
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
    Left/Right  seek -1s / +1s   (hold Shift for 5s)
    P           preview the selected range

The "Help" button (and the "?" button in the Options popup) open guide
windows explaining how to set and configure everything.

How it is put together:
    * PySide6 (Qt) provides the window, the video player and the buttons.
      It is only used for *previewing* and picking times.
    * ffmpeg (an external program) does the actual cutting/converting.
      We launch it in the background with QProcess so the UI doesn't freeze.
    * All times inside the program are stored in MILLISECONDS (integers),
      because that is what Qt's media player uses. We only convert to
      seconds when building the ffmpeg command.
"""
import shlex
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QRect, Qt, QProcess, QUrl
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPalette, QPen, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSlider, QStyle, QStyleOptionSlider, QTextBrowser, QVBoxLayout,
    QWidget,
)

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


class MarkerSlider(QSlider):
    """Seek bar that also paints the marked [start, end] range on top of it."""

    def __init__(self, *args):
        super().__init__(*args)
        self.start_ms = 0
        self.end_ms = 0

    def set_marks(self, start_ms: int, end_ms: int):
        """Store the marker positions (milliseconds) and repaint."""
        self.start_ms = start_ms
        self.end_ms = end_ms
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
        if dur <= 0 or self.end_ms <= self.start_ms:
            return  # nothing marked yet
        x1 = self._x_for(self.start_ms)
        x2 = self._x_for(min(dur, self.end_ms))

        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(
            QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
        cy = groove.center().y()

        highlight = self.palette().color(QPalette.Highlight)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # Translucent band across the groove over the selected range
        # (the playhead handle stays visible through it).
        band = QColor(highlight)
        band.setAlpha(90)
        p.fillRect(QRect(x1, cy - 3, max(1, x2 - x1), 6), band)
        # Solid marker lines at start and end, spanning the bar's height.
        p.setPen(QPen(highlight, 2))
        r = self.rect()
        p.drawLine(x1, r.top() + 1, x1, r.bottom() - 1)
        p.drawLine(x2, r.top() + 1, x2, r.bottom() - 1)
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

    def ffmpeg_args(self, ext: str, video: bool) -> list[str]:
        """Build the extra ffmpeg arguments from the current widget states.

        `video` is False for audio-only exports, so video filters are skipped.
        """
        args: list[str] = []
        vf: list[str] = []  # video filters, merged into a single -vf
        if video:
            if self.fps_chk.isChecked():
                args += ["-r", str(self.fps_slider.value())]
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
            if self.crf_chk.isChecked():
                args += ["-crf", str(self.crf_slider.value())]
            vbr = self.vbr_box.currentData()
            if vbr:
                args += ["-b:v", vbr]
        if self.speed_chk.isChecked():
            k = self.speed_slider.value() / 100.0
            args += ["-af", f"atempo={k:.3f}"]   # audio tempo
            if video:
                vf.append(f"setpts=PTS/{k:.3f}")  # video timestamps
        if vf:
            args += ["-vf", ",".join(vf)]
        # Audio options (every export keeps its audio stream); a bitrate makes
        # no sense for lossless PCM/FLAC, so skip it there.
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

<h3>Transport &amp; markers</h3>
<ul>
<li><b>Space</b> / Play-Pause &mdash; play or pause.</li>
<li><b>Left / Right</b> &mdash; seek 1 s back/forward (<b>Shift</b> = 5 s).</li>
<li><b>I / O</b> &mdash; set the start/end marker at the playhead.</li>
<li><b>P</b> / Preview &mdash; plays only the marked range and stops at the
end marker.</li>
</ul>

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
<li><b>Options…</b> &mdash; popup with extra ffmpeg settings (frame rate,
resolution, aspect ratio, scaling, quality, bitrates, speed, raw arguments).
It has its own "?" help button explaining each option.</li>
<li><b>Export…</b> &mdash; asks for a filename and runs ffmpeg in the
background; the status line shows progress and the saved path.</li>
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
        # Seek bar (paints the marked range) with the time readout beside it.
        self.slider = MarkerSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)             # real range is set once duration is known
        self.time_lbl = QLabel("00:00:00.000 / 00:00:00.000")

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
        self.preview_btn.setToolTip("Play only the marked range (P)")
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
        self.opts_btn = QPushButton("Options…")
        self.opts_btn.setToolTip(
            "Extra ffmpeg options for the export: frame rate, resolution, "
            "aspect ratio, scaling, quality, bitrates, speed, raw arguments…")
        self.export_btn = QPushButton("Export…")
        self.export_btn.setToolTip("Export the marked range with ffmpeg")
        self.status = QLabel("Open a video or audio file to begin.")
        # Kept as a member so the popup remembers its settings between openings.
        self.opts_dlg = ExportOptionsDialog(self)

        # ---------- layout ----------
        # Row 1: seek bar with the time readout at its right end.
        seek = QHBoxLayout()
        seek.addWidget(self.slider, 1)   # stretch=1: slider takes the spare space
        seek.addWidget(self.time_lbl)

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
        controls.addWidget(self.range_lbl, 1)
        controls.addWidget(self.help_btn)

        # Row 3: export options on the left, the export button on the right.
        export = QHBoxLayout()
        export.addWidget(QLabel("Format:"))
        export.addWidget(self.format_box)
        export.addWidget(self.audio_only_check)
        export.addWidget(self.fast_check)
        export.addWidget(self.opts_btn)
        export.addStretch(1)
        export.addWidget(self.export_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self.video, 1)   # stretch=1: video takes all spare space
        layout.addLayout(seek)
        layout.addLayout(controls)
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
        self.opts_btn.clicked.connect(self.open_options)
        self.export_btn.clicked.connect(self.export)
        self.help_btn.clicked.connect(self.open_help)

        # Fast copy is only offered for formats that allow stream copy; the
        # "Audio only" box is redundant for formats that already drop video.
        self.format_box.currentTextChanged.connect(self.update_export_options)
        self.update_export_options(self.format_box.currentText())

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
        self.slider.set_marks(0, 0)  # clear any old marks on the seek bar
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
        """Refresh the 'Start / End / length' text and the marks on the seek bar."""
        self.range_lbl.setText(
            f"Start: {fmt(self.start_ms)}    End: {fmt(self.end_ms)}    "
            f"({(self.end_ms - self.start_ms) / 1000:.2f}s)")
        self.slider.set_marks(self.start_ms, self.end_ms)

    def preview(self):
        """Play only the selected range (jump to start, auto-stop at end)."""
        self.player.setPosition(self.start_ms)
        self.previewing = True  # on_position() watches this flag to stop at end_ms
        self.player.play()

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
        fast = self.fast_check.isChecked() and spec["copy"]
        # ffmpeg takes seconds, so convert from our milliseconds.
        start = f"{self.start_ms / 1000:.3f}"
        dur = (self.end_ms - self.start_ms) / 1000
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
            args += self.opts_dlg.ffmpeg_args(
                ext,
                video=not spec["audio"] and not self.audio_only_check.isChecked())
        if self.audio_only_check.isChecked() and not spec["audio"]:
            # Strip the video stream from a video-container export.
            args += ["-vn"]
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