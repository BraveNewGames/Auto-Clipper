import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autoclipper.director_notes import NotesCatalog
from autoclipper.director_poc.core import Capture
from autoclipper.fkb import EditPlan, FKBStore, FootageKnowledgeBase, Provenance
from autoclipper.models import Part, POV, Session
from autoclipper.recommendations import OfflineProvider, recommend, refresh_recommendations
from autoclipper.resolve_builder import build_export
from autoclipper.resolve_manifest import compile_fkb_edit_plan
from autoclipper.store import save_session

from test_resolve_builder import fake_resolve


class DirectorIntegrationTests(unittest.TestCase):
    def test_offline_director_flow_preserves_provenance_and_sources(self):
        with TemporaryDirectory() as folder, TemporaryDirectory() as build_folder:
            root = Path(folder)
            video = root / "camera.mp4"
            video.write_bytes(b"synthetic placeholder bytes")
            captions = root / "camera.srt"
            captions.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nAda: Truck staging.\n\n"
                "2\n00:00:05,000 --> 00:00:06,000\nAda: Truck recovery.\n", encoding="utf-8")
            source = Session(session_id="synthetic-session", folder=folder, povs=[
                POV("ada", "Ada", [Part("camera", video_path=video.name, srt_path=captions.name,
                                        duration=10, duration_source="ffprobe")])
            ])
            source_file = save_session(source)
            original = {path: path.read_bytes() for path in (video, captions, source_file)}
            store = FKBStore(root)
            store.ingest(source, fps=60, timeline_start_frame=216000,
                         project_id="synthetic-project", timeline_id="synthetic-review")
            provider = OfflineProvider()
            before = store.update(lambda data: recommend(data, "truck", run_id="base", provider=provider))
            self.assertEqual([row.frames["start"] for row in before], [60, 300])

            catalog = NotesCatalog(store)
            first = catalog.create(
                Capture("Scratch", "synthetic-project", "Review", "synthetic-review", 300, 340,
                        "01:00:05:00", "01:00:05:40", 300, 340, 60, "01:00:05:15", "fixture"),
                "Truck recovery reaction", playback="paused")
            second = catalog.create(
                Capture("Scratch", "synthetic-project", "Review", "synthetic-review", 330, 359,
                        "01:00:05:30", "01:00:05:59", 330, 359, 60, "01:00:05:45", "fixture"),
                "Truck recovery payoff", playback="paused")
            ranked = store.update(refresh_recommendations)
            self.assertEqual([row.candidate_id for row in ranked], [before[1].candidate_id, before[0].candidate_id])
            scans = store.load()
            later_span = scans.transcript_spans[1]
            self.assertEqual(ranked[0].frames, later_span.frames)
            self.assertEqual(ranked[0].evidence_ids, [later_span.span_id, first.note_id, second.note_id])
            self.assertEqual(later_span.provenance, Provenance.TRANSCRIPT)
            self.assertEqual([note.provenance for note in scans.director_notes], [Provenance.USER] * 2)
            self.assertEqual(ranked[0].provenance, Provenance.AI_INFERENCE)
            self.assertFalse(ranked[0].verified)

            missing, = store.update(lambda data: recommend(
                data, "unfilmed moon landing", run_id="missing", provider=provider))
            self.assertEqual((missing.frames, missing.evidence_ids), ({}, []))
            plan = EditPlan("draft", [
                {"entry_id": "grounded", "kind": "candidate", "candidate_id": ranked[0].candidate_id},
                {"entry_id": "missing", "kind": "candidate", "candidate_id": missing.candidate_id,
                 "title": "Unfilmed ending", "provisional_frames": 12},
            ], revisions=[{"revision_id": "r1"}])
            store.update(lambda data: data.edit_plans.append(plan))
            saved = FKBStore(root).load()
            self.assertEqual(saved.candidate_moments[-1].frames, {})
            self.assertEqual(saved.candidate_moments[-1].provenance, Provenance.AI_INFERENCE)
            malformed = saved.to_dict()
            malformed["candidate_moments"][-1]["frames"] = {"start": 0, "end": 0}
            with self.assertRaises(ValueError):
                FootageKnowledgeBase.from_dict(malformed)

            inventory = [
                {"codec_type": "video", "r_frame_rate": "60/1", "duration": "10", "nb_frames": "600"},
                {"codec_type": "audio", "index": 1, "channels": 2, "tags": {"title": "Track1"}},
                {"codec_type": "audio", "index": 3, "channels": 2, "tags": {"title": "Track2"}},
            ]
            with patch("autoclipper.resolve_manifest.probe_streams", return_value=inventory):
                manifest = compile_fkb_edit_plan(source, saved, "draft", revision_id="r2", confirmations=[
                    {"part_id": "camera", "speaker": "Ada", "confirmed_by": "owner", "transcriber_track": 2},
                ])
            grounded, placeholder = manifest["segments"]
            self.assertEqual(grounded["candidate_ids"], [ranked[0].candidate_id])
            self.assertEqual(grounded["note_ids"], [first.note_id, second.note_id])
            self.assertEqual(placeholder["candidate_ids"], [missing.candidate_id])
            self.assertEqual((placeholder["title"], placeholder["duration_frames"],
                              placeholder["duration_source"]), ("Unfilmed ending", 12, "provisional"))
            self.assertFalse(any(key.startswith("session_") for key in placeholder))
            self.assertEqual(placeholder["angles"], [])

            sdk, _, state, project, _ = fake_resolve()
            pool = project.GetMediaPool.return_value
            base_import = pool.ImportMedia.side_effect
            slate = Path(build_folder) / "placeholder.mp4"
            slate.touch()

            def import_media(paths):
                clips = base_import(paths)
                if Path(paths[0]) == slate:
                    clip, = clips
                    properties = clip.GetClipProperty.side_effect
                    clip.GetClipProperty.side_effect = lambda key: "12" if key == "Frames" else properties(key)
                return clips

            pool.ImportMedia.side_effect = import_media
            with (patch.dict(sys.modules, {"DaVinciResolveScript": sdk}),
                  patch("autoclipper.resolve_builder.mkdtemp", return_value=build_folder),
                  patch("autoclipper.resolve_builder.render_placeholder", return_value=slate)):
                result = build_export(manifest)
            self.assertTrue(result["reopened"])
            self.assertEqual(result["edit_plan"]["revision_history"], [{"revision_id": "r1"}])
            self.assertEqual(result["segments"][1]["video"]["name"], "MISSING FOOTAGE: Unfilmed ending")
            self.assertEqual((len(state["video"]), len(state["audio"])), (2, 1))
            self.assertEqual({path: path.read_bytes() for path in original}, original)
