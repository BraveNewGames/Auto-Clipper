"""Run only during the owner-approved Resolve window, except for offline preparation and indexing."""

import argparse
from dataclasses import replace
from datetime import datetime
from functools import partial
import json
from math import floor
from pathlib import Path
from shutil import copyfile
from tempfile import gettempdir, mkdtemp

from ..models import Review as Selection, Suggestion
from ..resolve_builder import DIRECTOR_TEST_PREFIX, START_TIMECODE, build_export, validate_manifest
from ..resolve_manifest import _part_media, build_manifest
from ..store import _write_json, load_session
from .core import NoteStore, connect_review, tc_frame


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def temp_copy(path):
    path = path.resolve(strict=True)
    if not path.is_relative_to(Path(gettempdir()).resolve()):
        raise ValueError("Use an approved copy under system temp")
    return path


def prepare(args):
    source = temp_copy(args.session)
    confirmations = temp_copy(args.confirmations)
    metadata = temp_copy(args.metadata) if args.metadata else None
    scratch = Path(mkdtemp(prefix="autoclipper-director-a-"))
    copyfile(source, scratch / "session.json")
    print(f"Scratch directory: {scratch}", flush=True)
    session = load_session(scratch / "session.json")
    if not session.all_parts():
        raise ValueError("The session has no recordings")
    root = args.media_root.resolve(strict=True)
    for _, part in session.all_parts():
        if not part.video_path:
            raise ValueError("A session part has no video path")
        path = (root / part.video_path).resolve()
        if not path.is_relative_to(root):
            raise ValueError("A recording path lies outside the approved media root")
        part.video_path = str(path)
    confirmed = read(confirmations)["confirmations"]
    session.folder = str(scratch)
    session.session_id = "DirectorA REVIEW"
    roots = [metadata] if metadata else []
    for pov, part in session.all_parts():
        _, probed, problem, _ = _part_media(scratch, roots, pov, part, confirmed, args.fps)
        if problem:
            raise ValueError(f"Cannot verify recording part {part.part_id}: {problem}")
        part.duration = probed.duration
    first = min(0, *(part.offset for _, part in session.all_parts()))
    # Put the selection origin on the canonical session frame grid.
    first = floor(first * args.fps) / args.fps
    end = max(part.offset + part.duration for _, part in session.all_parts())
    proposal = Suggestion(suggestion_id="review", event_id="review", title="Full session review",
                          start=first, end=end)
    session = replace(session, suggestions=[proposal], reviews={"review": Selection(status="keep")})
    manifest = build_manifest(session, transcripts_dir=metadata, confirmations=confirmed, fps=args.fps)
    _write_json(scratch / "manifest.json", manifest)
    validate_manifest(manifest)
    print(f"Verified {len(manifest['segments'])} planned segments at {args.fps} fps")


def initialize_fkb(scratch, source, manifest, built):
    from ..fkb import FKBStore

    mappings = {}
    for segment in manifest["segments"]:
        for angle in segment["angles"]:
            if "mic" in angle:
                part_id = angle["part_id"]
                mapping = mappings.setdefault(part_id, angle["mic"])
                if mapping != angle["mic"]:
                    raise ValueError("Conflicting assembler microphone mappings")
    store = FKBStore(scratch)
    fps = manifest["fps"]
    store.ingest(source, fps=fps, timeline_start_frame=tc_frame(START_TIMECODE, fps),
                 project_id=built["project_id"], timeline_id=built["timeline_id"],
                 audio_by_part=mappings)
    return store.path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("prepare", help="Offline, probe approved read-only MP4 references")
    plan.add_argument("session", type=Path)
    plan.add_argument("confirmations", type=Path)
    plan.add_argument("media_root", type=Path)
    plan.add_argument("--metadata", type=Path, help="Temp directory of approved transcriber JSON copies")
    plan.add_argument("--fps", type=int, default=60)
    build = commands.add_parser("build", help="Live, create and reopen a fresh scratch project")
    build.add_argument("scratch", type=Path)
    index = commands.add_parser("index-notes", help="Offline, initialize the persistent scratch catalog")
    index.add_argument("scratch", type=Path)
    index.add_argument("session", type=Path, help="Approved session JSON under system temp")
    run = commands.add_parser("companion", help="Live, attach only to the exact built timeline")
    run.add_argument("scratch", type=Path)
    run.add_argument("--persistent-notes", action="store_true",
                     help="Use the initialized Director FKB in the scratch directory")
    run.add_argument("--hz", type=int, choices=range(5, 21), default=10, metavar="5..20")
    run.add_argument("--key", type=int, choices=(9, 10, 11, *range(13, 25)), default=11,
                     help="Function key for Ctrl+Alt+Shift+F<n> (default F11)")
    run.add_argument("--out-exclusive", action="store_true", help="Use only if A1 verifies exclusive marks")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args)
        return
    scratch = temp_copy(args.scratch)
    if not scratch.name.startswith("autoclipper-director-a-"):
        parser.error("Use the scratch directory printed by prepare")
    manifest = read(scratch / "manifest.json")
    result_path = scratch / "build.json"
    if args.command == "build":
        if result_path.exists():
            parser.error("This scratch directory already has a built project")
        name = f"{DIRECTOR_TEST_PREFIX}{datetime.now():%Y%m%d-%H%M%S}"
        result = build_export(manifest, project_name=name)
        _write_json(result_path, result)
        print(f"Reopened {name} / {result['timeline']}")
        return

    if args.command == "index-notes":
        source = load_session(temp_copy(args.session))
        print(initialize_fkb(scratch, source, manifest, read(result_path)))
        return

    from .window import run_window

    connect = partial(connect_review, read(result_path), manifest, not args.out_exclusive)
    persistent = None
    if args.persistent_notes:
        from ..director_notes import NotesCatalog
        from ..fkb import FKBStore

        store = FKBStore(scratch)
        session = store.load().session
        built = read(result_path)
        if (session.project_id, session.timeline_id, session.fps) != (
                built["project_id"], built["timeline_id"], manifest["fps"]):
            parser.error("The scratch FKB is bound to a different timeline or clock")
        persistent = NotesCatalog(store)
    run_window(connect, NoteStore(scratch / "notes.json"), scratch / "playhead.csv",
               args.hz, args.key, persistent=persistent)


if __name__ == "__main__":
    main()
