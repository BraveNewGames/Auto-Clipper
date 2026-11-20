import contextlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autoclipper.models import Part, POV, Review, Session, Suggestion
from autoclipper.resolve_manifest import build_manifest, main, render_report
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
            {"index": 0, "codec_type": "video", "r_frame_rate": "60/1"},
            {"index": 3, "codec_type": "audio", "channels": 1, "tags": {"title": "Track1"}},
            {"index": 7, "codec_type": "audio", "channels": 2, "tags": {"handler_name": "Track2"}},
        ]
        probe = patch("autoclipper.resolve_manifest.probe_streams", return_value=self.streams)
        self.probe = probe.start()
        self.addCleanup(probe.stop)

    def add_part(self, name, index=1, offset=0, duration=10, mp4=True, inferred=False):
        pov = next((pov for pov in self.session.povs if pov.name == name), None)
        if pov is None:
            pov = POV(name.lower(), name)
            self.session.povs.append(pov)
        stem = f"{pov.pov_id}-{index}"
        pov.parts.append(Part(stem, index, f"{stem}.mkv", duration=duration, offset=offset))
        if mp4:
            (self.folder / f"{stem}.mp4").touch()
        metadata = {"speaker_tracks": {"audio_track_2": name}}
        if inferred:
            metadata["relabel_history"] = [
                {"provenance": "inferred_from_labels", "changes": [{"track": 2, "to": name}]},
            ]
        dest = self.transcripts / stem
        dest.mkdir(parents=True)
        (dest / f"{stem}.json").write_text(json.dumps(metadata), encoding="utf-8")
        return stem

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
        self.assertEqual(a["angles"][0]["mic"]["resolve_channels"], [2, 3])
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
        self.assertEqual(codes, {"untimed", "duplicate_event", "overlapping_selection", "game_audio_unidentified"})
        report = render_report(manifest)
        for value in ("3 timed Keep selections", "Alpha P1 (verified, ch 2, 3)",
                      r"Test \| Clip (approved)", "Game audio not identified", "Source mode"):
            self.assertIn(value, report)

    def test_inferred_requires_matching_owner_confirmation(self):
        part_id = self.add_part("Alpha", inferred=True)
        self.select("pick", "EV-01", 1, 2)
        manifest = build_manifest(self.session)
        self.assertIn("mic_inferred_unconfirmed", {x["code"] for x in manifest["blockers"]})
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
        self.select("pick", "EV-01", 1, 2)
        manifest = build_manifest(self.session)
        self.assertIn("mic_unmapped", {x["code"] for x in manifest["blockers"]})

    def test_cli_only_writes_when_requested(self):
        part_id = self.add_part("Alpha", inferred=True)
        self.select("pick", "EV-01", 1, 2)
        save_session(self.session)
        output = self.root / "output"
        confirmations = self.folder / ".autoclipper" / "mic_confirmations.json"
        args = [str(self.folder), "--out", str(output), "--confirm-mic", part_id, "2", "Alpha"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main([*args, "--dry-run"]), 0)
            self.assertFalse(output.exists())
            self.assertFalse(confirmations.exists())
            self.assertEqual(main(args), 0)
        self.assertTrue(confirmations.is_file())
        self.assertTrue((output / "manifest.json").is_file())
        self.assertTrue((output / "report.md").is_file())


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
