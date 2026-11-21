"""Run only during the owner-approved Resolve window, except for offline preparation and indexing."""

import argparse
from datetime import datetime
from functools import partial
import json
import os
from pathlib import Path
from shutil import copyfile, copytree
from tempfile import gettempdir, mkdtemp

from ..resolve_builder import DIRECTOR_TEST_PREFIX, build_export, validate_manifest
from ..resolve_manifest import build_review_manifest, catalog_source_identity, pinned_input_digest
from ..media import resolve_recording
from ..store import _write_json, load_session
from .core import NoteStore, connect_review
from .receipt import validate_review_receipt


_SCRATCH_RECOVERY = ("Keep the existing catalog and its backup. See the legacy recovery steps in "
                     "autoclipper/director_poc/README.md.")


class SourceRootError(ValueError):
    """An explicitly supplied source root cannot be used."""


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def temp_copy(path):
    path = path.resolve(strict=True)
    if not path.is_relative_to(Path(gettempdir()).resolve()):
        raise ValueError("Use an approved copy under system temp")
    return path


def source_session(path, session_root=None):
    """Load a temp JSON with an optional in-memory source root and SRT preflight."""
    session = load_session(temp_copy(path))
    if session_root is not None:
        try:
            root = session_root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SourceRootError(f"--session-root does not exist: {session_root}") from exc
        if not root.is_dir():
            raise SourceRootError("--session-root must be an existing directory")
        session.folder = str(root)
    root = Path(session.folder).resolve()
    for _, part in session.all_parts():
        if part.srt_path:
            reference = Path(part.srt_path)
            srt = (root / reference).resolve()
            if not reference.is_absolute() and not srt.is_relative_to(root):
                raise ValueError(f"SRT path escapes session root for {part.part_id}: {part.srt_path}")
            if not srt.is_file():
                remedy = ("Check this absolute SRT path." if reference.is_absolute() else
                          "Supply --session-root with the recording folder.")
                raise ValueError(f"SRT missing for {part.part_id}: {part.srt_path}. {remedy}")
        if session_root is not None and part.video_path:
            saved = (root / part.video_path).resolve()
            media = resolve_recording(root, part.video_path)
            if not saved.is_relative_to(root) or media.path is None or not media.path.resolve().is_relative_to(root):
                raise ValueError(f"Recording missing or outside --session-root for {part.part_id}: "
                                 f"{part.video_path}")
    return session


def source_input_identity(session):
    """Bind indexing to the source root and exact SRT bytes used by prepare."""
    root = Path(session.folder).resolve()
    return {
        "root": os.path.normcase(str(root)),
        "srts": {part.part_id: pinned_input_digest(root / part.srt_path)
                 for _, part in session.all_parts() if part.srt_path},
    }


def prepare(args):
    source = temp_copy(args.session)
    confirmations = temp_copy(args.confirmations)
    metadata = temp_copy(args.metadata) if args.metadata else None
    scratch = Path(mkdtemp(prefix="autoclipper-director-a-"))
    copyfile(source, scratch / "session.json")
    pinned_confirmations = scratch / "mic_confirmations.json"
    copyfile(confirmations, pinned_confirmations)
    pinned_inputs = {"confirmations": {"path": pinned_confirmations.name,
                                        "sha256": pinned_input_digest(pinned_confirmations)}}
    if metadata is not None:
        pinned_input_digest(metadata)  # Reject links and reparse points before copying.
        pinned_metadata = scratch / "metadata"
        copytree(metadata, pinned_metadata)
        pinned_inputs["metadata"] = {"path": pinned_metadata.name,
                                     "sha256": pinned_input_digest(pinned_metadata)}
    print(f"Scratch directory: {scratch}", flush=True)
    session = source_session(scratch / "session.json", args.session_root)
    pinned_inputs["session_source"] = source_input_identity(session)
    catalog_source = catalog_source_identity(session)
    if not session.all_parts():
        raise ValueError("The session has no recordings")
    root = args.media_root.resolve(strict=True)
    for _, part in session.all_parts():
        if not part.video_path:
            raise ValueError("A session part has no video path")
        path = (root / part.video_path).resolve()
        source_path = resolve_recording(root, part.video_path, require_mp4=True).path
        if not path.is_relative_to(root) or (source_path is not None and
                                             not source_path.resolve().is_relative_to(root)):
            raise ValueError("A recording path lies outside the approved media root")
        part.video_path = str(path)
    confirmed = read(pinned_confirmations)["confirmations"]
    session.folder = str(scratch)
    session.session_id = "DirectorA REVIEW"
    manifest = build_review_manifest(session, transcripts_dir=pinned_metadata if metadata else None,
                                     confirmations=confirmed, fps=args.fps, _isolated_inputs=True)
    manifest["catalog_source"] = catalog_source
    manifest["pinned_inputs"] = pinned_inputs
    _write_json(scratch / "manifest.json", manifest)
    validate_manifest(manifest)
    print(f"Verified {len(manifest['segments'])} planned segments at {args.fps} fps")


def verified_review_origin(manifest, built):
    return validate_review_receipt(manifest, built).session_origin


def initialize_fkb(scratch, source, manifest, built, *, migrate_from=None):
    from ..fkb import FKBStore, ingest_session
    from .catalog_migration import migrate_legacy_catalog, persistent_mapping_digest

    binding = validate_review_receipt(manifest, built)
    persistent_mapping_digest(manifest)
    store = FKBStore(scratch)
    store.verify_writable()
    options = dict(fps=manifest["fps"], timeline_start_frame=binding.start_frame,
                   review_session_start_frame=binding.session_origin, project_id=built["project_id"],
                   timeline_id=built["timeline_id"], review_manifest=manifest)
    if migrate_from is None:
        if store.path.exists() and not store.load().source_mapping_digest:
            raise ValueError("Use a new scratch review and index-notes --migrate-from OLD_SCRATCH.")
        store.ingest(source, **options)
    else:
        try:
            if store.path.exists():
                raise ValueError("Migration needs a fresh destination with no catalog")
            old = temp_copy(migrate_from)
            incoming = ingest_session(source, **options)
            store.initialize(migrate_legacy_catalog(
                FKBStore(old).load(), read(old / "manifest.json"), read(old / "build.json"),
                incoming, manifest, built, binding))
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("Keep the old catalog. Supply its original manifest and receipt, or preserve "
                             f"the note text and reselect any unverifiable ranges. {exc}") from exc
    return store.path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("prepare", help="Offline, probe approved read-only MP4 references")
    plan.add_argument("session", type=Path)
    plan.add_argument("confirmations", type=Path)
    plan.add_argument("media_root", type=Path)
    plan.add_argument("--session-root", type=Path, help="Existing source folder for relative SRT and video paths")
    plan.add_argument("--metadata", type=Path, help="Temp directory of approved transcriber JSON copies")
    plan.add_argument("--fps", type=int, default=60)
    build = commands.add_parser("build", help="Live, create and reopen a fresh scratch project")
    build.add_argument("scratch", type=Path)
    index = commands.add_parser("index-notes", help="Offline, initialize the persistent scratch catalog")
    index.add_argument("scratch", type=Path)
    index.add_argument("session", type=Path, help="Approved session JSON under system temp")
    index.add_argument("--session-root", type=Path, help="Same source folder used by prepare")
    index.add_argument("--migrate-from", type=Path, help="Copy a verified legacy catalog from this scratch folder")
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
        try:
            prepare(args)
        except SourceRootError as exc:
            parser.error(str(exc))
        return
    try:
        scratch = temp_copy(args.scratch)
        if not scratch.name.startswith("autoclipper-director-a-"):
            raise ValueError("Use the scratch directory printed by prepare")
        manifest = read(scratch / "manifest.json")
    except (OSError, TypeError, ValueError) as exc:
        parser.error(f"Cannot open the scratch review. {exc} {_SCRATCH_RECOVERY}")
    result_path = scratch / "build.json"
    if args.command == "build":
        if result_path.exists():
            parser.error("This scratch directory already has a built project")
        name = f"{DIRECTOR_TEST_PREFIX}{datetime.now():%Y%m%d-%H%M%S}"
        try:
            result = build_export(manifest, project_name=name)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            parser.error(str(exc))
        _write_json(result_path, result)
        print(f"Reopened {name} / {result['timeline']}")
        return

    persistent = None
    try:
        built = read(result_path)
        if args.command == "index-notes":
            try:
                source = source_session(args.session, args.session_root)
                pinned_source = (manifest.get("pinned_inputs") or {}).get("session_source")
                if pinned_source is not None and pinned_source != source_input_identity(source):
                    raise ValueError("Session root or SRT contents differ from prepare. "
                                     "Use the same --session-root and unchanged SRTs, or prepare a new review.")
            except (OSError, ValueError) as exc:
                parser.error(f"Cannot index the source session. {exc}")
            print(initialize_fkb(scratch, source, manifest, built, migrate_from=args.migrate_from))
            return

        binding = validate_review_receipt(manifest, built)
        if args.persistent_notes:
            from ..director_notes import NotesCatalog
            from ..fkb import FKBStore
            from .catalog_migration import persistent_mapping_digest

            digest = persistent_mapping_digest(manifest)
            store = FKBStore(scratch)
            catalog = store.load()
            session = catalog.session
            if (session.project_id, session.timeline_id, session.fps, session.review_session_start_frame,
                    session.timeline_start_frame) != (
                    built["project_id"], built["timeline_id"], manifest["fps"], binding.session_origin,
                    binding.start_frame):
                raise ValueError("The scratch FKB is bound to a different timeline or clock")
            if catalog.mapping_stale or catalog.source_mapping_digest != digest:
                raise ValueError("The scratch FKB has no matching verified source mapping. Re-index or migrate the catalog.")
            persistent = NotesCatalog(store)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        parser.error(f"Cannot verify the scratch review build receipt or catalog. Keep manifest.json and "
                     f"build.json from the same new disposable build. {exc} {_SCRATCH_RECOVERY}")

    from .window import run_window

    connect = partial(connect_review, built, manifest, not args.out_exclusive)
    run_window(connect, NoteStore(scratch / "notes.json"), scratch / "playhead.csv",
               args.hz, args.key, persistent=persistent)


if __name__ == "__main__":
    main()
