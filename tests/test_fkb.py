import tempfile
import unittest
from pathlib import Path

from autoclipper.fkb import FKBStore, FrameRange, Session as FKBSession
from autoclipper.models import Part, POV, Session
from autoclipper.store import save_session


class FKBTest(unittest.TestCase):
    def test_ingest_and_refresh(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "camera.mp4").touch()
            (root / "camera.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nAda: Drop it!\n", encoding="utf-8")
            part = Part("part", video_path="camera.mp4", srt_path="camera.srt",
                        duration=2, offset=1)
            source = Session(session_id="test", folder=folder, povs=[POV("ada", "Ada", [part])])
            original = save_session(source)
            before = original.read_bytes()
            store = FKBStore(folder)
            store.ingest(source, fps=24)
            data = store.load()
            span, = data.transcript_spans
            recording, = data.coverage(FrameRange(24, 25))
            self.assertEqual(span.frames, {"start": 24, "end": 48})
            self.assertEqual(span.sources[0]["part_id"], recording.part_id)
            self.assertFalse(data.coverage(FrameRange(0, 24)))
            part.offset = 2
            store.ingest(source, fps=24)
            self.assertEqual(store.load().transcript_spans[0].span_id, span.span_id)
            self.assertTrue(store.backup.exists())
            self.assertEqual(original.read_bytes(), before)

    def test_rejects_invalid_range(self):
        for start, end in [(1, 1), (2, 1), (1.0, 2), (False, 2)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                FrameRange(start, end)

    def test_legacy_session_defaults_to_zero_review_origin(self):
        session = FKBSession.from_dict({"session_id": "synthetic", "fps": 60, "timeline_start_frame": 216000})
        self.assertEqual(session.review_session_start_frame, 0)

