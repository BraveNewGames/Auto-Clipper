"""Synthetic regression checks for copied sessions and MP4-only recordings."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from autoclipper.director_poc.__main__ import prepare, source_session
from autoclipper.fkb import ingest_session
from autoclipper.losslesscut import expected_filenames, export_reviewer
from autoclipper.media import resolve_recording
from autoclipper.models import Part, POV, Session, Suggestion
from autoclipper.resolve_manifest import catalog_source_identity
from autoclipper.resolve_manifest import build_review_manifest
from autoclipper.server import make_server


class Mp4SessionRootTests(unittest.TestCase):
    def test_override_prepare_and_catalog_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "media"
            media.mkdir()
            (media / "angle.mp4").touch()
            (media / "angle.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nSynthetic\n", encoding="utf-8")
            source = root / "session.json"
            session = Session(session_id="synthetic", folder=str(root / "stale"), povs=[
                POV("p", "Player", [Part("part", video_path="angle.mkv", srt_path="angle.srt",
                                         duration=1)])])
            source.write_text(json.dumps(session.to_dict()), encoding="utf-8")
            confirmations = root / "confirmations.json"
            confirmations.write_text(json.dumps({"confirmations": [
                {"part_id": "part", "transcriber_track": 1, "speaker": "Player", "confirmed_by": "owner"}
            ]}), encoding="utf-8")
            metadata = root / "metadata"
            metadata.mkdir()
            (metadata / "angle.json").write_text(json.dumps({
                "speaker_tracks": {"audio_track_1": "Player"}}), encoding="utf-8")
            scratch = root / "autoclipper-director-a-test"
            scratch.mkdir()
            streams = [{"codec_type": "video", "r_frame_rate": "60/1", "nb_frames": "60",
                        "duration": "1"},
                       {"codec_type": "audio", "index": 1, "channels": 2,
                        "tags": {"title": "Track1"}}]
            original = source.read_bytes()
            args = SimpleNamespace(session=source, confirmations=confirmations, media_root=media,
                                   session_root=media, metadata=metadata, fps=60)
            with (patch("autoclipper.director_poc.__main__.mkdtemp", return_value=str(scratch)),
                  patch("autoclipper.resolve_manifest.probe_streams", return_value=streams)):
                prepare(args)
            manifest = json.loads((scratch / "manifest.json").read_text(encoding="utf-8"))
            loaded = source_session(source, media)
            self.assertEqual(manifest["catalog_source"], catalog_source_identity(loaded))
            self.assertEqual(len(ingest_session(loaded, fps=60, review_manifest=manifest).transcript_spans), 1)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(manifest["source_mapping"]["parts"][0]["media"]["mp4_path"],
                             str(media / "angle.mp4"))
            self.assertIn("stale_mkv_reference", [w["code"] for w in manifest["warnings"]])
            loaded.povs[0].parts[0].video_path = "different.mp4"
            with self.assertRaisesRegex(ValueError, "source mapping disagrees"):
                ingest_session(loaded, fps=60, review_manifest=manifest)

    def test_missing_srt_is_explicit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "angle.mp4").touch()
            source = root / "session.json"
            source.write_text(json.dumps(Session(session_id="synthetic", folder=str(root / "stale"), povs=[
                POV("p", parts=[Part("part", video_path="angle.mkv", srt_path="missing.srt")])
            ]).to_dict()), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SRT missing.*--session-root"):
                source_session(source, root)

    def test_losslesscut_mp4_fallback_and_legacy_mkv(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "angle.mp4").touch()
            session = Session(session_id="synthetic", folder=str(root), povs=[
                POV("p", "Player", [Part("part", video_path="angle.mkv", duration=5)])])
            session.suggestions.append(Suggestion("s", "reviewer", event_id="E1",
                                                  title="Synthetic", start=1, end=2))
            self.assertEqual(resolve_recording(root, "angle.mkv").status, "stale_mkv")
            self.assertTrue(expected_filenames(session, "reviewer")[0].endswith(".mp4"))
            exported, = export_reviewer(session, "reviewer", root / "exports")
            self.assertEqual(exported.video_path, str(root / "angle.mp4"))
            self.assertIn("angle.mp4", (Path(exported.csv_path).parent / "README.txt").read_text())
            (root / "angle.mkv").touch()
            self.assertEqual(resolve_recording(root, "angle.mkv").status, "found")
            self.assertTrue(expected_filenames(session, "reviewer")[0].endswith(".mkv"))
            legacy_export, = export_reviewer(session, "reviewer", root / "legacy_exports")
            self.assertEqual(legacy_export.video_path, "angle.mkv")
            (root / "angle.mkv").unlink()
            (root / "angle.mp4").unlink()
            self.assertEqual(resolve_recording(root, "angle.mkv").status, "missing")
            missing_export, = export_reviewer(session, "reviewer", root / "missing_exports")
            self.assertIn("missing media: angle.mkv",
                          (Path(missing_export.csv_path).parent / "README.txt").read_text())

    def test_server_mic_choices_and_legacy_planner(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "angle.mp4").touch()
            metadata = root / "_Transcripts"
            metadata.mkdir()
            (metadata / "angle.json").write_text(json.dumps({
                "speaker_tracks": {"audio_track_1": "Player"}}), encoding="utf-8")
            part = Part("part", video_path="angle.mkv", duration=1)
            pov = POV("p", "Player", [part])
            session = Session(session_id="synthetic", folder=str(root), povs=[pov])
            streams = [{"codec_type": "video", "r_frame_rate": "60/1", "nb_frames": "60",
                        "duration": "1"},
                       {"codec_type": "audio", "index": 1, "channels": 2,
                        "tags": {"title": "Track1"}}]
            confirmations = [{"part_id": "part", "transcriber_track": 1, "speaker": "Player",
                              "confirmed_by": "owner"}]
            server = make_server(0)
            try:
                with (patch("autoclipper.resolve_manifest.probe_streams", return_value=streams),
                      patch("autoclipper.media.probe_streams", return_value=streams)):
                    choices = server.app._mic_choices(session, pov, part)
                    self.assertEqual(choices["mp4"], str(root / "angle.mp4"))
                    self.assertEqual(len(choices["options"]), 1)
                    only_mp4 = build_review_manifest(session, confirmations=confirmations)
                    (root / "angle.mkv").touch()
                    legacy = build_review_manifest(session, confirmations=confirmations)
                self.assertEqual(only_mp4["source_mapping"]["parts"][0]["media"]["mp4_path"],
                                 legacy["source_mapping"]["parts"][0]["media"]["mp4_path"])
                self.assertEqual(legacy["source_mapping"]["parts"][0]["source_path"],
                                 str(root / "angle.mkv"))
                self.assertNotIn("stale_mkv_reference", [w["code"] for w in legacy["warnings"]])
                (root / "angle.mp4").unlink()
                missing = build_review_manifest(session, confirmations=confirmations)
                self.assertIn("mp4_missing", [b["code"] for b in missing["blockers"]])
            finally:
                server.server_close()


if __name__ == "__main__":
    unittest.main()
