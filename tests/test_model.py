from pathlib import Path

import pytest

import av

from ugc_cutter.media import make_merged, make_proxy, scan_folder
from ugc_cutter.model import Clip, CutList

FIXTURES = Path(__file__).parent / "fixtures"


def two_clips():
    return CutList([Clip(Path("a.mov"), 10.0), Clip(Path("b.mov"), 5.0)])


def test_opens_with_a_line_at_each_clip_end_and_all_green():
    c = two_clips()
    assert c.total == 15.0
    assert c.lines() == [(10.0, True), (15.0, True)]
    assert all(s.keep for s in c.sections)
    assert not c.dirty


def test_cut_splits_a_section_and_both_sides_keep_its_colour():
    c = two_clips()
    c.toggle(2.0)
    assert c.add_cut(4.0)
    assert [(s.start, s.end, s.keep) for s in c.sections[:2]] == [(0.0, 4.0, False), (4.0, 10.0, False)]
    assert c.dirty


def test_toggle_only_changes_the_section_clicked():
    c = two_clips()
    c.add_cut(4.0)
    c.toggle(5.0)
    assert [s.keep for s in c.sections] == [True, False, True]
    c.toggle(5.0)
    assert [s.keep for s in c.sections] == [True, True, True]


def test_a_placed_line_can_be_removed_but_a_clip_end_cannot():
    c = two_clips()
    c.add_cut(4.0)
    assert c.removable_line_near(4.05, 0.1) == 4.0
    assert c.removable_line_near(10.0, 0.5) is None
    assert c.remove_cut(4.0)
    assert not c.remove_cut(10.0)
    assert len(c.sections) == 2


def test_undo_and_redo():
    c = two_clips()
    c.add_cut(4.0)
    c.toggle(1.0)
    assert c.undo() and c.sections[0].keep
    assert c.undo() and len(c.sections) == 2
    assert not c.undo()
    assert c.redo() and len(c.sections) == 3
    assert c.redo() and not c.sections[0].keep


def test_file_uses_times_within_each_clip():
    c = two_clips()
    c.add_cut(12.0)
    c.toggle(11.0)
    assert c.to_text().splitlines()[1:] == ["keep\ta.mov\t0.000\t10.000", "cut\tb.mov\t0.000\t2.000",
                                            "keep\tb.mov\t2.000\t5.000"]


def test_saved_work_comes_back_exactly():
    c = two_clips()
    c.add_cut(3.25)
    c.toggle(1.0)
    c.add_cut(12.5)
    c.toggle(13.0)
    again = two_clips()
    again.load_text(c.to_text())
    assert again.to_text() == c.to_text()
    assert not again.dirty


def test_a_clip_missing_from_the_file_stays_green():
    c = two_clips()
    c.load_text("cut\ta.mov\t0.000\t10.000\n")
    assert [(s.start, s.end, s.keep) for s in c.sections] == [(0.0, 10.0, False), (10.0, 15.0, True)]


def test_a_broken_file_is_reported_not_guessed_at():
    with pytest.raises(ValueError):
        two_clips().load_text("hello\n")


def test_next_kept_start_skips_red():
    c = two_clips()
    c.add_cut(4.0)
    c.toggle(1.0)
    assert c.next_kept_start(1.0) == 4.0
    c.toggle(5.0)
    assert c.next_kept_start(1.0) == 10.0


def test_clips_come_back_in_filming_order_not_name_order():
    clips, failed = scan_folder(FIXTURES)
    assert failed == []
    assert [c.name for c in clips] == ["z-filmed-first.mov", "a-filmed-second.mov"]


def test_fast_copies_stand_upright_and_join_with_every_clip_in_its_place(tmp_path):
    clips, _ = scan_folder(FIXTURES)
    cuts = CutList(clips)
    proxies = [make_proxy(c, tmp_path) for c in clips]
    merged = make_merged(clips, proxies, tmp_path)
    with av.open(str(merged)) as c:
        v = c.streams.video[0]
        assert (v.width, v.height) == (360, 640)  # the 192x108 test clips are stored sideways
        assert abs(c.duration / av.time_base - cuts.total) < 0.1
        times = [float(f.pts * f.time_base) for f in c.decode(video=0)]
    assert times == sorted(times)
    assert any(abs(t - clips[1].offset) < 0.05 for t in times)  # the second clip starts where it should
