"""The window: video on top, timeline underneath."""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QSettings, QStandardPaths, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QKeySequence, QPainter, QPen, QPolygonF, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QPushButton, QScrollBar, QVBoxLayout, QWidget)
from PySide6.QtCore import QPointF

from . import update
from .model import CUTS_FILE, ENVELOPE_RATE, Clip, CutList
from .version import VERSION

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


class UpdateTasks(QObject):
    """Update work runs on throwaway background threads, so closing the app mid-check can't crash it:
    these threads simply end with the program."""
    found = Signal(object)
    progress = Signal(int)
    downloaded = Signal(object)
    failed = Signal(str)

    def check(self):
        threading.Thread(target=self._check, daemon=True).start()

    def _check(self):
        newer = update.check_latest(VERSION)
        if newer:
            self._emit(self.found, newer)

    def download(self, url, path):
        threading.Thread(target=self._download, args=(url, path), daemon=True).start()

    def _download(self, url, path):
        try:
            update.download(url, path, lambda percent: self._emit(self.progress, percent))
            self._emit(self.downloaded, path)
        except OSError as e:
            self._emit(self.failed, str(e))

    @staticmethod
    def _emit(signal, value):
        try:
            signal.emit(value)
        except RuntimeError:  # the window has already closed
            pass


def worker_command():
    """How to start this same program as a worker, whether it's the built app or running from source."""
    if getattr(sys, "frozen", False):
        return sys.executable, ["--worker"]
    return sys.executable, ["-m", "ugc_cutter", "--worker"]


def start_worker(process, args):
    program, first = worker_command()
    if not getattr(sys, "frozen", False):
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONPATH", str(Path(__file__).resolve().parent.parent))
        process.setProcessEnvironment(env)
    process.setProcessChannelMode(QProcess.ForwardedChannels)
    process.start(program, first + [str(a) for a in args])


class EventFile:
    """Reads the lines a worker has added to its events file so far."""

    def __init__(self, path):
        self.path, self.position, self.partial = Path(path), 0, ""

    def read(self):
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as f:
            f.seek(self.position)
            text = f.read()
            self.position = f.tell()
        lines = (self.partial + text).split("\n")
        self.partial = lines.pop()
        return [json.loads(line) for line in lines if line]


class BackgroundLoader(QObject):
    """Has a worker read each clip's audio and make its fast-playback copy, while the window stays free."""
    loaded = Signal(int, object)
    proxy_ready = Signal(int, object)
    proxy_failed = Signal(int, str)
    merged_ready = Signal(object)

    def __init__(self, clips, cache_dir, proxies=True, parent=None):
        super().__init__(parent)
        self.work_dir = tempfile.TemporaryDirectory(prefix="ugc-cutter-")
        work = Path(self.work_dir.name)
        (work / "job.json").write_text(json.dumps({
            "cache": str(cache_dir), "proxies": proxies,
            "clips": [{"path": str(c.path), "duration": c.duration} for c in clips]}), encoding="utf-8")
        self.events = EventFile(work / "events.jsonl")
        self.process = QProcess(self)
        start_worker(self.process, ["prepare", work / "job.json", work / "events.jsonl"])
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._poll)
        self.timer.start()

    def _poll(self):
        for event in self.events.read():
            kind = event["kind"]
            if kind == "waveform":
                self.loaded.emit(event["index"], np.load(event["path"]))
            elif kind == "proxy":
                self.proxy_ready.emit(event["index"], event["path"])
            elif kind == "proxy_failed":
                self.proxy_failed.emit(event["index"], event["error"])
            elif kind == "merged":
                self.merged_ready.emit(event["path"])
        if self.process.state() == QProcess.NotRunning:
            self.timer.stop()

    def stop(self):
        self.timer.stop()
        if self.process.state() != QProcess.NotRunning:
            self.process.kill()
            self.process.waitForFinished(3000)
        self.work_dir.cleanup()


def scan_with_worker(folder, on_progress):
    """Reads the clips in a folder using a worker. Returns (clips, failed)."""
    with tempfile.TemporaryDirectory(prefix="ugc-cutter-") as work:
        events = EventFile(Path(work) / "events.jsonl")
        process = QProcess()
        start_worker(process, ["scan", folder, Path(work) / "events.jsonl"])
        result = None
        while result is None:
            running = process.state() != QProcess.NotRunning
            for event in events.read():
                if event["kind"] == "progress":
                    on_progress(event["n"], event["total"], event["name"])
                elif event["kind"] == "scanned":
                    result = event
            if result is None and not running:
                raise RuntimeError(f"couldn't read the folder (worker error: {process.exitCode()})")
            if result is None:
                process.waitForFinished(30)
                QApplication.processEvents()
        process.waitForFinished(3000)
    return [Clip(Path(c["path"]), c["duration"]) for c in result["clips"]], result["failed"]


class Timeline(QWidget):
    seek_requested = Signal(float)
    scrub_requested = Signal(float)
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
            self.scrub_requested.emit(self.t_of(event.position().x()))

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
        self.proxies = {}  # clip index -> fast-playback copy
        self.merged = None  # all the clips as one fast file: the timeline time is the file's time
        self.opening = False
        self.open_timer = QTimer(self)
        self.open_timer.setSingleShot(True)
        self.open_timer.setInterval(300)
        self.open_timer.timeout.connect(self._open_check)
        self.settle_target = None
        self.settle_timer = QTimer(self)
        self.settle_timer.setSingleShot(True)
        self.settle_timer.setInterval(120)
        self.settle_timer.timeout.connect(self._settle)
        self.scrub_target = None
        self.scrub_timer = QTimer(self)
        self.scrub_timer.setSingleShot(True)
        self.scrub_timer.setInterval(40)
        self.scrub_timer.timeout.connect(self._apply_scrub)
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
        self.pending = None
        self.proxies = {}
        self.merged = None
        self.media.stop()
        self.seek(0.0)

    @property
    def playing(self):
        return self.want_play

    def add_proxy(self, index, path):
        """A clip's fast copy, used the next time that clip opens if they can't all share one file."""
        self.proxies[index] = path

    def use_merged(self, path):
        """Switch to the one-file timeline, carrying on from the same place."""
        self.merged = path
        self.clip_index = None
        self._open(path, self.time)

    def _open(self, path, position):
        """Open a file and go to a position in it. The player can report the old file's status after a new
        one is set, so a file only counts as open once it has been seen loading."""
        self.pending = position
        self.opening = True
        self.media.setSource(QUrl.fromLocalFile(str(path)))
        if self.media.mediaStatus() == QMediaPlayer.LoadingMedia:
            self.opening = False  # already loading the last file, so no new "loading" report would come
        if self.want_play:
            self.media.play()
        else:
            self.media.pause()  # a paused player that wasn't told again after a new file never shows a frame
        self.open_timer.start()

    def _open_check(self):
        """If the player stayed quiet about loading, don't wait on it forever: the file is open by now."""
        if self.pending is None:
            return
        if self.media.mediaStatus() in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            self.opening = False
            self._on_status(self.media.mediaStatus())
        else:
            self.open_timer.start()

    def _settle(self):
        """A position set in the moment a file opens can be lost, so check that it landed."""
        target, self.settle_target = self.settle_target, None
        if target is not None and self.pending is None and not self.want_play \
                and abs(self.media.position() / 1000 - target) > 0.05:
            self.media.setPosition(int(target * 1000))

    def scrub(self, t):
        """Dragging sends far more positions than can be shown: go to the newest one, a frame at a time."""
        self.scrub_target = t
        self.time = min(max(0.0, t), self.cuts.total) if self.cuts else 0.0
        self.position_changed.emit(self.time)
        if not self.scrub_timer.isActive():
            self.scrub_timer.start()

    def _apply_scrub(self):
        if self.scrub_target is not None:
            target, self.scrub_target = self.scrub_target, None
            self.seek(target)

    def seek(self, t):
        if not self.cuts:
            return
        self.time = min(max(0.0, t), self.cuts.total)
        if self.merged:
            if self.pending is not None:
                self.pending = self.time
            else:
                self.media.setPosition(int(self.time * 1000))
            self.position_changed.emit(self.time)
            return
        index, local = self.cuts.clip_at(self.time)
        if index != self.clip_index:
            self.clip_index = index
            self._open(self.proxies.get(index, self.cuts.clips[index].path), local)
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
                local = self.time if self.merged else self.cuts.clip_at(self.time)[1]
                self.media.setPosition(int(local * 1000))
            self.media.play()
        else:
            self.media.pause()

    def _on_status(self, status):
        if status == QMediaPlayer.LoadingMedia:
            self.opening = False
        elif status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia) and self.pending is not None \
                and not self.opening:
            local, self.pending = self.pending, None
            self.media.setPosition(int(local * 1000))
            self.settle_target = local
            self.settle_timer.start()
            if self.want_play:
                self.media.play()
            else:
                self.media.pause()  # shows the frame at this position
        elif status == QMediaPlayer.EndOfMedia and self.want_play and self.cuts:
            if not self.merged and self.clip_index + 1 < len(self.cuts.clips):
                next_start = self.cuts.clips[self.clip_index + 1].offset
                target = self.cuts.next_kept_start(next_start) if self.skip_red else next_start
                if target is None:
                    self.set_playing(False)
                    self.seek(self.cuts.total)
                else:
                    self.seek(target)
            else:
                self.want_play = False
                self.time = self.cuts.total
                self.position_changed.emit(self.time)
        elif status == QMediaPlayer.InvalidMedia:
            self.want_play = False

    def _tick(self):
        if not (self.cuts and self.want_play and self.pending is None and (self.merged or self.clip_index is not None)):
            return
        if self.media.playbackState() != QMediaPlayer.PlayingState:
            return
        offset = 0.0 if self.merged else self.cuts.clips[self.clip_index].offset
        self.time = min(offset + self.media.position() / 1000, self.cuts.total)
        # look one tick ahead, so a red section is jumped over rather than heard for a moment
        ahead = min(self.time + self.ticker.interval() / 1000, self.cuts.total - 0.001)
        if self.skip_red and not self.cuts.sections[self.cuts.section_index(ahead)].keep:
            target = self.cuts.next_kept_start(ahead)
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
        self.timeline.scrub_requested.connect(self.player.scrub)
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
        self.fast_label = QLabel("")
        self.fast_label.setStyleSheet("color: #888;")
        self.update_button = QPushButton("Update available")
        self.update_button.hide()
        self.update_button.clicked.connect(self.install_update)
        for w in (open_button, self.play_button, self.skip_box, fit_button, self.save_button, self.update_button):
            w.setFocusPolicy(Qt.NoFocus)

        row = QHBoxLayout()
        for w in (open_button, self.play_button, self.time_label, self.skip_box, fit_button):
            row.addWidget(w)
        row.addWidget(self.update_button)
        row.addStretch(1)
        row.addWidget(self.fast_label)
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

        self.newer = None
        self.updates = UpdateTasks(self)
        self.updates.found.connect(self._on_update_found)
        self.updates.progress.connect(lambda percent: self.update_button.setText(f"Downloading {percent}%"))
        self.updates.failed.connect(self._on_update_failed)
        self.updates.downloaded.connect(self._run_installer)
        self.updates.check()

    # --- updating
    def _on_update_found(self, newer):
        self.newer = newer
        self.update_button.setToolTip(f"Version {newer['version']} is available. You have {VERSION}.")
        self.update_button.show()

    def install_update(self):
        """On Windows, fetch the installer and run it, which reopens the app. Elsewhere, show the release page."""
        if not (sys.platform == "win32" and getattr(sys, "frozen", False) and self.newer.get("installer_url")):
            QDesktopServices.openUrl(QUrl(update.RELEASES_PAGE))
            return
        if not self.confirm_discard():
            return
        path = Path(tempfile.gettempdir()) / update.INSTALLER_NAME
        self.update_button.setEnabled(False)
        self.updates.download(self.newer["installer_url"], path)

    def _on_update_failed(self, error):
        self.update_button.setEnabled(True)
        self.update_button.setText("Update available")
        self.statusBar().showMessage(f"Couldn't download the update: {error}")

    def _run_installer(self, path):
        if not QProcess.startDetached(str(path), ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]):
            self._on_update_failed("the installer wouldn't start")
            return
        if self.cuts:
            self.cuts.dirty = False  # already saved or discarded when the button was clicked
        self.close()

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
        try:
            clips, failed = scan_with_worker(folder, lambda n, total, name: self.statusBar().showMessage(
                f"Reading clip {n} of {total}: {name}"))
        except RuntimeError as e:
            self.statusBar().showMessage(str(e))
            return False
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
        self.fast_label.setText(f"Preparing fast playback: 0 of {len(clips)}")
        cache = QStandardPaths.writableLocation(QStandardPaths.CacheLocation)
        self.loader = BackgroundLoader(clips, cache, proxies=not os.environ.get("UGC_NO_PROXIES"), parent=self)
        self.loader.loaded.connect(lambda i, env: self.timeline.set_envelope(clips[i], env))
        self.proxies_done = 0
        self.loader.proxy_ready.connect(self._on_proxy)
        self.loader.merged_ready.connect(self._on_merged)
        self.loader.proxy_failed.connect(lambda i, error: self.statusBar().showMessage(
            f"Couldn't make a fast copy of {clips[i].name}, so it will play slower: {error}"))
        self._refresh()
        self.statusBar().showMessage(message)
        return True

    def _stop_loader(self):
        if self.loader:
            self.loader.stop()
            self.loader.setParent(None)
            self.loader = None

    def _on_proxy(self, index, path):
        self.player.add_proxy(index, path)
        self.proxies_done += 1
        total = len(self.cuts.clips)
        self.fast_label.setText(f"Preparing fast playback: {self.proxies_done} of {total}")

    def _on_merged(self, path):
        self.player.use_merged(path)
        self.fast_label.setText("Fast playback ready")

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
