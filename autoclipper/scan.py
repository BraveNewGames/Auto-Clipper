"""Discover recording parts in a flat session folder."""
from collections import defaultdict
from pathlib import Path
import re

from .media import probe_duration
from .models import Part, POV, Session

_VIDEO = {".mkv", ".mp4", ".mov", ".flv", ".ts", ".avi", ".webm"}
_PART = re.compile(r"^(.+?)[ _.-]+part[ _.-]*(\d+)$", re.I)
_OBS = re.compile(r"^(.+?)[ _.-]+(\d{4}-\d{2}-\d{2}[ _T]\d{2}[-:]\d{2}[-:]\d{2})$", re.I)


def scan_folder(folder) -> Session:
    folder = Path(folder).resolve()
    pairs = defaultdict(dict)
    for path in sorted(folder.iterdir(), key=lambda p: (p.name.casefold(), p.name)):
        suffix = path.suffix.lower()
        if path.is_file() and (suffix == ".srt" or suffix in _VIDEO):
            pairs[path.stem.casefold()].setdefault("srt" if suffix == ".srt" else "video", path)
    grouped = defaultdict(list)
    for files in pairs.values():
        stem = (files.get("video") or files["srt"]).stem
        numbered = _PART.match(stem)
        obs = _OBS.match(stem)
        name = (numbered or obs).group(1).strip() if numbered or obs else stem
        index = int(numbered.group(2)) if numbered else None
        stamp = obs.group(2).replace("T", " ").replace("_", " ") if obs else ""
        grouped[name.casefold()].append((name, index, stamp, stem, files))

    session = Session(session_id=folder.name, event_prefix=folder.name.split("-")[0].upper(),
                      folder=str(folder))
    used_ids = set()
    for key, entries in sorted(grouped.items()):
        pov_id = re.sub(r"[^\w]+", "-", key).strip("-") or "pov"
        base = pov_id
        serial = 2
        while pov_id in used_ids:
            pov_id = f"{base}-{serial}"
            serial += 1
        used_ids.add(pov_id)
        pov = POV(pov_id, entries[0][0])
        reserved = {index for _, index, _, _, _ in entries if index is not None}
        used = set()
        for _, index, _, _, files in sorted(entries, key=lambda e: (e[1] is None, e[1] or 0, e[2], e[3].casefold())):
            if index is None or index in used:
                index = 1
                while index in reserved or index in used:
                    index += 1
            used.add(index)
            video, srt = files.get("video"), files.get("srt")
            duration = probe_duration(video) if video else None
            pov.parts.append(Part(
                f"{pov_id}-p{index}", index, video.name if video else None,
                srt.name if srt else None, duration, "ffprobe" if duration is not None else None,
            ))
        pov.parts.sort(key=lambda part: part.index)
        session.povs.append(pov)
    return session

