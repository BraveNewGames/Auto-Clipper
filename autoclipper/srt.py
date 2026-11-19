"""Parse Whisper SRT files without discarding overlapping cues."""
from dataclasses import dataclass
from pathlib import Path
import re

from .models import parse_ts


@dataclass
class Cue:
    part_id: str
    pov_id: str
    index: int
    local_start: float
    local_end: float
    label: str | None
    speaker: str | None
    channel: str
    text: str


_TEAM = re.compile(r"^(\[team\]|\(team\)|team)(?:\s*:\s*|\s+|$)", re.I)
_NAMED = re.compile(r"^([^\W\d_][\w .'-]{0,39}):\s*", re.UNICODE)
_TIME = re.compile(r"^(\d+:\d{2}:\d{2}[,.]\d+)\s*-->\s*(\d+:\d{2}:\d{2}[,.]\d+)(?:\s|$)")


def split_label(line):
    line = line.strip()
    match = _TEAM.match(line)
    if match:
        return match[1], "team", None, line[match.end():]
    match = _NAMED.match(line)
    if match:
        name = match[1].strip()
        return name, "named", name, line[match.end():]
    return None, "unlabeled", None, line


def parse_srt_text(text, part_id="", pov_id="") -> list[Cue]:
    cues = []
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    for block in re.split(r"\n[ \t]*\n", text.strip()):
        lines = block.splitlines()
        if not lines:
            continue
        index = int(lines.pop(0).strip()) if lines[0].strip().isdecimal() else len(cues) + 1
        match = _TIME.match(lines[0].strip()) if lines else None
        if not match or len(lines) < 2:
            continue
        try:
            start, end = map(parse_ts, match.groups())
        except ValueError:
            continue
        if end <= start:
            continue
        label, channel, speaker, first = split_label(lines[1])
        content = "\n".join([first, *lines[2:]]).strip()
        cues.append(Cue(part_id, pov_id, index, start, end, label, speaker, channel, content))
    return cues


def parse_srt(path, part_id="", pov_id="") -> list[Cue]:
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    return parse_srt_text(text, part_id, pov_id)

