from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import asdict, replace
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from autoclipper.director_notes import NotesCatalog, review_payload
from autoclipper.director_poc.__main__ import initialize_fkb, main
from autoclipper.director_poc.core import Capture, Clock
from autoclipper.fkb import DirectorNote, EditPlan, FKBStore
from autoclipper.models import Part, POV, Session
from autoclipper.resolve_builder import build_export
from autoclipper.resolve_manifest import compile_fkb_edit_plan
from autoclipper.store import load_session

from test_resolve_builder import fake_resolve
from director_fixtures import confirmed_mics, synthetic_probe, verified_review


class DirectorPrepareTests(unittest.TestCase):
    def test_migrate_legacy_catalog_preserves_notes_and_validates_each_revision(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old, new = (root / f"autoclipper-director-a-{stage}" for stage in ("old", "new"))
            old.mkdir()
            new.mkdir()
            media = root / "angle.mp4"
            media.touch()
            source = Session(session_id="synthetic-session", folder=directory, povs=[
                POV("ada", "Ada", [Part("angle", video_path=media.name, duration=10)]),
            ])
            source_path = root / "source.json"
            source_path.write_text(json.dumps(source.to_dict()), encoding="utf-8")
            current = verified_review(source, 60)
            legacy = deepcopy(current)
            for key in ("mode", "review_session_frames", "source_mapping", "source_mapping_digest"):
                del legacy[key]
            first, = legacy["segments"]
            first.update(session_end=1, session_end_frame=60, duration_frames=60)
            first["angles"][0]["source_out_frame"] = 60
            second = deepcopy(first)
            second.update(segment_id="legacy-second", record_start_frame=60,
                          session_start=1, session_end=2, session_start_frame=60, session_end_frame=120)
            second["angles"][0].update(source_in_frame=60, source_out_frame=120)
            legacy["segments"].append(second)

            def receipt(manifest, suffix):
                sdk, _, _, project, reopened = fake_resolve()
                project.GetUniqueId.return_value = reopened.GetUniqueId.return_value = f"project-{suffix}"
                reopened.GetTimelineByIndex.return_value.GetUniqueId.return_value = f"timeline-{suffix}"
                with (patch.dict(sys.modules, {"DaVinciResolveScript": sdk}),
                      patch("autoclipper.resolve_builder.mkdtemp", return_value=directory)):
                    return build_export(manifest, project_name="AutoClipper DirectorA Test 20261120-010203")

            old_built, new_built = receipt(legacy, "old"), receipt(current, "new")
            with self.assertRaisesRegex(ValueError, "Persistent notes require verified source mapping"):
                initialize_fkb(old, source, legacy, old_built)
            old_store = FKBStore(old)
            old_store.ingest(source, fps=60, timeline_start_frame=216000,
                             project_id="project-old", timeline_id="timeline-old")
            capture = Capture(old_built["project"], "project-old", old_built["timeline"], "timeline-old",
                              10, 15, "01:00:00:10", "01:00:00:15", 10, 15, 60, "01:00:00:12", "then")
            note = DirectorNote("stable-note-id", {"start": 10, "end": 15}, "Original", "Original",
                                "then", "then", asdict(capture), marker_id="stable-note-id")
            snapshot = note.to_dict()
            del snapshot["revisions"]
            note.revisions.append(snapshot)
            note.text = "Revised"
            note.revision = 1
            note.frames = {"start": 70, "end": 75}
            note.capture = asdict(replace(capture, timeline_in=70, timeline_out_exclusive=75,
                                          in_timecode="01:00:01:10", out_timecode_exclusive="01:00:01:15",
                                          session_in=70, session_out_exclusive=75, playhead="01:00:01:12"))
            old_store.update(lambda data: data.director_notes.append(note))
            original = old_store.path.read_bytes()

            def write_review(scratch, manifest, built):
                for name, value in (("manifest.json", manifest), ("build.json", built)):
                    (scratch / name).write_text(json.dumps(value), encoding="utf-8")

            changed = deepcopy(legacy)
            for key in ("source_in_frame", "source_out_frame"):
                changed["segments"][0]["angles"][0][key] += 1
            write_review(old, changed, receipt(changed, "old"))
            with self.assertRaisesRegex(ValueError, "Cannot verify the same footage for note.*revision 0"):
                initialize_fkb(new, source, current, new_built, migrate_from=old)
            self.assertFalse(FKBStore(new).path.exists())

            write_review(old, legacy, old_built)
            write_review(new, current, new_built)
            with (patch.object(sys, "argv", ["director", "index-notes", str(new), str(source_path),
                                             "--migrate-from", str(old)]),
                  redirect_stdout(io.StringIO())):
                main()
            self.assertEqual(old_store.path.read_bytes(), original)
            new_store = FKBStore(new)
            migrated, = new_store.load().director_notes
            self.assertEqual((migrated.note_id, migrated.marker_id), (note.note_id, note.marker_id))
            self.assertEqual((migrated.original_text, migrated.text, migrated.revision), ("Original", "Revised", 1))
            self.assertEqual(migrated.revisions[0]["text"], "Original")
            self.assertEqual(migrated.capture["migrated_from"], note.capture)
            self.assertEqual(migrated.pov_ids, ["ada"])
            self.assertEqual(review_payload(migrated)["timeline_id"], "timeline-new")
            restored = NotesCatalog(new_store).restore_revision(migrated.note_id, 0)
            self.assertEqual(restored.source_mapping_digest, current["source_mapping_digest"])
            self.assertEqual(review_payload(restored)["timeline_id"], "timeline-new")
            new_store.update(lambda data: data.edit_plans.append(EditPlan("draft", [
                {"entry_id": "cut", "kind": "source", "title": "Migrated",
                 "frames": restored.frames, "note_ids": [restored.note_id]},
            ])))
            with synthetic_probe(source, 60):
                edit = compile_fkb_edit_plan(source, new_store.load(), "draft", revision_id="r1",
                                             confirmations=confirmed_mics(source))
            self.assertEqual(edit["segments"][0]["angles"][0]["source_in_frame"], 10)

    def test_scratch_prepare_uses_verified_coverage_without_resolve(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "media"
            media.mkdir()
            original_root = root / "original"
            original_root.mkdir()
            scratch = root / "autoclipper-director-a-test"
            scratch.mkdir()
            source = root / "session.json"
            confirmations = root / "mic_confirmations.json"
            session = Session(session_id="source", folder=str(original_root), povs=[
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
            metadata = root / "metadata"
            metadata.mkdir()
            for stem in ("first", "second"):
                (metadata / f"{stem}.json").write_text(
                    json.dumps({"speaker_tracks": {"audio_track_2": "Ada"}}), encoding="utf-8")
            conflicting_confirmations = original_root / ".autoclipper" / "mic_confirmations.json"
            conflicting_confirmations.parent.mkdir()
            conflicting_confirmations.write_text(json.dumps({"confirmations": []}), encoding="utf-8")
            conflicting_metadata = original_root / "_Transcripts"
            conflicting_metadata.mkdir()
            for stem in ("first", "second"):
                (conflicting_metadata / f"{stem}.json").write_text(
                    json.dumps({"speaker_tracks": {"audio_track_1": "Other"}}), encoding="utf-8")
            media_files = [media / name for name in ("first.mp4", "second.mp4")]
            for path in media_files:
                path.write_bytes(b"synthetic fixture")
            originals = {path: path.read_bytes() for path in [source, confirmations, *media_files,
                                                               *metadata.glob("*.json")]}
            inventory = [
                {"codec_type": "video", "r_frame_rate": "60/1", "nb_frames": "60", "duration": "1"},
                {"codec_type": "audio", "index": 1, "channels": 2, "tags": {"title": "Track1"}},
                {"codec_type": "audio", "index": 2, "channels": 2, "tags": {"title": "Track2"}},
            ]
            sdk = Mock()
            with (patch.object(sys, "argv", ["director", "prepare", str(source), str(confirmations),
                                             str(media), "--metadata", str(metadata)]),
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
            self.assertEqual(manifest["pinned_inputs"]["confirmations"]["path"], "mic_confirmations.json")
            self.assertEqual(manifest["pinned_inputs"]["metadata"]["path"], "metadata")
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

            (scratch / "build.json").write_text(json.dumps(built), encoding="utf-8")
            with (patch.object(sys, "argv", ["director", "index-notes", str(scratch), str(source)]),
                  redirect_stdout(io.StringIO())):
                main()
            store = FKBStore(scratch)
            self.assertEqual(store.load().session.review_session_start_frame, 60)
            self.assertEqual(store.load().pinned_inputs, manifest["pinned_inputs"])
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
            store.update(lambda data: data.edit_plans.append(EditPlan("draft", [
                {"entry_id": "selected", "kind": "source", "title": "Verified take",
                 "frames": note.frames, "note_ids": [note.note_id]},
            ])))
            live = load_session(source)
            with patch("autoclipper.resolve_manifest.probe_streams", return_value=inventory) as probe:
                edit = compile_fkb_edit_plan(live, store.load(), "draft", revision_id="r1")
            self.assertEqual(edit["source_mapping_digest"], manifest["source_mapping_digest"])
            angle, = edit["segments"][0]["angles"]
            self.assertEqual(angle["mp4_path"], str(media / "second.mp4"))
            self.assertEqual((angle["source_in_frame"], angle["source_out_frame"]), (0, 5))
            self.assertEqual(angle["mic"]["expected_owner_track"], 2)
            self.assertEqual([call.args[0] for call in probe.call_args_list], media_files)
            with patch("autoclipper.resolve_manifest.probe_streams", return_value=inventory):
                explicit = compile_fkb_edit_plan(live, store.load(), "draft", revision_id="r1",
                                                 confirmations=confirmations, transcripts_dir=metadata)
            self.assertEqual(explicit["source_mapping_digest"], edit["source_mapping_digest"])
            with self.assertRaisesRegex(ValueError, "Mic confirmations does not match"):
                compile_fkb_edit_plan(live, store.load(), "draft", revision_id="r1",
                                      confirmations=conflicting_confirmations)
            with self.assertRaisesRegex(ValueError, "Metadata does not match"):
                compile_fkb_edit_plan(live, store.load(), "draft", revision_id="r1",
                                      transcripts_dir=conflicting_metadata)
            pinned_file = scratch / "mic_confirmations.json"
            pinned_bytes = pinned_file.read_bytes()
            pinned_file.write_text(json.dumps({"confirmations": []}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Mic confirmations does not match"):
                compile_fkb_edit_plan(live, store.load(), "draft", revision_id="r1")
            pinned_file.write_bytes(pinned_bytes)
            pinned_metadata = scratch / "metadata" / "first.json"
            pinned_metadata.unlink()
            with self.assertRaisesRegex(ValueError, "Metadata does not match"):
                compile_fkb_edit_plan(live, store.load(), "draft", revision_id="r1")
            pinned_metadata.write_bytes((metadata / "first.json").read_bytes())
            legacy = store.load()
            legacy.pinned_inputs = None
            with self.assertRaisesRegex(ValueError, "older source-backed catalog"):
                compile_fkb_edit_plan(live, legacy, "draft", revision_id="r1")
            self.assertEqual((live.folder, live.povs[0].parts[1].video_path),
                             (str(original_root), "second.mkv"))
