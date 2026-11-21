import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autoclipper.director_notes import NotesCatalog
from autoclipper.director_poc.core import Capture, frame_tc
from autoclipper.fkb import EditPlan, FKBStore, FootageKnowledgeBase, FrameRange, Provenance
from autoclipper.models import Part, POV, Session
from autoclipper.recommendations import OfflineProvider, available_povs, recommend, refresh_recommendations
from autoclipper.resolve_builder import build_export
from autoclipper.resolve_manifest import build_review_manifest, compile_fkb_edit_plan
from autoclipper.store import save_session

from test_resolve_builder import fake_resolve
from director_fixtures import confirmed_mics, synthetic_probe, verified_review


class DirectorIntegrationTests(unittest.TestCase):
    def capture(self, first, end):
        return Capture("Scratch", "project", "Review", "timeline", first, end,
                       frame_tc(216000 + first, 60), frame_tc(216000 + end, 60),
                       first, end, 60, frame_tc(216000 + first, 60), "fixture")

    def test_note_plan_rejects_mapping_drift_and_changed_anchor(self):
        for change in ("offset", "retime", "delete", "text"):
            with self.subTest(change=change), TemporaryDirectory() as folder:
                part = Part("camera", video_path="camera.mp4", duration=10, offset=1)
                (Path(folder) / part.video_path).touch()
                source = Session(session_id="stable", folder=folder, povs=[POV("ada", "Ada", [part])])
                store = FKBStore(folder)
                store.ingest(source, fps=60, timeline_start_frame=216000, project_id="project",
                             timeline_id="timeline", review_manifest=verified_review(source, 60))
                catalog = NotesCatalog(store)
                note = catalog.create(self.capture(300, 320), "Great reaction", playback="paused")
                candidate, = store.update(lambda data: recommend(data, "reaction", run_id="notes"))
                store.update(lambda data: data.edit_plans.append(EditPlan("draft", [
                    {"entry_id": "cut", "kind": "candidate", "candidate_id": candidate.candidate_id},
                ])))

                def compile_draft():
                    return compile_fkb_edit_plan(source, store.load(), "draft", revision_id="r1",
                                                 confirmations=confirmed_mics(source))

                with synthetic_probe(source, 60):
                    segment, = compile_draft()["segments"]
                    self.assertEqual(segment["angles"][0]["source_in_frame"], 240)
                    if change == "offset":
                        part.offset = 2
                    elif change == "retime":
                        catalog.apply_range(catalog.preview_range(
                            note.note_id, self.capture(330, 350), playback="paused"))
                    elif change == "delete":
                        catalog.delete(note.note_id)
                    else:
                        catalog.edit_text(note.note_id, "New interpretation")
                    self.assertFalse(store.load().mapping_stale)
                    message = "source mapping changed" if change == "offset" else "Note anchor changed"
                    with self.assertRaisesRegex(ValueError, message):
                        compile_draft()
                    if change == "retime":
                        store.update(lambda data: recommend(data, "reaction", run_id="notes"))
                        with self.assertRaisesRegex(ValueError, "Note anchor selection changed"):
                            compile_draft()
                    if change != "offset":
                        store.update(lambda data: data.edit_plans[0].entries.__setitem__(0, {
                            "entry_id": "slate", "kind": "placeholder", "title": "Select new footage",
                            "provisional_frames": 20,
                        }))
                        self.assertEqual(compile_draft()["segments"][0]["kind"], "placeholder")

    def test_verified_availability_matches_review_catalog_and_edit(self):
        with TemporaryDirectory() as folder:
            source = Session(session_id="parity", folder=folder, povs=[
                POV("ada", "Ada", [
                    Part("short", video_path="short.mkv", duration=2, offset=0.005),
                    Part("overlap", index=2, video_path="overlap.mp4", duration=0.5, offset=0.5),
                ]),
                POV("bob", "Bob", [Part("other", video_path="other.mp4", duration=2)]),
            ])
            for name in ("short.mp4", "overlap.mp4", "other.mp4"):
                (Path(folder) / name).touch()
            with synthetic_probe(source, 60, {"short": 60}):
                confirmations = confirmed_mics(source)
                review = build_review_manifest(source, fps=60, confirmations=confirmations)
                store = FKBStore(folder)
                store.ingest(source, fps=60, timeline_start_frame=216000, project_id="project",
                             timeline_id="timeline", review_manifest=review)
                catalog = NotesCatalog(store)
                notes = [catalog.create(self.capture(first, end), "Coverage", playback="paused")
                         for first, end in [(0, 1), (30, 60), (60, 120)]]
                store.update(lambda data: data.edit_plans.append(EditPlan("draft", [
                    {"entry_id": "all", "kind": "source", "title": "All footage",
                     "frames": {"start": 0, "end": 120}},
                ])))
                db = store.load()
                draft = compile_fkb_edit_plan(source, db, "draft", revision_id="r1",
                                              confirmations=confirmations)

            expected = [(0, 30, ["ada", "bob"]), (30, 60, ["bob"]), (60, 120, ["bob"])]
            for manifest in (review, draft):
                self.assertEqual([(row["session_start_frame"], row["session_end_frame"],
                                   [angle["pov_id"] for angle in row["angles"]])
                                  for row in manifest["segments"]], expected)
                self.assertEqual(manifest["segments"][0]["angles"][0]["source_in_frame"], 0)
            self.assertEqual([note.pov_ids for note in notes], [["ada", "bob"], ["bob"], ["bob"]])
            self.assertEqual([note.source_mapping_digest for note in notes],
                             [review["source_mapping_digest"]] * 3)
            self.assertEqual([(pov, frames) for pov, _, frames in available_povs(db, FrameRange(0, 120))],
                             [("ada", FrameRange(0, 30)), ("bob", FrameRange(0, 120))])
            short, overlap, _ = db.recording_parts
            self.assertEqual(short.source_range, {"start": 0, "end": 60})
            self.assertEqual(overlap.playable_intervals, [])

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
                         project_id="synthetic-project", timeline_id="synthetic-review",
                         review_manifest=verified_review(source, 60))
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

            with synthetic_probe(source, 60):
                manifest = compile_fkb_edit_plan(source, saved, "draft", revision_id="r2",
                                                 confirmations=confirmed_mics(source))
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
