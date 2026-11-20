from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from autoclipper.director_poc.core import Clock, NoteStore, Review


IDENTITY = {"project": "AutoClipper DirectorA Test 20261120-010203", "project_id": "p1",
            "timeline": "DirectorA REVIEW", "timeline_id": "t1", "fps": 60}


def segment(record, seconds, duration, part, source=0):
    return {"record_start_frame": record, "session_start": seconds, "duration_frames": duration,
            "angles": [{"pov_id": "pov", "part_id": part, "source_in_frame": source}] if part else []}


def manifest(*segments):
    return {"fps": 60, "segments": segments}


class DirectorClockTests(unittest.TestCase):
    def test_nonzero_origin_and_cross_segment_boundary(self):
        clock = Clock(manifest(segment(0, 5, 60, "one", 120), segment(60, 6, 60, "two")),
                      "01:00:00:00", 216000)
        self.assertEqual(clock.timecode(62), "01:00:01:02")
        self.assertEqual(clock.relative("01:00:01:02"), 62)
        self.assertEqual(clock.session_range(58, 63), (358, 363))
        self.assertEqual(clock.timeline_frame(362), 62)
        self.assertEqual(clock.sources(359), {"pov": ("one", 179)})
        self.assertEqual(clock.sources(360), {"pov": ("two", 0)})
        with self.assertRaises(ValueError):
            clock.relative("01:00:00:60")

    def test_empty_or_skipped_intervals_fail_visibly(self):
        for middle in ((), (segment(60, 1, 60, None),)):
            with self.subTest(middle=middle):
                clock = Clock(manifest(segment(0, 0, 60, "one"), *middle,
                                       segment(120 if middle else 60, 2, 60, "two")),
                              "01:00:00:00", 216000)
                self.assertEqual(clock.session_range(58, 60), (58, 60))
                self.assertEqual(clock.sources(61), {})
                with self.assertRaises(ValueError):
                    clock.session_range(58, 63)
                with self.assertRaises(ValueError):
                    clock.timeline_frame(61)


class DirectorCaptureTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.manager = self.api.GetProjectManager.return_value
        self.project = self.manager.GetCurrentProject.return_value
        self.timeline = self.project.GetCurrentTimeline.return_value
        for obj, name, uid in ((self.project, "project", "project_id"),
                               (self.timeline, "timeline", "timeline_id")):
            obj.GetName.return_value = IDENTITY[name]
            obj.GetUniqueId.return_value = IDENTITY[uid]
        self.timeline.GetStartFrame.return_value = 216000
        self.timeline.GetStartTimecode.return_value = "01:00:00:00"
        self.timeline.GetSettings.return_value = {"timelineFrameRate": "60"}
        self.marks = {"in": 10, "out": 39}
        self.timeline.GetMarkInOut.return_value = {"video": self.marks}
        self.timeline.GetCurrentTimecode.return_value = "01:00:00:50"
        self.manifest = manifest(segment(0, 0, 120, "one"))
        self.review = Review(self.api, IDENTITY, self.manifest)
        self.timeline.reset_mock()

    def test_capture_freezes_marks_before_save_using_only_getters(self):
        capture = self.review.capture()
        self.marks.update({"in": 20, "out": 59})
        self.timeline.GetCurrentTimecode.return_value = "01:00:01:00"
        with TemporaryDirectory() as directory:
            note = NoteStore(Path(directory) / "notes.json").add(capture, "Observation")
        self.assertEqual((note["timeline_in"], note["timeline_out_exclusive"]), (10, 40))
        self.assertEqual((note["session_in"], note["session_out_exclusive"]), (10, 40))
        self.assertEqual(note["playhead"], "01:00:00:50")
        self.assertEqual(note["out_timecode_exclusive"], "01:00:00:40")
        self.assertTrue(all(call[0].startswith("Get") for call in self.timeline.mock_calls))

    def test_exclusive_and_missing_or_point_marks(self):
        other = Review(self.api, IDENTITY, self.manifest, out_inclusive=False)
        self.assertEqual(other.capture().timeline_out_exclusive, 39)
        for marks in ({}, {"in": 10}, {"in": 10, "out": 10}, {"in": 30, "out": 10}):
            with self.subTest(marks=marks):
                self.timeline.GetMarkInOut.return_value = {"video": marks}
                with self.assertRaisesRegex(ValueError, "distinct"):
                    self.review.capture()

    def test_store_distinct_overlap_persistence_and_revision(self):
        capture = self.review.capture()
        overlapping = replace(capture, session_out_exclusive=45, timeline_out_exclusive=45,
                              out_timecode_exclusive="01:00:00:45")
        point = replace(capture, session_out_exclusive=capture.session_in)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "notes.json"
            store = NoteStore(path)
            with self.assertRaises(ValueError):
                store.add(point, "Point")
            with self.assertRaises(ValueError):
                store.add(None, "Missing range")
            a = store.add(capture, "First")
            b = store.add(overlapping, " Second ")
            self.assertNotEqual(a["note_id"], b["note_id"])
            store.revise(b["note_id"], "Updated")
            persisted = NoteStore(path).list()
        self.assertEqual(persisted[0], a)
        self.assertEqual(persisted[1]["original_text"], " Second ")
        self.assertEqual(persisted[1]["text"], "Updated")
        self.assertEqual(persisted[1]["session_out_exclusive"], 45)
        self.assertEqual(persisted[1]["revisions"][0]["text"], " Second ")

    def test_wrong_identity_and_lost_connection(self):
        self.timeline.GetUniqueId.return_value = "other"
        with self.assertRaisesRegex(RuntimeError, "review timeline"):
            self.review.capture()
        self.project.GetUniqueId.return_value = "other"
        with self.assertRaisesRegex(RuntimeError, "scratch project"):
            self.review.sample()
        self.api.GetProjectManager.return_value = None
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            self.review.sample()
        self.timeline.SetCurrentTimecode.assert_not_called()

    def test_explicit_seek_and_marker_actions(self):
        with TemporaryDirectory() as directory:
            note = NoteStore(Path(directory) / "notes.json").add(self.review.capture(), "Observation")
        self.timeline.GetCurrentTimecode.return_value = "01:00:00:10"
        self.assertEqual(self.review.jump(note), "Target 01:00:00:10, read back 01:00:00:10")
        self.timeline.SetCurrentTimecode.assert_called_once_with("01:00:00:10")
        self.timeline.GetMarkers.return_value = {10: {"name": "Existing"}}
        with self.assertRaisesRegex(ValueError, "already has"):
            self.review.marker(note, "add")
        self.timeline.AddMarker.assert_not_called()
        self.timeline.GetMarkers.return_value = {}
        self.review.marker(note, "add")
        self.timeline.AddMarker.assert_called_once_with(10, "Blue", "DirectorA note", "", 30,
                                                         note["note_id"])
        self.review.marker(note, "delete")
        self.timeline.DeleteMarkerByCustomData.assert_called_once_with(note["note_id"])
