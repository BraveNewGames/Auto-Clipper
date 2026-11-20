from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from autoclipper.director_poc.__main__ import main
from autoclipper.director_poc.core import Clock
from autoclipper.models import Part, POV, Session


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
