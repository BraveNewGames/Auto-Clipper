"""Read container durations with ffprobe when available."""
import json
import math
from pathlib import Path
from dataclasses import dataclass
import shutil
import struct
import subprocess

FFMPEG_BIN = Path("C:/Program Files/FFMPEG/5.1.2/bin")


@dataclass(frozen=True)
class RecordingSource:
    path: Path | None
    status: str  # found, stale_mkv, or missing


def resolve_recording(folder, reference, *, require_mp4=False) -> RecordingSource:
    """Resolve a saved recording reference without changing the session."""
    if not reference:
        return RecordingSource(None, "missing")
    saved = Path(folder) / reference
    if saved.is_file():
        if require_mp4 and saved.suffix.lower() == ".mkv":
            remux = saved.with_suffix(".mp4")
            return RecordingSource(remux, "found") if remux.is_file() else RecordingSource(None, "missing")
        return RecordingSource(saved, "found")
    if saved.suffix.lower() == ".mkv":
        mp4 = saved.with_suffix(".mp4")
        if mp4.is_file():
            return RecordingSource(mp4, "stale_mkv")
    return RecordingSource(None, "missing")


def probe_duration(path) -> float | None:
    executable = shutil.which("ffprobe") or FFMPEG_BIN / "ffprobe.exe"
    try:
        result = subprocess.run(
            [str(executable), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(Path(path).resolve())],
            capture_output=True, text=True, check=True, timeout=20,
        )
        duration = float(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return duration if math.isfinite(duration) and duration > 0 else None



def probe_streams(path) -> list[dict]:
    """Return ffprobe's MP4 stream inventory, or an empty list on failure."""
    executable = shutil.which("ffprobe") or FFMPEG_BIN / "ffprobe.exe"
    try:
        result = subprocess.run(
            [str(executable), "-v", "error", "-show_streams", "-of", "json",
             str(Path(path).resolve())],
            capture_output=True, encoding="utf-8", errors="replace", check=True, timeout=20,
        )
        streams = json.loads(result.stdout).get("streams", [])
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    # Older ffprobe releases omit OBS track titles stored in trak/udta/name.
    # Join the container names to ffprobe's inventory by track ID.
    if any(stream.get("codec_type") == "audio" for stream in streams):
        titles = _mp4_track_titles(path)
        for stream in streams:
            try:
                track_id = int(str(stream.get("id")), 0)
            except ValueError:
                continue
            if title := titles.get(track_id):
                stream.setdefault("tags", {}).setdefault("title", title)
    return streams


def _mp4_boxes(handle, start, end):
    """Yield immediate ISO BMFF box type and payload bounds."""
    while start + 8 <= end:
        handle.seek(start)
        header = handle.read(8)
        if len(header) != 8:
            return
        size, kind = struct.unpack(">I4s", header)
        header_size = 8
        if size == 1:
            extended = handle.read(8)
            if len(extended) != 8:
                return
            size = struct.unpack(">Q", extended)[0]
            header_size = 16
        elif size == 0:
            size = end - start
        if size < header_size or start + size > end:
            return
        yield kind, start + header_size, start + size
        start += size


def _mp4_track_titles(path):
    titles = {}
    try:
        with Path(path).open("rb") as handle:
            end = handle.seek(0, 2)
            for kind, first, last in _mp4_boxes(handle, 0, end):
                if kind != b"moov":
                    continue
                for kind, left, right in _mp4_boxes(handle, first, last):
                    if kind != b"trak":
                        continue
                    track_id, title = None, None
                    for kind, lo, hi in _mp4_boxes(handle, left, right):
                        if kind == b"tkhd" and hi - lo >= 16:
                            handle.seek(lo)
                            version = handle.read(1)
                            offset = {b"\x00": 12, b"\x01": 20}.get(version)
                            if offset is not None and lo + offset + 4 <= hi:
                                handle.seek(lo + offset)
                                track_id = int.from_bytes(handle.read(4), "big")
                        elif kind == b"udta":
                            for child, begin, finish in _mp4_boxes(handle, lo, hi):
                                if child == b"name" and 0 < finish - begin <= 1024:
                                    handle.seek(begin)
                                    title = handle.read(finish - begin).decode("utf-8", errors="replace").strip("\x00 ")
                    if track_id and title:
                        titles[track_id] = title
    except OSError:
        return {}
    return titles
