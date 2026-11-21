from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from autoclipper.director_notes import NotesCatalog
from autoclipper.director_poc.__main__ import initialize_fkb, main
from autoclipper.director_poc.core import Capture, Clock
from autoclipper.fkb import FKBStore
from autoclipper.models import Part, POV, Session
from autoclipper.resolve_builder import build_export

from test_resolve_builder import fake_resolve


class DirectorPrepareTests(unittest.TestCase):
    def test_scratch_prepare_uses_verified_coverage_without_resolve(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "media"
            media.mkdir()
            scratch = root / "autoclipper-director-a-test"
            scratch.mkdir()
            source = root / "session.json"
            confirmations = root / "mic_confirmations.json"
            session = Session(session_id="source", folder=str(media), povs=[
                POV("ada", "Ada", [
                    Part("first", video_path="first.mkv", duration=3, offset=1),
                    Part("second", index=2, video_path="second.mkv", duration=4, offset=3),
                ])
            ])
            source.write_text(json.dumps(session.to_dict()), encoding="utf-8")
            confirmations.write_text(json.dumps({"confirmations": [
                {"part_id": part.part_id, "transcriber_track": 2, "speaker": "Ada", "confirmed_by": "owner"}
                for _, part in session.all_parts()
            ]}), encoding="utf-8")
            media_files = [media / name for name in ("first.mp4", "second.mp4")]
            for path in media_files:
                path.write_bytes(b"synthetic fixture")
            originals = {path: path.read_bytes() for path in [source, confirmations, *media_files]}
            inventory = [
                {"codec_type": "video", "r_frame_rate": "60/1", "nb_frames": "60", "duration": "1"},
                {"codec_type": "audio", "index": 1, "channels": 2, "tags": {"title": "Track1"}},
                {"codec_type": "audio", "index": 2, "channels": 2, "tags": {"title": "Track2"}},
            ]
            sdk = Mock()
            with (patch.object(sys, "argv", ["director", "prepare", str(source), str(confirmations), str(media)]),
                  patch.dict(sys.modules, {"DaVinciResolveScript": sdk}),
                  patch("autoclipper.director_poc.__main__.mkdtemp", return_value=str(scratch)),
                  patch("autoclipper.resolve_manifest.probe_streams", return_value=inventory) as probe,
                  redirect_stdout(io.StringIO())):
                main()
            sdk.scriptapp.assert_not_called()
            self.assertEqual(probe.call_count, 2)
            manifest = json.loads((scratch / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["mode"], "director_review")
            self.assertEqual(manifest["session_id"], "DirectorA REVIEW")
            self.assertEqual(manifest["requested_session_frames"], [0, 420])
            self.assertEqual(manifest["review_session_frames"], [60, 240])
            self.assertEqual([segment["kind"] for segment in manifest["segments"]],
                             ["media", "coverage_gap", "media"])
            clock = Clock(manifest, "01:00:00:00", 216000)
            self.assertEqual(clock.session_range(120, 125), (180, 185))
            with self.assertRaisesRegex(ValueError, "gap"):
                clock.session_range(59, 121)
            self.assertEqual((scratch / "session.json").read_bytes(), originals[source])
            self.assertEqual({path: path.read_bytes() for path in originals}, originals)

            sdk, _, _, project, reopened = fake_resolve()
            timeline = reopened.GetTimelineByIndex.return_value
            project.GetMediaPool.return_value.CreateEmptyTimeline.return_value.GetName.return_value = "DirectorA REVIEW"
            timeline.GetName.return_value = "DirectorA REVIEW"
            timeline.GetUniqueId.return_value = "timeline"
            project.GetUniqueId.return_value = reopened.GetUniqueId.return_value = "project"
            with (patch.dict(sys.modules, {"DaVinciResolveScript": sdk}),
                  patch("autoclipper.resolve_builder.mkdtemp", return_value=directory)):
                built = build_export(manifest, project_name="AutoClipper DirectorA Test 20261120-010203")
            for unverified_manifest, unverified_result in (
                    (manifest, {key: value for key, value in built.items() if key != "review_session_frames"}),
                    ({key: value for key, value in manifest.items() if key != "review_session_frames"}, built),
                    (manifest, {**built, "review_session_frames": [0, 180]}),
                    (manifest, {**built, "reopened": False})):
                with self.subTest(manifest=unverified_manifest.get("review_session_frames"),
                                  result=unverified_result):
                    with self.assertRaisesRegex(ValueError, "Build a new disposable review"):
                        initialize_fkb(scratch, session, unverified_manifest, unverified_result)
                    self.assertFalse(FKBStore(scratch).path.exists())

            initialize_fkb(scratch, session, manifest, built)
            store = FKBStore(scratch)
            self.assertEqual(store.load().session.review_session_start_frame, 60)
            catalog = NotesCatalog(store)
            capture = Capture("Scratch", "project", "Review", "timeline", 120, 125,
                              "01:00:02:00", "01:00:02:05", 180, 185, 60,
                              "01:00:02:00", "fixture")
            note = catalog.create(capture, "Verified rebased range", playback="paused")
            self.assertEqual(note.frames, {"start": 180, "end": 185})
            with self.assertRaisesRegex(ValueError, "bound session frame origin"):
                catalog.create(replace(capture, session_in=181, session_out_exclusive=186),
                               "Shifted range", playback="paused")
            self.assertEqual(len(store.load().director_notes), 1)
