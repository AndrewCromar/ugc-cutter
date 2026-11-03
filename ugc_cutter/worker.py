"""All the reading and converting of video happens here, in a separate process.

The window's video player and the av library each bring their own copy of FFmpeg. Using both in one
process can freeze it, and a separate process also keeps the window responsive during heavy work.

The worker reports by adding JSON lines to a file, because a windowed Windows app has no usable output.
"""
import json
import sys
from pathlib import Path

from .media import cached_envelope, make_merged, make_proxy, prune_cache, scan_folder
from .model import Clip


def _writer(events_path):
    handle = open(events_path, "a", encoding="utf-8")

    def emit(**event):
        handle.write(json.dumps(event) + "\n")
        handle.flush()
    return emit


def scan(folder, events_path):
    emit = _writer(events_path)
    clips, failed = scan_folder(folder, lambda n, total, name: emit(kind="progress", n=n, total=total, name=name))
    emit(kind="scanned", clips=[{"path": str(c.path), "duration": c.duration} for c in clips], failed=failed)


def prepare(job_path, events_path):
    emit = _writer(events_path)
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    cache = Path(job["cache"])
    prune_cache(cache / "waveforms")
    prune_cache(cache / "proxies")
    clips = []
    t = 0.0
    for row in job["clips"]:
        clips.append(Clip(Path(row["path"]), row["duration"], t))
        t += row["duration"]
    for i, clip in enumerate(clips):
        try:
            cached_envelope(clip, cache / "waveforms")
            emit(kind="waveform", index=i, path=str(_waveform_path(clip, cache)))
        except Exception as e:  # a waveform is only a help, so carry on without this one
            emit(kind="waveform_failed", index=i, error=str(e))
    if not job.get("proxies", True):
        return
    made = []
    for i, clip in enumerate(clips):
        try:
            path = make_proxy(clip, cache / "proxies")
            made.append(path)
            emit(kind="proxy", index=i, path=str(path))
        except Exception as e:  # playback still works from the original clip
            emit(kind="proxy_failed", index=i, error=str(e))
    if len(made) == len(clips):
        try:
            merged = make_merged(clips, made, cache / "proxies")
            if merged:
                emit(kind="merged", path=str(merged))
        except Exception as e:  # the separate copies still play
            emit(kind="merge_failed", error=str(e))


def _waveform_path(clip, cache):
    from .media import _cache_key
    return cache / "waveforms" / f"{_cache_key(clip)}.npy"


def main(args):
    if args[0] == "scan":
        scan(args[1], args[2])
    elif args[0] == "prepare":
        prepare(args[1], args[2])
    else:
        raise SystemExit(f"unknown worker job: {args[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
