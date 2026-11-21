"""Build one Resolve Studio compilation from a Stage 2 manifest."""

import argparse
import json
import re
import shutil
import subprocess
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from tempfile import mkdtemp
from textwrap import fill

from .media import FFMPEG_BIN
from .resolve_stage1 import append_clip, isolate_mic, read_item, require


EXPORT_PREFIX = "AutoClipper Export "
TEST_PROJECT_PREFIX = "AutoClipper Stage2 Test "
DIRECTOR_TEST_PREFIX = "AutoClipper DirectorA Test "
START_TIMECODE = "01:00:00:00"


def validate_manifest(manifest):
    """Preflight the complete plan before making contact with Resolve."""
    if manifest["schema_version"] != 1:
        raise ValueError("Expected a schema_version 1 Resolve manifest")
    if manifest["blockers"]:
        reasons = "; ".join(item["message"] for item in manifest["blockers"])
        raise ValueError(f"Manifest has blockers: {reasons}")
    fps = manifest["fps"]
    if type(fps) is not int or fps <= 0 or not manifest["segments"]:
        raise ValueError("Expected a positive integer fps and at least one segment")
    if not isinstance(manifest["session_id"], str) or not manifest["session_id"].strip():
        raise ValueError("Expected a nonempty session ID")

    mode = manifest.get("mode")
    review = mode == "director_review"
    edit = mode == "director_edit_plan"
    if edit and any(not manifest["edit_plan"].get(key) for key in ("plan_id", "revision_id")):
        raise ValueError("Edit manifest needs plan and revision IDs")
    if review:
        origin, review_end = manifest["review_session_frames"]
        if (type(origin) is not int or type(review_end) is not int or origin >= review_end
                or not manifest["segments"][0]["angles"] or not manifest["segments"][-1]["angles"]):
            raise ValueError("Review needs covered first and last segments")
    media = {}
    segment_ids = set()
    record = 0
    for segment in manifest["segments"]:
        length = segment["duration_frames"]
        if (type(length) is not int or type(segment["record_start_frame"]) is not int
                or length <= 0 or segment["record_start_frame"] != record):
            raise ValueError("Segments need adjacent record ranges")
        if review:
            if (origin + record != segment["session_start_frame"]
                    or segment["session_end_frame"] - segment["session_start_frame"] != length
                    or segment["kind"] != ("media" if segment["angles"] else "coverage_gap")):
                raise ValueError("Review segment differs from the declared session coverage")
        elif edit and segment["kind"] == "placeholder":
            if (segment["angles"] or any(key in segment for key in (
                    "session_start_frame", "session_end_frame", "session_start", "session_end", "audio_angle"))
                    or not isinstance(segment["title"], str) or not segment["title"].strip()
                    or segment.get("placeholder_reason") not in ("planned_missing_beat", "source_unavailable")):
                raise ValueError("Placeholder needs a label and no source coordinates")
        elif edit:
            if (segment["kind"] != "media" or not segment["angles"]
                    or type(segment["session_start_frame"]) is not int
                    or type(segment["session_end_frame"]) is not int
                    or segment["session_end_frame"] - segment["session_start_frame"] != length):
                raise ValueError("Expected bounded media or a labeled placeholder")
        elif not segment["angles"]:
            raise ValueError("Segments need at least one angle")
        if not edit and ("audio_angle" in segment or segment.get("kind") == "placeholder"):
            raise ValueError("Slates and independent audio choices require an edit plan")
        # Read marker identity during preflight, before making a project.
        marker_data(segment)
        if edit:
            if segment["segment_id"] in segment_ids:
                raise ValueError("Edit segment IDs must be unique")
            segment_ids.add(segment["segment_id"])
        record += length
        povs = set()
        paths = set()
        angles = segment["angles"]
        audio = segment.get("audio_angle") if edit else None
        if audio is not None and audio not in angles:
            angles = [*angles, audio]
        for angle in angles:
            path = Path(angle["mp4_path"])
            if not path.is_absolute() or path.suffix.lower() != ".mp4":
                raise ValueError("Each angle needs an absolute MP4 path")
            path = path.resolve()
            pov = angle["pov_id"]
            name = angle["pov_name"]
            if pov in povs or path in paths or not isinstance(name, str) or not name.strip():
                raise ValueError("Each segment needs distinct, named POV sources")
            povs.add(pov)
            paths.add(path)
            first, last = angle["source_in_frame"], angle["source_out_frame"]
            if type(first) is not int or type(last) is not int or first < 0 or last - first != length:
                raise ValueError("Each angle must cover the whole segment in source frames")
            if (review or edit) and (type(angle.get("source_frame_count")) is not int
                                     or last > angle["source_frame_count"]):
                raise ValueError("Director angle exceeds verified source frames")
            mic = angle["mic"]
            track = mic["transcriber_track"]
            expected_track = mic.get("expected_owner_track", track)
            ordinal = mic.get("mp4_audio_ordinal", track)
            title = mic.get("mp4_audio_title")
            channels = mic["resolve_channels"]
            if (mic["status"] not in ("verified", "owner_confirmed") or type(track) is not int
                    or track <= 0 or type(expected_track) is not int or expected_track != track
                    or type(ordinal) is not int or ordinal <= 0
                    or mic.get("mp4_audio_channels", 2) != 2
                    or mic.get("resolve_channels_status", "expected") != "expected"
                    or (title is not None and not isinstance(title, str)) or len(channels) != 2
                    or any(type(ch) is not int or ch <= 0 for ch in channels) or channels[0] == channels[1]):
                raise ValueError(f"Unverified stereo mic for {name}")
            binding = (pov, name, expected_track, ordinal, title, tuple(channels))
            if media.setdefault(path, binding) != binding:
                raise ValueError(f"Conflicting POV or mic mappings for {path}")
    if review and origin + record != review_end:
        raise ValueError("Review length differs from the declared session coverage")
    return media


def marker_data(segment):
    base = {key: segment[key] for key in ("event_id", "suggestion_id", "title", "segment_id")}
    base.update({key: segment[key] for key in ("candidate_ids", "note_ids", "placeholder_reason") if key in segment})
    return base


def project_is_active(manager, project, name, *, project_id=None):
    current = manager.GetCurrentProject()
    return bool(current and current.GetName() == name
                and current.GetUniqueId() == (project_id if project_id is not None else project.GetUniqueId()))


def require_director_project_manager(manager):
    names = manager.GetProjectListInCurrentFolder()
    require(names is not None, "Could not enumerate projects in the current folder")
    current = manager.GetCurrentProject()
    if current:
        name = current.GetName()
        require(name == "Untitled Project" and name.casefold() not in {n.casefold() for n in names},
                "Director requires Project Manager. Save and close the active project first.")
        pool = current.GetMediaPool()
        root = pool.GetRootFolder() if pool else None
        require(current.GetTimelineCount() == 0 and root and root.GetClipList() == []
                and root.GetSubFolderList() == [],
                "Director requires Project Manager or its empty default Untitled Project.")
        project_id = require(current.GetUniqueId(), "Missing default project ID")
        require(project_is_active(manager, current, name, project_id=project_id),
                "Director project context changed before the project-manager operation")
    return names


def create_director_project(manager, name):
    media_dir = mkdtemp(prefix="autoclipper-export-media-")
    names = require_director_project_manager(manager)
    require(name.casefold() not in {n.casefold() for n in names}, f"Export project already exists: {name}")
    return require(manager.CreateProject(name, media_dir), "Resolve refused to create an export project")


def create_export_project(manager, name):
    current = manager.GetCurrentProject()
    active_name = current.GetName() if current else ""
    owned = any(active_name.startswith(prefix) and active_name[len(prefix):].strip()
                for prefix in (EXPORT_PREFIX, TEST_PROJECT_PREFIX, "AutoClipper Stage1 "))
    require(not current or active_name == "Untitled Project" or owned,
            "Save and close the active owner project before starting a Resolve export")
    if active_name == "Untitled Project":
        root = current.GetMediaPool().GetRootFolder()
        require(current.GetTimelineCount() == 0 and root.GetClipList() == [] and root.GetSubFolderList() == [],
                "The active Untitled Project contains work. Save and close it before exporting.")
    names = manager.GetProjectListInCurrentFolder()
    require(names is not None, "Could not enumerate projects in the current folder")
    require(name.casefold() not in {n.casefold() for n in [*names, active_name]},
            f"Export project already exists: {name}")
    project = require(manager.CreateProject(name, mkdtemp(prefix="autoclipper-export-media-")),
                      "Resolve refused to create an export project")
    require(project_is_active(manager, project, name), "New export project is not active")
    require(project.GetTimelineCount() == 0, "New export project already contains timelines")
    return project


def stereo_channels(mapping):
    tracks = list(mapping["track_mapping"].values())
    require(len(tracks) == 1 and tracks[0]["type"] == "stereo"
            and not tracks[0]["mute"] and len(tracks[0]["channel_idx"]) == 2,
            "Expected one unmuted stereo owner-mic pair")
    return tracks[0]["channel_idx"]


def _track_label(value):
    """Canonical form so 'Track2', 'Track 2' and 'track 02' compare equal."""
    label = str(value).strip().casefold()
    match = re.fullmatch(r"track\s*0*(\d+)", label)
    return f"track{match[1]}" if match else label


def import_sources(pool, media, fps):
    clips = {}
    for path, (_, name, track, ordinal, title, channels) in media.items():
        items = pool.ImportMedia([str(path)])
        require(items and len(items) == 1, f"Expected one imported clip for {path}")
        clip = items[0]
        require(Path(clip.GetClipProperty("File Path")).resolve() == path, f"Imported path differs for {path}")
        require(Fraction(clip.GetClipProperty("FPS")) == fps, f"Unexpected imported rate for {path}")
        audio = json.loads(clip.GetAudioMapping())
        inventory = audio["track_mapping"]
        matching = [key for key, entry in inventory.items()
                    if set(entry.get("channel_idx", [])) == set(channels)]
        key = str(ordinal)
        entry = inventory.get(key, {})
        labels = {str(entry[field]).strip().casefold() for field in ("name", "title", "track_name")
                  if entry.get(field)}
        track_labels = [re.fullmatch(r"track\s*0*(\d+)", label, re.I) for label in labels]
        title_conflicts = title and any(
            _track_label(label) not in {_track_label(title), _track_label(name), f"track{track}", "soundhandler"}
            for label in labels
        )
        embedded = audio.get("embedded_audio_channels", max(channels))
        require(matching == [key] and entry.get("channel_idx") == list(channels)
                and entry.get("type") == "stereo" and embedded >= max(channels)
                and not audio.get("linked_audio")
                and not title_conflicts and all(label is None or int(label[1]) == track for label in track_labels),
                f"Owner mic mismatch or ambiguity for {name} ({path}): "
                f"expected Resolve track {track} at inventory position {ordinal} "
                f"to uniquely map to embedded channels {list(channels)}")
        isolate_mic(clip, ordinal, list(channels))
        require(clip.SetName(name) and clip.GetName() == name, f"Could not set source clip name to {name}")
        clips[path] = clip
    return clips


def build_segment(resolve, project, pool, timeline, segment, multicam_name, clips, fps):
    sources = []
    for angle in segment["angles"]:
        clip = clips[Path(angle["mp4_path"]).resolve()]
        first, last = angle["source_in_frame"], angle["source_out_frame"]
        require(int(clip.GetClipProperty("Frames")) >= last, f"Source range exceeds media for {angle['part_id']}")
        sources.append((clip, first, last - 1, angle["part_id"]))

    modified = []
    try:
        for clip, first, last, part_id in sources:
            original = clip.GetMarkInOut()
            require(isinstance(original, dict), f"Could not read preexisting source marks for {part_id}")
            modified.append((clip, original, part_id))
            require(clip.SetMarkInOut(first, last), f"Could not set source marks for {part_id}")
            marks = clip.GetMarkInOut()
            expected = {"in": first, "out": last}
            require(marks.get("video") == marks.get("audio") == expected,
                    f"Source mark readback differs for {part_id}")
        items = pool.CreateMulticamClip([clip for clip, *_ in sources], {
            "name": multicam_name,
            "startTimecode": START_TIMECODE,
            "frameRate": fps,
            "angleSyncMode": resolve.MULTICAM_ANGLE_SYNC_IN,
            "angleNameMode": resolve.MULTICAM_ANGLE_NAME_CLIP,
            "multicamAudioMode": resolve.MULTICAM_AUDIO_SOURCE,
            "useFullClipExtents": False,
            "createBinForSourceClips": False,
        })
    except BaseException as original:
        try:
            restore_source_marks(modified)
        except Exception as restore_error:
            # The restore failure happened after, not because of, the original error.
            original.cleanup_failures = [*getattr(original, "cleanup_failures", []), restore_error]
        raise
    else:
        restore_source_marks(modified)
    require(items and len(items) == 1, f"Resolve did not create one multicam for {multicam_name}")
    multicam = items[0]
    length = segment["duration_frames"]
    require(multicam.GetClipProperty("Type") == "Multicam" and int(multicam.GetClipProperty("Frames")) == length,
            f"Unexpected multicam type or length for {multicam_name}")
    require(project.SetCurrentTimeline(timeline), "Could not activate the export timeline")
    offset = segment["record_start_frame"]
    record = timeline.GetStartFrame() + offset
    audio = segment.get("audio_angle")
    append_clip(pool, multicam, 0, length, record, 1, with_audio=audio is None)
    if audio is not None:
        source = clips[Path(audio["mp4_path"]).resolve()]
        require(int(source.GetClipProperty("Frames")) >= audio["source_out_frame"],
                "Selected audio range exceeds source media")
        append_clip(pool, source, audio["source_in_frame"], audio["source_out_frame"], record, 1, audio_only=True)
    marker = marker_data(segment)
    note = f"Event {marker['event_id']} | Suggestion {marker['suggestion_id']} | {marker['title']}"
    require(timeline.AddMarker(offset, "Blue", marker["title"] or marker["event_id"], note,
                               length, json.dumps(marker)),
            f"Could not add marker for {multicam_name}")


def render_placeholder(folder, segment, fps):
    """Create one silent labeled slate. Keep its scratch media for project reopen."""
    label = "MISSING FOOTAGE\n" + fill(segment["title"], width=38)
    (folder / "label.txt").write_text(label, encoding="utf-8")
    target = folder / "placeholder.mp4"
    executable = shutil.which("ffmpeg") or FFMPEG_BIN / "ffmpeg.exe"
    source = f"color=c=0x151a27:s=1280x720:r={fps}"
    draw = ("drawtext=textfile=label.txt:expansion=none:fontcolor=white:fontsize=48:"
            "line_spacing=14:x=(w-tw)/2:y=(h-th)/2")
    try:
        subprocess.run([str(executable), "-v", "error", "-nostdin", "-f", "lavfi", "-i", source,
                        "-vf", draw, "-frames:v", str(segment["duration_frames"]), "-an", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", str(target)], cwd=folder, capture_output=True, check=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Could not render a labeled slate with the installed ffmpeg") from exc
    return target


def build_placeholder(project, pool, timeline, segment, path):
    clips = pool.ImportMedia([str(path)])
    require(clips and len(clips) == 1, "Could not import the missing-footage slate")
    clip = clips[0]
    name = f"MISSING FOOTAGE: {segment['title']}"
    require(clip.SetName(name), "Could not label the missing-footage slate")
    length = segment["duration_frames"]
    require(int(clip.GetClipProperty("Frames")) == length, "Slate duration differs from the plan")
    require(project.SetCurrentTimeline(timeline), "Could not activate the edit timeline")
    offset = segment["record_start_frame"]
    append_clip(pool, clip, 0, length, timeline.GetStartFrame() + offset, 1)
    require(timeline.AddMarker(offset, "Red", name, segment["placeholder_reason"], length,
                               json.dumps(marker_data(segment))), "Could not label the missing beat")


def restore_source_marks(modified):
    failures = []
    for clip, original, part_id in reversed(modified):
        try:
            require(clip.ClearMarkInOut(), "Could not clear temporary marks")
            for kind in ("video", "audio"):
                marks = original.get(kind)
                if marks:
                    require(clip.SetMarkInOut(marks["in"], marks["out"], kind), f"Could not restore {kind} marks")
            restored = clip.GetMarkInOut()
            require(all((restored.get(kind) or {}) == (original.get(kind) or {})
                        for kind in ("video", "audio")), "Restored marks differ")
        except (AttributeError, KeyError, RuntimeError, TypeError) as exc:
            failures.append(f"{part_id}: {exc}")
    require(not failures, f"Could not restore source marks: {', '.join(failures)}")


def read_angles(multicam, segment):
    timeline = multicam.GetTimeline()
    if not timeline:
        return None  # Stage 1 could only verify these in the Resolve UI.
    expected = {Path(angle["mp4_path"]).resolve(): angle for angle in segment["angles"]}
    require(timeline.GetTrackCount("video") == len(expected), "Multicam angle count differs from manifest")
    rows = []
    for index in range(1, len(expected) + 1):
        items = timeline.GetItemListInTrack("video", index)
        require(items and len(items) == 1, "Expected one source item per angle")
        row = read_item(items[0])
        angle = expected.pop(Path(row["path"]).resolve(), None)
        require(angle and row["source_start"] == angle["source_in_frame"]
                and row["source_end"] in (angle["source_out_frame"] - 1, angle["source_out_frame"])
                and row["duration"] == segment["duration_frames"]
                and row["record_start"] == timeline.GetStartFrame(), "Multicam angle differs from manifest")
        row["angle_name"] = timeline.GetTrackName("video", index)
        require(row["angle_name"] == angle["pov_name"], "Multicam angle name differs from manifest")
        rows.append(row)
    return rows


def read_export(timeline, segments, names, placeholders=None):
    placeholders = placeholders or {}
    record = timeline.GetStartFrame()
    tracks = {kind: sorted(timeline.GetItemListInTrack(kind, 1) or [], key=lambda item: item.GetStart())
              for kind in ("video", "audio")}
    expected_audio = sum(bool(segment["angles"]) for segment in segments)
    require(len(tracks["video"]) == expected_audio + len(placeholders) and len(tracks["audio"]) == expected_audio,
            "Export must have one video and one audio item per segment")
    videos, audios = iter(tracks["video"]), iter(tracks["audio"])
    rows = []
    for segment, name in zip(segments, names):
        start = record + segment["record_start_frame"]
        length = segment["duration_frames"]
        if segment.get("kind") == "placeholder":
            video = next(videos)
            row = read_item(video)
            marker = json.loads(timeline.GetMarkerCustomData(segment["record_start_frame"]))
            require(Path(row["path"]).resolve() == placeholders[segment["segment_id"]]
                    and row["record_start"] == start and row["record_end"] == start + length
                    and row["duration"] == length and row["source_start"] == 0
                    and marker == marker_data(segment), "Missing-footage slate differs from the plan")
            rows.append({**segment, "record_start_frame": start, "video": row})
            continue
        if not segment["angles"]:
            rows.append({**segment, "record_start_frame": start})
            continue
        video, audio = next(videos), next(audios)
        multicam = video.GetMediaPoolItem()
        selected_audio = segment.get("audio_angle")
        for item in (video,) if selected_audio is not None else (video, audio):
            media = item.GetMediaPoolItem()
            require(media and media.GetUniqueId() == multicam.GetUniqueId() and media.GetName() == name
                    and media.GetClipProperty("Type") == "Multicam"
                    and int(media.GetClipProperty("Frames")) == length
                    and item.GetStart() == start and item.GetEnd() == start + length
                    and item.GetDuration() == length and item.GetSourceStartFrame() == 0,
                    f"Parent media or record range differs for {name}")
        if selected_audio is not None:
            actual = read_item(audio)
            require(Path(actual["path"]).resolve() == Path(selected_audio["mp4_path"]).resolve()
                    and actual["record_start"] == start and actual["record_end"] == start + length
                    and actual["duration"] == length and actual["source_start"] == selected_audio["source_in_frame"],
                    f"Selected microphone source or placement differs for {name}")
        audio_mapping = json.loads(audio.GetSourceAudioChannelMapping())
        stereo_channels(audio_mapping)
        marker = json.loads(timeline.GetMarkerCustomData(segment["record_start_frame"]))
        require(marker == marker_data(segment), f"Marker readback differs for {name}")
        timing = {key: segment[key] for key in ("kind", "session_start_frame", "session_end_frame", "audio_angle")
                  if key in segment}
        rows.append({
            **marker, **timing, "multicam": name, "record_start_frame": start, "duration_frames": length,
            "video": read_item(video), "audio": read_item(audio), "audio_mapping": audio_mapping,
            "angles": read_angles(multicam, segment),
            "planned_angles": segment["angles"], "unavailable": segment["unavailable"],
        })
    return rows


def build_export(manifest, *, dry_run=False, project_name=None):
    """Return the plan or a structured readback of the saved and reopened export."""
    media = validate_manifest(manifest)
    export_name = re.escape(f"{EXPORT_PREFIX}{manifest['session_id']} ")
    if project_name is not None and not re.fullmatch(
            rf"(?:{TEST_PROJECT_PREFIX}|{DIRECTOR_TEST_PREFIX}|{export_name})\d{{8}}-\d{{6}}", project_name):
        raise ValueError("Explicit project name must be a timestamped Stage2 Test, DirectorA Test or Export name")
    warnings = list(manifest["warnings"]) + [
        {"code": "short_segment", "event_id": segment["event_id"], "segment_id": segment["segment_id"],
         "message": f"Short multicam segment ({segment['duration_frames']} frames)."}
        for segment in manifest["segments"] if segment["angles"] and segment["duration_frames"] < manifest["fps"]
    ]
    if dry_run:
        return {"mode": "dry-run", "manifest": manifest, "warnings": warnings}
    for path in media:
        if not path.is_file():
            raise ValueError(f"MP4 is missing: {path}")

    slates = {}
    for segment in manifest["segments"]:
        if segment.get("kind") == "placeholder":
            folder = Path(mkdtemp(prefix="autoclipper-missing-footage-"))
            slates[segment["segment_id"]] = render_placeholder(folder, segment, manifest["fps"]).resolve()

    import DaVinciResolveScript

    resolve = require(DaVinciResolveScript.scriptapp("Resolve"), "Resolve is unavailable")
    manager = require(resolve.GetProjectManager(), "Resolve project manager is unavailable")
    session = manifest["session_id"]
    name = project_name or f"{EXPORT_PREFIX}{session} {datetime.now():%Y%m%d-%H%M%S}"
    director = (manifest.get("mode") in {"director_review", "director_edit_plan"}
                or name.startswith(DIRECTOR_TEST_PREFIX))
    project_id = None
    if director:
        project = create_director_project(manager, name)
        project_id = require(project.GetUniqueId(), "Missing new Director project ID")
        require(project_is_active(manager, project, name, project_id=project_id),
                "New Director project is not active")
        require(project.GetTimelineCount() == 0, "New Director project already contains timelines")
    else:
        project = create_export_project(manager, name)
    fps = manifest["fps"]
    require(project.SetSettings({"timelineFrameRate": str(fps)}), "Could not set export frame rate")
    pool = require(project.GetMediaPool(), "Export media pool is unavailable")
    timeline = require(pool.CreateEmptyTimeline(session, START_TIMECODE), "Could not create export timeline")
    require(Fraction(timeline.GetSettings()["timelineFrameRate"]) == fps, "Export timeline rate differs")
    clips = import_sources(pool, media, fps)
    segments = manifest["segments"]
    names = [f"{index:03d} {segment['segment_id']}" for index, segment in enumerate(segments, 1)]
    for segment, multicam_name in zip(segments, names):
        if segment["angles"]:
            build_segment(resolve, project, pool, timeline, segment, multicam_name, clips, fps)
        elif segment.get("kind") == "placeholder":
            build_placeholder(project, pool, timeline, segment, slates[segment["segment_id"]])
    read_export(timeline, segments, names, slates)
    require(project_is_active(manager, project, name, project_id=project_id), "Export project is no longer active")
    require(manager.SaveProject(), f"Could not save {name}")
    require(project_is_active(manager, project, name, project_id=project_id), "Export project changed before reload")
    require(manager.CloseProject(project), f"Saved {name}, but could not close it for verification")
    if director:
        require_director_project_manager(manager)
    reopened = require(manager.LoadProject(name), f"Saved {name}, but could not reopen it for verification")
    if director:
        require(reopened.GetUniqueId() == project_id, "Reopened Director project ID differs")
    require(project_is_active(manager, reopened, name, project_id=project_id), "Reopened export is not active")
    require(reopened.GetTimelineCount() == 1, "Reopened project must contain exactly one timeline")
    timeline = require(reopened.GetTimelineByIndex(1), "Reopened project has no timeline")
    require(timeline.GetName() == session and Fraction(timeline.GetSettings()["timelineFrameRate"]) == fps,
            "Reopened timeline name or frame rate differs")
    require(reopened.SetCurrentTimeline(timeline), "Could not activate the reopened export timeline")

    root = reopened.GetMediaPool().GetRootFolder()
    persisted = {}
    for clip in root.GetClipList() or []:
        path = clip.GetClipProperty("File Path")
        if path and Path(path).resolve() in media:
            key = Path(path).resolve()
            require(key not in persisted and key.is_file(), f"Unexpected duplicate or offline source: {key}")
            _, pov_name, _, _, _, channels = media[key]
            status = clip.GetClipProperty("Online Status")
            require(clip.GetName() == pov_name and "offline" not in str(status).casefold(),
                    f"Reopened media name or online status differs for {key}")
            require(stereo_channels(json.loads(clip.GetAudioMapping())) == list(channels),
                    f"Reopened mic mapping differs for {key}")
            persisted[key] = {"path": str(key), "pov_name": pov_name, "mic_channels": list(channels)}
    require(persisted.keys() == media.keys(), "Reopened export is missing source media")
    rows = read_export(timeline, segments, names, slates)
    limitations = [
        "Source audio provides one switchable owner-mic pair. Game audio is not identified by manifest v1.",
        "Reported source handles are not included in the marked multicam extents.",
    ]
    if any(row["angles"] is None for row in rows):
        limitations.append("Resolve did not expose the internal angle timeline. Verify angle names, switching and "
                           "segment-specific trims for repeated source media in the UI.")
    identity = {}
    if name.startswith(DIRECTOR_TEST_PREFIX):
        identity = {"project_id": require(reopened.GetUniqueId(), "Missing project ID"),
                    "timeline_id": require(timeline.GetUniqueId(), "Missing timeline ID")}
    review = {key: manifest[key] for key in ("mode", "review_session_frames", "uncovered_edges", "edit_plan")
              if key in manifest}
    return {"project": name, "timeline": session, "fps": fps, "reopened": True, **identity, **review,
            "sources": list(persisted.values()), "segments": rows,
            "warnings": warnings, "limitations": limitations}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--project", help="Optional new project name: AutoClipper Stage2 Test YYYYMMDD-HHMMSS "
                                          "or AutoClipper Export <session_id> YYYYMMDD-HHMMSS")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Print the plan without contacting Resolve")
    mode.add_argument("--execute", action="store_true",
                      help="Approved Resolve window only. Save and close the active owner project first.")
    parser.add_argument("--result-file", type=Path,
                        help="Also write the JSON result to this new file (for callers such as the UI)")
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        if args.result_file is not None and args.result_file.exists():
            raise ValueError(f"Result file already exists: {args.result_file}")
        result = build_export(manifest, dry_run=args.dry_run, project_name=args.project)
        if args.result_file is not None:
            from .store import _write_json
            _write_json(args.result_file, result)
        print(json.dumps(result, indent=2))
    except (AttributeError, ImportError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        parser.exit(1, describe_failure(exc))


def describe_failure(exc):
    """Original error first, then its causes, then later cleanup failures, each labelled."""
    lines = [f"Resolve export stopped: {exc}"]
    cause = exc.__cause__
    while cause is not None:
        lines.append(f"Caused by: {cause}")
        cause = cause.__cause__
    lines.extend(f"Cleanup also failed: {failure}" for failure in getattr(exc, "cleanup_failures", []))
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
