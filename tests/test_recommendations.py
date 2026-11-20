import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from autoclipper.fkb import DirectorNote, FKBStore, FrameRange, Provenance, ingest_session
from autoclipper.models import Part, POV, Session
from autoclipper.recommendations import (
    Proposal, RecommendationProvider, available_povs, import_recommendations,
    recommend, refresh_recommendations, search_evidence, set_candidate_status,
)
from autoclipper.reviews import import_review, session_packet_lines


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        folder = self.enter_temp_folder()
        (folder / "one.mp4").touch()
        (folder / "two.mp4").touch()
        (folder / "one.srt").write_text(
            "1\n00:00:01,100 --> 00:00:02,100\nAda: The teamwork disaster begins.\n\n"
            "2\n00:00:03,200 --> 00:00:04,800\nAda: Drop the piano!\n\n"
            "3\n00:00:08,000 --> 00:00:09,000\nAda: Quiet hallway.\n",
            encoding="utf-8")
        self.source = Session(session_id="synthetic-rec", folder=str(folder), povs=[
            POV("ada", "Ada", [
                Part("one", video_path="one.mp4", srt_path="one.srt", duration=4),
                Part("two", index=2, video_path="two.mp4", duration=6, offset=6),
            ])
        ])
        self.db = ingest_session(self.source, fps=24, timeline_start_frame=3600)
        self.span, self.piano, self.hallway = self.db.transcript_spans

    def enter_temp_folder(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def review(self, **changes):
        return {"reviewer": "Claude", "suggestions": [{
            "id": "C-01", "title": "Teamwork disaster", "kind": "clip_now",
            "start": "00:00:01.100", "end": "00:00:02.100",
            **changes,
        }]}

    def note(self):
        return DirectorNote("note", {"start": 28, "end": 48}, "Funny teamwork disaster",
                            "Funny teamwork disaster", "now", "now", {}, tags=["funniest"])

    def test_packet_quote_resolves_to_one_member_on_session_clock(self):
        packet = session_packet_lines(self.source)
        self.assertEqual(len(packet[0].members), 2)
        raw = self.review(quotes=[{"line": packet[0].packet_id, "text": self.span.text}])
        before = self.source.to_dict()
        result, = import_recommendations(self.db, self.source, json.dumps(raw), run_id="r1",
                                         model_version="test")
        self.assertEqual(before, self.source.to_dict())
        self.assertEqual(result.frames, {"start": 26, "end": 51})
        self.assertEqual(result.evidence_ids, [self.span.span_id])
        self.assertEqual((result.model, result.run_id, result.model_version),
                         ("Claude", "r1", "test"))
        self.assertEqual(result.provenance, Provenance.AI_INFERENCE)
        self.assertFalse(result.verified)
        self.assertEqual(available_povs(self.db, FrameRange(**result.frames))[0][0], "ada")

    def test_unsupported_and_untimed_review_rows_have_no_range(self):
        for row in (
            self.review(start=70, end=80),
            self.review(kind="needs_filming"),
            self.review(quotes=[{"text": "Drop the piano!"}]),
            self.review(quotes=[{"line": "L0001"}]),
            self.review(quotes=[{"line": "L0001", "text": "The teamwork disaster never begins."}]),
        ):
            candidate, = import_recommendations(self.db, self.source, row, run_id="missing")
            self.assertEqual(candidate.frames, {})
            self.assertEqual(candidate.evidence_ids, [])
            self.assertIn("No supported session range", candidate.uncertainty)

    def test_plain_quote_and_partial_coverage(self):
        raw = self.review(end=8, quotes=[{"text": self.piano.text}])
        candidate, = import_recommendations(self.db, self.source, raw, run_id="gap")
        self.assertEqual(candidate.evidence_ids, [self.piano.span_id])
        self.assertIn("coverage is incomplete", candidate.uncertainty)
        self.assertEqual([row[2] for row in available_povs(self.db, FrameRange(**candidate.frames))],
                         [FrameRange(26, 96), FrameRange(144, 192)])
        self.db.mapping_stale = True
        candidate, = import_recommendations(self.db, self.source, raw, run_id="gap")
        self.assertIn("mapping is stale", candidate.uncertainty)
        self.assertEqual(available_povs(self.db, FrameRange(**candidate.frames)), [])

    def test_repeated_quotes_bind_to_timed_occurrences_and_span_ids(self):
        path = Path(self.source.folder) / "one.srt"
        path.write_text(path.read_text(encoding="utf-8").replace("Drop the piano!", self.span.text),
                        encoding="utf-8")
        self.db = ingest_session(self.source, fps=24)
        first, second, _ = self.db.transcript_spans
        packet = session_packet_lines(self.source)
        self.assertEqual(len(packet[0].members), 2)
        raw = self.review(end=5, quotes=[
            {"line": packet[0].packet_id, "text": first.text, "time": "00:00:03.200"},
            {"span_id": first.span_id, "text": first.text},
        ])
        candidate, = import_recommendations(self.db, self.source, raw, run_id="repeated")
        self.assertEqual(candidate.evidence_ids, [second.span_id, first.span_id])
        for index, hint in enumerate(({}, {"line": packet[0].packet_id}, {"time": 2.125})):
            ambiguous = self.review(end=5, quotes=[{"text": first.text, **hint}])
            candidate, = import_recommendations(self.db, self.source, ambiguous,
                                                 run_id=f"ambiguous-{index}")
            self.assertEqual(candidate.evidence_ids, [])
            self.assertEqual(candidate.frames, {})
            self.assertIn("ambiguous", candidate.uncertainty)

        legacy = import_review(self.source, self.review(end=5, quotes=[
            {"line": packet[0].packet_id, "text": first.text},
        ]), None)
        self.assertIsNotNone(legacy[0].quotes[0]["time"])
        candidate, = import_recommendations(self.db, self.source, run_id="legacy-repeated")
        self.assertEqual(candidate.evidence_ids, [])
        self.assertIn("ambiguous", candidate.uncertainty)

    def test_reused_packet_id_does_not_override_quote_timestamp(self):
        path = Path(self.source.folder) / "one.srt"
        path.write_text(path.read_text(encoding="utf-8").replace("Quiet hallway.", self.span.text),
                        encoding="utf-8")
        self.db = ingest_session(self.source, fps=24)
        first, _, last = self.db.transcript_spans
        raw = self.review(end=9, quotes=[
            {"line": "L0001", "text": first.text, "time": 8},
            {"text": first.text, "time": 1.1},
        ])
        candidate, = import_recommendations(self.db, self.source, raw, run_id="stale-id")
        self.assertEqual(candidate.evidence_ids, [last.span_id, first.span_id])
        outside = self.review(quotes=[{"line": "L0001", "text": first.text, "time": 8}])
        candidate, = import_recommendations(self.db, self.source, outside, run_id="outside")
        self.assertEqual(candidate.evidence_ids, [])
        self.assertEqual(candidate.frames, {})

    def test_legacy_import_and_note_revisions_roundtrip(self):
        import_review(self.source, self.review(), None)
        store = FKBStore(self.source.folder)
        store.initialize(self.db)
        candidate, = store.update(lambda db: import_recommendations(db, self.source, run_id="saved"))
        self.assertIn("keywords only", candidate.uncertainty)
        store.update(lambda db: set_candidate_status(db, candidate.candidate_id, "keep"))

        def add_note(db):
            db.director_notes.append(self.note())
            return refresh_recommendations(db)

        enriched, = store.update(add_note)
        self.assertEqual(enriched.evidence_ids, [self.span.span_id, "note"])
        self.assertEqual(enriched.revisions[-1]["evidence_ids"], [self.span.span_id])
        self.assertEqual(enriched.status, "keep")
        self.assertEqual(store.update(refresh_recommendations), [enriched])

        def retime_note(db):
            db.director_notes[0].frames = {"start": 192, "end": 216}
            return refresh_recommendations(db)

        retimed, = store.update(retime_note)
        self.assertEqual(retimed.evidence_ids, [self.span.span_id])
        self.assertEqual(retimed.revisions[-1]["evidence_ids"], enriched.evidence_ids)
        self.assertEqual(retimed.status, "keep")
        self.assertEqual(len(store.load().candidate_moments), 1)

    def test_search_uses_original_note_text_metadata_and_filters(self):
        query = "funniest teamwork disasters"
        self.assertEqual([hit.evidence_id for hit in search_evidence(self.db, query)],
                         [self.span.span_id])
        self.db.director_notes.append(self.note())
        self.assertEqual(search_evidence(self.db, query)[0].provenance, Provenance.USER)
        self.assertEqual([hit.evidence_id for hit in search_evidence(
            self.db, "funniest", speaker="ADA", frames=FrameRange(26, 52))],
            [self.span.span_id])
        self.assertEqual(search_evidence(self.db, query, frames=FrameRange(51, 60)), [])
        self.assertEqual(search_evidence(self.db, query, speaker="Nobody"), [])
        self.db.director_notes[0].deleted = True
        self.assertEqual(search_evidence(self.db, "funniest"), [])

    def test_offline_provider_and_missing_query(self):
        first = recommend(self.db, "teamwork disasters", run_id="offline")
        self.assertEqual(first, recommend(self.db, "teamwork disasters", run_id="offline"))
        self.assertEqual(first[0].evidence_ids, [self.span.span_id])
        missing, = recommend(self.db, "load stolen loot", run_id="missing")
        self.assertEqual(missing.frames, {})
        self.assertIn("No supported session moment", missing.uncertainty)

    def test_search_rerun_keeps_candidate_decision_and_note_context(self):
        query = "teamwork hallway"
        before = recommend(self.db, query, run_id="rerank")
        hallway = next(candidate for candidate in before
                       if candidate.evidence_ids == [self.hallway.span_id])
        set_candidate_status(self.db, hallway.candidate_id, "reject")
        note = self.note()
        note.frames = self.hallway.frames
        self.db.director_notes.append(note)
        after = recommend(self.db, query, run_id="rerank")
        self.assertEqual(after[0].evidence_ids, [note.note_id])
        reranked = next(item for item in after if item.candidate_id == hallway.candidate_id)
        self.assertEqual(reranked.status, "reject")
        self.assertEqual(reranked.evidence_ids, [self.hallway.span_id, note.note_id])
        self.assertEqual(reranked.revisions[-1]["evidence_ids"], [self.hallway.span_id])

    def test_mock_provider_cannot_select_outside_query_evidence(self):
        provider = Mock(spec=RecommendationProvider)
        provider.model, provider.version = "mock", "test"
        provider.propose.return_value = [
            Proposal("An inference", self.span.span_id),
            Proposal("Wrong range", self.hallway.span_id),
            Proposal("Wrong session", "other-session-span"),
        ]
        valid, *invalid = recommend(self.db, "teamwork", run_id="mock", provider=provider)
        self.assertEqual(valid.frames, self.span.frames)
        self.assertFalse(valid.verified)
        self.assertEqual((valid.model, valid.model_version), ("mock", "test"))
        self.assertTrue(all(not candidate.frames and not candidate.evidence_ids
                            for candidate in invalid))
        query, hits = provider.propose.call_args.args
        self.assertEqual(query, "teamwork")
        self.assertEqual([hit.evidence_id for hit in hits], [self.span.span_id])

    def test_note_only_proposal_loses_range_after_retime(self):
        self.db.director_notes.append(self.note())
        note_candidate = recommend(self.db, "funniest", run_id="notes")[0]
        self.assertEqual(note_candidate.evidence_ids, ["note"])
        self.assertFalse(note_candidate.verified)
        self.db.director_notes[0].frames = {"start": 30, "end": 49}
        results = {item.candidate_id: item for item in refresh_recommendations(self.db)}
        retimed = results[note_candidate.candidate_id]
        self.assertEqual(retimed.frames, {})
        self.assertEqual(retimed.revisions[-1]["frames"], note_candidate.frames)

    def test_mismatched_session_is_rejected_before_ingest(self):
        self.source.session_id = "some-other-session"
        with self.assertRaisesRegex(ValueError, "same session"):
            import_recommendations(self.db, self.source, self.review(), run_id="wrong")


if __name__ == "__main__":
    unittest.main()
