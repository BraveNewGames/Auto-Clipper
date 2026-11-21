import contextlib
from fractions import Fraction
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from autoclipper.models import Part, POV, Review, Session, Suggestion
from autoclipper.resolve_manifest import (
    _video_frames, build_manifest, build_review_manifest, compile_edit_plan, compile_fkb_edit_plan, main,
    render_report,
)
from autoclipper.store import save_session


class ResolveManifestTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.folder = self.root / "session"
        self.folder.mkdir()
        self.transcripts = self.root / "_Transcripts"
        self.session = Session(session_id="synthetic", folder=str(self.folder))
        self.streams = [
            {"index": 0, "codec_type": "video", "r_frame_rate": "60/1", "duration": "10"},
            {"index": 3, "codec_type": "audio", "channels": 1, "tags": {"title": "Track1"}},
            {"index": 7, "codec_type": "audio", "channels": 2, "tags": {"handler_name": "Track2"}},
        ]
        probe = patch("autoclipper.resolve_manifest.probe_streams", return_value=self.streams)
        self.probe = probe.start()
        self.durations = {}
        self.probe.side_effect = lambda path: [
            {**self.streams[0], "duration": str(self.durations[path.stem])}, *self.streams[1:]
        ]
        self.addCleanup(probe.stop)

    def add_part(self, name, index=1, offset=0, duration=10, mp4=True, inferred=False, video_duration=None):
        pov = next((pov for pov in self.session.povs if pov.name == name), None)
        if pov is None:
            pov = POV(name.lower(), name)
            self.session.povs.append(pov)
        stem = f"{pov.pov_id}-{index}"
        pov.parts.append(Part(stem, index, f"{stem}.mkv", duration=duration, offset=offset))
        self.durations[stem] = duration if video_duration is None else video_duration
        if mp4:
            (self.folder / f"{stem}.mp4").touch()
        metadata = {
            "speaker_tracks": [{"track": 2, "speaker": name, "speaker_template": "{Recorder}",
                                "index": 7, "title": "Track2"}],
            "recorder": {"name": name, "source": "filename", "tracks": [2]},
        }
        if inferred:
            metadata = {
                "speaker_tracks": {"audio_track_2": name},
                "original_speaker_tracks": {"2": "Another speaker"},
                "relabel_history": [{"provenance": "inferred_from_labels", "changes": [
                    {"track": 2, "from": "Another speaker", "to": name}
                ]}],
            }
        self.write_metadata(stem, metadata)
        return stem

    def write_metadata(self, stem, metadata):
        dest = self.transcripts / stem
        dest.mkdir(parents=True, exist_ok=True)
        (dest / f"{stem}.json").write_text(json.dumps(metadata), encoding="utf-8")

    def select(self, sid, event, first, last, approved=(None, None), status="keep"):
        self.session.suggestions.append(Suggestion(sid, "reviewer", event_id=event, title="Test | Clip",
                                                   start=first, end=last))
        self.session.reviews[sid] = Review(status, approved_start=approved[0], approved_end=approved[1])

    def test_four_povs_offsets_and_part_boundary(self):
        self.add_part("Alpha", offset=-1, duration=3)
        self.add_part("Alpha", 2, offset=2)
        for name in ("Bravo", "Charlie", "Delta"):
            self.add_part(name, offset=0.125)
        self.select("pick", "EV-01", 1, 3)
        manifest = build_manifest(self.session)
        self.assertEqual(manifest["blockers"], [])
        a, b = manifest["segments"]
        self.assertEqual([(s["session_start"], s["session_end"], s["record_start_frame"],
                           s["duration_frames"], s["pieces"]) for s in (a, b)],
                         [(1, 2, 0, 60, 2), (2, 3, 60, 60, 2)])
        self.assertEqual([len(s["angles"]) for s in (a, b)], [4, 4])
        self.assertEqual([x["source_in_frame"] for x in a["angles"]], [120, 53, 53, 53])
        self.assertEqual([x["source_out_frame"] for x in a["angles"]], [180, 113, 113, 113])
        self.assertEqual(a["angles"][0]["handle_after_frames"], 0)
        self.assertEqual(b["angles"][0]["part_index"], 2)
        self.assertEqual(a["angles"][0]["mic"]["mp4_audio_stream_index"], 7)
        mic = a["angles"][0]["mic"]
        self.assertEqual(mic["resolve_channels"], [2, 3])
        self.assertEqual(mic["resolve_channels_status"], "expected")
        self.assertEqual(mic["expected_owner_track"], 2)
        self.assertEqual(mic["mp4_audio_ordinal"], 2)
        self.assertEqual(mic["mp4_audio_channels"], 2)
        self.assertEqual(mic["mp4_audio_title"], "Track2")
        self.assertEqual(self.probe.call_count, 5)

    def test_gap_and_missing_pov_preserve_time(self):
        self.add_part("Alpha")
        self.add_part("Bravo", duration=2)
        self.add_part("Bravo", 2, offset=3)
        self.session.povs.append(POV("charlie", "Charlie"))
        self.select("pick", "EV-01", 1, 4)
        segments = build_manifest(self.session)["segments"]
        self.assertEqual([s["record_start_frame"] for s in segments], [0, 60, 120])
        self.assertEqual([x["reason"] for x in segments[1]["unavailable"]], ["recording_gap", "no_coverage"])
        self.assertEqual([x["pov_id"] for x in segments[1]["angles"]], ["alpha"])

    def test_review_clips_uncovered_edges_preserves_internal_gap_and_short_tail(self):
        self.add_part("Alpha", offset=1, duration=4, video_duration=1)
        self.add_part("Alpha", 2, offset=3, duration=3, video_duration=2)
        self.add_part("Bravo", offset=3.1, duration=1)
        self.session.povs.append(POV("charlie", "Charlie"))
        manifest = build_review_manifest(self.session)
        self.assertEqual(manifest["blockers"], [])
        self.assertEqual(manifest["requested_session_frames"], [0, 360])
        self.assertEqual(manifest["review_session_frames"], [60, 300])
        self.assertEqual([(s["session_start_frame"], s["session_end_frame"]) for s in manifest["uncovered_edges"]],
                         [(0, 60), (300, 360)])
        segments = manifest["segments"]
        self.assertEqual([(s["record_start_frame"], s["duration_frames"], s["kind"]) for s in segments],
                         [(0, 60, "media"), (60, 60, "coverage_gap"), (120, 6, "media"),
                          (126, 60, "media"), (186, 54, "media")])
        self.assertEqual(segments[1]["unavailable"][0]["reason"], "recording_gap")
        self.assertEqual(segments[2]["angles"][0]["part_id"], "alpha-2")
        self.assertEqual([a["source_in_frame"] for a in segments[3]["angles"]], [6, 0])
        self.assertEqual(segments[-1]["angles"][0]["source_out_frame"], 120)
        self.assertEqual(self.probe.call_count, 3)

    def test_review_omits_ambiguous_overlapping_angle_and_keeps_resolved_frames(self):
        self.add_part("Alpha", offset=-0.01, duration=2)
        self.add_part("Alpha", 2, offset=1, duration=2)
        self.add_part("Bravo", duration=3)
        manifest = build_review_manifest(self.session)
        self.assertEqual(manifest["blockers"], [])
        self.assertEqual(manifest["review_session_frames"], [-1, 180])
        overlap = next(s for s in manifest["segments"] if s["session_start_frame"] == 60)
        self.assertEqual([a["pov_id"] for a in overlap["angles"]], ["bravo"])
        self.assertEqual(overlap["unavailable"][0]["reason"], "overlapping_parts")
        recovered = next(s for s in manifest["segments"] if s["session_start_frame"] == 119)
        self.assertEqual(recovered["angles"][0]["source_in_frame"], 59)

    def test_review_does_not_guess_missing_media_or_mic(self):
        self.add_part("Alpha", mp4=False)
        self.add_part("Bravo", inferred=True)
        manifest = build_review_manifest(self.session)
        self.assertEqual({row["code"] for row in manifest["blockers"]},
                         {"mp4_missing", "mic_unverified", "no_angles"})
        self.assertEqual(manifest["segments"], [])

    def test_review_uses_reported_frame_count_or_exact_timestamp_duration(self):
        self.assertEqual(_video_frames({"nb_frames": "2", "duration": "0.033333"}, 60), 2)
        self.assertEqual(_video_frames({"duration_ts": 2, "time_base": "1/60"}, 60), 2)
        self.assertEqual(_video_frames({"duration": "0.033333"}, 60), 1)

    def test_edit_plan_preserves_order_choices_and_provenance_across_split_and_gaps(self):
        self.add_part("Alpha", duration=2)
        self.add_part("Alpha", 2, offset=2, duration=2)
        self.add_part("Bravo", duration=3)
        entries = [
            {"entry_id": "later", "kind": "source", "title": "Later first", "session_in": 100,
             "session_out_exclusive": 190, "pov_id": "alpha", "audio_pov_id": "bravo",
             "note_ids": ["note-a"], "candidate_ids": ["candidate-a"]},
            {"entry_id": "missing", "kind": "placeholder", "title": "Intended beat", "provisional_frames": 15},
            {"entry_id": "early", "kind": "source", "title": "Earlier last", "session_in": 5,
             "session_out_exclusive": 10},
        ]
        plan = {"plan_id": "plan", "revision_id": "r2", "session_id": self.session.session_id, "fps": 60,
                "revision_history": [{"revision_id": "r1"}], "entries": entries}
        manifest = compile_edit_plan(self.session, plan)
        self.assertEqual(manifest["blockers"], [])
        self.assertEqual(manifest["edit_plan"]["revision_history"], [{"revision_id": "r1"}])
        plan["revision_history"][0]["revision_id"] = "changed"
        self.assertEqual(manifest["edit_plan"]["revision_history"][0]["revision_id"], "r1")
        segments = manifest["segments"]
        self.assertEqual([s["kind"] for s in segments], ["media", "media", "placeholder", "placeholder", "media"])
        self.assertEqual([(s["record_start_frame"], s["duration_frames"]) for s in segments],
                         [(0, 20), (20, 60), (80, 10), (90, 15), (105, 5)])
        self.assertEqual([segments[i]["angles"][0]["part_id"] for i in (0, 1)], ["alpha-1", "alpha-2"])
        self.assertEqual(segments[1]["audio_angle"]["source_in_frame"], 120)
        self.assertEqual(segments[1]["note_ids"], ["note-a"])
        self.assertEqual(segments[2]["candidate_ids"], ["candidate-a"])
        for segment in segments[2:4]:
            self.assertNotIn("session_start_frame", segment)
            self.assertNotIn("session_end", segment)
        self.assertEqual([s["placeholder_reason"] for s in segments[2:4]],
                         ["source_unavailable", "planned_missing_beat"])
        self.assertEqual([a["pov_id"] for a in segments[-1]["angles"]], ["alpha", "bravo"])
        self.assertIn("Intended beat", render_report(manifest))

    def test_edit_plan_rejects_placeholder_with_source_coordinates(self):
        plan = {"plan_id": "plan", "revision_id": "r1", "session_id": self.session.session_id, "fps": 60,
                "revision_history": [], "entries": [{"entry_id": "missing", "kind": "placeholder",
                    "title": "Missing", "provisional_frames": 10, "session_in": 0}]}
        with self.assertRaisesRegex(ValueError, "no source range"):
            compile_edit_plan(self.session, plan)
        self.probe.assert_not_called()

    def test_fkb_adapter_uses_session_clock_and_existing_revision_history(self):
        self.add_part("Alpha")
        plan = SimpleNamespace(plan_id="plan", timeline_id="prior-timeline", revisions=[{"revision_id": "r1"}],
                               entries=[{"entry_id": "cut", "kind": "source", "title": "Grounded cut",
                                         "frames": {"start": 9, "end": 19}, "pov_id": "alpha"}])
        digest = build_review_manifest(self.session)["source_mapping_digest"]
        knowledge = SimpleNamespace(mapping_stale=False, edit_plans=[plan], director_notes=[],
                                    transcript_spans=[], source_mapping_digest=digest,
                                    session=SimpleNamespace(session_id=self.session.session_id, fps=60))
        with self.assertRaisesRegex(ValueError, "source mapping is unverified"):
            compile_fkb_edit_plan(self.session, knowledge, "plan", revision_id="r2")
        plan.source_mapping_digest = digest
        manifest = compile_fkb_edit_plan(self.session, knowledge, "plan", revision_id="r2")
        segment, = manifest["segments"]
        self.assertEqual((segment["session_start_frame"], segment["session_end_frame"]), (9, 19))
        self.assertEqual(manifest["edit_plan"], {"plan_id": "plan", "revision_id": "r2",
                                                "revision_history": [{"revision_id": "r1"}]})
        self.assertEqual(plan.entries[0]["frames"], {"start": 9, "end": 19})
        knowledge.mapping_stale = True
        with self.assertRaisesRegex(ValueError, "stale FKB source mapping"):
            compile_fkb_edit_plan(self.session, knowledge, "plan", revision_id="r2")

    def test_selection_ranges_order_warnings_and_report(self):
        self.add_part("Alpha")
        self.select("z", "EV-02", 4, 5, approved=(1, 2))
        self.select("b", "EV-01", 1, 3)
        self.select("a", "EV-01", 1, 2)
        self.select("untimed", "EV-03", 2, 2)
        self.select("skip", "EV-04", 0, 2, status="reject")
        manifest = build_manifest(self.session)
        self.assertEqual([s["suggestion_id"] for s in manifest["segments"]], ["a", "b", "z"])
        self.assertEqual([s["range_source"] for s in manifest["segments"]], ["ai", "ai", "approved"])
        self.assertEqual(len({s["segment_id"] for s in manifest["segments"]}), 3)
        codes = {w["code"] for w in manifest["warnings"]}
        self.assertEqual(codes, {"untimed", "duplicate_event", "overlapping_selection",
                                 "game_audio_unidentified", "stale_mkv_reference"})
        report = render_report(manifest)
        for value in ("3 timed Keep selections", "Alpha P1 (verified, expected ch 2, 3)",
                      r"Test \| Clip (approved)", "Game audio not identified", "Source mode"):
            self.assertIn(value, report)

    def test_inferred_requires_matching_owner_confirmation(self):
        part_id = self.add_part("Alpha", inferred=True)
        self.select("pick", "EV-01", 1, 2)
        manifest = build_manifest(self.session)
        self.assertIn("mic_unverified", {x["code"] for x in manifest["blockers"]})
        self.assertEqual(manifest["segments"][0]["unavailable"][0]["reason"], "mic_blocked")
        confirmation = {"part_id": part_id, "transcriber_track": 2, "speaker": "Alpha",
                        "confirmed_by": "owner"}
        manifest = build_manifest(self.session, confirmations=[confirmation])
        self.assertEqual(manifest["blockers"], [])
        self.assertEqual(manifest["segments"][0]["angles"][0]["mic"]["status"], "owner_confirmed")

    def test_missing_mp4_and_fps_mismatch(self):
        self.add_part("Alpha", mp4=False)
        self.add_part("Bravo")
        self.select("pick", "EV-01", 1, 2)
        manifest = build_manifest(self.session, fps=30)
        self.assertEqual({"mp4_missing", "fps_mismatch", "no_angles"},
                         {x["code"] for x in manifest["blockers"]})
        self.assertEqual(self.probe.call_count, 1)

    def test_generic_tags_do_not_establish_track_identity(self):
        self.add_part("Alpha")
        self.streams[2]["tags"] = {"handler_name": "SoundHandler"}
        self.write_metadata("alpha-1", {"speaker_tracks": {"audio_track_2": "Alpha"}})
        self.select("pick", "EV-01", 1, 2)
        confirmation = {"part_id": "alpha-1", "transcriber_track": 2, "speaker": "Alpha",
                        "confirmed_by": "owner"}
        manifest = build_manifest(self.session, confirmations=[confirmation])
        self.assertIn("mic_unmapped", {x["code"] for x in manifest["blockers"]})

    def test_unknown_provenance_fails_closed_but_owner_can_confirm(self):
        part = self.add_part("Alpha")
        self.select("pick", "EV", 1, 2)
        raw = json.loads((self.transcripts / part / f"{part}.json").read_text())
        cases = [
            {"speaker_tracks": {"audio_track_2": "Alpha"}},
            {**raw, "original_speaker_tracks": {"2": "Another speaker"}},
            {**raw, "relabel_history": [{"source": "inferred", "changes": [{"track": 2}]}]},
            {**raw, "relabel_history": [{"provenance": "manual", "changes": {"2": "Alpha"}}]},
            {**raw, "relabel_history": "unknown"},
            {**raw, "speaker_tracks": [None, "Alpha"]},
            {**raw, "relabel_provenance": "unknown"},
            {"speaker_tracks": "Alpha"},
            ["unexpected top-level shape"],
        ]
        confirmation_path = self.root / "confirmations.json"
        confirmation_path.write_text(json.dumps({"schema_version": 1, "confirmations": [
            {"part_id": part, "transcriber_track": 2, "speaker": "Alpha", "confirmed_by": "owner"},
        ]}))
        for data in cases:
            with self.subTest(data=data):
                self.write_metadata(part, data)
                manifest = build_manifest(self.session)
                self.assertIn("mic_unverified", {x["code"] for x in manifest["blockers"]})
                confirmed = build_manifest(self.session, confirmations=confirmation_path)
                self.assertEqual(confirmed["blockers"], [])
                self.assertEqual(confirmed["segments"][0]["angles"][0]["mic"]["status"],
                                 "owner_confirmed")

    def test_actual_video_duration_splits_short_recording_gap(self):
        self.add_part("Alpha", duration=1800.492, video_duration=1800.399)
        self.add_part("Alpha", 2, offset=1800.492)
        self.add_part("Bravo", offset=1700, duration=200)
        self.select("pick", "EV", 1800, 1801)
        manifest = build_manifest(self.session)
        self.assertEqual(manifest["blockers"], [])
        left, gap, right = manifest["segments"]
        self.assertEqual([(s["session_start"], s["session_end"]) for s in (left, gap, right)],
                         [(1800, 1800.399), (1800.399, 1800.492), (1800.492, 1801)])
        self.assertEqual([s["duration_frames"] for s in (left, gap, right)], [24, 6, 30])
        self.assertEqual(gap["unavailable"][0]["reason"], "recording_gap")
        self.assertEqual(left["angles"][0]["source_out_frame"], 108023)
        self.assertEqual(right["angles"][0]["source_in_frame"], 0)
        self.assertEqual(self.session.povs[0].parts[1].offset, 1800.492)
        self.assertIn("short_gap_segment", render_report(manifest))
        self.assertIn("0.093 s", render_report(manifest))
        self.assertEqual(left["angles"][-1]["source_out_frame"], gap["angles"][0]["source_in_frame"])
        self.assertEqual(gap["angles"][0]["source_out_frame"], right["angles"][-1]["source_in_frame"])

    def test_video_duration_must_be_verified(self):
        self.add_part("Alpha", video_duration="N/A")
        self.select("pick", "EV", 1, 2)
        manifest = build_manifest(self.session)
        self.assertIn("duration_unverified", {x["code"] for x in manifest["blockers"]})
        self.assertEqual(manifest["segments"][0]["angles"], [])

    def test_continuing_pov_has_contiguous_source_frames_over_fractional_boundaries(self):
        self.add_part("Alpha", offset=-0.1)
        self.add_part("Bravo")
        self.add_part("Bravo", 2, offset=1)
        self.select("pick", "EV", 0.1, 2.105)
        alpha, bravo = (pov.parts for pov in self.session.povs)
        for phase in range(17):
            alpha[0].offset = -phase / 1000
            for boundary in range(985, 1015):
                with self.subTest(phase=phase, boundary=boundary):
                    self.durations["bravo-1"] = bravo[1].offset = boundary / 1000
                    manifest = build_manifest(self.session)
                    self.assertEqual(manifest["blockers"], [])
                    left, right = manifest["segments"]
                    self.assertEqual(left["angles"][0]["source_out_frame"],
                                     right["angles"][0]["source_in_frame"])
                    self.assertEqual(left["duration_frames"], right["record_start_frame"])
                    self.assertEqual(left["duration_frames"] + right["duration_frames"],
                                     int(Fraction("2.005") * 60 + Fraction(1, 2)))

    def test_cli_only_writes_when_requested(self):
        part_id = self.add_part("Alpha", inferred=True)
        self.select("pick", "EV-01", 1, 2)
        save_session(self.session)
        confirmations = self.folder / ".autoclipper" / "mic_confirmations.json"
        export_root = self.folder / "autoclipper_exports" / "resolve"
        args = [str(self.folder), "--confirm-mic", part_id, "2", "Alpha"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main([*args, "--dry-run"]), 0)
            self.assertFalse(export_root.exists())
            self.assertFalse(confirmations.exists())
            self.assertEqual(main(args), 0)
        self.assertTrue(confirmations.is_file())
        output, = export_root.iterdir()
        self.assertTrue((output / "manifest.json").is_file())
        self.assertTrue((output / "report.md").is_file())

    def test_director_cli_requires_separate_scratch_output_and_keeps_confirmations_there(self):
        part_id = self.add_part("Alpha", inferred=True)
        save_session(self.session)
        source_files = sorted(self.folder.rglob("*"))
        scratch_root = self.root / "temp"
        output = scratch_root / "director"
        args = [str(self.folder), "--full-session", "--transcripts", str(self.transcripts),
                "--confirm-mic", part_id, "2", "Alpha"]
        with (contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors,
              patch("autoclipper.resolve_manifest.gettempdir", return_value=str(scratch_root))):
            for destination in ([], ["--out", str(self.folder / "output")],
                                ["--out", str(self.root / "elsewhere")]):
                with self.subTest(destination=destination), self.assertRaises(SystemExit) as raised:
                    main([*args, *destination])
                self.assertEqual(raised.exception.code, 2)
            self.assertIn("requires an explicit --out", errors.getvalue())
            self.assertIn("separate from the source session", errors.getvalue())
            self.assertEqual(sorted(self.folder.rglob("*")), source_files)
            self.assertEqual(main([*args, "--dry-run"]), 0)
            self.assertFalse(scratch_root.exists())
            self.assertEqual(main([*args, "--out", str(output)]), 0)
        confirmations = json.loads((output / "mic_confirmations.json").read_text(encoding="utf-8"))
        self.assertEqual(confirmations["confirmations"][0]["part_id"], part_id)
        self.assertTrue((output / "manifest.json").is_file())
        self.assertTrue((output / "report.md").is_file())
        self.assertEqual(sorted(self.folder.rglob("*")), source_files)


class MP4InventoryTests(unittest.TestCase):
    def test_older_ffprobe_track_titles_join_by_container_id(self):
        from autoclipper.media import probe_streams

        def box(kind, payload):
            return (len(payload) + 8).to_bytes(4, "big") + kind + payload

        def track(track_id, title):
            header = box(b"tkhd", bytes(12) + track_id.to_bytes(4, "big"))
            metadata = box(b"udta", box(b"name", title))
            return box(b"trak", header + metadata)

        inventory = {"streams": [
            {"codec_type": "audio", "index": 5, "id": "0x18"},
            {"codec_type": "audio", "index": 8, "id": "0x0b"},
        ]}
        with TemporaryDirectory() as folder:
            path = Path(folder) / "synthetic.mp4"
            path.write_bytes(box(b"mdat", b"") + box(b"moov", track(11, b"Track1") + track(24, b"Track2")))
            with patch("autoclipper.media.subprocess.run") as run:
                run.return_value.stdout = json.dumps(inventory)
                streams = probe_streams(path)
        self.assertEqual([s["tags"]["title"] for s in streams], ["Track2", "Track1"])
