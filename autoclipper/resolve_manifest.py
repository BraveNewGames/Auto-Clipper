"""Build offline Resolve multicam manifests from reviewed selections."""

import argparse
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from fractions import Fraction
import json
from math import floor, isfinite
from pathlib import Path
import re

from .mapping import map_range
from .media import probe_streams
from .models import Part, POV, Session
from .resolve_stage1 import plan_placements
from .store import _write_json, load_session


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _load_confirmations(path):
    return _read_json(path) if path.exists() else {"schema_version": 1, "confirmations": []}


def _metadata(roots, stems):
    for root in roots:
        for stem in stems:
            for path in (root / stem / f"{stem}.json", root / f"{stem}.json"):
                if path.is_file():
                    try:
                        data = _read_json(path)
                    except (OSError, ValueError):
                        return {}
                    return data if isinstance(data, dict) else {}
    return {}


def _track_number(value):
    return type(value) is int and value > 0


def _tracks(metadata):
    tracks = metadata.get("speaker_tracks")
    if isinstance(tracks, list):
        return [item for item in tracks if isinstance(item, dict) and _track_number(item.get("track"))]
    if isinstance(tracks, dict):
        return [
            {"track": int(match[1]), "speaker": speaker}
            for label, speaker in tracks.items()
            if (match := re.fullmatch(r"audio_track_([1-9][0-9]*)", label))
        ]
    return []


def _normalized(value):
    return value.strip().casefold() if isinstance(value, str) else ""


def _recorder_evidence(metadata, track, speaker):
    """Verify the original recorder attribution produced by the transcriber."""
    rows = metadata.get("speaker_tracks")
    recorder = metadata.get("recorder")
    return (
        isinstance(rows, list)
        and len(_tracks(metadata)) == len(rows)
        and all(isinstance(row.get("speaker"), str) for row in rows)
        and metadata.get("original_speaker_tracks") is None
        and metadata.get("relabel_history") is None
        and not any("relabel" in key or "provenance" in key
                    for key in track)
        and not any(("relabel" in key and key != "relabel_history") or "provenance" in key
                    for key in metadata)
        and track.get("speaker_template") == "{Recorder}"
        and isinstance(recorder, dict)
        and _normalized(recorder.get("name")) == _normalized(speaker)
        and recorder.get("source") == "filename"
        and recorder.get("tracks") == [track["track"]]
        and type(track.get("index")) is int
        and bool(_normalized(track.get("title")))
    )

def _audio_mapping(audio, track):
    """Identify an MP4 stream and provide provisional Resolve channel numbers."""
    titles = {_normalized(track.get("title")).replace(" ", ""), f"track{track['track']}"} - {""}
    named = [
        stream for stream in audio
        if any(_normalized(value).replace(" ", "") in titles
               for key, value in stream.get("tags", {}).items()
               if key.casefold() in {"title", "handler_name"})
    ]
    index = track.get("index")
    indexed = [stream for stream in audio if type(index) is int and stream["index"] == index]
    if len(named) > 1 or (named and type(index) is int and named != indexed):
        return None
    candidates = named or indexed
    if len(candidates) != 1:
        return None
    stream = candidates[0]
    tags = stream.get("tags", {})
    title = tags.get("title") or tags.get("handler_name")
    ordinal = audio.index(stream) + 1
    counts = [item.get("channels") for item in audio[:ordinal]]
    if not all(_track_number(count) for count in counts):
        return None
    return {
        "mp4_audio_stream_index": stream["index"],
        "mp4_audio_ordinal": ordinal,
        "mp4_audio_title": title,
        "mp4_audio_channels": counts[-1],
        "expected_owner_track": track["track"],
        "resolve_channels": list(range(sum(counts[:-1]) + 1, sum(counts) + 1)),
        "resolve_channels_status": "expected",
    }


def _mic(metadata, streams, part_id, speaker, confirmations):
    tracks = _tracks(metadata)
    confirmed_tracks = {
        item["transcriber_track"] for item in confirmations
        if isinstance(item, dict) and item.get("part_id") == part_id
        and _track_number(item.get("transcriber_track"))
        and _normalized(item.get("speaker")) == _normalized(speaker)
        and item.get("confirmed_by") == "owner"
    }
    if len(confirmed_tracks) > 1:
        return None, "mic_unverified"
    if confirmed_tracks:
        number = next(iter(confirmed_tracks))
        candidates = [track for track in tracks if track["track"] == number]
        track = candidates[0] if len(candidates) == 1 else {"track": number}
    else:
        candidates = [track for track in tracks
                      if _normalized(track.get("speaker")) == _normalized(speaker)]
        if len(candidates) != 1:
            return None, "mic_unverified"
        track = candidates[0]
        if sum(row["track"] == track["track"] for row in tracks) != 1:
            return None, "mic_unverified"
        if not _recorder_evidence(metadata, track, speaker):
            return None, "mic_unverified"

    audio = sorted((stream for stream in streams if stream.get("codec_type") == "audio"),
                   key=lambda stream: stream["index"])
    mapping = _audio_mapping(audio, track)
    conflict = None
    if mapping is None and confirmed_tracks and set(track) - {"track"}:
        # The owner's listening confirmation is authoritative: when the transcriber's
        # index/title for the confirmed track contradicts the MP4, map the confirmed number
        # against the MP4's own audio inventory only (a stream titled Track<N>).
        mapping = _audio_mapping(audio, {"track": track["track"]})
        if mapping is not None:
            inventory = ", ".join(
                f"#{stream['index']} {(stream.get('tags') or {}).get('title') or '(untitled)'}" for stream in audio)
            conflict = (f"Metadata said track {track['track']} is stream index {track.get('index')} titled "
                        f"{track.get('title')!r}; the MP4 has audio streams {inventory}; owner confirmed track "
                        f"{track['track']} (MP4 stream #{mapping['mp4_audio_stream_index']}).")
    if mapping is None:
        return None, "mic_unmapped"
    mic = {
        "status": "owner_confirmed" if confirmed_tracks else "verified",
        "transcriber_track": track["track"], "speaker": speaker,
        **mapping,
        "evidence": ("owner confirmation + MP4 probe" if confirmed_tracks
                     else "transcriber recorder attribution + MP4 probe"),
    }
    if conflict:
        mic["metadata_conflict"] = conflict
        mic["evidence"] += " (overrides conflicting transcriber metadata)"
    return mic, None

def _rate(stream):
    for key in ("r_frame_rate", "avg_frame_rate"):
        try:
            rate = Fraction(stream.get(key) or "0")
        except (ValueError, ZeroDivisionError):
            continue
        if rate > 0:
            return rate
    return None


def _positive_duration(value):
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    return duration if isfinite(duration) and duration > 0 else None


def _video_frames(stream, fps):
    try:
        count = int(stream["nb_frames"])
    except (KeyError, TypeError, ValueError):
        count = 0
    if count > 0:
        return count
    try:
        duration = Fraction(stream["duration_ts"]) * Fraction(stream["time_base"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        duration = Fraction(str(_positive_duration(stream.get("duration")) or 0))
    return max(0, floor(duration * fps))


def _part_media(folder, roots, pov, part, confirmations, fps, *, frame_count=False):
    source = folder / part.video_path if part.video_path else None
    mp4 = source.with_suffix(".mp4") if source and source.suffix.lower() == ".mkv" else source
    if not mp4 or mp4.suffix.lower() != ".mp4" or not mp4.is_file():
        return None, part, "mp4_missing", False

    streams = probe_streams(mp4)
    video = [stream for stream in streams if stream.get("codec_type") == "video"]
    if len(video) != 1:
        return None, part, "fps_mismatch", False
    duration = _positive_duration(video[0].get("duration"))
    frames = _video_frames(video[0], fps) if frame_count else None
    if duration is None and not frames:
        return None, part, "duration_unverified", False
    source_part = replace(part, duration=duration, duration_source="ffprobe")
    if _rate(video[0]) != Fraction(str(fps)):
        return None, source_part, "fps_mismatch", False
    stems = [source.stem]
    if part.srt_path:
        stems.append(Path(part.srt_path).stem)
    metadata = _metadata(roots, stems)
    mic, problem = _mic(metadata, streams, part.part_id, pov.name, confirmations)
    game = any(_normalized(track.get("speaker")).strip("[]") == "game audio"
               for track in _tracks(metadata))
    media = {"mp4_path": str(mp4.resolve()), "mic": mic}
    if frame_count:
        media["source_frame_count"] = frames
    return media, source_part, problem, game


def _selections(session, manifest):
    selected = []
    warnings = manifest["warnings"]
    for suggestion in session.suggestions:
        review = session.reviews.get(suggestion.suggestion_id)
        if not review or review.status != "keep":
            continue
        approved = review.approved_start is not None and review.approved_end is not None
        start, end = ((review.approved_start, review.approved_end) if approved
                      else (suggestion.start, suggestion.end))
        if not (isfinite(start) and isfinite(end) and start < end):
            warnings.append({"code": "untimed", "message": "Skipped untimed Keep selection.",
                             "event_id": suggestion.event_id})
            continue
        selected.append((start, end, suggestion, "approved" if approved else "ai"))

    selected.sort(key=lambda item: (item[0], item[2].event_id, item[2].suggestion_id))
    counts = Counter(suggestion.event_id for _, _, suggestion, _ in selected)
    latest_end = float("-inf")
    seen = set()
    for start, end, suggestion, _ in selected:
        event = suggestion.event_id
        if counts[event] > 1 and event not in seen:
            warnings.append({"code": "duplicate_event", "event_id": event,
                             "message": "Multiple Keep selections for this event export separately."})
            seen.add(event)
        if start < latest_end:
            warnings.append({"code": "overlapping_selection", "event_id": event,
                             "message": "This Keep selection overlaps an earlier selection."})
        latest_end = max(latest_end, end)
    return selected, counts


_PROBLEMS = {
    "mp4_missing": "OBS-remuxed MP4 is missing.",
    "fps_mismatch": "MP4 video frame rate differs from the timeline or could not be verified.",
    "mic_unmapped": "Owner mic could not be matched to a unique MP4 audio stream.",
    "mic_unverified": "Owner must listen to and confirm the owner mic track before export.",
    "duration_unverified": "MP4 source duration could not be verified.",
    "source_frame_range": "MP4 video cannot supply the full segment frame range.",
    "overlapping_parts": "Multiple parts cover this segment for the same POV.",
}


def _record_frame(time, origin, rate):
    """Use Stage 1 half-up rounding relative to the selection's frame origin."""
    return floor((Fraction(str(time)) - origin) * rate + Fraction(1, 2))


def _angle(pov, part, media, placement, first_frame, last_frame, rate):
    if placement is None or not placement.record_in <= first_frame < last_frame <= placement.record_out:
        return None
    source_in = placement.source_in + first_frame - placement.record_in
    source_out = placement.source_in + last_frame - placement.record_in
    limit = floor(Fraction(str(part.duration)) * rate)
    return {
        "pov_id": pov.pov_id, "pov_name": pov.name, "part_id": part.part_id,
        "part_index": part.index, **media,
        "source_in_frame": source_in, "source_out_frame": source_out,
        "handle_before_frames": source_in,
        "handle_after_frames": limit - source_out,
    }

def _manifest_inputs(session, transcripts_dir, confirmations, fps):
    folder = Path(session.folder).resolve()
    roots = ([Path(transcripts_dir)] if transcripts_dir is not None else [])
    roots += [folder / "_Transcripts", folder.parent / "_Transcripts"]
    if confirmations is None:
        confirmations = _load_confirmations(folder / ".autoclipper" / "mic_confirmations.json")
    elif isinstance(confirmations, (str, Path)):
        confirmations = _load_confirmations(Path(confirmations))
    if isinstance(confirmations, dict):
        confirmations = confirmations["confirmations"]

    fps = 60 if fps is None else fps
    rate = Fraction(str(fps))
    if rate <= 0:
        raise ValueError("Expected a positive timeline frame rate.")
    fps = int(rate) if rate.denominator == 1 else float(rate)
    manifest = {
        "schema_version": 1, "session_id": session.session_id, "session_folder": str(folder),
        "fps": fps, "created_at": datetime.now(timezone.utc).isoformat(),
        "blockers": [], "warnings": [], "segments": [],
    }
    return folder, roots, confirmations, rate, manifest


def build_manifest(session, transcripts_dir=None, confirmations=None, fps=None) -> dict:
    folder, roots, confirmations, rate, manifest = _manifest_inputs(session, transcripts_dir, confirmations, fps)
    fps = manifest["fps"]
    selected, counts = _selections(session, manifest)
    if not selected:
        manifest["blockers"].append({"code": "no_selection", "part_id": None, "pov_name": None,
                                     "message": "No timed Keep selections."})
        return manifest

    # Probe before splitting so video stream ends, including sub-second gaps,
    # determine coverage. Keep the session duration only for unavailable media.
    last_selection_end = max(end for _, end, _, _ in selected)
    media_cache = {
        part.part_id: _part_media(folder, roots, pov, part, confirmations, fps)
        for pov, part in session.all_parts() if part.offset < last_selection_end
    }
    coverage_session = replace(session, povs=[
        replace(pov, parts=[
            replace(media_cache[part.part_id][1] if part.part_id in media_cache else part, srt_path=None)
            for part in pov.parts
        ])
        for pov in session.povs
    ])
    parts = {part.part_id: part for _, part in coverage_session.all_parts()}

    reported = set()
    record = 0
    game_identified = False

    def block(code, pov=None, part_id=None):
        key = (code, pov.pov_id if pov else None, part_id)
        if key not in reported:
            reported.add(key)
            manifest["blockers"].append({
                "code": code, "part_id": part_id, "pov_name": pov.name if pov else None,
                "message": _PROBLEMS[code],
            })

    for start, end, suggestion, range_source in selected:
        coverage = map_range(coverage_session, start, end, pad_before=0, pad_after=0)
        boundaries = sorted({start, end} | {x for r in coverage for x in (r.session_start, r.session_end)})
        origin = Fraction(str(start))
        frames = [_record_frame(boundary, origin, rate) for boundary in boundaries]
        placements = {}
        event_segments = []
        for index, (first, last) in enumerate(zip(boundaries, boundaries[1:])):
            first_frame, last_frame = frames[index:index + 2]
            length = last_frame - first_frame
            if length == 0:
                manifest["warnings"].append({"code": "subframe_segment", "event_id": suggestion.event_id,
                                             "message": "Skipped coverage interval shorter than one frame."})
                continue
            angles, unavailable = [], []
            for pov in coverage_session.povs:
                ranges = [r for r in coverage if r.pov_id == pov.pov_id]
                active = [r for r in ranges if r.session_start <= first and last <= r.session_end]
                reason = "recording_gap" if any(
                    part.offset < last and part.duration is not None for part in pov.parts
                ) and any(part.offset >= last for part in pov.parts) else "no_coverage"
                if len(active) > 1:
                    block("overlapping_parts", pov)
                elif active:
                    part_id = active[0].part_id
                    media, _, problem, game = media_cache[part_id]
                    source_part = parts[part_id]
                    game_identified |= game
                    if problem is None:
                        if part_id not in placements:
                            plan = Session(povs=[replace(pov, parts=[source_part])])
                            try:
                                placement, = plan_placements(plan, start, end, rate)
                            except ValueError:
                                placement = None
                            placements[part_id] = placement
                        angle = _angle(pov, source_part, media, placements[part_id],
                                       first_frame, last_frame, rate)
                        if angle is not None:
                            angles.append(angle)
                            conflict = angle["mic"].get("metadata_conflict")
                            if conflict and ("mic_metadata_conflict", part_id) not in reported:
                                reported.add(("mic_metadata_conflict", part_id))
                                manifest["warnings"].append({
                                    "code": "mic_metadata_conflict", "pov_name": pov.name,
                                    "part_id": part_id, "message": conflict,
                                })
                            continue
                        problem = "source_frame_range"
                    block(problem, pov, part_id)
                    reason = "mic_blocked" if problem.startswith("mic_") else problem
                else:
                    for part in pov.parts:
                        if part.duration is None and part.offset < last:
                            block("duration_unverified", pov, part.part_id)
                if reason == "recording_gap" and last - first < 1:
                    manifest["warnings"].append({
                        "code": "short_gap_segment", "event_id": suggestion.event_id, "pov_name": pov.name,
                        "message": f"Recording gap at {first:.3f}-{last:.3f} s ({last - first:.3f} s).",
                    })
                unavailable.append({"pov_id": pov.pov_id, "pov_name": pov.name, "reason": reason})
            if not angles:
                manifest["warnings"].append({"code": "no_angles", "event_id": suggestion.event_id,
                                             "message": "No available angles for this segment."})
            event_segments.append({
                "event_id": suggestion.event_id, "suggestion_id": suggestion.suggestion_id,
                "reviewer": suggestion.reviewer, "title": suggestion.title, "range_source": range_source,
                "session_start": first, "session_end": last, "record_start_frame": record,
                "duration_frames": length, "angles": angles, "unavailable": unavailable,
            })
            record += length
        event = suggestion.event_id
        prefix = f"{event}-{suggestion.suggestion_id}" if counts[event] > 1 else event
        for number, segment in enumerate(event_segments, 1):
            segment.update(segment_id=f"{prefix}-{number}", piece=number, pieces=len(event_segments))
            manifest["segments"].append(segment)
    if not manifest["segments"] or any(not segment["angles"] for segment in manifest["segments"]):
        manifest["blockers"].append({"code": "no_angles", "part_id": None, "pov_name": None,
                                     "message": "An export needs an angle in every segment."})
    if not game_identified:
        manifest["warnings"].append({"code": "game_audio_unidentified", "message": "Game audio not identified."})
    return manifest


@dataclass(frozen=True)
class _CoveredPart:
    part_id: str
    index: int
    first: int
    end: int
    media: dict


def _review_coverage(session, transcripts_dir, confirmations, fps):
    folder, roots, confirmations, rate, manifest = _manifest_inputs(session, transcripts_dir, confirmations, fps)
    fps = manifest["fps"]
    if type(fps) is not int:
        raise ValueError("Director timelines require integer fps")
    coverage = {pov.pov_id: [] for pov in session.povs}
    first, end = 0, 0
    game_identified = False
    for pov, part in session.all_parts():
        start = _record_frame(part.offset, 0, rate)
        first = min(first, start)
        media, _, problem, game = _part_media(folder, roots, pov, part, confirmations, fps,
                                             frame_count=True)
        estimated = _positive_duration(part.duration)
        if estimated:
            end = max(end, start + _record_frame(estimated, 0, rate))
        if not problem and not media["source_frame_count"]:
            problem = "duration_unverified"
        if problem:
            manifest["blockers"].append({"code": problem, "part_id": part.part_id, "pov_name": pov.name,
                                         "message": _PROBLEMS[problem]})
            continue
        frames = media["source_frame_count"]
        end = max(end, start + frames)
        coverage[pov.pov_id].append(_CoveredPart(part.part_id, part.index, start, start + frames, media))
        game_identified |= game
        if conflict := media["mic"].get("metadata_conflict"):
            manifest["warnings"].append({"code": "mic_metadata_conflict", "part_id": part.part_id,
                                         "pov_name": pov.name, "message": conflict})
    if not game_identified:
        manifest["warnings"].append({"code": "game_audio_unidentified", "message": "Game audio not identified."})
    return manifest, coverage, first, end


def _coverage_spans(povs, coverage, first, end):
    boundaries = {first, end}
    for parts in coverage.values():
        boundaries.update(frame for part in parts for frame in (part.first, part.end) if first < frame < end)
    cuts = sorted(boundaries)
    for left, right in zip(cuts, cuts[1:]):
        angles, unavailable = [], []
        for pov in povs:
            parts = coverage[pov.pov_id]
            active = [part for part in parts if part.first <= left and right <= part.end]
            if len(active) == 1:
                part, = active
                angles.append({
                    "pov_id": pov.pov_id, "pov_name": pov.name, "part_id": part.part_id,
                    "part_index": part.index, **part.media,
                    "source_in_frame": left - part.first, "source_out_frame": right - part.first,
                    "handle_before_frames": left - part.first, "handle_after_frames": part.end - right,
                })
                continue
            if active:
                reason = "overlapping_parts"
            elif any(part.end <= left for part in parts) and any(part.first >= right for part in parts):
                reason = "recording_gap"
            else:
                reason = "no_coverage"
            unavailable.append({"pov_id": pov.pov_id, "pov_name": pov.name, "reason": reason})
        yield left, right, angles, unavailable


def _frame_segment(fps, first, end, record, angles, unavailable, *, entry_id, title, kind, range_source):
    return {
        "event_id": entry_id, "suggestion_id": entry_id, "title": title,
        "range_source": range_source, "kind": kind,
        "session_start_frame": first, "session_end_frame": end,
        "session_start": first / fps, "session_end": end / fps,
        "record_start_frame": record, "duration_frames": end - first,
        "angles": angles, "unavailable": unavailable,
    }


def build_review_manifest(session, transcripts_dir=None, confirmations=None, fps=None):
    """Build a frame-grid review without compressing interior gaps or trusting estimated tails."""
    manifest, coverage, first, end = _review_coverage(session, transcripts_dir, confirmations, fps)
    manifest.update(mode="director_review", requested_session_frames=[first, end])
    spans = list(_coverage_spans(session.povs, coverage, first, end))
    playable = [index for index, span in enumerate(spans) if span[2]]
    if not playable:
        manifest["blockers"].append({"code": "no_angles", "message": "Review has no usable coverage."})
        return manifest

    start_idx, end_idx = playable[0], playable[-1]
    origin, last = spans[start_idx][0], spans[end_idx][1]
    manifest["review_session_frames"] = [origin, last]
    manifest["uncovered_edges"] = [
        {"session_start_frame": left, "session_end_frame": right, "unavailable": unavailable}
        for left, right, _, unavailable in spans[:start_idx] + spans[end_idx + 1:]
    ]
    if manifest["uncovered_edges"]:
        manifest["warnings"].append({"code": "uncovered_edges",
                                     "message": "Review clips to the first and last usable source frames."})

    for number, (left, right, angles, unavailable) in enumerate(spans[start_idx:end_idx + 1], 1):
        for missing in unavailable:
            if missing["reason"] == "overlapping_parts":
                manifest["warnings"].append({"code": "overlapping_parts", "pov_name": missing["pov_name"],
                                             "message": f"Ambiguous coverage at session frames [{left}, {right})."})
        segment = _frame_segment(manifest["fps"], left, right, left - origin, angles, unavailable,
                                 entry_id="review", title="Full session review",
                                 kind="media" if angles else "coverage_gap", range_source="session")
        segment.update(segment_id=f"review-{number}", piece=number, pieces=end_idx - start_idx + 1)
        manifest["segments"].append(segment)
    return manifest


def render_report(manifest) -> str:
    def cell(value):
        return str(value).replace("|", r"\|").replace("\r", " ").replace("\n", " ")

    segments = manifest["segments"]
    selected = {segment["suggestion_id"] for segment in segments}
    summary = (f"Full-session review, {len(segments)} segments, {manifest['fps']} fps."
               if manifest.get("mode") == "director_review"
               else f"{len(selected)} timed Keep selections, {len(segments)} segments, {manifest['fps']} fps.")
    lines = [
        "# Resolve timeline manifest",
        "",
        summary,
        "",
        "Multicam audio uses Resolve Source mode with one switchable owner-mic pair.",
        "",
        "| Segment | Selection | Session seconds | Record frame | Frames | Angles and mics | Unavailable |",
        "| --- | --- | --- | ---: | ---: | --- | --- |",
    ]
    for segment in segments:
        angles = ", ".join(
            f"{angle['pov_name']} P{angle['part_index']} ({angle['mic']['status']}, "
            f"expected ch {', '.join(map(str, angle['mic']['resolve_channels']))})" for angle in segment["angles"]
        )
        missing = ", ".join(f"{pov['pov_name']} ({pov['reason']})" for pov in segment["unavailable"])
        row = (segment["segment_id"], f"{segment['title']} ({segment['range_source']})",
               f"{segment['session_start']:.3f}–{segment['session_end']:.3f}",
               segment["record_start_frame"], segment["duration_frames"], angles or "—", missing or "—")
        lines.append("| " + " | ".join(map(cell, row)) + " |")
    for heading in ("blockers", "warnings"):
        lines.extend(("", f"## {heading.title()}", ""))
        for issue in manifest[heading]:
            context = " / ".join(filter(None, (issue.get("event_id"), issue.get("pov_name"), issue.get("part_id"))))
            prefix = f"{context} — " if context else ""
            lines.append(f"- {cell(prefix + issue['message'])} (`{issue['code']}`)")
        if not manifest[heading]:
            lines.append("None.")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_folder", type=Path)
    parser.add_argument("--transcripts", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--full-session", action="store_true", help="Plan a coverage-aware Director review")
    parser.add_argument("--confirm-mic", action="append", nargs=3, metavar=("PART_ID", "TRACK", "SPEAKER"))
    args = parser.parse_args(argv)
    try:
        session = load_session(args.session_folder)
        path = Path(session.folder) / ".autoclipper" / "mic_confirmations.json"
        confirmations = _load_confirmations(path)
        for part_id, track, speaker in args.confirm_mic or []:
            part = session.part_by_id(part_id)
            track = int(track)
            if track < 1 or not speaker.strip():
                raise ValueError("Expected a positive mic track and nonempty speaker.")
            rows = confirmations["confirmations"]
            rows[:] = [item for item in rows if item.get("part_id") != part.part_id
                       or item.get("transcriber_track") != track]
            rows.append({"part_id": part_id, "transcriber_track": track, "speaker": speaker,
                         "confirmed_by": "owner", "confirmed_at": datetime.now(timezone.utc).isoformat()})
        plan = build_review_manifest if args.full_session else build_manifest
        manifest = plan(session, args.transcripts, confirmations)
        report = render_report(manifest)
        if not args.dry_run:
            out = args.out or (Path(session.folder) / "autoclipper_exports" / "resolve"
                               / datetime.now().strftime("%Y%m%d-%H%M%S"))
            out.mkdir(parents=True, exist_ok=True)
            if (out / "manifest.json").exists() or (out / "report.md").exists():
                raise ValueError("The output directory already contains a Resolve export.")
            if args.confirm_mic:
                _write_json(path, confirmations)
            _write_json(out / "manifest.json", manifest)
            (out / "report.md").write_text(report, encoding="utf-8")
        print(report, end="")
        return 1 if manifest["blockers"] else 0
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, f"{parser.prog}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
