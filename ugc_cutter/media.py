"""Reading clips: finding them in a folder, filming order, duration and the audio waveform."""
import hashlib
import os
import re
import time
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

from .model import ENVELOPE_RATE, Clip

VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v"}
PROXY_SHORT_SIDE = 360  # pixels
PROXY_VERSION = 1  # raise this when the settings change, so old copies get remade
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


def _cache_key(clip):
    stat = clip.path.stat()
    return hashlib.sha1(f"{clip.name}|{stat.st_size}|{stat.st_mtime_ns}".encode()).hexdigest()


def _rotation_filters(rotation):
    rotation = int(rotation or 0) % 360
    if rotation == 270:  # phones report -90: turn it clockwise to stand it upright
        return [("transpose", "clock")]
    if rotation == 90:
        return [("transpose", "cclock")]
    if rotation == 180:
        return [("hflip", None), ("vflip", None)]
    return []


def proxy_path(clip, cache_dir):
    return Path(cache_dir) / f"{_cache_key(clip)}-v{PROXY_VERSION}.mov"


def make_proxy(clip, cache_dir, should_stop=lambda: False):
    """A small copy of a clip that seeks instantly: low resolution, a keyframe every few frames, no
    reordered frames. Phone HEVC is slow to jump around in, because reaching any frame means decoding
    from the last keyframe, which can be a second or more back. The sound is copied untouched and the
    picture keeps its original timestamps, so the copy lines up with the original exactly.
    Returns the path, or None if it was stopped."""
    final = proxy_path(clip, cache_dir)
    if final.exists():
        return final
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(final.stem + f".{os.getpid()}.part.mov")
    with av.open(str(clip.path)) as peek:  # the picture size has to be known before anything is written
        first_frame = next(peek.decode(video=0))
        size = _proxy_size(first_frame)
    source = av.open(str(clip.path))
    output = av.open(str(tmp), "w", format="mov")
    finished = False
    try:
        video_in = source.streams.video[0]
        video_in.thread_type = "AUTO"
        audio_in = source.streams.audio[0] if source.streams.audio else None
        rate = video_in.average_rate or Fraction(30)
        video_out = output.add_stream("libx264", rate=rate, options={
            "preset": "ultrafast", "crf": "27", "g": "6", "bf": "0", "tune": "fastdecode"})
        video_out.time_base = video_in.time_base
        video_out.codec_context.time_base = video_in.time_base
        video_out.pix_fmt = "yuv420p"
        video_out.width, video_out.height = size
        audio_out = output.add_stream_from_template(audio_in) if audio_in else None

        graph = None
        streams = [s for s in (video_in, audio_in) if s is not None]
        for packet in source.demux(streams):
            if should_stop():
                return None
            if audio_in is not None and packet.stream is audio_in:
                if packet.dts is not None:
                    packet.stream = audio_out
                    output.mux(packet)
                continue
            for frame in packet.decode():
                if graph is None:
                    graph, sink = _proxy_graph(frame, video_in)
                graph.vpush(frame)
                while True:
                    try:
                        filtered = sink.pull()
                    except (av.error.BlockingIOError, av.error.EOFError):
                        break
                    filtered.time_base = video_in.time_base
                    output.mux(video_out.encode(filtered))
        if graph is not None:
            output.mux(video_out.encode(None))
        finished = True
    finally:
        output.close()
        source.close()
        if not finished:
            tmp.unlink(missing_ok=True)
    tmp.replace(final)
    return final


def _proxy_size(frame):
    """Upright width and height, with the short side at PROXY_SHORT_SIDE."""
    w, h = frame.width, frame.height
    if int(frame.rotation or 0) % 180:
        w, h = h, w
    scale = PROXY_SHORT_SIDE / min(w, h)
    return 2 * round(w * scale / 2), 2 * round(h * scale / 2)


def _proxy_graph(frame, stream):
    graph = av.filter.Graph()
    last = graph.add_buffer(template=stream)
    for name, argument in _rotation_filters(frame.rotation):
        node = graph.add(name, argument) if argument else graph.add(name)
        last.link_to(node)
        last = node
    w, h = _proxy_size(frame)
    scale = graph.add("scale", f"{w}:{h}:flags=fast_bilinear")
    last.link_to(scale)
    fmt = graph.add("format", "yuv420p")
    scale.link_to(fmt)
    sink = graph.add("buffersink")
    fmt.link_to(sink)
    graph.configure()
    return graph, sink


def make_merged(clips, proxies, cache_dir):
    """All the fast copies joined into one file, each starting at its place on the timeline.

    Switching between files is slow (over a second), and jumping within a file is not (tens of ms).
    With one file, playing across clips is seamless and every jump, including over red, is a quick one.
    Returns None if the clips can't share a file, such as a mix of portrait and landscape: the app then
    plays the copies one at a time."""
    key = hashlib.sha1("|".join(p.name for p in proxies).encode()).hexdigest()
    final = Path(cache_dir) / f"merged-{key}.mov"
    if final.exists():
        return final
    sizes = set()
    for path in proxies:
        with av.open(str(path)) as c:
            sizes.add((c.streams.video[0].width, c.streams.video[0].height, bool(c.streams.audio)))
    if len(sizes) != 1:
        return None
    tmp = final.with_name(final.stem + f".{os.getpid()}.part.mov")
    output = av.open(str(tmp), "w", format="mov")
    try:
        outs = {}
        for clip, path in zip(clips, proxies):
            with av.open(str(path)) as source:
                streams = list(source.streams.video[:1]) + list(source.streams.audio[:1])
                for stream in streams:
                    if stream.type not in outs:
                        outs[stream.type] = (output.add_stream_from_template(stream), -1)
                for packet in source.demux(streams):
                    if packet.dts is None:  # the empty packet that ends each stream
                        continue
                    kind = packet.stream.type
                    out, last = outs[kind]
                    shift = int(round(clip.offset / packet.time_base))
                    packet.dts += shift
                    packet.pts += shift
                    if packet.dts <= last:  # a clip's last packets can run a hair past the next clip's start
                        continue
                    outs[kind] = (out, packet.dts)
                    packet.stream = out
                    output.mux(packet)
    except Exception:
        output.close()
        tmp.unlink(missing_ok=True)
        raise
    output.close()
    tmp.replace(final)
    return final


def prune_cache(cache_dir, days=21):
    """Fast-playback copies are easy to remake, so old ones don't stay on the disk."""
    cutoff = time.time() - days * 86400
    for path in Path(cache_dir).glob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def cached_envelope(clip, cache_dir):
    """The waveform, kept outside the footage folder so that folder only ever gains cuts.txt."""
    path = Path(cache_dir) / f"{_cache_key(clip)}.npy"
    if path.exists():
        try:
            return np.load(path)
        except (OSError, ValueError):
            pass
    env = read_envelope(clip)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, env)
    return env
