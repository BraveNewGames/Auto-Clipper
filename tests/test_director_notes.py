from dataclasses import replace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from autoclipper.director_notes import NotesCatalog, review_payload
from autoclipper.director_poc.core import Capture
from autoclipper.fkb import FKBStore, FootageKnowledgeBase, Session, TranscriptSpan


class NotesTests(unittest.TestCase):
    def setUp(self):
        folder = self.temp()
        self.store = FKBStore(folder)
        self.store.initialize(FootageKnowledgeBase(
            Session("test", 24, 86400, "project", "timeline"),
            transcript_spans=[TranscriptSpan(
                "cue", "Move the truck", "Ada", "high", False, {"start": 16, "end": 20}, [])]))
        self.capture = Capture("Project", "project", "Timeline", "timeline", 10, 20,
                               "01:00:00:10", "01:00:00:20", 10, 20, 24,
                               "01:00:00:22", "now")
        self.catalog = NotesCatalog(self.store)

    def temp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return folder.name

    def create(self, text):
        return self.catalog.create(self.capture, text, playback="paused")

    def test_same_range_independence_and_revision_recovery(self):
        a = self.create(" Preserve this ")
        b = self.create("Other")
        self.assertNotEqual(a.note_id, b.note_id)
        self.assertEqual(len(self.catalog.search("truck")), 2)
        edited = self.catalog.edit_text(a.note_id, "Append")
        self.assertEqual(edited.text, " Preserve this \nAppend")
        self.assertEqual(edited.frames, a.frames)
        self.catalog.edit_text(a.note_id, "Replace", append=False)
        self.catalog.restore_revision(a.note_id, 1)
        self.catalog.delete(a.note_id)
        reopened = NotesCatalog(self.store)
        self.assertEqual([row.note_id for row in reopened.search()], [b.note_id])
        recovered = reopened.recover(a.note_id)
        self.assertEqual(recovered.text, edited.text)
        self.assertEqual(recovered.original_text, a.text)

    def test_preview_then_apply_and_restore_range(self):
        note = self.create("Action")
        retake = replace(self.capture, session_in=12, timeline_in=12, in_timecode="01:00:00:12")
        pending = self.catalog.preview_range(note.note_id, retake, playback="paused")
        self.assertEqual((pending.old.start, pending.new.start), (10, 12))
        self.assertEqual(self.catalog.search()[0].frames, note.frames)
        with self.assertRaisesRegex(ValueError, "disagrees with the preview"):
            self.catalog.apply_range(replace(pending, new=pending.old))
        self.catalog.apply_range(pending)
        self.assertEqual(review_payload(self.catalog.search()[0])["session_in"], 12)
        with self.assertRaises(ValueError):
            self.catalog.apply_range(pending)
        self.assertEqual(self.catalog.restore_revision(note.note_id, 0).frames, note.frames)

    def test_capture_guards(self):
        for playback, delayed in ((None, False), ("playing", False), ("paused", True)):
            with self.assertRaises(ValueError):
                self.catalog.create(self.capture, "Note", playback=playback, delayed=delayed)
        with self.assertRaises(ValueError):
            self.catalog.create(replace(self.capture, timeline_id="wrong"), "Note", playback="paused")
        self.assertFalse(self.catalog.search())
        self.assertFalse(self.create("Good").capture["playback_api_verified"])

    def test_rejects_consistently_shifted_session_ranges(self):
        note = self.create("Original")
        before = self.store.path.read_bytes()
        for shift in (-5, 5, 600):
            shifted = replace(self.capture, session_in=self.capture.session_in + shift,
                              session_out_exclusive=self.capture.session_out_exclusive + shift)
            with self.subTest(shift=shift):
                with self.assertRaisesRegex(ValueError, "bound session frame origin"):
                    self.catalog.create(shifted, "Shifted", playback="paused")
                with self.assertRaisesRegex(ValueError, "bound session frame origin"):
                    self.catalog.preview_range(note.note_id, shifted, playback="paused")
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_generation_failure_and_user_customization(self):
        provider = Mock(name="fixture-provider")
        provider.name = "fixture"
        provider.generate.side_effect = RuntimeError("offline")
        catalog = NotesCatalog(self.store, provider)
        note = self.create("Observation")
        with self.assertRaises(RuntimeError):
            catalog.refresh_metadata(note.note_id)
        self.assertEqual(catalog.search()[0].text, "Observation")
        catalog.edit_metadata(note.note_id, title="Handwritten")
        catalog.edit_text(note.note_id, "A different description", append=False)

        def generate(_):
            catalog.edit_metadata(note.note_id, tags=["custom"])
            return "Generated", ["generated"]

        provider.generate.side_effect = generate
        result = catalog.refresh_metadata(note.note_id)
        self.assertEqual(result.title, "Handwritten")
        self.assertEqual(result.tags, ["custom"])
        self.assertEqual(catalog.search("custom")[0].note_id, note.note_id)

    def test_drops_metadata_generated_from_superseded_text(self):
        note = self.create("First")
        provider = Mock()
        provider.name = "fixture"

        def generate(_):
            self.catalog.edit_text(note.note_id, "Second", append=False)
            return "Old title", ["old"]

        provider.generate.side_effect = generate
        result = NotesCatalog(self.store, provider).refresh_metadata(note.note_id)
        self.assertEqual((result.text, result.title), ("Second", ""))
