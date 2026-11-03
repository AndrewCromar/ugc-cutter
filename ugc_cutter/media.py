"""Reading clips: finding them in a folder, filming order, duration and the audio waveform."""
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

import av
import numpy as np

from .model import Clip

VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v"}
ENVELOPE_RATE = 100  # waveform points per second
FILENAME_TIME = re.compile(r"(\w{3} \d{1,2} \d{4}), (\d{1,2}) (\d{2}) (\d{2}) ([AP]M)")


def _parse_iso(value):
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def filming_time(path, container):
    """When a clip was filmed: the camera's own date, then the date in the name, then the file's date."""
    tags = dict(container.metadata)
    for stream in container.streams:
        for key, value in stream.metadata.items():
            tags.setdefault(key, value)
    for key in ("com.apple.quicktime.creationdate", "creation_time"):
        t = _parse_iso(tags.get(key))
        if t:
            return t.timestamp()
    m = FILENAME_TIME.search(path.stem)
    if m:
        text = f"{m[1]} {m[2]}:{m[3]}:{m[4]} {m[5]}"
        return datetime.strptime(text, "%b %d %Y %I:%M:%S %p").replace(tzinfo=timezone.utc).timestamp()
    return path.stat().st_mtime


def _duration(container):
    if container.duration:
        return container.duration / av.time_base
    v = container.streams.video[0]
    return float(v.duration * v.time_base) if v.duration else 0.0


def scan_folder(folder, progress=None):
    """Every readable video in the folder (not subfolders) in filming order, plus any that failed."""
    found, failed = [], []
    paths = sorted(p for p in Path(folder).iterdir()
                   if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS and not p.name.startswith("."))
    for n, path in enumerate(paths, 1):
        if progress:
            progress(n, len(paths), path.name)
        try:
            with av.open(str(path)) as container:
                if not container.streams.video:
                    raise ValueError("no video in the file")
                duration = _duration(container)
                if duration <= 0:
                    raise ValueError("the file has no length")
                found.append((filming_time(path, container), path.name, Clip(path, duration)))
        except Exception as e:  # e.g. a clip Dropbox is still downloading
            failed.append((path.name, str(e)))
    found.sort(key=lambda row: row[:2])
    return [row[2] for row in found], failed


def read_envelope(clip):
    """Loudness peaks at ENVELOPE_RATE points a second, from 0 to 1. Silent if the clip has no audio."""
    points = int(clip.duration * ENVELOPE_RATE) + 1
    env = np.zeros(points, dtype=np.float32)
    with av.open(str(clip.path)) as container:
        if not container.streams.audio:
            return env
        stream = container.streams.audio[0]
        rate = 4000
        step = rate // ENVELOPE_RATE
        resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
        parts = []
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                parts.append(out.to_ndarray().ravel())
        for out in resampler.resample(None):
            parts.append(out.to_ndarray().ravel())
    if not parts:
        return env
    samples = np.abs(np.concatenate(parts).astype(np.float32)) / 32768.0
    count = min(points, len(samples) // step)
    if count:
        env[:count] = samples[:count * step].reshape(count, step).max(axis=1)
    return env


def cached_envelope(clip, cache_dir):
    """The waveform, kept outside the footage folder so that folder only ever gains cuts.txt."""
    stat = clip.path.stat()
    key = hashlib.sha1(f"{clip.name}|{stat.st_size}|{stat.st_mtime_ns}".encode()).hexdigest()
    path = Path(cache_dir) / f"{key}.npy"
    if path.exists():
        try:
            return np.load(path)
        except (OSError, ValueError):
            pass
    env = read_envelope(clip)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, env)
    return env
