"""Plan reviewer-specific, non-overlapping LosslessCut CSV passes."""
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata

from .mapping import map_range

LOSSLESSCUT_SETUP = """Open the source video listed in the reviewer's README.
Choose File > Import project > CSV and select one pass for that video.
Export segments as separate files and set the output name template to
${SEG_LABEL}${EXT}
Switch to the next pass by replacing the imported segments, then repeat.
"""


@dataclass
class ExportFile:
    reviewer: str
    pov_name: str
    part_id: str
    video_path: str | None
    pass_no: int
    csv_path: str
    rows: list[tuple[float, float, str]]


def _safe(value, limit=120):
    cleaned = "".join(ch for ch in value if ch not in '<>:"/\\|?*'
                      and not unicodedata.category(ch).startswith("C"))
    cleaned = " ".join(cleaned.split())[:limit].rstrip(". ") or "Untitled"
    if re.fullmatch(r"CON|PRN|AUX|NUL|(?:COM|LPT)[1-9\u00b9\u00b2\u00b3]", cleaned.split(".")[0], re.I):
        cleaned = "_" + cleaned[:limit - 1]
    return cleaned


def segment_label(event_id, title, pov_name, piece=1, pieces=1, reviewer_tag=None) -> str:
    prefix = f"{_safe(event_id, 24)} - "
    suffix = f" - {_safe(pov_name, 30)}"
    if pieces > 1:
        suffix += f" ({piece} of {pieces})"
    if reviewer_tag:
        suffix += f" [{_safe(reviewer_tag, 24)}]"
    return prefix + _safe(title, max(1, 120 - len(prefix) - len(suffix))) + suffix


def _times(item):
    if hasattr(item, "local_start"):
        return item.local_start, item.local_end
    return item[0], item[1]


def assign_passes(ranges) -> list[list]:
    passes = []
    ends = []
    for item in sorted(ranges, key=_times):
        start, end = _times(item)
        for index, prior_end in enumerate(ends):
            if prior_end <= start:
                passes[index].append(item)
                ends[index] = end
                break
        else:
            passes.append([item])
            ends.append(end)
    return passes


def _plan(session, reviewer, statuses):
    selected = {None, "keep", "maybe"} if statuses is None else set(statuses)
    event_reviewers = defaultdict(set)
    for suggestion in session.suggestions:
        if suggestion.event_id:
            event_reviewers[suggestion.event_id].add(suggestion.reviewer)
    groups = {}
    used_labels = set()
    for suggestion in session.suggestions:
        if suggestion.reviewer != reviewer or suggestion.kind != "clip_now":
            continue
        review = session.reviews.get(suggestion.suggestion_id)
        if (review.status if review else None) not in selected:
            continue
        start = review.approved_start if review and review.approved_start is not None else suggestion.start
        end = review.approved_end if review and review.approved_end is not None else suggestion.end
        tag = None
        if len(event_reviewers[suggestion.event_id]) > 1:
            tag = suggestion.reviewer_name or reviewer
        for local in map_range(session, start, end):
            label = segment_label(suggestion.event_id or suggestion.suggestion_id,
                                  suggestion.title, local.pov_name,
                                  local.piece, local.pieces, tag)
            base = label
            serial = 2
            while label.casefold() in used_labels:
                suffix = f" [{serial}]"
                label = base[:120 - len(suffix)].rstrip(". ") + suffix
                serial += 1
            used_labels.add(label.casefold())
            groups.setdefault(local.part_id, (local, []))[1].append(
                (local.local_start, local.local_end, label))
    return groups


def export_reviewer(session, reviewer, out_dir=None, statuses=None) -> list[ExportFile]:
    root = Path(out_dir) if out_dir is not None else Path(session.folder) / "autoclipper_exports"
    directory = root / _safe(reviewer, 80)
    groups = _plan(session, reviewer, statuses)
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    manifest = [LOSSLESSCUT_SETUP, "CSV -> source video", ""]
    used_stems = set()
    for part_id, (local, rows) in groups.items():
        part = session.part_by_id(part_id)
        base = f"{_safe(local.pov_name, 80)}_part{local.part_index}"
        stem = base
        serial = 2
        while stem.casefold() in used_stems:
            stem = f"{base}_{serial}"
            serial += 1
        used_stems.add(stem.casefold())
        for number, partition in enumerate(assign_passes(rows), 1):
            path = directory / f"{stem}_pass{number}.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                for start, end, label in partition:
                    handle.write(f'{start:.3f},{end:.3f},"{label}"\r\n')
            source = part.video_path or f"(no video found; transcript: {part.srt_path})"
            manifest.append(f"{path.name} -> {source}")
            files.append(ExportFile(reviewer, local.pov_name, part_id, part.video_path,
                                    number, str(path), partition))
    (directory / "README.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    return files


def export_all(session, out_dir=None, statuses=None) -> list[ExportFile]:
    reviewers = sorted({suggestion.reviewer for suggestion in session.suggestions})
    return [file for reviewer in reviewers
            for file in export_reviewer(session, reviewer, out_dir, statuses)]


def expected_filenames(session, reviewer) -> list[str]:
    names = []
    for part_id, (_, rows) in _plan(session, reviewer, None).items():
        video = session.part_by_id(part_id).video_path
        extension = Path(video).suffix if video else ".mkv"
        names.extend(label + extension for _, _, label in rows)
    return names

