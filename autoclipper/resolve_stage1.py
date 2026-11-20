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
    """Use one common source and timeline rate and round ties toward +infinity."""
    rate = Fraction(str(fps))
    if rate <= 0 or Fraction(str(end)) <= Fraction(str(start)):
        raise ValueError("Expected a positive frame rate and a nonempty event")

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
        source_out = source_in + record_out - record_in
        if not 0 <= source_in < source_out <= floor(Fraction(str(part.duration)) * rate):
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


def verify_item(item, source_in, source_out, record_in):
    row = read_item(item)
    print(json.dumps(row))
    length = source_out - source_in
    require(
        row["source_start"] == source_in
        and row["source_end"] in (source_out - 1, source_out)
        and row["record_start"] == record_in
        and row["record_end"] == record_in + length
        and row["duration"] == length,
        "Resolve readback differs from planned source or record range",
    )
    return row


def append_video(pool, clip, first, last, record, track):
    # The documented API leaves the end convention unspecified. Submit the
    # last included source frame and check Resolve's resulting edit length.
    items = pool.AppendToTimeline([{
        "mediaPoolItem": clip,
        "startFrame": first,
        "endFrame": last - 1,
        "recordFrame": record,
        "trackIndex": track,
        "mediaType": 1,
    }])
    require(items and len(items) == 1, "Resolve did not append exactly one video item")
    return items[0]


def assemble(project_name, inputs, fps, stacked):
    # This module is only imported after CLI preflight and outside dry-run.
    import DaVinciResolveScript

    resolve = require(DaVinciResolveScript.scriptapp("Resolve"), "Resolve is unavailable")
    manager = require(resolve.GetProjectManager(), "Resolve project manager is unavailable")
    current = manager.GetCurrentProject()
    names = manager.GetProjectListInCurrentFolder()
    require(names is not None, "Could not enumerate projects in the current folder")
    existing = list(names) + ([current.GetName()] if current else [])
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
        imported = pool.ImportMedia([{"FilePath": str(path)}])
        require(imported and len(imported) == 1, "Expected one imported clip per path")
        clip = imported[0]
        require(Path(clip.GetClipProperty("File Path")).resolve() == path, "Imported path differs")
        require(Fraction(clip.GetClipProperty("FPS")) == fps, "Imported source rate differs")
        require(int(clip.GetClipProperty("Frames")) >= placement.source_out, "MP4 range out of bounds")
        print(json.dumps({
            "part_id": placement.part_id,
            "path": str(path),
            "start_tc": clip.GetClipProperty("Start TC"),
            "audio_mapping": clip.GetAudioMapping(),
        }))
        clips.append(clip)

    if stacked:
        while timeline.GetTrackCount("video") < len(clips):
            require(timeline.AddTrack("video"), "Could not add a video track")
        for track, ((placement, path), clip) in enumerate(zip(inputs, clips), 1):
            item = append_video(pool, clip, placement.source_in, placement.source_out, record, track)
            row = verify_item(item, placement.source_in, placement.source_out, record)
            require(Path(row["path"]).resolve() == path, "Stacked item references unexpected media")
    else:
        for (placement, _), clip in zip(inputs, clips):
            require(
                clip.SetMarkInOut(placement.source_in, placement.source_out - 1),
                "Could not set angle In/Out marks",
            )
        multicam = pool.CreateMulticamClip(clips, {
            "name": "Stage 1 Angles",
            "startTimecode": "01:00:00:00",
            "frameRate": fps,
            "angleSyncMode": resolve.MULTICAM_ANGLE_SYNC_IN,
            "useFullClipExtents": False,
            "createBinForSourceClips": False,
        })
        require(multicam and len(multicam) == 1, "Resolve did not create one multicam clip")
        require(project.SetCurrentTimeline(timeline), "Could not reactivate scratch timeline")
        length = inputs[0][0].record_out
        item = append_video(pool, multicam[0], 0, length, record, 1)
        verify_item(item, 0, length, record)
        require(
            item.GetMediaPoolItem().GetUniqueId() == multicam[0].GetUniqueId(),
            "Parent edit references unexpected multicam media",
        )

        # Read the underlying angle's actual source trims, not only the parent edit.
        angles = require(multicam[0].GetTimeline(), "Cannot read multicam angle timeline")
        require(angles.GetTrackCount("video") == 2, "Expected two angle tracks")
        expected = {path: placement for placement, path in inputs}
        for track in (1, 2):
            items = angles.GetItemListInTrack("video", track)
            require(items and len(items) == 1, "Expected one source item per angle")
            path = Path(items[0].GetMediaPoolItem().GetClipProperty("File Path")).resolve()
            require(path in expected, "Unexpected or duplicate angle media reference")
            placement = expected.pop(path)
            verify_item(items[0], placement.source_in, placement.source_out, angles.GetStartFrame())

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
    parser.add_argument("--project", help="Unused project name beginning with AutoClipper Stage1 ")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Print the plan without opening Resolve")
    mode.add_argument("--execute", action="store_true", help="Use only during the approved Resolve window")
    parser.add_argument("--stacked", action="store_true", help="Use the stacked-video timing fallback")
    args = parser.parse_args(argv)
    try:
        session = Session.from_dict(json.loads(args.session_copy.read_text(encoding="utf-8")))
        if len(args.source) != 2 or len({key for key, _ in args.source}) != 2:
            raise ValueError("Supply two distinct part IDs with --source")
        ids = {key for key, _ in args.source}
        session = replace(session, povs=[
            replace(pov, parts=[part for part in pov.parts if part.part_id in ids])
            for pov in session.povs
        ])
        placements = plan_placements(session, args.start, args.end, args.fps)
        length = floor((Fraction(str(args.end)) - Fraction(str(args.start))) * args.fps + Fraction(1, 2))
        if (len(placements) != 2 or len({p.pov_id for p in placements}) != 2
                or any(p.record_in != 0 or p.record_out != length for p in placements)):
            raise ValueError("Stage 1 requires one fully covering part per POV")
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
            "placements": [{**asdict(p), "mp4": str(path)} for p, path in inputs],
        }, indent=2))
        if args.execute:
            prefix = "AutoClipper Stage1 "
            if not args.project or not args.project.startswith(prefix) or not args.project[len(prefix):].strip():
                raise ValueError(f"Use a fresh --project name beginning with {prefix!r}")
            assemble(args.project, inputs, args.fps, args.stacked)
    except (AttributeError, ImportError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        parser.exit(1, f"Stage 1 stopped: {exc}\n")


if __name__ == "__main__":
    main()
