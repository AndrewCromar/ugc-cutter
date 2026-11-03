"""Real mouse and key events on the real window, with no screen needed."""
import os
import shutil
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QStandardPaths, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ugc_cutter.app import MainWindow
from ugc_cutter.model import CUTS_FILE

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def app():
    QStandardPaths.setTestModeEnabled(True)
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path):
    for f in FIXTURES.glob("*.mov"):
        shutil.copy(f, tmp_path)
    w = MainWindow()
    w.show()
    assert w.open_folder(tmp_path)
    QTest.qWait(300)
    yield w
    w.cuts.dirty = False
    w.close()


def x_at(w, t):
    return QPoint(int(w.timeline.x_of(t)), w.timeline.height() // 2)


def test_two_clips_load_end_to_end_with_a_line_at_each_end(window):
    assert [c.name for c in window.cuts.clips] == ["z-filmed-first.mov", "a-filmed-second.mov"]
    assert [clip_end for _, clip_end in window.cuts.lines()] == [True, True]
    assert all(s.keep for s in window.cuts.sections)


def test_left_click_moves_the_playhead_even_into_the_next_clip(window):
    QTest.mouseClick(window.timeline, Qt.LeftButton, pos=x_at(window, 3.0))
    QTest.qWait(300)
    assert abs(window.player.time - 3.0) < 0.05


def test_right_click_places_a_line_and_right_clicking_it_removes_it(window):
    QTest.mouseClick(window.timeline, Qt.RightButton, pos=x_at(window, 1.0))
    assert len(window.cuts.sections) == 3
    QTest.mouseClick(window.timeline, Qt.RightButton, pos=x_at(window, 1.0))
    assert len(window.cuts.sections) == 2


def test_shift_right_click_turns_one_section_red_and_back(window):
    QTest.mouseClick(window.timeline, Qt.RightButton, pos=x_at(window, 1.0))
    QTest.mouseClick(window.timeline, Qt.RightButton, Qt.ShiftModifier, x_at(window, 0.5))
    assert [s.keep for s in window.cuts.sections] == [False, True, True]
    QTest.mouseClick(window.timeline, Qt.RightButton, Qt.ShiftModifier, x_at(window, 0.5))
    assert [s.keep for s in window.cuts.sections] == [True, True, True]


def test_play_shows_frames_and_carries_on_into_the_next_clip(window):
    frames = []
    window.video.videoSink().videoFrameChanged.connect(lambda f: f.isValid() and frames.append(f.startTime()))
    window.player.seek(1.5)
    QTest.qWait(200)
    window.toggle_play()
    assert window.player.playing
    QTest.qWait(1500)
    assert window.player.time > 2.2  # past the end of the first clip, which is 2 s long
    assert frames
    window.toggle_play()
    assert not window.player.playing


def test_skip_red_jumps_over_a_red_section(window):
    window.cuts.toggle(0.5)  # the whole first clip is red
    window.skip_box.setChecked(True)
    window.player.seek(0.0)
    QTest.qWait(200)
    window.toggle_play()
    QTest.qWait(700)
    assert window.player.time >= 2.0
    window.toggle_play()


def test_save_writes_the_file_and_reopening_brings_the_cuts_back(window, tmp_path):
    QTest.mouseClick(window.timeline, Qt.RightButton, pos=x_at(window, 1.0))
    QTest.mouseClick(window.timeline, Qt.RightButton, Qt.ShiftModifier, x_at(window, 0.5))
    assert window.cuts.dirty
    QTest.mouseClick(window.save_button, Qt.LeftButton)
    assert not window.cuts.dirty
    saved = (tmp_path / CUTS_FILE).read_text(encoding="utf-8")
    assert "cut\tz-filmed-first.mov\t0.000\t" in saved
    assert window.open_folder(tmp_path)
    assert (tmp_path / CUTS_FILE).read_text(encoding="utf-8") == window.cuts.to_text()


def test_only_cuts_txt_is_added_to_the_folder(window, tmp_path):
    window.save()
    QTest.qWait(300)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a-filmed-second.mov", CUTS_FILE, "z-filmed-first.mov"]


def test_wheel_zoom_keeps_the_time_under_the_pointer(window):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QWheelEvent
    pos = QPointF(window.timeline.width() / 2, 50)
    before = window.timeline.t_of(pos.x())
    event = QWheelEvent(pos, window.timeline.mapToGlobal(pos), QPoint(0, 0), QPoint(0, 480), Qt.NoButton, Qt.NoModifier,
                        Qt.NoScrollPhase, False)
    QApplication.sendEvent(window.timeline, event)
    assert window.timeline.px_per_sec > window.width() / window.cuts.total * 1.5
    assert abs(window.timeline.t_of(pos.x()) - before) < 0.01


def wait_for_fast_playback(window, seconds=30):
    for _ in range(seconds * 20):
        if window.player.merged:
            QTest.qWait(200)
            return
        QTest.qWait(50)
    raise AssertionError("fast playback never became ready")


def test_fast_copy_swaps_in_and_timeline_positions_still_land_right(window):
    window.player.seek(3.0)
    wait_for_fast_playback(window)
    assert window.fast_label.text() == "Fast playback ready"
    assert abs(window.player.time - 3.0) < 0.05
    assert abs(window.player.media.position() / 1000 - 3.0) < 0.1  # 1 s into the second clip
    window.player.seek(1.0)
    QTest.qWait(200)
    assert abs(window.player.media.position() / 1000 - 1.0) < 0.1


def test_fast_copy_plays_across_clips_and_skips_red(window):
    wait_for_fast_playback(window)
    window.cuts.toggle(0.5)  # the whole first clip is red
    window.skip_box.setChecked(True)
    window.player.seek(0.0)
    QTest.qWait(200)
    window.toggle_play()
    QTest.qWait(600)
    assert window.player.time > 2.0
    window.toggle_play()


def test_playing_to_the_very_end_stops_cleanly(window):
    wait_for_fast_playback(window)
    window.player.seek(window.cuts.total - 0.3)
    QTest.qWait(200)
    window.toggle_play()
    QTest.qWait(900)
    assert not window.player.playing


def test_without_fast_copies_it_still_plays_clip_by_clip(app, tmp_path, monkeypatch):
    monkeypatch.setenv("UGC_NO_PROXIES", "1")
    for f in FIXTURES.glob("*.mov"):
        shutil.copy(f, tmp_path)
    w = MainWindow()
    w.show()
    assert w.open_folder(tmp_path)
    QTest.qWait(300)
    w.player.seek(1.5)
    QTest.qWait(300)
    w.toggle_play()
    QTest.qWait(1500)
    assert w.player.clip_index == 1
    assert w.player.merged is None
    w.toggle_play()
    w.cuts.dirty = False
    w.close()
