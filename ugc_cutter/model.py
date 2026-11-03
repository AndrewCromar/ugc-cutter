"""The cut list: pure logic with no GUI, so it can be tested on its own.

The timeline is one list of sections in order. Each section is kept (green) or cut (red).
A section never crosses the end of a clip, so clip ends are always lines on the timeline.
"""
from dataclasses import dataclass, replace
from pathlib import Path

CUTS_FILE = "cuts.txt"
HEADER = "# ugc-cutter cuts v1: action<TAB>clip<TAB>start seconds<TAB>end seconds, times are within the clip"
MIN_SECTION = 0.02  # seconds: never make a section shorter than this
ENVELOPE_RATE = 100  # waveform points per second


@dataclass
class Clip:
    path: Path
    duration: float
    offset: float = 0.0  # where this clip starts on the timeline

    @property
    def name(self):
        return self.path.name

    @property
    def end(self):
        return self.offset + self.duration


@dataclass
class Section:
    start: float
    end: float
    keep: bool = True


class CutList:
    def __init__(self, clips):
        self.clips = clips
        t = 0.0
        for c in clips:
            c.offset = t
            t += c.duration
        self.total = t
        self.sections = [Section(c.offset, c.end) for c in clips]
        self._undo, self._redo = [], []
        self.dirty = False

    # --- finding things
    def clip_at(self, t):
        """(clip index, seconds into that clip) for a timeline time."""
        for i, c in enumerate(self.clips):
            if t < c.end:
                return i, max(0.0, t - c.offset)
        last = len(self.clips) - 1
        return last, self.clips[last].duration

    def section_index(self, t):
        for i, s in enumerate(self.sections):
            if t < s.end:
                return i
        return len(self.sections) - 1

    def is_clip_end(self, t):
        return any(abs(c.end - t) < 1e-6 for c in self.clips)

    def lines(self):
        """Every line on the timeline: (time, is it a clip end)."""
        return [(s.end, self.is_clip_end(s.end)) for s in self.sections]

    def removable_line_near(self, t, tolerance):
        """The closest cut line a person placed, within tolerance seconds, or None."""
        best = None
        for s in self.sections:
            d = abs(s.end - t)
            if d <= tolerance and not self.is_clip_end(s.end) and (best is None or d < abs(best - t)):
                best = s.end
        return best

    def next_kept_start(self, t):
        """Where the next kept section starts at or after t, or None if there isn't one."""
        for s in self.sections:
            if s.keep and s.end > t:
                return max(s.start, t)
        return None

    # --- changes
    def _snapshot(self):
        self._undo.append([replace(s) for s in self.sections])
        self._redo.clear()
        self.dirty = True

    def add_cut(self, t):
        i = self.section_index(t)
        s = self.sections[i]
        if t - s.start < MIN_SECTION or s.end - t < MIN_SECTION:
            return False
        self._snapshot()
        self.sections[i:i + 1] = [Section(s.start, t, s.keep), Section(t, s.end, s.keep)]
        return True

    def remove_cut(self, line):
        """Remove a placed line. The joined section takes the colour of the part before the line."""
        for i, s in enumerate(self.sections[:-1]):
            if abs(s.end - line) < 1e-6 and not self.is_clip_end(s.end):
                self._snapshot()
                self.sections[i:i + 2] = [Section(s.start, self.sections[i + 1].end, s.keep)]
                return True
        return False

    def toggle(self, t):
        self._snapshot()
        s = self.sections[self.section_index(t)]
        s.keep = not s.keep

    def undo(self):
        if not self._undo:
            return False
        self._redo.append(self.sections)
        self.sections = self._undo.pop()
        self.dirty = True
        return True

    def redo(self):
        if not self._redo:
            return False
        self._undo.append(self.sections)
        self.sections = self._redo.pop()
        self.dirty = True
        return True

    # --- the text file
    def to_text(self):
        rows = [HEADER]
        for s in self.sections:
            i, _ = self.clip_at(s.start + MIN_SECTION / 4)
            c = self.clips[i]
            rows.append("\t".join(["keep" if s.keep else "cut", c.name,
                                   f"{s.start - c.offset:.3f}", f"{s.end - c.offset:.3f}"]))
        return "\n".join(rows) + "\n"

    def save(self, folder):
        path = Path(folder) / CUTS_FILE
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(self.to_text(), encoding="utf-8", newline="\n")
        tmp.replace(path)
        self.dirty = False
        return path

    def load_text(self, text):
        """Bring back saved work. Clips the file doesn't mention stay fully green."""
        saved = {}
        for line in text.splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) != 4 or parts[0] not in ("keep", "cut"):
                raise ValueError(f"bad line in {CUTS_FILE}: {line!r}")
            saved.setdefault(parts[1], []).append((float(parts[2]), float(parts[3]), parts[0] == "keep"))
        sections = []
        for c in self.clips:
            rows = sorted(saved.get(c.name, []))
            rows = [r for r in rows if r[0] < c.duration]
            if not rows:
                sections.append(Section(c.offset, c.end))
                continue
            starts = [r[0] for r in rows]
            for j, (a, _b, keep) in enumerate(rows):
                # sections must fill the clip with no gaps, whatever the file says
                start = 0.0 if j == 0 else starts[j]
                end = starts[j + 1] if j + 1 < len(rows) else c.duration
                sections.append(Section(c.offset + start, c.offset + end, keep))
        self.sections = sections
        self.dirty = False
