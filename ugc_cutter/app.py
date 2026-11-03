"""The window: video on top, timeline underneath."""
import sys
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QSettings, QStandardPaths, QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QPen, QPolygonF, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QPushButton, QScrollBar, QVBoxLayout, QWidget)
from PySide6.QtCore import QPointF

from .media import ENVELOPE_RATE, cached_envelope, scan_folder
from .model import CUTS_FILE, CutList

FRAME = 1 / 30
LINE_GRAB_PX = 6  # a right click this close to a placed line removes it
GREEN = QColor(46, 160, 67, 90)
RED = QColor(218, 54, 51, 130)
BACKGROUND = QColor(24, 24, 27)
WAVE = QColor(235, 235, 240, 190)
CUT_LINE = QColor(255, 255, 255)
CLIP_LINE = QColor(88, 166, 255)
PLAYHEAD = QColor(255, 214, 10)


def clock(t):
    t = max(0.0, t)
    return f"{int(t // 60)}:{t % 60:05.2f}"


class WaveformLoader(QThread):
    """Reads each clip's audio in the background so the window opens straight away."""
    loaded = Signal(int, object)

    def __init__(self, clips, cache_dir):
        super().__init__()
        self.clips, self.cache_dir = clips, cache_dir

    def run(self):
        for i, clip in enumerate(self.clips):
            if self.isInterruptionRequested():
                return
            try:
                self.loaded.emit(i, cached_envelope(clip, self.cache_dir))
            except Exception as e:  # a waveform is only a help, so carry on without this one
                print(f"no waveform for {clip.name}: {e}", file=sys.stderr)


class Timeline(QWidget):
    seek_requested = Signal(float)
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cuts = None
        self.playhead = 0.0
        self.px_per_sec = 10.0
        self.scroll = 0.0
        self.envelope = None
        self.setMinimumHeight(150)
        self.setFocusPolicy(Qt.NoFocus)
        self.bar = None

    def set_cuts(self, cuts):
        self.cuts = cuts
        self.envelope = np.zeros(int(cuts.total * ENVELOPE_RATE) + 2, dtype=np.float32)
        self.fit()

    def set_envelope(self, clip, env):
        start = int(clip.offset * ENVELOPE_RATE)
        n = min(len(env), len(self.envelope) - start)
        if n > 0:
            self.envelope[start:start + n] = env[:n]
            self.update()

    # --- position
    def fit(self):
        if self.cuts and self.cuts.total > 0:
            self.px_per_sec = max(1.0, self.width()) / self.cuts.total
            self.scroll = 0.0
            self._sync_bar()
            self.update()

    def x_of(self, t):
        return (t - self.scroll) * self.px_per_sec

    def t_of(self, x):
        t = self.scroll + x / self.px_per_sec
        return min(max(0.0, t), self.cuts.total) if self.cuts else 0.0

    def _visible(self):
        return self.width() / self.px_per_sec

    def _clamp(self):
        self.scroll = min(max(0.0, self.scroll), max(0.0, self.cuts.total - self._visible()))

    def _sync_bar(self):
        if not self.bar or not self.cuts:
            return
        self.bar.blockSignals(True)
        self.bar.setRange(0, int(max(0.0, self.cuts.total - self._visible()) * 100))
        self.bar.setPageStep(int(self._visible() * 100))
        self.bar.setValue(int(self.scroll * 100))
        self.bar.blockSignals(False)

    def on_bar(self, value):
        self.scroll = value / 100
        self.update()

    def set_playhead(self, t, follow=False):
        self.playhead = t
        if follow and self.cuts:
            x = self.x_of(t)
            if x < 0 or x > self.width() * 0.9:
                self.scroll = t - self._visible() * 0.1
                self._clamp()
                self._sync_bar()
        self.update()

    def resizeEvent(self, event):
        if self.cuts:
            # stay fitted if the whole video was showing
            if self.scroll == 0 and self.px_per_sec * self.cuts.total <= event.oldSize().width() + 1:
                self.fit()
            else:
                self._clamp()
                self._sync_bar()

    # --- mouse
    def wheelEvent(self, event):
        """The wheel zooms in and out around the pointer. Hold Shift to scroll sideways."""
        if not self.cuts:
            return
        steps = event.angleDelta().y() / 120 or event.angleDelta().x() / 120
        if event.modifiers() & Qt.ShiftModifier:
            self.scroll -= steps * self._visible() * 0.1
        else:
            x = event.position().x()
            anchor = self.t_of(x)
            fitted = self.width() / max(self.cuts.total, 0.001)
            self.px_per_sec = min(max(fitted, self.px_per_sec * 1.25 ** steps), 2000.0)
            self.scroll = anchor - x / self.px_per_sec
        self._clamp()
        self._sync_bar()
        self.update()

    def mousePressEvent(self, event):
        if not self.cuts:
            return
        x = event.position().x()
        t = self.t_of(x)
        if event.button() == Qt.LeftButton:
            self.seek_requested.emit(t)
        elif event.button() == Qt.RightButton:
            if event.modifiers() & Qt.ShiftModifier:
                self.cuts.toggle(t)
            else:
                line = self.cuts.removable_line_near(t, LINE_GRAB_PX / self.px_per_sec)
                if line is not None:
                    self.cuts.remove_cut(line)
                else:
                    self.cuts.add_cut(t)
            self.changed.emit()
            self.update()

    def mouseMoveEvent(self, event):
        if self.cuts and event.buttons() & Qt.LeftButton:
            self.seek_requested.emit(self.t_of(event.position().x()))

    # --- drawing
    def paintEvent(self, event):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, BACKGROUND)
        if not self.cuts:
            p.setPen(QColor(160, 160, 170))
            p.drawText(self.rect(), Qt.AlignCenter, "Open a folder of clips")
            return
        ruler = 18
        top, bottom = ruler, h

        for s in self.cuts.sections:
            a, b = self.x_of(s.start), self.x_of(s.end)
            if b >= 0 and a <= w:
                p.fillRect(int(a), top, int(b - a) + 1, bottom - top, GREEN if s.keep else RED)

        # the waveform: one vertical line per pixel, using the loudest point under it
        middle = (top + bottom) / 2
        half = (bottom - top) / 2 - 3
        p.setPen(WAVE)
        count = len(self.envelope)
        for x in range(w):
            i = int(self.t_of(x) * ENVELOPE_RATE)
            j = int(self.t_of(x + 1) * ENVELOPE_RATE) + 1
            if i < count:
                height = float(np.sqrt(self.envelope[i:min(j, count)].max())) * half
                if height > 0.5:
                    p.drawLine(x, int(middle - height), x, int(middle + height))

        # the time ruler
        p.fillRect(0, 0, w, ruler, QColor(39, 39, 42))
        p.setFont(QFont("sans-serif", 8))
        step = next((s for s in (0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300) if s * self.px_per_sec >= 70), 600)
        t = int(self.scroll / step) * step
        while t <= self.scroll + self._visible():
            x = int(self.x_of(t))
            p.setPen(QColor(120, 120, 130))
            p.drawLine(x, ruler - 5, x, ruler)
            p.setPen(QColor(190, 190, 200))
            p.drawText(x + 3, ruler - 5, clock(t))
            t += step

        for line, clip_end in self.cuts.lines():
            x = int(self.x_of(line))
            if -2 <= x <= w + 2:
                p.setPen(QPen(CLIP_LINE if clip_end else CUT_LINE, 2 if clip_end else 1))
                p.drawLine(x, ruler, x, h)

        x = self.x_of(self.playhead)
        if -8 <= x <= w + 8:
            p.setPen(QPen(PLAYHEAD, 2))
            p.drawLine(int(x), 0, int(x), h)
            p.setBrush(PLAYHEAD)
            p.drawPolygon(QPolygonF([QPointF(x - 6, 0), QPointF(x + 6, 0), QPointF(x, 9)]))


class Player(QObject):
    """Plays the clips as one long video, moving between clip files when it reaches the end of one."""
    position_changed = Signal(float)
    problem = Signal(str)

    def __init__(self, video_widget, parent=None):
        super().__init__(parent)
        self.media = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.media.setAudioOutput(self.audio)
        self.media.setVideoOutput(video_widget)
        self.cuts = None
        self.clip_index = None
        self.pending = None
        self.want_play = False
        self.skip_red = False
        self.time = 0.0
        self.media.mediaStatusChanged.connect(self._on_status)
        self.media.errorOccurred.connect(lambda _e, text: self.problem.emit(text))
        self.ticker = QTimer(self)
        self.ticker.setInterval(30)
        self.ticker.timeout.connect(self._tick)
        self.ticker.start()

    def set_cuts(self, cuts):
        self.cuts = cuts
        self.clip_index = None
        self.want_play = False
        self.media.stop()
        self.seek(0.0)

    @property
    def playing(self):
        return self.want_play

    def seek(self, t):
        if not self.cuts:
            return
        self.time = min(max(0.0, t), self.cuts.total)
        index, local = self.cuts.clip_at(self.time)
        if index != self.clip_index:
            self.clip_index = index
            self.pending = local
            self.media.setSource(QUrl.fromLocalFile(str(self.cuts.clips[index].path)))
        elif self.pending is not None:
            self.pending = local  # the clip is still opening: go here once it has
        else:
            self.media.setPosition(int(local * 1000))
        self.position_changed.emit(self.time)

    def toggle(self):
        self.set_playing(not self.want_play)

    def set_playing(self, playing):
        if not self.cuts:
            return
        if playing and self.time >= self.cuts.total - 0.01:
            self.seek(0.0)
        self.want_play = playing
        if self.pending is not None:
            return  # _on_status starts it once the clip has opened
        if playing:
            if self.media.mediaStatus() == QMediaPlayer.EndOfMedia:
                self.media.setPosition(int(self.cuts.clip_at(self.time)[1] * 1000))
            self.media.play()
        else:
            self.media.pause()

    def _on_status(self, status):
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia) and self.pending is not None:
            local, self.pending = self.pending, None
            self.media.setPosition(int(local * 1000))
            if self.want_play:
                self.media.play()
            else:
                self.media.pause()  # shows the frame at this position
        elif status == QMediaPlayer.EndOfMedia and self.want_play and self.cuts:
            if self.clip_index + 1 < len(self.cuts.clips):
                self.seek(self.cuts.clips[self.clip_index + 1].offset)
            else:
                self.want_play = False
                self.time = self.cuts.total
                self.position_changed.emit(self.time)
        elif status == QMediaPlayer.InvalidMedia:
            self.want_play = False

    def _tick(self):
        if not (self.cuts and self.want_play and self.pending is None and self.clip_index is not None):
            return
        if self.media.playbackState() != QMediaPlayer.PlayingState:
            return
        self.time = self.cuts.clips[self.clip_index].offset + self.media.position() / 1000
        if self.skip_red and not self.cuts.sections[self.cuts.section_index(self.time)].keep:
            target = self.cuts.next_kept_start(self.time)
            if target is None:
                self.set_playing(False)
                self.seek(self.cuts.total)
            else:
                self.seek(target)
            return
        self.position_changed.emit(self.time)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("UGC Cutter")
        self.resize(1200, 860)
        self.folder = None
        self.cuts = None
        self.loader = None

        self.video = QVideoWidget()
        self.video.setStyleSheet("background: black;")
        self.video.setFocusPolicy(Qt.NoFocus)
        self.player = Player(self.video, self)

        self.timeline = Timeline()
        self.timeline.seek_requested.connect(self.player.seek)
        self.timeline.changed.connect(self._refresh)
        self.player.position_changed.connect(self._on_position)
        self.player.problem.connect(lambda text: self.statusBar().showMessage(f"Playback problem: {text}"))
        self.bar = QScrollBar(Qt.Horizontal)
        self.bar.setFocusPolicy(Qt.NoFocus)
        self.bar.valueChanged.connect(self.timeline.on_bar)
        self.timeline.bar = self.bar

        open_button = QPushButton("Open folder")
        open_button.clicked.connect(self.choose_folder)
        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(self.toggle_play)
        self.skip_box = QCheckBox("Skip red when playing")
        self.skip_box.toggled.connect(self._on_skip)
        fit_button = QPushButton("Show all")
        fit_button.clicked.connect(self.timeline.fit)
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self.save)
        self.time_label = QLabel("0:00.00")
        self.time_label.setFont(QFont("monospace", 11))
        self.summary = QLabel("")
        for w in (open_button, self.play_button, self.skip_box, fit_button, self.save_button):
            w.setFocusPolicy(Qt.NoFocus)

        row = QHBoxLayout()
        for w in (open_button, self.play_button, self.time_label, self.skip_box, fit_button):
            row.addWidget(w)
        row.addStretch(1)
        row.addWidget(self.summary)
        row.addWidget(self.save_button)

        help_text = QLabel("Left click: move playhead  |  Space: play/pause  |  Right click: add cut, or remove a cut "
                           "line  |  Shift + right click: green/red  |  Wheel: zoom  |  Arrows: nudge a frame "
                           "(Shift: 1 s)  |  Ctrl+Z: undo")
        help_text.setStyleSheet("color: #888;")

        layout = QVBoxLayout()
        layout.addWidget(self.video, 1)
        layout.addLayout(row)
        layout.addWidget(self.timeline)
        layout.addWidget(self.bar)
        layout.addWidget(help_text)
        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

        for keys, action in (("Space", self.toggle_play), ("Left", lambda: self.nudge(-FRAME)),
                             ("Right", lambda: self.nudge(FRAME)), ("Shift+Left", lambda: self.nudge(-1)),
                             ("Shift+Right", lambda: self.nudge(1)), ("Ctrl+S", self.save), ("Ctrl+Z", self.undo),
                             ("Ctrl+Shift+Z", self.redo), ("Ctrl+Y", self.redo), ("Ctrl+O", self.choose_folder)):
            QShortcut(QKeySequence(keys), self, activated=action, context=Qt.ApplicationShortcut)

    # --- opening
    def choose_folder(self):
        if not self.confirm_discard():
            return
        settings = QSettings("drewdisciplined", "ugc-cutter")
        start = settings.value("last_folder", str(Path.home()))
        folder = QFileDialog.getExistingDirectory(self, "Choose the folder with your clips", start)
        if folder:
            settings.setValue("last_folder", folder)
            self.open_folder(folder)

    def open_folder(self, folder):
        self._stop_loader()
        self.folder = Path(folder)
        self.statusBar().showMessage("Reading clips...")
        QApplication.processEvents()
        clips, failed = scan_folder(folder, lambda n, total, name: (
            self.statusBar().showMessage(f"Reading clip {n} of {total}: {name}"), QApplication.processEvents()))
        if not clips:
            self.cuts = None
            self.timeline.cuts = None
            self.timeline.update()
            self.statusBar().showMessage(f"No videos found in {folder}")
            return False
        self.cuts = CutList(clips)
        message = f"Opened {len(clips)} clips"
        saved = self.folder / CUTS_FILE
        if saved.exists():
            try:
                self.cuts.load_text(saved.read_text(encoding="utf-8"))
                message += f", with your cuts from {CUTS_FILE}"
            except ValueError as e:
                message += f", but couldn't read {CUTS_FILE} ({e})"
        if failed:
            message += ". Couldn't open: " + ", ".join(name for name, _ in failed)
        self.timeline.set_cuts(self.cuts)
        self.player.set_cuts(self.cuts)
        cache = Path(QStandardPaths.writableLocation(QStandardPaths.CacheLocation)) / "waveforms"
        self.loader = WaveformLoader(clips, cache)
        self.loader.loaded.connect(lambda i, env: self.timeline.set_envelope(clips[i], env))
        self.loader.start()
        self._refresh()
        self.statusBar().showMessage(message)
        return True

    def _stop_loader(self):
        if self.loader:
            self.loader.requestInterruption()
            self.loader.wait()
            self.loader = None

    # --- actions
    def toggle_play(self):
        self.player.toggle()
        self.play_button.setText("Pause" if self.player.playing else "Play")

    def nudge(self, seconds):
        self.player.seek(self.player.time + seconds)

    def undo(self):
        if self.cuts and self.cuts.undo():
            self._refresh()

    def redo(self):
        if self.cuts and self.cuts.redo():
            self._refresh()

    def save(self):
        if not self.cuts:
            return False
        try:
            path = self.cuts.save(self.folder)
        except OSError as e:
            QMessageBox.critical(self, "Couldn't save", str(e))
            return False
        self._refresh()
        self.statusBar().showMessage(f"Saved {path}")
        return True

    def confirm_discard(self):
        if not (self.cuts and self.cuts.dirty):
            return True
        answer = QMessageBox.question(self, "Unsaved cuts", "Save your cuts first?",
                                      QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if answer == QMessageBox.Save:
            return self.save()
        return answer == QMessageBox.Discard

    def closeEvent(self, event):
        if self.confirm_discard():
            self._stop_loader()
            event.accept()
        else:
            event.ignore()

    # --- display
    def _on_skip(self, on):
        self.player.skip_red = on

    def _on_position(self, t):
        self.timeline.set_playhead(t, follow=self.player.playing)
        self.time_label.setText(f"{clock(t)} / {clock(self.cuts.total)}" if self.cuts else clock(t))
        if not self.player.playing:
            self.play_button.setText("Play")

    def _refresh(self):
        self.timeline.update()
        if not self.cuts:
            return
        kept = sum(s.end - s.start for s in self.cuts.sections if s.keep)
        self.summary.setText(f"Keeping {clock(kept)}")
        name = self.folder.name if self.folder else ""
        self.setWindowTitle(f"{'*' if self.cuts.dirty else ''}{name} - UGC Cutter")
