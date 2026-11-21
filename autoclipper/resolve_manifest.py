"""Build offline Resolve multicam manifests from reviewed selections."""

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from fractions import Fraction
from hashlib import sha256
import json
from math import floor, isfinite
import os
from pathlib import Path
import stat
from stat import S_ISREG
import re
from tempfile import gettempdir

from .mapping import map_range
from .media import probe_streams, resolve_recording
from .models import Part, POV, Session
from .resolve_stage1 import plan_placements
from .store import _write_json, load_session


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _load_confirmations(path):
    return _read_json(path) if path is not None and path.exists() else {"schema_version": 1, "confirmations": []}


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


def _reference_track(metadata, streams, mic):
    """Resolve the OBS Track1 mix only when its identity and stereo mapping agree."""
    audio = sorted((s for s in streams if s.get("codec_type") == "audio"),
                   key=lambda s: s["index"])
    matches = [s for s in audio if _normalized((s.get("tags") or {}).get("title")).replace(" ", "") == "track1"]
    reason = None
    if len(matches) != 1:
        reason = "Track1 is missing or ambiguous in the MP4 audio inventory."
    else:
        stream = matches[0]
        ordinal = audio.index(stream) + 1
        counts = [s.get("channels") for s in audio[:ordinal]]
        labels = [row["speaker"] for row in _tracks(metadata)
                  if row["track"] == 1 and _normalized(row.get("speaker"))]
        if mic and (stream["index"] == mic["mp4_audio_stream_index"]
                      or ordinal == mic["mp4_audio_ordinal"]):
            reason = "Track1 is the verified owner mic, not a separate group mix."
        elif len(labels) > 1 or any(_normalized(label).strip("[]") not in
                                  {"team", "game audio", "system audio", "desktop audio",
                                   "discord", "discord audio", "mix", "group mix"}
                                  for label in labels):
            reason = f"Metadata labels Track1 as {', '.join(repr(label) for label in labels)}; its group/system mix identity is unverified."
        elif not all(_track_number(count) for count in counts) or counts[-1] != 2:
            reason = "Track1 is not a verified stereo pair."
        else:
            return {"status": "verified", "mp4_audio_stream_index": stream["index"],
                    "mp4_audio_ordinal": ordinal, "mp4_audio_title": "Track1",
                    "resolve_channels": list(range(sum(counts[:-1]) + 1, sum(counts) + 1)),
                    "resolve_channels_status": "expected", "evidence": "MP4 Track1 title + stereo stream inventory"}
    return {"status": "omitted", "reason": reason}

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


def _part_media(folder, roots, pov, part, confirmations, fps, *, frame_count=False, reference=False):
    source = folder / part.video_path if part.video_path else None
    resolved = resolve_recording(folder, part.video_path, require_mp4=True)
    mp4 = resolved.path
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
    if reference:
        media["reference_track"] = _reference_track(metadata, streams, mic)
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

def _manifest_inputs(session, transcripts_dir, confirmations, fps, *, isolated_inputs=False):
    folder = Path(session.folder).resolve()
    roots = ([Path(transcripts_dir)] if transcripts_dir is not None else [])
    if not isolated_inputs:
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
        part.part_id: _part_media(folder, roots, pov, part, confirmations, fps, reference=True)
        for pov, part in session.all_parts() if part.offset < last_selection_end
    }
    for pov, part in session.all_parts():
        if part.part_id in media_cache and resolve_recording(folder, part.video_path).status == "stale_mkv":
            manifest["warnings"].append({
                "code": "stale_mkv_reference", "part_id": part.part_id, "pov_name": pov.name,
                "message": "Saved MKV is absent; using the same-stem MP4. Session JSON was not changed.",
            })
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
    # Prefer the POV with the most selected frames, then use a covered valid POV.
    totals = Counter()
    for segment in manifest["segments"]:
        for angle in segment["angles"]:
            totals[angle["pov_id"]] += segment["duration_frames"]
    preference = sorted(totals, key=lambda pov_id: (-totals[pov_id], pov_id))
    warned_parts = set()
    for segment in manifest["segments"]:
        for angle in segment["angles"]:
            ref = angle["reference_track"]
            if ref["status"] != "verified" and angle["part_id"] not in warned_parts:
                warned_parts.add(angle["part_id"])
                manifest["warnings"].append({"code": "reference_track_unverified",
                    "part_id": angle["part_id"], "pov_name": angle["pov_name"],
                    "message": f"GROUP REFERENCE AUDIO omitted for this part: {ref['reason']}"})
        choice = next((angle for pov_id in preference for angle in segment["angles"]
                       if angle["pov_id"] == pov_id and angle["reference_track"]["status"] == "verified"), None)
        if choice:
            segment["reference_audio"] = {key: choice[key] for key in
                ("pov_id", "pov_name", "part_id", "mp4_path", "source_in_frame", "source_out_frame")}
            segment["reference_audio"].update(
                record_start_frame=segment["record_start_frame"],
                duration_frames=segment["duration_frames"],
                track=choice["reference_track"])
        else:
            segment["reference_audio"] = None
            manifest["warnings"].append({"code": "reference_audio_gap",
                "event_id": segment["event_id"], "segment_id": segment["segment_id"],
                "message": f"GROUP REFERENCE AUDIO has a gap for {segment['segment_id']}: no covered verified mix."})
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


def catalog_source_identity(session):
    """Identify the session layout supplied to the catalog, before scratch path rebasing."""
    return {
        "session_id": session.session_id,
        "parts": [[pov.pov_id, part.part_id, part.index, part.video_path, str(Fraction(str(part.offset)))]
                  for pov, part in session.all_parts()],
    }


def source_mapping_digest(manifest: dict) -> str:
    """Return the digest of a Director manifest's verified source mapping."""
    mapping = manifest.get("source_mapping")
    if not isinstance(mapping, dict) or mapping.get("version") != 1:
        raise ValueError("Verified source mapping is missing. Rebuild the review and reconcile the catalog.")
    return sha256(json.dumps(mapping, sort_keys=True, separators=(",", ":"), allow_nan=False)
                  .encode("utf-8")).hexdigest()


def pinned_input_digest(path):
    """Hash one file or a directory by sorted relative names and file bytes."""
    path = Path(path)

    def reject_link(item):
        info = os.lstat(item)
        if (os.path.islink(item) or
                getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise ValueError(f"Pinned input contains a link or reparse point: {item}")

    reject_link(path)

    def hash_file(file, digest):
        with file.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)

    if path.is_file():
        digest = sha256()
        hash_file(path, digest)
        return digest.hexdigest()
    if not path.is_dir():
        raise ValueError(f"Pinned input is missing: {path}")
    digest = sha256()
    files = []
    pending = [path]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                item = Path(entry.path)
                reject_link(item)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(item)
                elif entry.is_file(follow_symlinks=False):
                    files.append(item)
                else:
                    raise ValueError(f"Pinned metadata contains an unsupported file: {item}")
    for file in sorted(files, key=lambda item: item.relative_to(path).as_posix()):
        name = file.relative_to(path).as_posix().encode("utf-8")
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        size = file.stat().st_size
        digest.update(size.to_bytes(8, "big"))
        hash_file(file, digest)
    return digest.hexdigest()


def _verified_pinned_input(root, record, supplied, label, *, directory=False):
    if supplied is not None and not isinstance(supplied, (str, Path)):
        kind = "metadata directory" if directory else "confirmations file"
        raise ValueError(f"Pass a {kind} path matching the input used at prepare.")
    try:
        relative = Path(record["path"])
        expected = record["sha256"]
        if (relative.is_absolute() or ".." in relative.parts or not relative.parts
                or not isinstance(expected, str) or len(expected) != 64):
            raise ValueError("invalid pinned input identity")
        pinned = root / relative
        if not pinned.resolve().is_relative_to(root.resolve()):
            raise ValueError("pinned input leaves scratch directory")
        if not (pinned.is_dir() if directory else pinned.is_file()):
            raise ValueError("pinned copy is missing or has the wrong type")
        if pinned_input_digest(pinned) != expected:
            raise ValueError("pinned copy changed")
        if supplied is not None:
            supplied = Path(supplied)
            if not (supplied.is_dir() if directory else supplied.is_file()):
                raise ValueError("explicit input is missing or has the wrong type")
            if pinned_input_digest(supplied) != expected:
                raise ValueError("explicit input differs from prepared copy")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} does not match the prepared input ({exc}). "
                         "Restore the scratch copy or pass the same verified input used at prepare.") from exc
    return supplied if supplied is not None else pinned


def _source_file_identity(path):
    stat = path.stat()
    if not S_ISREG(stat.st_mode):
        raise OSError("Expected a regular source media file")
    return [stat.st_size, stat.st_mtime_ns]


def verify_source_media_identity(manifest, required_paths):
    """Recheck the verified file identities before a live Director build."""
    retry = "Rebuild the review manifest or recompile the edit from verified source media."
    try:
        digest = source_mapping_digest(manifest)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Cannot verify Director source media. {retry}") from exc
    if digest != manifest.get("source_mapping_digest"):
        raise ValueError(f"Verified Director source mapping differs from its digest. {retry}")
    rows = manifest["source_mapping"].get("parts")
    if not isinstance(rows, list):
        raise ValueError(f"Verified Director source mapping has no recording parts. {retry}")
    verified = set()
    for row in rows:
        if not isinstance(row, dict) or "media" not in row:
            raise ValueError(f"Verified Director source mapping has an invalid recording part. {retry}")
        media = row["media"]
        if media is None:
            continue
        expected = row.get("file_identity")
        if (not isinstance(media, dict) or not isinstance(media.get("mp4_path"), str)
                or not isinstance(expected, list) or len(expected) != 2
                or any(type(value) is not int for value in expected)):
            raise ValueError(f"Verified Director source file identity is missing or invalid. {retry}")
        try:
            path = Path(media["mp4_path"]).resolve(strict=True)
            identity = _source_file_identity(path)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"Verified Director source media is unavailable. {retry}") from exc
        if identity != expected:
            raise ValueError(f"Verified Director source media changed. {retry}")
        verified.add(path)
    if not set(required_paths) <= verified:
        raise ValueError(f"A Director source is missing from the verified media mapping. {retry}")


def _review_coverage(session, transcripts_dir, confirmations, fps, *, isolated_inputs=False):
    folder, roots, confirmations, rate, manifest = _manifest_inputs(
        session, transcripts_dir, confirmations, fps, isolated_inputs=isolated_inputs)
    fps = manifest["fps"]
    if type(fps) is not int:
        raise ValueError("Director timelines require integer fps")
    coverage = {pov.pov_id: [] for pov in session.povs}
    first, end = 0, 0
    game_identified = False
    rows = {}
    for pov, part in session.all_parts():
        if not part.part_id or part.part_id in rows:
            raise ValueError("Director recording part IDs must be unique and nonempty")
        start = _record_frame(part.offset, 0, rate)
        row = rows[part.part_id] = {
            "part_id": part.part_id, "pov_id": pov.pov_id, "index": part.index,
            "offset": str(Fraction(str(part.offset))),
            "source_path": str((folder / part.video_path).resolve()) if part.video_path else None,
            "session_start_frame": start, "media": None, "playable_intervals": [],
        }
        if resolve_recording(folder, part.video_path).status == "stale_mkv":
            manifest["warnings"].append({
                "code": "stale_mkv_reference", "part_id": part.part_id, "pov_name": pov.name,
                "message": "Saved MKV is absent; using the same-stem MP4. Session JSON was not changed.",
            })
        first = min(first, start)
        media, _, problem, game = _part_media(folder, roots, pov, part, confirmations, fps,
                                             frame_count=True)
        estimated = _positive_duration(part.duration)
        if estimated:
            end = max(end, start + _record_frame(estimated, 0, rate))
        if not problem and not media["source_frame_count"]:
            problem = "duration_unverified"
        if problem:
            row["problem"] = problem
            manifest["blockers"].append({"code": problem, "part_id": part.part_id, "pov_name": pov.name,
                                         "message": _PROBLEMS[problem]})
            continue
        frames = media["source_frame_count"]
        row["media"] = media
        row["file_identity"] = _source_file_identity(Path(media["mp4_path"]))
        end = max(end, start + frames)
        coverage[pov.pov_id].append(_CoveredPart(part.part_id, part.index, start, start + frames, media))
        game_identified |= game
        if conflict := media["mic"].get("metadata_conflict"):
            manifest["warnings"].append({"code": "mic_metadata_conflict", "part_id": part.part_id,
                                         "pov_name": pov.name, "message": conflict})
    if not game_identified:
        manifest["warnings"].append({"code": "game_audio_unidentified", "message": "Game audio not identified."})
    spans = list(_coverage_spans(session.povs, coverage, first, end))
    for left, right, angles, _ in spans:
        for angle in angles:
            intervals = rows[angle["part_id"]]["playable_intervals"]
            if intervals and intervals[-1][1] == left:
                intervals[-1][1] = right
            else:
                intervals.append([left, right])
    manifest["source_mapping"] = {"version": 1, "fps": fps, "parts": list(rows.values())}
    manifest["source_mapping_digest"] = source_mapping_digest(manifest)
    return manifest, coverage, first, end, spans


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


def build_review_manifest(session, transcripts_dir=None, confirmations=None, fps=None, *, _isolated_inputs=False):
    """Build a frame-grid review without compressing interior gaps or trusting estimated tails."""
    manifest, _, first, end, spans = _review_coverage(
        session, transcripts_dir, confirmations, fps, isolated_inputs=_isolated_inputs)
    manifest.update(mode="director_review", requested_session_frames=[first, end],
                    catalog_source=catalog_source_identity(session))
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


def compile_edit_plan(session, plan, transcripts_dir=None, confirmations=None, *, _isolated_inputs=False):
    """Compile the assembler projection of an EditPlan, without importing Resolve.

    The projection has plan_id, session_id, fps, revision_id, revision_history
    and ordered entries. A source entry has session_in/session_out_exclusive and
    optional pov_id/audio_pov_id. A placeholder has only provisional_frames and
    a title for its timing. Both may carry candidate_ids and note_ids.
    """
    if plan["session_id"] != session.session_id:
        raise ValueError("EditPlan belongs to another session")
    if type(plan["fps"]) is not int or plan["fps"] <= 0:
        raise ValueError("EditPlan requires a positive integer fps")
    if not plan["entries"] or not isinstance(plan["revision_history"], (list, tuple)):
        raise ValueError("EditPlan needs entries and a revision history")
    if any(not isinstance(plan[key], str) or not plan[key].strip() for key in ("plan_id", "revision_id")):
        raise ValueError("EditPlan needs plan and revision IDs")

    pov_ids = {pov.pov_id for pov in session.povs}
    entry_ids = set()
    for entry in plan["entries"]:
        entry_id, title = entry["entry_id"], entry["title"]
        if not isinstance(entry_id, str) or not entry_id.strip() or entry_id in entry_ids:
            raise ValueError("EditPlan entry IDs must be nonempty and unique")
        entry_ids.add(entry_id)
        if not isinstance(title, str) or not title.strip():
            raise ValueError("Every EditPlan entry needs a title")
        if entry["kind"] == "placeholder":
            if (any(entry.get(key) is not None for key in (
                    "session_in", "session_out_exclusive", "pov_id", "audio_pov_id"))
                    or type(entry["provisional_frames"]) is not int or entry["provisional_frames"] <= 0):
                raise ValueError("A placeholder needs provisional frames and no source range or POV")
        elif entry["kind"] == "source":
            first, end = entry["session_in"], entry["session_out_exclusive"]
            if type(first) is not int or type(end) is not int or first >= end:
                raise ValueError("A source entry needs a nonempty session-frame range")
            if any(entry.get(key) is not None and entry[key] not in pov_ids for key in ("pov_id", "audio_pov_id")):
                raise ValueError("EditPlan references an unknown POV")
        else:
            raise ValueError("Expected a source or placeholder entry")
        for key in ("candidate_ids", "note_ids"):
            refs = entry.get(key, ())
            if not isinstance(refs, (tuple, list)) or any(not isinstance(ref, str) or not ref for ref in refs):
                raise ValueError(f"Expected IDs in {key}")

    manifest, coverage, _, _, _ = _review_coverage(
        session, transcripts_dir, confirmations, plan["fps"], isolated_inputs=_isolated_inputs)
    if any(entry["kind"] == "source" for entry in plan["entries"]) and "source_mapping_digest" in plan:
        if plan["source_mapping_digest"] != manifest["source_mapping_digest"]:
            raise ValueError("Verified source mapping changed. Reconcile the catalog and save a new edit plan.")
    manifest.update(mode="director_edit_plan", edit_plan={
        "plan_id": plan["plan_id"], "revision_id": plan["revision_id"],
        "revision_history": deepcopy(list(plan["revision_history"])),
    })
    record = 0
    for entry in plan["entries"]:
        entry_id, title = entry["entry_id"], entry["title"]
        provenance = {key: list(entry.get(key, ())) for key in ("candidate_ids", "note_ids")}
        pov, audio_pov = entry.get("pov_id"), entry.get("audio_pov_id")
        if entry["kind"] == "placeholder":
            spans = [(0, entry["provisional_frames"], [], [])]
        else:
            spans = list(_coverage_spans(session.povs, coverage, entry["session_in"],
                                         entry["session_out_exclusive"]))
        for number, (first, end, available, missing) in enumerate(spans, 1):
            angles = [angle for angle in available if pov is None or angle["pov_id"] == pov]
            audio = next((angle for angle in available if angle["pov_id"] == audio_pov), None)
            if angles and (audio_pov is None or audio is not None):
                segment = _frame_segment(plan["fps"], first, end, record, angles, missing, entry_id=entry_id,
                                         title=title, kind="media", range_source="edit_plan")
                if audio is not None:
                    segment["audio_angle"] = audio
            else:
                cause = "planned_missing_beat" if entry["kind"] == "placeholder" else "source_unavailable"
                segment = {
                    "event_id": entry_id, "suggestion_id": entry_id, "title": title, "range_source": "edit_plan",
                    "kind": "placeholder", "placeholder_reason": cause, "record_start_frame": record,
                    "duration_source": "provisional" if cause == "planned_missing_beat" else "requested_range",
                    "duration_frames": end - first, "angles": [], "unavailable": missing,
                }
                if cause == "source_unavailable":
                    manifest["warnings"].append({"code": cause, "event_id": entry_id,
                                                 "message": "Requested video or microphone coverage is unavailable."})
            segment.update(segment_id=f"{len(manifest['segments']) + 1:03d}", piece=number,
                           pieces=len(spans), requested_pov_id=pov, requested_audio_pov_id=audio_pov, **provenance)
            manifest["segments"].append(segment)
            record += end - first
    return manifest


def compile_fkb_edit_plan(session, knowledge_base, plan_id, *, revision_id,
                          transcripts_dir=None, confirmations=None):
    """Project one current FKB EditPlan into the offline assembler.

    FKB entries use `frames: {"start": ..., "end": ...}` for source ranges.
    A placeholder leaves `frames` absent and supplies `provisional_frames`.
    """
    if knowledge_base.mapping_stale:
        raise ValueError("Refresh stale FKB source mapping before compiling an edit")
    plans = [plan for plan in knowledge_base.edit_plans if plan.plan_id == plan_id]
    if len(plans) != 1:
        raise ValueError("Expected one FKB edit plan with the requested ID")
    plan, = plans
    candidates = {candidate.candidate_id: candidate for candidate in getattr(knowledge_base, "candidate_moments", ())}
    notes = {note.note_id: note for note in knowledge_base.director_notes if not note.deleted}
    spans = {span.span_id for span in knowledge_base.transcript_spans}
    digest = getattr(plan, "source_mapping_digest", None)

    def validate_note(note_id, revision, frames=None):
        note = notes.get(note_id)
        if (note is None or revision is None or note.revision != revision
                or note.source_mapping_digest != digest or not digest
                or (frames is not None and note.frames != frames)):
            raise ValueError("Note anchor changed or is unverified. Select the current range or use an explicit placeholder.")

    entries = []
    for entry in plan.entries:
        if any(key in entry for key in ("session_in", "session_out_exclusive")):
            raise ValueError("FKB entries use frames.start and frames.end")
        projected = dict(entry)
        frames = projected.pop("frames", None)
        if projected["kind"] != "placeholder":
            for note_id in projected.get("note_ids", ()):
                validate_note(note_id, plan.note_revisions.get(note_id))
        if projected["kind"] == "candidate":
            candidate = candidates.get(projected.pop("candidate_id"))
            if candidate is None or frames is not None:
                raise ValueError("Candidate entries must reference a saved candidate without frame overrides")
            frames = candidate.frames or None
            saved_anchor = plan.candidate_note_anchors.get(candidate.candidate_id)
            if candidate.note_anchor_id:
                if saved_anchor != [candidate.note_anchor_id, candidate.note_anchor_revision]:
                    raise ValueError("Note anchor selection changed. Save a new selection or use an explicit placeholder.")
                validate_note(candidate.note_anchor_id, candidate.note_anchor_revision, candidate.frames)
            elif saved_anchor or (frames and candidate.evidence_ids and not spans.intersection(candidate.evidence_ids)):
                raise ValueError("Candidate note anchor is unverified. Select it again or use an explicit placeholder.")
            projected["kind"] = "source" if frames else "placeholder"
            projected.setdefault("title", candidate.logline)
            projected["candidate_ids"] = list(dict.fromkeys(
                [candidate.candidate_id, *projected.get("candidate_ids", ())]))
            projected["note_ids"] = list(dict.fromkeys([
                *projected.get("note_ids", ()), *(key for key in candidate.evidence_ids if key in notes)
            ]))
            if not frames and "provisional_frames" not in projected:
                raise ValueError("Untimed candidates require provisional placeholder frames")
        if frames is not None:
            projected["session_in"], projected["session_out_exclusive"] = frames["start"], frames["end"]
        entries.append(projected)
    if any(entry["kind"] == "source" for entry in entries) and (
            not digest or digest != getattr(knowledge_base, "source_mapping_digest", None)):
        raise ValueError("EditPlan source mapping is unverified. Reconcile the catalog and save a new edit plan.")
    projection = {
        "plan_id": plan.plan_id, "session_id": knowledge_base.session.session_id,
        "fps": knowledge_base.session.fps, "revision_id": revision_id,
        "revision_history": plan.revisions, "entries": entries,
        "source_mapping_digest": digest,
    }
    resolution = getattr(knowledge_base, "source_resolution", None)
    isolated_inputs = False
    if resolution and any(entry["kind"] == "source" for entry in entries):
        if resolution["catalog_source"] != catalog_source_identity(session):
            raise ValueError("Verified source mapping changed. Reconcile the catalog and save a new edit plan.")
        pinned = getattr(knowledge_base, "pinned_inputs", None)
        isolated_inputs = True
        if pinned is None:
            if (confirmations is None or not isinstance(confirmations, (str, Path))
                    or not Path(confirmations).is_file()):
                raise ValueError("This older source-backed catalog has no pinned inputs. Pass the same "
                                 "confirmations file used at prepare explicitly, and metadata directory if used.")
            if transcripts_dir is not None and (not isinstance(transcripts_dir, (str, Path))
                                                or not Path(transcripts_dir).is_dir()):
                raise ValueError("Pass the same metadata directory used at prepare, or omit it if none was used.")
        else:
            scratch = getattr(knowledge_base, "_scratch_dir", None)
            if scratch is None:
                raise ValueError("Load the pinned catalog from its scratch FKB before compiling.")
            scratch = Path(scratch)
            confirmations = _verified_pinned_input(
                scratch, pinned["confirmations"], confirmations, "Mic confirmations")
            if "metadata" in pinned:
                transcripts_dir = _verified_pinned_input(
                    scratch, pinned["metadata"], transcripts_dir, "Metadata", directory=True)
            elif transcripts_dir is not None:
                raise ValueError("No metadata was pinned at prepare; omit transcripts_dir.")
        paths = resolution["paths"]
        session = replace(session, povs=[
            replace(pov, parts=[replace(part, video_path=paths[part.part_id]) for part in pov.parts])
            for pov in session.povs
        ])
    return compile_edit_plan(session, projection, transcripts_dir, confirmations,
                             _isolated_inputs=isolated_inputs)


def render_report(manifest) -> str:
    def cell(value):
        return str(value).replace("|", r"\|").replace("\r", " ").replace("\n", " ")

    segments = manifest["segments"]
    reference_mode = manifest.get("mode") not in {"director_review", "director_edit_plan"}
    selected = {segment["suggestion_id"] for segment in segments}
    label = {"director_review": "Full-session review", "director_edit_plan": "Director edit plan"}.get(
        manifest.get("mode"), f"{len(selected)} timed Keep selections")
    summary = f"{label}, {len(segments)} segments, {manifest['fps']} fps."
    lines = [
        "# Resolve timeline manifest",
        "",
        summary,
        "",
        "Multicam audio uses Resolve Source mode with one switchable owner-mic pair.",
        "",
    ]
    if reference_mode:
        lines.insert(-1, "GROUP REFERENCE AUDIO is a separate enabled track from a verified Track1 mix; disable or mute it before the final render if it is not wanted.")
    lines.extend((
        "| Segment | Selection | Session seconds | Record frame | Frames | Angles and mics |"
        + (" Group reference audio |" if reference_mode else "") + " Unavailable |",
        "| --- | --- | --- | ---: | ---: | --- |"
        + (" --- |" if reference_mode else "") + " --- |",
    ))
    for segment in segments:
        angles = ", ".join(
            f"{angle['pov_name']} P{angle['part_index']} ({angle['mic']['status']}, "
            f"expected ch {', '.join(map(str, angle['mic']['resolve_channels']))})" for angle in segment["angles"]
        )
        missing = ", ".join(f"{pov['pov_name']} ({pov['reason']})" for pov in segment["unavailable"])
        session_span = (f"{segment['session_start']:.3f}–{segment['session_end']:.3f}"
                        if "session_start" in segment else "Placeholder")
        reference = segment.get("reference_audio")
        reference_text = (f"{reference['pov_name']} / {reference['part_id']} "
                          f"(ch {', '.join(map(str, reference['track']['resolve_channels']))})"
                          if reference else "gap" if "reference_audio" in segment else "—")
        row = (segment["segment_id"], f"{segment['title']} ({segment['range_source']})", session_span,
               segment["record_start_frame"], segment["duration_frames"], angles or "—",
               *((reference_text,) if reference_mode else ()), missing or "—")
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


def director_output(out, session_input, session):
    if out is None:
        raise ValueError("Director requires an explicit --out pointing to an approved scratch directory.")
    out = out.resolve()
    temp = Path(gettempdir()).resolve()
    source_input = session_input.resolve()
    if source_input.is_file():
        source_input = source_input.parent
    sources = (source_input, Path(session.folder).resolve())
    if (out == temp or not out.is_relative_to(temp)
            or any(out.is_relative_to(source) or source.is_relative_to(out) for source in sources)):
        raise ValueError("Director --out must be a scratch directory under system temp, separate from the source session.")
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_folder", type=Path)
    parser.add_argument("--transcripts", type=Path)
    parser.add_argument("--out", type=Path, help="For Director, an approved scratch directory under system temp")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--full-session", action="store_true", help="Plan a coverage-aware Director review")
    parser.add_argument("--confirm-mic", action="append", nargs=3, metavar=("PART_ID", "TRACK", "SPEAKER"))
    args = parser.parse_args(argv)
    try:
        session = load_session(args.session_folder)
        out = args.out
        if args.full_session:
            if out is not None or not args.dry_run:
                out = director_output(out, args.session_folder, session)
            path = out / "mic_confirmations.json" if out is not None else None
            if path is not None and path.is_symlink():
                raise ValueError("Director mic confirmations must be stored directly in the scratch directory.")
        else:
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
            out = out or (Path(session.folder) / "autoclipper_exports" / "resolve"
                          / datetime.now().strftime("%Y%m%d-%H%M%S"))
            out.mkdir(parents=True, exist_ok=True)
            outputs = (out / "manifest.json", out / "report.md")
            if any(file.exists() or (args.full_session and file.is_symlink()) for file in outputs):
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
