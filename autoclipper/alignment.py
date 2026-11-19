"""Explicit offsets, duration-based chaining and coverage diagnostics."""
from .models import parse_ts


def set_offset(session, part_id, offset, source="manual", confidence=None):
    part = session.part_by_id(part_id)
    part.offset = parse_ts(offset)
    part.offset_source = source
    part.offset_confidence = confidence


def chain_parts(session, pov_id=None) -> None:
    for pov in session.povs:
        if pov_id is not None and pov.pov_id != pov_id:
            continue
        next_offset = None
        for index, part in enumerate(sorted(pov.parts, key=lambda item: item.index)):
            anchored = index == 0 or part.offset_source == "manual"
            if not anchored and next_offset is not None:
                set_offset(session, part.part_id, next_offset, "chained")
                anchored = True
            next_offset = (part.offset + part.duration
                           if anchored and part.duration is not None else None)


def coverage_report(session) -> dict:
    povs = {}
    all_intervals = []
    for pov in session.povs:
        intervals = []
        unknown = []
        for part in pov.parts:
            if part.duration is None:
                unknown.append(part.part_id)
            elif part.duration > 0:
                intervals.append((part.offset, part.offset + part.duration))
        covered, gaps, overlaps = [], [], []
        for start, end in sorted(intervals):
            if covered and start <= covered[-1][1]:
                prior_end = covered[-1][1]
                if start < prior_end:
                    overlaps.append([start, min(end, prior_end)])
                covered[-1][1] = max(end, prior_end)
            else:
                if covered:
                    gaps.append([covered[-1][1], start])
                covered.append([start, end])
        all_intervals.extend(covered)
        povs[pov.pov_id] = {"covered": covered, "gaps": gaps, "overlaps": overlaps,
                            "unknown_durations": unknown}
    span = ([min(pair[0] for pair in all_intervals), max(pair[1] for pair in all_intervals)]
            if all_intervals else None)
    return {"povs": povs, "session_span": span}

