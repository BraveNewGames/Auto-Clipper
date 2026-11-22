"""Add one rough-cut timeline to the currently open Auto-Clipper-created Resolve project.

Opt-in alternative to creating a new project. Only rough-cut manifests (edit-plan mode with a
``rough_cut`` block) may use it. Pre-existing timelines, bins, media and clips are never renamed,
overwritten or deleted. Existing source items for the same MP4 are reused when they already carry
the exact mic mapping the manifest needs; otherwise nothing is changed.
"""

import json
import re
from fractions import Fraction
from pathlib import Path
from tempfile import mkdtemp

from .resolve_builder import (EXPORT_PREFIX, REFERENCE_TRACK_NAME, START_TIMECODE, append_reference,
                              build_placeholder, build_segment, import_one_reference, import_sources,
                              read_export, render_placeholder, stereo_channels, validate_manifest)
from .resolve_manifest import verify_source_media_identity
from .resolve_stage1 import require

OWNED_PROJECT = re.compile(re.escape(EXPORT_PREFIX) + r"\S.* \d{8}-\d{6}")
MAX_NAME = 120


def is_rough_cut(manifest):
    return manifest.get("mode") == "director_edit_plan" and isinstance(manifest.get("rough_cut"), dict)


def require_owned_open_project(manager):
    current = manager.GetCurrentProject()
    require(current, "No Resolve project is open. Open the Auto-Clipper project to add to, or build into a "
                     "new project instead.")
    name = current.GetName()
    require(isinstance(name, str) and OWNED_PROJECT.fullmatch(name),
            f"The open project “{name}” was not created by Auto-Clipper (expected a name like "
            f"“{EXPORT_PREFIX}<session> YYYYMMDD-HHMMSS”). Nothing was changed.")
    return current, name


def list_timelines(project):
    count = project.GetTimelineCount()
    require(type(count) is int, "Could not enumerate the project's timelines")
    rows = []
    for index in range(1, count + 1):
        timeline = require(project.GetTimelineByIndex(index), f"Could not read timeline {index}")
        rows.append((timeline.GetUniqueId(), timeline.GetName()))
    return rows


def walk_clips(folder):
    """(unique_id, name) of every pool item below a folder."""
    return [(clip.GetUniqueId(), clip.GetName()) for clip in clips_below(folder)]


def unique_names(base, timeline_names, clip_names, segment_ids):
    """First 'base', 'base (2)', ... whose timeline and multicam names are all unused (case-insensitive)."""
    taken = {n.casefold() for n in (*timeline_names, *clip_names) if isinstance(n, str)}
    for attempt in range(1, 1000):
        timeline = base if attempt == 1 else f"{base} ({attempt})"
        multicams = [f"{timeline} | {index:03d} {sid}" for index, sid in enumerate(segment_ids, 1)]
        if timeline.casefold() not in taken and not any(m.casefold() in taken for m in multicams):
            return timeline, multicams
    raise RuntimeError("Could not find an unused timeline name")


def find_reusable(root, path, expect):
    """Existing, healthy, non-multicam root-bin item for the same MP4 path, or None.

    Raises when an item for the path exists but is not safe to reuse (nothing is changed).
    """
    matches = []
    for clip in root.GetClipList() or []:
        raw = clip.GetClipProperty("File Path")
        if isinstance(raw, str) and raw and Path(raw).resolve() == path:
            matches.append(clip)
    if not matches:
        return None
    pov_name, channels, fps = expect
    for clip in matches:
        try:
            ok = (clip.GetClipProperty("Type") != "Multicam" and clip.GetName() == pov_name
                  and "offline" not in str(clip.GetClipProperty("Online Status")).casefold()
                  and Fraction(clip.GetClipProperty("FPS")) == fps
                  and stereo_channels(json.loads(clip.GetAudioMapping())) == list(channels))
        except (RuntimeError, KeyError, TypeError, ValueError, ZeroDivisionError):
            ok = False
        if ok:
            return clip
    raise RuntimeError(f"The project already has an item for {path} that is not mapped to {pov_name}'s mic "
                       "as this cut needs. It was left untouched and nothing was added.")


def find_reference(folder, path, track, fps):
    for clip in folder.GetClipList() or []:
        raw = clip.GetClipProperty("File Path")
        if isinstance(raw, str) and raw and Path(raw).resolve() == path:
            try:
                if (clip.GetName() == REFERENCE_TRACK_NAME and Fraction(clip.GetClipProperty("FPS")) == fps
                        and stereo_channels(json.loads(clip.GetAudioMapping())) == track["resolve_channels"]):
                    return clip
            except (RuntimeError, KeyError, TypeError, ValueError, ZeroDivisionError):
                pass
            raise RuntimeError(f"The {REFERENCE_TRACK_NAME} bin already has an item for {path} that is not "
                               "mapped as this cut needs. It was left untouched and nothing was added.")
    return None


def build_into_open_project(manifest, resolve, manager, media, slates, timeline_base):
    """Create the timeline, verify only it, and return the readback. Raises on any failure."""
    fps = manifest["fps"]
    project, project_name = require_owned_open_project(manager)
    project_id = require(project.GetUniqueId(), "Missing open project ID")

    def still_active():
        current = manager.GetCurrentProject()
        return bool(current and current.GetName() == project_name and current.GetUniqueId() == project_id)

    require(Fraction(str(project.GetSetting("timelineFrameRate"))) == fps,
            f"The open project's timeline frame rate differs from this cut ({fps} fps). "
            "Project settings are not changed; nothing was added.")
    pool = require(project.GetMediaPool(), "Media pool is unavailable")
    root = require(pool.GetRootFolder(), "Media pool root is unavailable")
    before_timelines = list_timelines(project)
    before_clips = walk_clips(root)
    before_ids = {uid for uid, _ in before_clips}
    segments = manifest["segments"]
    timeline_name, names = unique_names(
        timeline_base, [n for _, n in before_timelines], [n for _, n in before_clips],
        [segment["segment_id"] for segment in segments])

    # Decide reuse before changing anything. This only reads.
    clips, to_import = {}, {}
    for path, binding in media.items():
        found = find_reusable(root, path, (binding[1], binding[5], fps))
        if found is None:
            to_import[path] = binding
        else:
            clips[path] = found
    reference_folder = next((f for f in root.GetSubFolderList() or [] if f.GetName() == REFERENCE_TRACK_NAME), None)
    references = {Path(ref["mp4_path"]).resolve(): ref["track"]
                  for segment in segments if (ref := segment.get("reference_audio"))}
    reference_clips, reference_import = {}, {}
    for path, track in references.items():
        found = find_reference(reference_folder, path, track, fps) if reference_folder else None
        if found is None:
            reference_import[path] = track
        else:
            reference_clips[path] = found
    require(still_active(), "The open project changed before the build started")

    created = {"timeline": None, "folder": None}
    try:
        timeline = require(pool.CreateEmptyTimeline(timeline_name, START_TIMECODE), "Could not create the timeline")
        created["timeline"] = timeline
        require(Fraction(timeline.GetSettings()["timelineFrameRate"]) == fps, "New timeline rate differs")
        clips.update(import_sources(pool, to_import, fps))
        if reference_import:
            folder = reference_folder
            if folder is None:
                folder = require(pool.AddSubFolder(root, REFERENCE_TRACK_NAME), "Could not create reference audio bin")
                created["folder"] = folder
            require(pool.SetCurrentFolder(folder), "Could not select reference audio bin")
            try:
                for path, track in reference_import.items():
                    reference_clips[path] = import_one_reference(pool, path, track, clips[path], fps)
            finally:
                require(pool.SetCurrentFolder(root), "Could not restore source media bin")
        if references:
            require(timeline.AddTrack("audio", "stereo"), "Could not add stereo GROUP REFERENCE AUDIO track")
            require(timeline.SetTrackName("audio", 2, REFERENCE_TRACK_NAME)
                    and timeline.GetTrackName("audio", 2) == REFERENCE_TRACK_NAME
                    and timeline.GetTrackSubType("audio", 2) == "stereo",
                    "Could not label stereo GROUP REFERENCE AUDIO track")
            require(timeline.SetTrackEnable("audio", 2, True) and timeline.GetIsTrackEnabled("audio", 2),
                    "Could not enable GROUP REFERENCE AUDIO track")
        for segment, multicam_name in zip(segments, names):
            if segment["angles"]:
                build_segment(resolve, project, pool, timeline, segment, multicam_name, clips, fps)
                append_reference(pool, timeline, segment, reference_clips)
            elif segment.get("kind") == "placeholder":
                build_placeholder(project, pool, timeline, segment, slates[segment["segment_id"]])
        read_export(timeline, segments, names, slates)
        require(still_active(), "The open project is no longer active")
        require(manager.SaveProject(), f"Could not save {project_name}")
        require(still_active(), "The open project changed after saving")

        # Read back only the new timeline; everything else must be exactly as before.
        after = list_timelines(project)
        new_id = timeline.GetUniqueId()
        require(after[:len(before_timelines)] == before_timelines and len(after) == len(before_timelines) + 1
                and after[-1] == (new_id, timeline_name),
                "Existing timelines changed or the new timeline is not the only addition")
        found = require(project.GetTimelineByIndex(len(after)), "Could not reopen the new timeline by index")
        require(found.GetUniqueId() == new_id and found.GetName() == timeline_name
                and Fraction(found.GetSettings()["timelineFrameRate"]) == fps,
                "New timeline name, ID or frame rate differs")
        require(project.SetCurrentTimeline(found), "Could not activate the new timeline")
        rows = read_export(found, segments, names, slates)
        sources = []
        for path, binding in media.items():
            clip = clips[path]
            status = clip.GetClipProperty("Online Status")
            require(clip.GetName() == binding[1] and "offline" not in str(status).casefold()
                    and stereo_channels(json.loads(clip.GetAudioMapping())) == list(binding[5]),
                    f"Source media name, status or mic mapping differs for {path}")
            sources.append({"path": str(path), "pov_name": binding[1], "mic_channels": list(binding[5]),
                            "reused": path not in to_import})
    except BaseException as original:
        failures = rollback(pool, root, project, created, before_ids)
        original.cleanup_failures = [*getattr(original, "cleanup_failures", []), *failures]
        raise
    return {"project": project_name, "timeline": timeline_name, "added_timeline": True, "fps": fps,
            "reopened": False, "preexisting_timelines": [name for _, name in before_timelines],
            "reused_sources": [s["path"] for s in sources if s["reused"]],
            "imported_sources": [s["path"] for s in sources if not s["reused"]],
            "reused_reference_sources": [str(p) for p in references if p not in reference_import],
            "sources": sources, "segments": rows}


def rollback(pool, root, project, created, before_ids):
    """Remove only what this run created and can identify exactly. Return what could not be removed."""
    failures = []

    def attempt(label, action):
        try:
            if not action():
                failures.append(f"{label}: Resolve refused to remove it; it was left in the project")
        except Exception as exc:
            failures.append(f"{label}: {exc}; it was left in the project")

    if created["timeline"] is not None:
        attempt("new timeline", lambda: pool_delete_timeline(project, pool, created["timeline"]))
    try:
        new_items = [clip for clip in clips_below(root) if clip.GetUniqueId() not in before_ids]
    except Exception as exc:
        failures.append(f"could not list new media pool items ({exc}); any items created by this run were left")
        new_items = []
    if new_items:
        attempt(f"{len(new_items)} new media pool item(s)", lambda: pool.DeleteClips(new_items))
    if created["folder"] is not None:
        attempt(f"new {REFERENCE_TRACK_NAME} bin", lambda: pool.DeleteFolders([created["folder"]]))
    return failures


def pool_delete_timeline(project, pool, timeline):
    return pool.DeleteTimelines([timeline])


def clips_below(folder, depth=0):
    rows = list(folder.GetClipList() or [])
    if depth < 8:
        for sub in folder.GetSubFolderList() or []:
            rows.extend(clips_below(sub, depth + 1))
    return rows


def add_timeline_to_open_project(manifest, timeline_base):
    """Entry point used by resolve_builder.build_export(add_to_open=True)."""
    if not is_rough_cut(manifest):
        raise ValueError("Adding to the open project is only available for AI rough cuts")
    media = validate_manifest(manifest)
    for path in media:
        if not path.is_file():
            raise ValueError(f"MP4 is missing: {path}")
    verify_source_media_identity(manifest, media)
    slates = {}
    for segment in manifest["segments"]:
        if segment.get("kind") == "placeholder":
            folder = Path(mkdtemp(prefix="autoclipper-missing-footage-"))
            slates[segment["segment_id"]] = render_placeholder(folder, segment, manifest["fps"]).resolve()
    import DaVinciResolveScript

    resolve = require(DaVinciResolveScript.scriptapp("Resolve"), "Resolve is unavailable")
    manager = require(resolve.GetProjectManager(), "Resolve project manager is unavailable")
    base = re.sub(r"\s+", " ", str(timeline_base or manifest["rough_cut"].get("title") or manifest["session_id"])).strip()
    result = build_into_open_project(manifest, resolve, manager, media, slates, base[:MAX_NAME] or manifest["session_id"])
    return result
