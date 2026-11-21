"""Synthetic Director scratch folder (prepared, built with a fake Resolve, indexed, with notes)."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory, mkdtemp
from types import SimpleNamespace
from unittest.mock import patch

from autoclipper.director_notes import NotesCatalog
from autoclipper.director_poc.__main__ import main
from autoclipper.director_poc.core import Capture, frame_tc
from autoclipper.fkb import FKBStore
from autoclipper.models import Part, POV, Session
from autoclipper.resolve_builder import build_export
from autoclipper.store import _write_json

from director_fixtures import confirmed_mics, synthetic_probe
from test_resolve_builder import fake_resolve

SRT_A = ("1\n00:00:05,000 --> 00:00:08,000\nAda: Truck staging near the bridge.\n\n"
         "2\n00:00:20,000 --> 00:00:23,000\nAda: We are going to flip that truck.\n\n"
         "3\n00:00:40,000 --> 00:00:42,000\nAda: Nice recovery everyone.\n")
SRT_B = ("1\n00:00:12,000 --> 00:00:15,000\nBob: I saw that from the other side.\n\n"
         "2\n00:00:16,000 --> 00:00:18,000\nTeam: Everyone behind the bridge.\n")
FPS = 60


def make_scratch(case, *, with_notes=True):
    """Return a namespace describing a fresh scratch under system temp. Cleans up with the test case."""
    holder = TemporaryDirectory(prefix="autoclipper-bridge-test-")
    case.addCleanup(holder.cleanup)
    temp = Path(holder.name)
    recordings = temp / "recordings"
    recordings.mkdir()
    for name in ("a1.mp4", "b1.mp4"):
        (recordings / name).write_bytes(b"synthetic fixture " + name.encode())
    (recordings / "a1.srt").write_text(SRT_A, encoding="utf-8")
    (recordings / "b1.srt").write_text(SRT_B, encoding="utf-8")
    source = Session(session_id="synthetic", folder=str(recordings), povs=[
        POV("ada", "Ada", [Part("a1", video_path="a1.mp4", srt_path="a1.srt", duration=60)]),
        POV("bob", "Bob", [Part("b1", video_path="b1.mp4", srt_path="b1.srt", duration=30, offset=10.0)]),
    ])
    source_file, confirmations = temp / "session.json", temp / "confirmations.json"
    _write_json(source_file, source.to_dict())
    _write_json(confirmations, {"confirmations": confirmed_mics(source)})
    scratch = temp / "autoclipper-director-a-bridge"
    scratch.mkdir()
    with (patch.object(sys, "argv", ["director", "prepare", str(source_file), str(confirmations), str(recordings)]),
          patch("autoclipper.director_poc.__main__.mkdtemp", return_value=str(scratch)),
          synthetic_probe(source, FPS), redirect_stdout(io.StringIO())):
        main()
    manifest = json.loads((scratch / "manifest.json").read_text(encoding="utf-8"))
    sdk, _, _, project, reopened = fake_resolve()
    project.GetMediaPool.return_value.CreateEmptyTimeline.return_value.GetName.return_value = "DirectorA REVIEW"
    reopened.GetTimelineByIndex.return_value.GetName.return_value = "DirectorA REVIEW"
    pool = project.GetMediaPool.return_value
    base_import = pool.ImportMedia.side_effect
    frame_counts = {"a1": "3600", "b1": "1800"}

    def import_media(paths):
        clips = base_import(paths)
        for clip in clips:
            properties = clip.GetClipProperty.side_effect
            stem = Path(paths[0]).stem
            clip.GetClipProperty.side_effect = (
                lambda key, properties=properties, stem=stem:
                frame_counts.get(stem, "600") if key == "Frames" else properties(key))
        return clips

    pool.ImportMedia.side_effect = import_media
    with (patch.dict(sys.modules, {"DaVinciResolveScript": sdk}),
          patch("autoclipper.resolve_builder.mkdtemp", return_value=str(temp))):
        built = build_export(manifest, project_name="AutoClipper DirectorA Test 20261120-010203")
    (scratch / "build.json").write_text(json.dumps(built), encoding="utf-8")
    with (patch.object(sys, "argv", ["director", "index-notes", str(scratch), str(scratch / "session.json")]),
          redirect_stdout(io.StringIO())):
        main()
    store = FKBStore(scratch)
    notes = []
    if with_notes:
        catalog = NotesCatalog(store)
        for first, end, text in ((1200, 1380, "Truck flip near the bridge"), (2400, 2520, "Recovery moment"),
                                 (1300, 1500, "Second angle on the flip")):
            capture = Capture(built["project"], built["project_id"], built["timeline"], built["timeline_id"],
                              first, end, frame_tc(216000 + first, FPS), frame_tc(216000 + end, FPS),
                              first, end, FPS, frame_tc(216000 + first, FPS), "fixture")
            notes.append(catalog.create(capture, text, playback="paused"))
    return SimpleNamespace(temp=temp, recordings=recordings, scratch=scratch, source=source, store=store,
                           confirmations=confirmations, manifest=manifest, built=built, notes=notes,
                           probe=lambda: synthetic_probe(source, FPS))
