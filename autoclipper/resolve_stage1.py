"""Offline timing and scratch-only Resolve Studio Stage 1 experiment."""

import argparse
import json
from dataclasses import asdict, dataclass, replace
from fractions import Fraction
from math import floor
from pathlib import Path
from tempfile import mkdtemp

from .mapping import map_range
from .models import Session


@dataclass(frozen=True)
class Placement:
    pov_id: str
    part_id: str
    source_in: int
    source_out: int  # Exclusive
    record_in: int  # Relative to the timeline start
    record_out: int  # Exclusive


def plan_placements(session, start, end, fps):
    """Round to the nearest frame, with ties toward +infinity.

    Floor the source duration. A part-boundary clamp can shift the last
    piece back by up to one frame, or shorten it by one frame when needed.
    """
    rate = Fraction(str(fps))
    if rate <= 0 or Fraction(str(end)) <= Fraction(str(start)):
        raise ValueError("Expected a positive frame rate and a nonempty event")

    for _, part in session.all_parts():
        if part.duration is None and part.offset < end:
            raise ValueError(f"Unknown media duration for {part.part_id}")

    # Disable map_range's SRT fallback, keeping this conversion independent of files.
    session = replace(session, povs=[
        replace(pov, parts=[replace(part, srt_path=None) for part in pov.parts])
        for pov in session.povs
    ])
    origin = Fraction(str(start))
    placements = []
    for piece in map_range(session, start, end, pad_before=0, pad_after=0):
        part = session.part_by_id(piece.part_id)
        if piece.duration_unknown:
            raise ValueError(f"Unverified media duration for {piece.part_id}")
        first = Fraction(str(piece.session_start))
        last = Fraction(str(piece.session_end))
        record_in = floor((first - origin) * rate + Fraction(1, 2))
        record_out = floor((last - origin) * rate + Fraction(1, 2))
        source_in = floor((first - Fraction(str(part.offset))) * rate + Fraction(1, 2))
        # Use the destination length so adjoining pieces share a boundary and
        # both angles have the same length despite different source frame phases.
        source_limit = floor(Fraction(str(part.duration)) * rate)
        length = min(record_out - record_in, source_limit)
        record_out = record_in + length
        # At a fractional boundary, preserve the record length by moving the
        # source In back if independent rounding would overrun the source.
        source_in = min(source_in, source_limit - length)
        source_out = source_in + length
        if not 0 <= source_in < source_out <= source_limit:
            raise ValueError(f"Empty or out-of-bounds frame range for {piece.part_id}")
        placements.append(Placement(
            piece.pov_id, piece.part_id, source_in, source_out, record_in, record_out,
        ))
    return placements


def require(value, message):
    if not value:
        raise RuntimeError(message)
    return value


def read_item(item):
    clip = require(item.GetMediaPoolItem(), "Timeline item has no media reference")
    return {
        "name": clip.GetName(),
        "path": clip.GetClipProperty("File Path"),
        "source_start": item.GetSourceStartFrame(),
        "source_end": item.GetSourceEndFrame(),
        "record_start": item.GetStart(),
        "record_end": item.GetEnd(),
        "duration": item.GetDuration(),
    }


def verify_item(item, source_in, source_out, record_in, *, verify_source_end=True):
    row = read_item(item)
    print(json.dumps(row))
    length = source_out - source_in
    require(
        row["source_start"] == source_in
        and (not verify_source_end or row["source_end"] in (source_out - 1, source_out))
        and row["record_start"] == record_in
        and row["record_end"] == record_in + length
        and row["duration"] == length,
        "Resolve readback differs from planned source or record range",
    )
    return row


def append_clip(pool, clip, first, last, record, track, *, with_audio=False):
    # Resolve 21.2.0.6 treated the append endpoint as exclusive.
    info = {
        "mediaPoolItem": clip,
        "startFrame": first,
        "endFrame": last,
        "recordFrame": record,
        "trackIndex": track,
    }
    if not with_audio:
        info["mediaType"] = 1
    require(pool.AppendToTimeline([info]), "Resolve did not append the clip")


def isolate_mic(clip, track, channels):
    mapping = json.loads(clip.GetAudioMapping())
    source = mapping["track_mapping"].get(str(track))
    require(
        source and source["type"] == "stereo" and source["channel_idx"] == channels,
        "Requested mic track and channels do not match the imported source",
    )
    mapping["track_mapping"] = {"1": {**source, "mute": False}}
    require(clip.SetAudioMapping(json.dumps(mapping)), "Resolve refused the mic mapping")
    readback = json.loads(clip.GetAudioMapping())
    require(readback == mapping, "Mic mapping readback differs from the requested mapping")
    return readback


def assemble(project_name, inputs, fps, stacked, mics=None):
    prefix = "AutoClipper Stage1 "
    require(project_name and project_name.startswith(prefix) and project_name[len(prefix):].strip(),
            f"Use a fresh --project name beginning with {prefix!r}")
    # This module is only imported after CLI preflight and outside dry-run.
    import DaVinciResolveScript

    resolve = require(DaVinciResolveScript.scriptapp("Resolve"), "Resolve is unavailable")
    manager = require(resolve.GetProjectManager(), "Resolve project manager is unavailable")
    current = manager.GetCurrentProject()
    current_name = current.GetName() if current else None
    require(
        not current or current_name == "Untitled Project"
        or (isinstance(current_name, str) and current_name.startswith(prefix)
            and current_name[len(prefix):].strip()),
        "Close the active owner project before executing. Use Project Manager, "
        "the default Untitled Project, or an AutoClipper Stage1 scratch project.",
    )
    if current_name == "Untitled Project":
        root = current.GetMediaPool().GetRootFolder()
        require(current.GetTimelineCount() == 0 and root.GetClipList() == []
                and root.GetSubFolderList() == [],
                "The active Untitled Project contains work. Save and close it before executing.")
    names = manager.GetProjectListInCurrentFolder()
    require(names is not None, "Could not enumerate projects in the current folder")
    existing = list(names) + ([current_name] if current_name else [])
    require(
        project_name.casefold() not in {name.casefold() for name in existing},
        "Scratch project name already exists",
    )
    project = require(
        manager.CreateProject(project_name, mkdtemp(prefix="autoclipper-resolve-media-")),
        "Resolve refused to create a new scratch project",
    )
    active = manager.GetCurrentProject()
    require(active and active.GetUniqueId() == project.GetUniqueId(), "Scratch project is not active")
    require(project.GetTimelineCount() == 0, "Scratch project already contains timelines")
    require(project.SetSettings({"timelineFrameRate": str(fps)}), "Could not set timeline fps")
    pool = require(project.GetMediaPool(), "Scratch media pool is unavailable")
    timeline = require(pool.CreateEmptyTimeline("Stage 1", "01:00:00:00"), "Cannot create timeline")
    require(project.SetCurrentTimeline(timeline), "Could not activate scratch timeline")
    require(Fraction(timeline.GetSettings()["timelineFrameRate"]) == fps, "Timeline rate differs")
    record = timeline.GetStartFrame()
    clips = []
    for placement, path in inputs:
        # Studio 21.2.0.6 accepts the path list for these MP4s. The documented
        # dictionary form returned an empty list in the live scratch test.
        imported = pool.ImportMedia([str(path)])
        require(imported and len(imported) == 1, "Expected one imported clip per path")
        clip = imported[0]
        require(Path(clip.GetClipProperty("File Path")).resolve() == path, "Imported path differs")
        require(Fraction(clip.GetClipProperty("FPS")) == fps, "Imported source rate differs")
        require(int(clip.GetClipProperty("Frames")) >= placement.source_out, "MP4 range out of bounds")
        audio_mapping = (
            isolate_mic(clip, *mics[placement.part_id]) if mics else json.loads(clip.GetAudioMapping())
        )
        print(json.dumps({
            "part_id": placement.part_id,
            "path": str(path),
            "start_tc": clip.GetClipProperty("Start TC"),
            "audio_mapping": audio_mapping,
        }))
        clips.append(clip)

    if stacked:
        while timeline.GetTrackCount("video") < len(clips):
            require(timeline.AddTrack("video"), "Could not add a video track")
        for track, ((placement, path), clip) in enumerate(zip(inputs, clips), 1):
            append_clip(pool, clip, placement.source_in, placement.source_out, record, track)
            items = timeline.GetItemListInTrack("video", track)
            require(items and len(items) == 1, "Expected one item on the stacked video track")
            row = verify_item(items[0], placement.source_in, placement.source_out, record)
            require(Path(row["path"]).resolve() == path, "Stacked item references unexpected media")
    else:
        for (placement, _), clip in zip(inputs, clips):
            require(
                clip.SetMarkInOut(placement.source_in, placement.source_out - 1),
                "Could not set angle In/Out marks",
            )
            marks = clip.GetMarkInOut()
            print(json.dumps({"part_id": placement.part_id, "source_marks": marks}))
            expected = {"in": placement.source_in, "out": placement.source_out - 1}
            for kind in ("video", "audio") if mics else ("video",):
                require(marks.get(kind) == expected, f"Angle {kind} marks differ from the plan")
        options = {
            "name": "Stage 1 Angles",
            "startTimecode": "01:00:00:00",
            "frameRate": fps,
            "angleSyncMode": resolve.MULTICAM_ANGLE_SYNC_IN,
            "useFullClipExtents": False,
            "createBinForSourceClips": False,
        }
        if mics:
            options["multicamAudioMode"] = resolve.MULTICAM_AUDIO_SOURCE
        multicam = pool.CreateMulticamClip(clips, options)
        require(multicam and len(multicam) == 1, "Resolve did not create one multicam clip")
        require(
            multicam[0].GetClipProperty("Type") == "Multicam",
            "Created media is not reported as Multicam",
        )
        require(project.SetCurrentTimeline(timeline), "Could not reactivate scratch timeline")
        length = inputs[0][0].record_out
        append_clip(pool, multicam[0], 0, length, record, 1, with_audio=bool(mics))
        require(int(multicam[0].GetClipProperty("Frames")) == length, "Multicam media length differs")
        # The composite source-end getter returned 1918 for a 1920-frame
        # full-length edit. Check parent record bounds and media length instead.
        for kind in ("video", "audio") if mics else ("video",):
            items = timeline.GetItemListInTrack(kind, 1)
            require(items and len(items) == 1, f"Expected one multicam parent {kind} item")
            item = items[0]
            verify_item(item, 0, length, record, verify_source_end=False)
            require(
                item.GetMediaPoolItem().GetUniqueId() == multicam[0].GetUniqueId(),
                "Parent edit references unexpected multicam media",
            )
            if kind == "audio":
                mapping = json.loads(item.GetSourceAudioChannelMapping())
                print(json.dumps({"parent_audio_mapping": mapping}))
                tracks = list(mapping["track_mapping"].values())
                require(
                    len(tracks) == 1 and tracks[0]["type"] == "stereo" and not tracks[0]["mute"]
                    and len(tracks[0]["channel_idx"]) == 2,
                    "Expected one unmuted stereo output on the multicam parent",
                )

        # Read the underlying angle's actual source trims, not only the parent edit.
        angles = multicam[0].GetTimeline()
        if angles:
            require(angles.GetTrackCount("video") == 2, "Expected two angle tracks")
            expected = {path: placement for placement, path in inputs}
            for track in (1, 2):
                items = angles.GetItemListInTrack("video", track)
                require(items and len(items) == 1, "Expected one source item per angle")
                path = Path(items[0].GetMediaPoolItem().GetClipProperty("File Path")).resolve()
                require(path in expected, "Unexpected or duplicate angle media reference")
                placement = expected.pop(path)
                verify_item(items[0], placement.source_in, placement.source_out, angles.GetStartFrame())
        else:
            print("Native angle timeline unavailable through the SDK. Verify angles in the Edit page.")

    active = manager.GetCurrentProject()
    require(active and active.GetUniqueId() == project.GetUniqueId(), "Scratch project is not active")
    require(manager.SaveProject(), "Could not save the scratch project")
    print(f"Saved scratch project {project_name}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_copy", type=Path, help="Scratch copy of session.json")
    parser.add_argument("start", type=float, help="Event start in session seconds")
    parser.add_argument("end", type=float, help="Event end in session seconds")
    parser.add_argument("--fps", type=int, required=True, help="Common integer source/timeline fps")
    parser.add_argument("--source", nargs=2, action="append", required=True, metavar=("PART_ID", "MP4"))
    parser.add_argument("--mic", nargs=3, action="append", metavar=("PART_ID", "TRACK", "CHANNELS"),
                        help="Native audio: one entry per source, with comma-separated embedded channels")
    parser.add_argument("--project", help="Unused project name beginning with AutoClipper Stage1 ")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Print the plan without opening Resolve")
    mode.add_argument("--execute", action="store_true",
                      help="Approved Resolve window only. Save and close owner projects first. "
                           "Use Project Manager, an empty default Untitled Project, or a Stage 1 scratch project.")
    parser.add_argument("--stacked", action="store_true", help="Use the stacked-video timing fallback")
    args = parser.parse_args(argv)
    try:
        session = Session.from_dict(json.loads(args.session_copy.read_text(encoding="utf-8")))
        if len(args.source) != 2 or len({key for key, _ in args.source}) != 2:
            raise ValueError("Supply two distinct part IDs with --source")
        ids = {key for key, _ in args.source}
        mics = {}
        if args.mic:
            if args.stacked or len(args.mic) != 2 or {row[0] for row in args.mic} != ids:
                raise ValueError("Supply one --mic entry per source for native multicam")
            for part_id, raw_track, raw_channels in args.mic:
                track = int(raw_track)
                channels = [int(channel) for channel in raw_channels.split(",")]
                if track <= 0 or len(channels) != 2 or len(set(channels)) != 2 or min(channels) <= 0:
                    raise ValueError("Expected a positive track and two distinct positive stereo channels")
                mics[part_id] = (track, channels)
        session = replace(session, povs=[
            replace(pov, parts=[part for part in pov.parts if part.part_id in ids])
            for pov in session.povs
        ])
        placements = plan_placements(session, args.start, args.end, args.fps)
        if (len(placements) != 2 or len({p.pov_id for p in placements}) != 2
                or any(p.record_in != 0 for p in placements)
                or placements[0].record_out != placements[1].record_out):
            raise ValueError("Stage 1 requires one fully covering part per POV with matching frame lengths")
        for placement in placements:
            part = session.part_by_id(placement.part_id)
            if not part.offset <= args.start < args.end <= part.offset + part.duration:
                raise ValueError("Both source parts must cover the entire unrounded event")
        by_id = {p.part_id: p for p in placements}
        inputs = []
        for part_id, raw_path in args.source:
            path = Path(raw_path)
            source = Path(session.part_by_id(part_id).video_path or "")
            if path.suffix.casefold() != ".mp4" or path.stem.casefold() != source.stem.casefold():
                raise ValueError(f"Supply the same-stem OBS MP4 for {part_id}")
            path = path.resolve()
            if args.execute and not path.is_file():
                raise ValueError(f"MP4 is missing for {part_id}")
            inputs.append((by_id[part_id], path))
        if inputs[0][1] == inputs[1][1]:
            raise ValueError("Supply two distinct MP4 paths")
        print(json.dumps({
            "fps": args.fps,
            "session_range": [args.start, args.end],
            "mode": "stacked" if args.stacked else "multicam",
            "placements": [
                {**asdict(p), "mp4": str(path), **(
                    {"mic": {"track": mics[p.part_id][0], "channels": mics[p.part_id][1]}} if mics else {}
                )}
                for p, path in inputs
            ],
        }, indent=2))
        if args.execute:
            assemble(args.project, inputs, args.fps, args.stacked, mics)
    except (AttributeError, ImportError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        parser.exit(1, f"Stage 1 stopped: {exc}\n")


if __name__ == "__main__":
    main()
