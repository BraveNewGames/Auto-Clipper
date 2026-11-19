"""Intersect session timeline windows with individual recording parts."""
from dataclasses import dataclass
from pathlib import Path

from .srt import parse_srt


@dataclass
class LocalRange:
    pov_id: str
    pov_name: str
    part_id: str
    part_index: int
    local_start: float
    local_end: float
    session_start: float
    session_end: float
    clipped_start: bool
    clipped_end: bool
    piece: int
    pieces: int
    duration_unknown: bool


def session_to_local(session, part_id, t) -> float:
    return t - session.part_by_id(part_id).offset


def local_to_session(session, part_id, t) -> float:
    return t + session.part_by_id(part_id).offset


def _estimated_duration(session, part):
    if not part.srt_path:
        return None
    try:
        cues = parse_srt(Path(session.folder) / part.srt_path)
    except OSError:
        return None
    return max((cue.local_end for cue in cues), default=None)


def map_range(session, start, end, pad_before=None, pad_after=None) -> list[LocalRange]:
    if end <= start:
        return []
    padded_start = start - (session.pad_before if pad_before is None else pad_before)
    padded_end = end + (session.pad_after if pad_after is None else pad_after)
    ranges = []
    for pov in session.povs:
        pieces = []
        for part in sorted(pov.parts, key=lambda item: (item.offset, item.index)):
            if part.offset >= end:
                break
            duration = part.duration
            unknown = duration is None or part.duration_source == "srt_estimate"
            if duration is None:
                duration = _estimated_duration(session, part)
            if duration is None:
                continue
            part_end = part.offset + duration
            if min(end, part_end) <= max(start, part.offset):
                continue
            first = max(padded_start, part.offset)
            last = min(padded_end, part_end)
            if last > first:
                pieces.append(LocalRange(
                    pov.pov_id, pov.name, part.part_id, part.index,
                    first - part.offset, last - part.offset, first, last,
                    first > padded_start, last < padded_end, len(pieces) + 1, 0, unknown,
                ))
        for piece in pieces:
            piece.pieces = len(pieces)
        ranges.extend(pieces)
    return ranges

