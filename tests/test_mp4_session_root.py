"""Synthetic regression checks for copied sessions and MP4-only recordings."""
import json
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import sys
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from autoclipper.director_poc.__main__ import main, prepare, source_session
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
            other = root / "other-media"
            other.mkdir()
            (other / "angle.mp4").touch()
            (other / "angle.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nDifferent synthetic text\n", encoding="utf-8")
            (scratch / "build.json").write_text("{}", encoding="utf-8")
            error = io.StringIO()
            with (patch.object(sys, "argv", ["director", "index-notes", str(scratch), str(source),
                                             "--session-root", str(other)]),
                  patch("autoclipper.director_poc.__main__.initialize_fkb") as index,
                  redirect_stderr(error), self.assertRaises(SystemExit)):
                main()
            self.assertIn("Session root or SRT contents differ from prepare", error.getvalue())
            self.assertIn("Cannot index the source session", error.getvalue())
            index.assert_not_called()
            (media / "angle.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nChanged synthetic text\n", encoding="utf-8")
            error = io.StringIO()
            with (patch.object(sys, "argv", ["director", "index-notes", str(scratch), str(source),
                                             "--session-root", str(media)]),
                  patch("autoclipper.director_poc.__main__.initialize_fkb") as index,
                  redirect_stderr(error), self.assertRaises(SystemExit)):
                main()
            self.assertIn("Session root or SRT contents differ from prepare", error.getvalue())
            index.assert_not_called()
            (media / "angle.srt").unlink()
            error = io.StringIO()
            with (patch.object(sys, "argv", ["director", "index-notes", str(scratch), str(source),
                                             "--session-root", str(media)]),
                  redirect_stderr(error), self.assertRaises(SystemExit)):
                main()
            self.assertIn("Cannot index the source session. SRT missing", error.getvalue())
            self.assertNotIn("build receipt", error.getvalue())

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
            with self.assertRaisesRegex(ValueError, "--session-root does not exist"):
                source_session(source, root / "absent")
            confirmations = root / "confirmations.json"
            confirmations.write_text('{"confirmations": []}', encoding="utf-8")
            missing_error = io.StringIO()
            with (patch.object(sys, "argv", ["director", "prepare", str(source), str(confirmations),
                                             str(root), "--session-root", str(root)]),
                  patch("autoclipper.director_poc.__main__.mkdtemp") as scratch,
                  redirect_stderr(missing_error), self.assertRaises(SystemExit)):
                main()
            self.assertIn("SRT missing for part", missing_error.getvalue())
            scratch.assert_not_called()
            error = io.StringIO()
            with (patch.object(sys, "argv", ["director", "prepare", str(source), str(confirmations),
                                             str(root), "--session-root", str(root / "absent")]),
                  patch("autoclipper.director_poc.__main__.mkdtemp") as scratch,
                  redirect_stderr(error), self.assertRaises(SystemExit)):
                main()
            self.assertIn("--session-root does not exist", error.getvalue())
            self.assertNotIn("Traceback", error.getvalue())
            scratch.assert_not_called()

    def test_absolute_srt_outside_root_is_valid_without_override(self):
        with TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "recordings"
            root.mkdir()
            srt = parent / "captions.srt"
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nSynthetic\n", encoding="utf-8")
            source = parent / "session.json"
            source.write_text(json.dumps(Session(session_id="synthetic", folder=str(root), povs=[
                POV("p", parts=[Part("part", srt_path=str(srt))])
            ]).to_dict()), encoding="utf-8")
            self.assertEqual(source_session(source).all_parts()[0][1].srt_path, str(srt))

    def test_relative_srt_escape_and_video_outside_override_are_rejected(self):
        with TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "recordings"
            root.mkdir()
            (parent / "outside.srt").touch()
            (parent / "outside.mp4").touch()
            source = parent / "session.json"
            part = Part("part", video_path="../outside.mp4", srt_path="../outside.srt")
            session = Session(session_id="synthetic", folder=str(root), povs=[POV("p", parts=[part])])
            source.write_text(json.dumps(session.to_dict()), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SRT path escapes session root"):
                source_session(source, root)
            confirmations = parent / "confirmations.json"
            confirmations.write_text('{"confirmations": []}', encoding="utf-8")
            with (patch.object(sys, "argv", ["director", "prepare", str(source), str(confirmations),
                                             str(root), "--session-root", str(root)]),
                  patch("autoclipper.director_poc.__main__.mkdtemp") as scratch,
                  redirect_stderr(io.StringIO()), self.assertRaises(SystemExit)):
                main()
            scratch.assert_not_called()
            part.srt_path = None
            source.write_text(json.dumps(session.to_dict()), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Recording missing or outside --session-root"):
                source_session(source, root)

    def test_symlinked_mp4_fallback_outside_root_is_rejected(self):
        with TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "recordings"
            root.mkdir()
            outside = parent / "outside.mp4"
            outside.touch()
            try:
                (root / "angle.mp4").symlink_to(outside)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")
            source = parent / "session.json"
            source.write_text(json.dumps(Session(session_id="synthetic", folder=str(parent / "stale"), povs=[
                POV("p", parts=[Part("part", video_path="angle.mkv")])
            ]).to_dict()), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Recording missing or outside --session-root"):
                source_session(source, root)

    def test_symlinked_srt_reports_prepare_error_before_scratch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "angle.mp4").touch()
            target = root / "captions.srt"
            target.write_text("1\n00:00:00,000 --> 00:00:01,000\nSynthetic\n", encoding="utf-8")
            try:
                (root / "angle.srt").symlink_to(target)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")
            source = root / "session.json"
            source.write_text(json.dumps(Session(session_id="synthetic", folder=str(root), povs=[
                POV("p", parts=[Part("part", video_path="angle.mp4", srt_path="angle.srt")])
            ]).to_dict()), encoding="utf-8")
            confirmations = root / "confirmations.json"
            confirmations.write_text('{"confirmations": []}', encoding="utf-8")
            error = io.StringIO()
            with (patch.object(sys, "argv", ["director", "prepare", str(source), str(confirmations),
                                             str(root), "--session-root", str(root)]),
                  patch("autoclipper.director_poc.__main__.mkdtemp") as scratch,
                  redirect_stderr(error), self.assertRaises(SystemExit)):
                main()
            self.assertIn("Cannot prepare the source session. Pinned input contains a link or reparse point",
                          error.getvalue())
            self.assertNotIn("Traceback", error.getvalue())
            scratch.assert_not_called()

    def test_empty_folder_uses_explicit_root_for_both_commands_and_old_manifest_warns(self):
        with TemporaryDirectory() as directory:
            parent = Path(directory)
            media = parent / "media"
            media.mkdir()
            (media / "angle.mp4").touch()
            source = parent / "session.json"
            source.write_text(json.dumps(Session(session_id="synthetic", folder="", povs=[
                POV("p", "Player", [Part("part", video_path="angle.mp4", duration=1)])
            ]).to_dict()), encoding="utf-8")
            self.assertEqual(source_session(source).folder, str(parent))
            confirmations = parent / "confirmations.json"
            confirmations.write_text(json.dumps({"confirmations": [
                {"part_id": "part", "transcriber_track": 1, "speaker": "Player",
                 "confirmed_by": "owner"}]}), encoding="utf-8")
            metadata = parent / "metadata"
            metadata.mkdir()
            (metadata / "angle.json").write_text(json.dumps({
                "speaker_tracks": {"audio_track_1": "Player"}}), encoding="utf-8")
            scratch = parent / "autoclipper-director-a-empty"
            scratch.mkdir()
            streams = [{"codec_type": "video", "r_frame_rate": "60/1", "nb_frames": "60",
                        "duration": "1"},
                       {"codec_type": "audio", "index": 1, "channels": 2,
                        "tags": {"title": "Track1"}}]
            with (patch("autoclipper.director_poc.__main__.mkdtemp", return_value=str(scratch)),
                  patch("autoclipper.resolve_manifest.probe_streams", return_value=streams)):
                prepare(SimpleNamespace(session=source, confirmations=confirmations, media_root=media,
                                        session_root=media, metadata=metadata, fps=60))
            manifest_path = scratch / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["pinned_inputs"]["session_source"]["root"], str(media).casefold())
            (scratch / "build.json").write_text("{}", encoding="utf-8")
            args = ["director", "index-notes", str(scratch), str(source), "--session-root", str(media)]
            with (patch.object(sys, "argv", args),
                  patch("autoclipper.director_poc.__main__.initialize_fkb", return_value=scratch / "catalog") as index):
                main()
            index.assert_called_once()
            manifest["pinned_inputs"].pop("session_source")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            warning = io.StringIO()
            with (patch.object(sys, "argv", args),
                  patch("autoclipper.director_poc.__main__.initialize_fkb", return_value=scratch / "catalog") as index,
                  redirect_stderr(warning)):
                main()
            self.assertIn("older manifest has no pinned session root or SRT digests", warning.getvalue())
            index.assert_called_once()
            implicit = parent / "autoclipper-director-a-implicit"
            implicit.mkdir()
            with (patch("autoclipper.director_poc.__main__.mkdtemp", return_value=str(implicit)),
                  patch("autoclipper.resolve_manifest.probe_streams", return_value=streams)):
                prepare(SimpleNamespace(session=source, confirmations=confirmations, media_root=media,
                                        session_root=None, metadata=metadata, fps=60))
            implicit_manifest = json.loads((implicit / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(implicit_manifest["pinned_inputs"]["session_source"]["root"],
                             str(parent).casefold())
            (implicit / "build.json").write_text("{}", encoding="utf-8")
            with (patch.object(sys, "argv", ["director", "index-notes", str(implicit), str(source)]),
                  patch("autoclipper.director_poc.__main__.initialize_fkb", return_value=implicit / "catalog") as index):
                main()
            index.assert_called_once()

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
            self.assertEqual(exported.video_path, "angle.mp4")
            self.assertIn("angle.mp4", (Path(exported.csv_path).parent / "README.txt").read_text())
            csv_bytes = Path(exported.csv_path).read_bytes()
            (root / "sub").mkdir()
            (root / "sub" / "angle.mp4").touch()
            session.povs[0].parts[0].video_path = "sub/angle.mkv"
            nested, = export_reviewer(session, "reviewer", root / "nested_exports")
            self.assertEqual(nested.video_path, "sub/angle.mp4")
            self.assertIn("sub/angle.mp4", (Path(nested.csv_path).parent / "README.txt").read_text())
            self.assertEqual(Path(nested.csv_path).read_bytes(), csv_bytes)
            session.povs[0].parts[0].video_path = "angle.mkv"
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
