from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from autoclipper.director_notes import format_session_range
from autoclipper.director_poc.core import Clock, NoteStore, Review, marker_name
from autoclipper.director_poc.window import marker_readback
from autoclipper.fkb import FrameRange, Session as FKBSession


IDENTITY = {"project": "AutoClipper DirectorA Test 20261120-010203", "project_id": "p1",
            "timeline": "DirectorA REVIEW", "timeline_id": "t1", "fps": 60}


def segment(record, seconds, duration, part, source=0):
    return {"record_start_frame": record, "session_start": seconds, "duration_frames": duration,
            "angles": [{"pov_id": "pov", "part_id": part, "source_in_frame": source}] if part else []}


def manifest(*segments):
    return {"fps": 60, "segments": segments}


class DirectorClockTests(unittest.TestCase):
    def test_note_timecodes_preserve_exclusive_out_with_zero_or_nonzero_origin(self):
        for origin in (0, 60):
            with self.subTest(origin=origin):
                session = FKBSession("fixture", 60, 216000, review_session_start_frame=origin)
                self.assertEqual(
                    format_session_range(session, FrameRange(origin + 59, origin + 60)),
                    "In 01:00:00:59 | Out 01:00:01:00 (exclusive) | 1 frame",
                )
        session = FKBSession("fixture", 60, 216000, review_session_start_frame=60)
        self.assertEqual(format_session_range(session, FrameRange(130, 150)),
                         "In 01:00:01:10 | Out 01:00:01:30 (exclusive) | 20 frames")

    def test_marker_labels_and_missing_readback(self):
        self.assertEqual(marker_name("  Note\n title  ", "Other text"), "Note title")
        self.assertEqual(marker_name("  ", "\n First\tline \n second line"), "First line second line")
        self.assertEqual(marker_name("", "x" * 100), "x" * 69 + "...")
        self.assertEqual(marker_name("", ""), "DirectorA note")
        self.assertEqual(marker_readback(None), "No marker found for this note.")
        self.assertEqual(marker_readback({}), "No marker found for this note.")
        self.assertIn("Renamed", marker_readback({"name": "Renamed"}))

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
        self.timeline.GetMarkers.return_value = {}
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

    def test_persistent_catalog_with_mock_resolve(self):
        from autoclipper.director_notes import NotesCatalog, review_payload
        from autoclipper.fkb import FKBStore, FootageKnowledgeBase, RecordingPart

        capture = self.review.capture()
        self.marks.update({"in": 20, "out": 59})
        with TemporaryDirectory() as folder:
            store = FKBStore(folder)
            store.initialize(FootageKnowledgeBase(FKBSession("synthetic", 60, 216000, "p1", "t1"), recording_parts=[
                RecordingPart("one", "pov", "Player", None, None, 2, "fixture", 0, "fixture", None,
                              {"start": 0, "end": 120}, {"start": 0, "end": 120}, {"transcriber_track": 2}, True),
            ], source_mapping_digest="fixture"))
            catalog = NotesCatalog(store)
            note = catalog.create(capture, "Observation", playback="paused")
            self.assertEqual(note.frames, {"start": 10, "end": 40})
            self.assertEqual(store.load().recording_parts[0].audio_stream_mapping,
                             {"transcriber_track": 2})
            self.timeline.AddMarker.assert_not_called()
            self.timeline.SetCurrentTimecode.assert_not_called()
            self.timeline.GetMarkers.return_value = {}
            note = catalog.edit_metadata(note.note_id, title="  Reviewed moment  ")
            self.review.marker(review_payload(note), "add")
            self.timeline.AddMarker.assert_called_once_with(
                10, "Blue", "Reviewed moment", "", 30, note.note_id)

    def test_persistent_catalog_rejects_shifted_manifest_capture(self):
        from autoclipper.director_notes import NotesCatalog
        from autoclipper.fkb import FKBStore, FootageKnowledgeBase, Session

        shifted_manifest = manifest(segment(0, 5, 120, "one"))
        shifted = Review(self.api, IDENTITY, shifted_manifest).capture()
        self.assertEqual((shifted.timeline_in, shifted.session_in), (10, 310))
        with TemporaryDirectory() as folder:
            store = FKBStore(folder)
            store.initialize(FootageKnowledgeBase(Session("test", 60, 216000, "p1", "t1"),
                                                  source_mapping_digest="fixture"))
            catalog = NotesCatalog(store)
            with self.assertRaisesRegex(ValueError, "bound session frame origin"):
                catalog.create(shifted, "Shifted marks", playback="paused")
            self.assertEqual(catalog.search(), [])

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
        self.timeline.AddMarker.assert_called_once_with(10, "Blue", "Observation", "", 30,
                                                         note["note_id"])
        marks = {note["note_id"]: {"name": "Renamed in Resolve", "customData": note["note_id"]}}
        self.timeline.GetMarkers.return_value = {10: {**marks[note["note_id"]], "duration": 30}}
        self.timeline.GetMarkerByCustomData.side_effect = marks.get
        self.timeline.DeleteMarkerByCustomData.side_effect = lambda key: bool(
            marks.pop(key) and self.timeline.GetMarkers.return_value.pop(10))
        self.assertEqual(self.review.marker(note, "read")["name"], "Renamed in Resolve")
        self.timeline.GetMarkerByCustomData.assert_called_with(note["note_id"])
        self.assertIsNone(self.review.marker(note, "delete"))
        self.timeline.DeleteMarkerByCustomData.assert_called_once_with(note["note_id"])
        self.assertEqual(marker_readback(self.review.marker(note, "read")), "No marker found for this note.")

    def test_retimed_marker_is_idempotent_stale_and_deletable_by_uuid(self):
        with TemporaryDirectory() as directory:
            store = NoteStore(Path(directory) / "notes.json")
            note = store.add(self.review.capture(), "Same title")
            self.marks.update({"in": 70, "out": 99})
            other = store.add(self.review.capture(), "Same title")
        markers = {}
        self.timeline.GetMarkers.side_effect = lambda: dict(markers)

        def add(frame, color, name, text, duration, uuid):
            markers[frame] = {"name": name, "duration": duration, "customData": uuid}
            return True

        def first_match(uuid):
            return next((frame for frame, marker in markers.items() if marker["customData"] == uuid), None)

        def delete(uuid):
            frame = first_match(uuid)
            return markers.pop(frame, None) is not None

        self.timeline.AddMarker.side_effect = add
        self.timeline.GetMarkerByCustomData.side_effect = lambda uuid: markers.get(first_match(uuid))
        self.timeline.DeleteMarkerByCustomData.side_effect = delete
        self.review.marker(note, "add")
        self.review.marker(note, "add")
        self.timeline.AddMarker.assert_called_once()
        self.review.marker(other, "add")
        self.assertEqual(markers[10]["name"], markers[70]["name"])
        self.assertNotEqual(markers[10]["customData"], markers[70]["customData"])
        self.assertEqual(self.review.marker(other, "read")["customData"], other["note_id"])
        self.timeline.GetCurrentTimecode.return_value = "01:00:01:10"
        self.assertEqual(self.review.jump(other), "Target 01:00:01:10, read back 01:00:01:10")
        self.timeline.SetCurrentTimecode.reset_mock()
        retimed = {**note, "session_in": 20, "session_out_exclusive": 50}
        for operation in ("add", "read"):
            with self.assertRaisesRegex(ValueError, "stale"):
                self.review.marker(retimed, operation)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.review.jump(retimed)
        self.timeline.SetCurrentTimecode.assert_not_called()
        self.assertEqual(set(markers), {10, 70})
        self.review.marker(retimed, "delete")
        self.timeline.DeleteMarkerByCustomData.assert_called_once_with(note["note_id"])
        self.assertIsNone(self.review.marker(retimed, "read"))
        self.timeline.GetCurrentTimecode.return_value = "01:00:00:20"
        self.assertEqual(self.review.jump(retimed), "Target 01:00:00:20, read back 01:00:00:20")
        self.review.marker(retimed, "add")
        self.assertEqual(set(markers), {20, 70})

    def test_duplicate_marker_uuid_refuses_first_match_operations(self):
        with TemporaryDirectory() as directory:
            note = NoteStore(Path(directory) / "notes.json").add(self.review.capture(), "Observation")
        row = {"name": "Observation", "duration": 30, "customData": note["note_id"]}
        self.timeline.GetMarkers.return_value = {10: row, 50: {**row, "name": "Renamed duplicate"}}
        self.timeline.GetMarkerByCustomData.return_value = row
        for operation in ("add", "read", "delete"):
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, "Multiple markers"):
                self.review.marker(note, operation)
        with self.assertRaisesRegex(ValueError, "Multiple markers"):
            self.review.jump(note)
        self.timeline.GetMarkerByCustomData.assert_not_called()
        self.timeline.DeleteMarkerByCustomData.assert_not_called()
        self.timeline.SetCurrentTimecode.assert_not_called()
