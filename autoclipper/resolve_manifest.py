"""Build offline Resolve multicam manifests from reviewed selections."""

import argparse
from collections import Counter
from dataclasses import replace
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
                    return _read_json(path)
    return {}


def _tracks(metadata):
    tracks = metadata.get("speaker_tracks") or {}
    if isinstance(tracks, list):
        return [item for item in tracks if isinstance(item.get("track"), int)]
    return [
        {"track": int(match[1]), "speaker": speaker}
        for label, speaker in tracks.items()
        if (match := re.fullmatch(r"audio_track_(\d+)", label))
    ]


def _normalized(value):
    return str(value or "").strip().casefold()


def _inferred(metadata, track, speaker):
    # The latest relabel of this track determines provenance.
    for entry in reversed(metadata.get("relabel_history") or []):
        changes = entry.get("changes") or []
        if not changes or any(
            str(change.get("track")) == str(track)
            and _normalized(change.get("to", speaker)) == _normalized(speaker)
            for change in changes
        ):
            return _normalized(entry.get("provenance")).startswith("inferred")
    return False


def _audio_mapping(audio, track):
    """Match an explicit stream index or an unambiguous track title."""
    titles = {_normalized(track.get("title")).replace(" ", ""), f"track{track['track']}"} - {""}
    named = [
        stream for stream in audio
        if any(_normalized(value).replace(" ", "") in titles
               for key, value in stream.get("tags", {}).items()
               if key.casefold() in {"title", "handler_name"})
    ]
    indexed = [stream for stream in audio if stream["index"] == track.get("index")]
    if len(named) > 1 or (named and indexed and named != indexed):
        return None
    candidates = named or indexed
    if len(candidates) != 1:
        return None
    stream = candidates[0]
    channels = [item.get("channels") for item in audio[:audio.index(stream) + 1]]
    if any(not isinstance(count, int) or count <= 0 for count in channels):
        return None
    return stream["index"], list(range(sum(channels[:-1]) + 1, sum(channels) + 1))


def _mic(metadata, streams, part_id, speaker, confirmations):
    tracks = _tracks(metadata)
    owner_tracks = [track for track in tracks if _normalized(track.get("speaker")) == _normalized(speaker)]
    if len(owner_tracks) != 1:
        return None, "mic_unmapped"
    track = owner_tracks[0]
    confirmed = any(
        item.get("part_id") == part_id and item.get("transcriber_track") == track["track"]
        and _normalized(item.get("speaker")) == _normalized(speaker)
        and item.get("confirmed_by") == "owner"
        for item in confirmations
    )
    if _inferred(metadata, track["track"], speaker) and not confirmed:
        return None, "mic_inferred_unconfirmed"
    audio = sorted((stream for stream in streams if stream.get("codec_type") == "audio"),
                   key=lambda stream: stream["index"])
    mapping = _audio_mapping(audio, track)
    if mapping is None:
        return None, "mic_unmapped"
    index, channels = mapping
    return {
        "status": "owner_confirmed" if confirmed else "verified",
        "transcriber_track": track["track"], "speaker": speaker,
        "mp4_audio_stream_index": index, "resolve_channels": channels,
        "evidence": "speaker_tracks + MP4 probe" + (" + owner confirmation" if confirmed else ""),
    }, None


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


def _part_media(folder, roots, pov, part, confirmations, fps):
    source = folder / part.video_path if part.video_path else None
    mp4 = source.with_suffix(".mp4") if source and source.suffix.lower() == ".mkv" else source
    if not mp4 or mp4.suffix.lower() != ".mp4" or not mp4.is_file():
        return None, part, "mp4_missing", False

    streams = probe_streams(mp4)
    video = [stream for stream in streams if stream.get("codec_type") == "video"]
    if len(video) != 1 or _rate(video[0]) != Fraction(str(fps)):
        return None, part, "fps_mismatch", False
    duration = _positive_duration(video[0].get("duration"))
    if duration is None and (part.duration is None or part.duration_source == "srt_estimate"):
        return None, part, "duration_unverified", False

    # Keep synced boundaries from the session. Clamp source frames against the
    # actual video stream when placing angles, independently of those boundaries.
    source_part = replace(part, duration=duration, duration_source="ffprobe") if duration else part
    stems = [source.stem]
    if part.srt_path:
        stems.append(Path(part.srt_path).stem)
    metadata = _metadata(roots, stems)
    mic, problem = _mic(metadata, streams, part.part_id, pov.name, confirmations)
    game = any(_normalized(track.get("speaker")).strip("[]") == "game audio"
               for track in _tracks(metadata))
    return {"mp4_path": str(mp4.resolve()), "mic": mic}, source_part, problem, game


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
    "mic_inferred_unconfirmed": "Owner must confirm the inferred mic label before export.",
    "duration_unverified": "MP4 source duration could not be verified.",
    "source_frame_range": "MP4 video cannot supply the full segment frame range.",
    "overlapping_parts": "Multiple parts cover this segment for the same POV.",
}


def _duration_frames(start, end, fps):
    # A virtual clock gives even fully uncovered segments the Stage 1 record
    # length. Give it spare duration so the planner's source clamp is inactive.
    clock = Session(povs=[POV(parts=[Part("clock", offset=start, duration=end - start + 1)])])
    return plan_placements(clock, start, end, fps)[0].record_out


def _angle(pov, part, media, start, end, fps, duration_frames):
    # Use Stage 1 source bounds and require the full planned segment length.
    plan = Session(povs=[replace(pov, parts=[part])])
    placements = plan_placements(plan, start, end, fps)
    if len(placements) != 1 or placements[0].record_in != 0 or placements[0].record_out != duration_frames:
        return None
    placement = placements[0]
    limit = floor(Fraction(str(part.duration)) * Fraction(str(fps)))
    return {
        "pov_id": pov.pov_id, "pov_name": pov.name, "part_id": part.part_id,
        "part_index": part.index, **media,
        "source_in_frame": placement.source_in, "source_out_frame": placement.source_out,
        "handle_before_frames": placement.source_in,
        "handle_after_frames": limit - placement.source_out,
    }


def build_manifest(session, transcripts_dir=None, confirmations=None, fps=None) -> dict:
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
    selected, counts = _selections(session, manifest)
    if not selected:
        manifest["blockers"].append({"code": "no_selection", "part_id": None, "pov_name": None,
                                     "message": "No timed Keep selections."})
        return manifest

    # SRT and cue contents are unnecessary for Resolve manifests.
    coverage_session = replace(session, povs=[
        replace(pov, parts=[replace(part, srt_path=None) for part in pov.parts])
        for pov in session.povs
    ])
    parts = {part.part_id: part for _, part in session.all_parts()}
    media_cache = {}
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
        event_segments = []
        for first, last in zip(boundaries, boundaries[1:]):
            try:
                length = _duration_frames(first, last, fps)
            except ValueError:
                manifest["warnings"].append({"code": "subframe_segment", "event_id": suggestion.event_id,
                                             "message": "Skipped coverage interval shorter than one frame."})
                continue
            angles, unavailable = [], []
            for pov in session.povs:
                ranges = [r for r in coverage if r.pov_id == pov.pov_id]
                active = [r for r in ranges if r.session_start <= first and last <= r.session_end]
                reason = "recording_gap" if any(
                    part.offset < last and part.duration is not None for part in pov.parts
                ) and any(part.offset >= last for part in pov.parts) else "no_coverage"
                if len(active) > 1:
                    block("overlapping_parts", pov)
                elif active:
                    part_id = active[0].part_id
                    if part_id not in media_cache:
                        media_cache[part_id] = _part_media(folder, roots, pov, parts[part_id], confirmations, fps)
                    media, source_part, problem, game = media_cache[part_id]
                    game_identified |= game
                    if problem is None:
                        try:
                            angle = _angle(pov, source_part, media, first, last, fps, length)
                        except ValueError:
                            angle = None
                        if angle is not None:
                            angles.append(angle)
                            continue
                        problem = "source_frame_range"
                    block(problem, pov, part_id)
                    reason = "mic_blocked" if problem.startswith("mic_") else problem
                else:
                    for part in pov.parts:
                        if part.duration is None and part.offset < last:
                            block("duration_unverified", pov, part.part_id)
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


def render_report(manifest) -> str:
    def cell(value):
        return str(value).replace("|", r"\|").replace("\r", " ").replace("\n", " ")

    segments = manifest["segments"]
    selected = {segment["suggestion_id"] for segment in segments}
    lines = [
        "# Resolve timeline manifest",
        "",
        f"{len(selected)} timed Keep selections, {len(segments)} segments, {manifest['fps']} fps.",
        "",
        "Multicam audio uses Resolve Source mode with one switchable owner-mic pair.",
        "",
        "| Segment | Selection | Session seconds | Record frame | Frames | Angles and mics | Unavailable |",
        "| --- | --- | --- | ---: | ---: | --- | --- |",
    ]
    for segment in segments:
        angles = ", ".join(
            f"{angle['pov_name']} P{angle['part_index']} ({angle['mic']['status']}, "
            f"ch {', '.join(map(str, angle['mic']['resolve_channels']))})" for angle in segment["angles"]
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
        manifest = build_manifest(session, args.transcripts, confirmations)
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
