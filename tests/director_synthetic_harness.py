"""Opt-in Director smoke test using real media tools and a fake Resolve."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autoclipper.director_notes import NotesCatalog
from autoclipper.director_poc.__main__ import initialize_fkb, main
from autoclipper.director_poc.core import Capture
from autoclipper.director_poc.receipt import validate_review_receipt
from autoclipper.fkb import EditPlan, FKBStore
from autoclipper.models import Part, POV, Session
from autoclipper.resolve_builder import build_export
from autoclipper.resolve_manifest import compile_fkb_edit_plan
from autoclipper.store import _write_json

from test_resolve_builder import fake_resolve


class DirectorSyntheticHarness(unittest.TestCase):
    def test_separate_media_root_through_edit_and_file_drift_refusal(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg or not shutil.which("ffprobe"):
            self.skipTest("FFmpeg and FFprobe must be on PATH")

        # Keep the real SDK unavailable outside the one mocked build.
        sdk_guard = patch.dict(sys.modules, {"DaVinciResolveScript": None})
        sdk_guard.start()
        self.addCleanup(sdk_guard.stop)
        with TemporaryDirectory(prefix="autoclipper-director-harness-") as directory:
            temp = Path(directory)
            media = temp / "media"
            original = temp / "original-session"
            scratch = temp / "autoclipper-director-a-demo"
            for folder in (media, original, scratch):
                folder.mkdir()
            for stem, duration in (("a1", 2), ("a2", 2), ("b1", 4)):
                subprocess.run(
                    [ffmpeg, "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                     f"color=s=320x180:r=60:d={duration}", "-f", "lavfi", "-i",
                     f"anullsrc=r=48000:cl=stereo:d={duration}", "-c:v", "libx264", "-c:a", "aac",
                     "-metadata:s:a:0", "handler_name=Track1", str(media / f"{stem}.mp4")],
                    check=True, timeout=30,
                )
            source = Session(session_id="synthetic", folder=str(original), povs=[
                POV("a", "A", [Part("a1", video_path="a1.mp4", offset=.005, duration=2),
                               Part("a2", index=2, video_path="a2.mp4", offset=1.5, duration=2)]),
                POV("b", "B", [Part("b1", video_path="b1.mp4", duration=5)]),
            ])
            source_file = temp / "session.json"
            mic_file = temp / "confirmations.json"
            confirmations = [
                {"part_id": part.part_id, "speaker": pov.name, "transcriber_track": 1, "confirmed_by": "owner"}
                for pov, part in source.all_parts()
            ]
            _write_json(source_file, source.to_dict())
            _write_json(mic_file, {"confirmations": confirmations})
            with (patch.object(sys, "argv", ["director", "prepare", str(source_file), str(mic_file), str(media)]),
                  patch("autoclipper.director_poc.__main__.mkdtemp", return_value=str(scratch)),
                  redirect_stdout(io.StringIO())):
                main()
            manifest = json.loads((scratch / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["review_session_frames"], [0, 240])
            overlap = next(row for row in manifest["segments"] if row["session_start_frame"] == 90)
            self.assertEqual([angle["pov_id"] for angle in overlap["angles"]], ["b"])

            sdk, _, _, project, reopened = fake_resolve()
            project.GetMediaPool.return_value.CreateEmptyTimeline.return_value.GetName.return_value = "DirectorA REVIEW"
            reopened.GetTimelineByIndex.return_value.GetName.return_value = "DirectorA REVIEW"
            with (patch.dict(sys.modules, {"DaVinciResolveScript": sdk}),
                  patch("autoclipper.resolve_builder.mkdtemp", return_value=str(temp))):
                built = build_export(manifest, project_name="AutoClipper DirectorA Test 20261120-010203")
            binding = validate_review_receipt(manifest, built)
            self.assertEqual((binding.session_origin, binding.start_frame), (0, 216000))

            initialize_fkb(scratch, source, manifest, built)
            store = FKBStore(scratch)
            capture = Capture(built["project"], built["project_id"], built["timeline"], built["timeline_id"],
                              130, 150, "01:00:02:10", "01:00:02:30", 130, 150, 60, "01:00:02:10", "fixture")
            note = NotesCatalog(store).create(capture, "Synthetic note", playback="paused")
            plan = EditPlan("draft", [
                {"entry_id": "note", "kind": "source", "title": "Synthetic note",
                 "frames": note.frames, "note_ids": [note.note_id], "pov_id": "a"},
            ])
            store.update(lambda data: data.edit_plans.append(plan))
            edit = compile_fkb_edit_plan(source, store.load(), "draft", revision_id="r1", confirmations=mic_file)
            dry_run = build_export(edit, dry_run=True)
            segment, = dry_run["manifest"]["segments"]
            angle, = segment["angles"]
            self.assertEqual((angle["part_id"], angle["source_in_frame"], angle["source_out_frame"]),
                             ("a2", 40, 60))

            changed = media / "a1.mp4"
            prior = changed.stat()
            os.utime(changed, ns=(prior.st_atime_ns, prior.st_mtime_ns + 2_000_000_000))
            for candidate in (manifest, edit):
                with self.subTest(mode=candidate["mode"]), self.assertRaisesRegex(ValueError, "source media changed"):
                    build_export(candidate)
