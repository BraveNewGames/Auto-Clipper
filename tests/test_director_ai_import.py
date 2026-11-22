import copy
import json
import unittest
from unittest.mock import patch

from autoclipper import director_ai as dx
from autoclipper import rough_cut as rc
from autoclipper.director_notes import NotesCatalog
from director_scratch import make_scratch

GOOD = {
    "schema": dx.SCHEMA, "export_id": "<ID>", "title": "Bridge flip", "summary": "A short edit.",
    "clips": [
        {"id": "C01", "kind": "source", "title": "Flip", "note_ids": ["N01"], "note": "N01",
         "pad_before": 1, "pad_after": 1, "pov": None, "audio_pov": None, "why": "funny"},
        {"id": "C02", "kind": "source", "title": "Other side", "note_ids": ["N02"],
         "start_line": "S0003", "end_line": "S0003", "pov": "bob", "audio_pov": "bob",
         "quotes": [{"line": "S0003", "text": "I saw that from the other side"}]},
        {"id": "P01", "kind": "placeholder", "title": "Title card", "seconds": 3},
        {"id": "C03", "kind": "source", "title": "Recovery", "note_ids": ["N03"], "session_in_frame": 2400,
         "session_out_frame": 2520, "in_tc": "01:00:40:00", "out_tc": "01:00:42:00", "pov": "ada",
         "quotes": [{"line": "S0005", "text": "Nice recovery everyone"}]},
    ],
}


class ImportBase(unittest.TestCase):
    def setUp(self):
        self.s = make_scratch(self)
        self.probe = self.s.probe()
        self.probe.start()
        self.addCleanup(self.probe.stop)
        self.b = self.open()
        self.export_id = dx.export_packet(self.b)["export_id"]

    def open(self):
        return dx.open_scratch(str(self.s.scratch), str(self.s.recordings))

    def answer(self, mutate=None, **top):
        data = copy.deepcopy(GOOD)
        data["export_id"] = top.pop("export_id", self.export_id)
        data.update(top)
        if mutate:
            mutate(data)
        return json.dumps(data)

    def catalog_bytes(self):
        return {p: p.read_bytes() for p in (self.s.store.path, self.s.store.backup) if p.exists()}

    def imp(self, text, **kw):
        kw.setdefault("provider", "Claude")
        kw.setdefault("companion_closed", True)
        return dx.import_response(self.open(), text, **kw)

    def rejected(self, text, *needles, **kw):
        before = self.catalog_bytes()
        result = self.imp(text, **kw)
        self.assertFalse(result["ok"], result)
        joined = "\n".join(result["errors"])
        for needle in needles:
            self.assertIn(needle, joined)
        self.assertEqual(self.catalog_bytes(), before, "catalog must stay byte-identical")
        return result


class GateTests(ImportBase):
    def test_valid_round_trip_saves_plan_with_provenance(self):
        result = self.imp(self.answer())
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["plan_id"], f"ai-{self.export_id}-1")
        plan, = self.s.store.load().edit_plans
        revision = plan.revisions[0]
        self.assertEqual((revision["provenance"], revision["provider"], revision["export_id"], revision["schema"]),
                         ("ai_inference", "Claude", self.export_id, dx.SCHEMA))
        self.assertEqual(revision["prompt_sha256"], dx.load_export_record(self.b, self.export_id)["prompt_sha256"])
        uuids = {n.note_id for n in self.s.notes}
        self.assertEqual({nid for e in plan.entries if e["kind"] == "source" for nid in e["note_ids"]}, uuids)
        self.assertEqual(plan.note_revisions, {n.note_id: 0 for n in self.s.notes})
        self.assertEqual(plan.source_mapping_digest, self.b.digest)
        manifest, summary = dx.compile_plan(self.open(), plan.plan_id)
        self.assertEqual(manifest["mode"], "director_edit_plan")
        self.assertEqual(manifest["edit_plan"]["revision_history"][0]["export_id"], self.export_id)
        rows = [(s["event_id"], s["kind"], s.get("session_start_frame"), s.get("session_end_frame"),
                 [a["pov_id"] for a in s["angles"]]) for s in manifest["segments"]]
        self.assertEqual(rows, [
            ("C01", "media", 1140, 1440, ["ada", "bob"]),
            ("C02", "media", 1320, 1500, ["bob"]),
            ("P01", "placeholder", None, None, []),
            ("C03", "media", 2400, 2520, ["ada"]),
        ])
        self.assertEqual(manifest["segments"][1]["audio_angle"]["pov_id"], "bob")
        # Group Reference Audio stays OFF for bridge builds.
        self.assertFalse(any("reference_audio" in s or "reference_angle" in s for s in manifest["segments"]))
        self.assertEqual((summary["segments"], summary["slates"], summary["blockers"]), (4, 1, 0))
        # Saving again never overwrites: a second plan gets the next id.
        self.assertEqual(self.imp(self.answer())["plan_id"], f"ai-{self.export_id}-2")
        self.assertEqual(len(dx.list_plans(self.open())), 2)

    def test_response_copy_is_optional(self):
        self.imp(self.answer(), save_response=False)
        self.assertFalse((self.s.scratch / ".autoclipper" / "director-ai" / "responses").exists())
        self.imp(self.answer())
        self.assertEqual(len(list((self.s.scratch / ".autoclipper" / "director-ai" / "responses").iterdir())), 1)

    def test_companion_must_be_closed(self):
        with self.assertRaises(dx.DirectorError) as caught:
            self.imp(self.answer(), companion_closed=False)
        self.assertEqual(caught.exception.code, "E_COMPANION")

    def test_schema_and_export_refusals(self):
        self.rejected(json.dumps({"suggestions": [{"title": "x"}]}), "E_SCHEMA", "AI Review")
        self.rejected("not json at all", "E_SCHEMA")
        self.rejected(self.answer(export_id="dx-000000000000"), "E_EXPORT_UNKNOWN")
        self.rejected(self.answer(schema="autoclipper.rough_cut.v1"), "E_SCHEMA")

    def test_mapping_change_since_export(self):
        binding = self.open()
        binding.kb.source_mapping_digest = "0" * 64
        result = dx.import_response(binding, self.answer(), provider="x", companion_closed=True)
        self.assertTrue(result["stale"])
        self.assertIn("E_MAPPING_CHANGED", result["errors"][0])

    def test_note_unknown_stale_deleted(self):
        self.rejected(self.answer(lambda d: d["clips"][0].update(note_ids=["N09"], note="N09")), "E_NOTE_UNKNOWN")
        NotesCatalog(self.s.store).edit_text(self.s.notes[0].note_id, "More thoughts")
        result = self.rejected(self.answer(), "E_NOTE_STALE", "N01", "revision 1")
        self.assertTrue(result["repair_prompt"])
        NotesCatalog(self.s.store).delete(self.s.notes[0].note_id)
        self.rejected(self.answer(), "E_NOTE_DELETED")

    def test_unanchored_clip_outside_cited_windows(self):
        def mutate(data):
            clip = data["clips"][1]
            clip.update(note_ids=["N03"], start_line="S0001", end_line="S0001",
                        quotes=[{"line": "S0001", "text": "Truck staging near the bridge"}], pov=None, audio_pov=None)
        self.rejected(self.answer(mutate), "E_UNANCHORED")

        def no_notes(data):
            data["clips"][0].pop("note_ids")
        self.rejected(self.answer(no_notes), "E_UNANCHORED")

    def test_coverage_and_mic_errors_use_shared_messages(self):
        def mutate(data):
            data["clips"][3].update(pov="bob")  # Bob has no verified video at 40-42 s
        self.rejected(self.answer(mutate), "Clip #4 (C03)", "Bob", "no verified video")

        def mic(data):
            data["clips"][3].update(audio_pov="bob")
        self.rejected(self.answer(mic), "no verified own mic")

    def test_quote_tc_and_shape_errors(self):
        self.rejected(self.answer(lambda d: d["clips"][1]["quotes"][0].update(text="This was never said at all")),
                      "not verbatim")
        self.rejected(self.answer(lambda d: d["clips"][3].update(in_tc="01:00:41:00")), "disagrees")
        self.rejected(self.answer(lambda d: d["clips"][0].update(start_line="S0001")), "exactly one anchor")
        self.rejected(self.answer(lambda d: d["clips"][0].update(pov="nobody")), "pov ids")
        self.rejected(self.answer(lambda d: d["clips"][0].update(suggestion_id="x")), "unsupported keys")
        self.rejected(self.answer(lambda d: d["clips"].append(copy.deepcopy(d["clips"][0]))), "duplicate id")

    def test_one_bad_entry_saves_nothing(self):
        result = self.rejected(self.answer(lambda d: d["clips"][3].update(in_tc="01:00:41:00")), "disagrees")
        self.assertEqual(self.s.store.load().edit_plans, [])
        self.assertIn(self.export_id, result["repair_prompt"])
        self.assertIn(dx.SCHEMA, result["repair_prompt"])
        self.assertIn("Director plan", result["repair_prompt"])
        self.assertTrue(result["report"].startswith("Rejected"))

    def test_out_of_review_window_and_negative_frames(self):
        def mutate(data):
            data["clips"][3].update(session_in_frame=-60, session_out_frame=60, in_tc="00:59:59:00",
                                    out_tc="01:00:01:00")
        self.rejected(self.answer(mutate), "outside the session window")

    def test_fkb_changed_during_validation(self):
        original = rc.gate_plan

        def racing(*args, **kwargs):
            outcome = original(*args, **kwargs)
            NotesCatalog(self.s.store).edit_metadata(self.s.notes[1].note_id, title="Changed by companion")
            return outcome

        before_plans = None
        with patch("autoclipper.rough_cut.gate_plan", racing):
            with self.assertRaises(dx.DirectorError) as caught:
                self.imp(self.answer())
        self.assertEqual(caught.exception.code, "E_FKB_CHANGED")
        self.assertEqual(self.s.store.load().edit_plans, before_plans or [])
        self.assertEqual(self.s.store.load().director_notes[1].title, "Changed by companion")

    def test_gated_plan_cannot_be_forged(self):
        with self.assertRaises(TypeError):
            dx.GatedPlan(object(), plan={}, snapshot={}, export_id="x", response_sha256="x", provider="x", title="x")
        with self.assertRaises(TypeError):
            dx.GatedPlan()
        with self.assertRaises(TypeError):
            dx._save_validated(self.b, {"plan": {"plan_id": "x", "entries": []}}, "", False)
        self.assertEqual(self.s.store.load().edit_plans, [])

    def test_ai_review_import_is_unaffected_by_bridge_answers(self):
        from autoclipper import reviews
        self.assertFalse(hasattr(reviews, "director_ai"))
        # A bridge answer has no "suggestions": the shared extractor does not treat it as an AI Review.
        with self.assertRaises(ValueError):
            rc.extract_json(self.answer(), key="suggestions")


class RemediationTests(ImportBase):
    def test_unsaved_response_copy_warning_reaches_the_result(self):
        real = dx._write_new

        def failing(binding, names, filename, text):
            if names == ("responses",):
                raise OSError("disk full")
            return real(binding, names, filename, text)

        with patch("autoclipper.director_ai._write_new", failing):
            result = self.imp(self.answer())
        self.assertTrue(result["ok"])
        self.assertIn("W_RESPONSE_NOT_SAVED", [w.get("code") for w in result["warnings"]])
        self.assertEqual(len(self.s.store.load().edit_plans), 1)

    def test_full_transcript_prompt_states_the_real_anchoring_rule(self):
        full = dx.export_packet(self.b, full_transcript=True)["prompt"]
        windowed = dx.export_packet(self.b)["prompt"]
        self.assertIn("must overlap the range of a note it cites", full)
        self.assertNotIn("transcript context shown for a note", full.split("OUTPUT")[0])
        self.assertIn("transcript context shown for a note", windowed)
        # The validator stays strict: a full-transcript export still rejects a clip far from its cited note.
        full_id = dx.export_packet(self.b, full_transcript=True)["export_id"]

        def mutate(data):
            clip = data["clips"][1]
            clip.update(note_ids=["N03"], start_line="S0001", end_line="S0001",
                        quotes=[{"line": "S0001", "text": "Truck staging near the bridge"}], pov=None, audio_pov=None)
        self.rejected(self.answer(mutate, export_id=full_id), "E_UNANCHORED")

    def test_clip_on_a_part_the_compile_excluded_is_rejected(self):
        from autoclipper import resolve_manifest as rm
        real = rm.compile_fkb_edit_plan

        def excluding(*args, **kwargs):
            manifest = real(*args, **kwargs)
            manifest["blockers"].append({"code": "mic_unverified", "part_id": "b1", "pov_name": "Bob",
                                         "message": "synthetic exclusion"})
            return manifest

        before = self.catalog_bytes()
        with patch("autoclipper.resolve_manifest.compile_fkb_edit_plan", excluding):
            result = self.imp(self.answer())
        self.assertFalse(result["ok"])
        self.assertIn("excluded recording part", "\n".join(result["errors"]))
        self.assertEqual(self.catalog_bytes(), before)

    def test_changed_note_frames_with_same_revision_are_stale(self):
        def move(data):
            note = next(n for n in data.director_notes if n.note_id == self.s.notes[0].note_id)
            note.frames = {"start": note.frames["start"] + 10, "end": note.frames["end"]}
        self.s.store.update(move)
        self.assertEqual(self.s.store.load().director_notes[0].revision, 0)
        self.rejected(self.answer(), "E_NOTE_STALE")

    def test_plan_ids_are_validated_before_use_in_folder_names(self):
        binding = self.open()
        for bad in ("../evil", "a/b", "a\\b", "x\n", "", ".hidden", "a" * 101, 5, None):
            with self.subTest(plan=bad):
                with self.assertRaises(dx.DirectorError):
                    dx.write_edit_files(binding, bad, {})
                with self.assertRaises(dx.DirectorError):
                    dx.compile_plan(binding, bad)
        self.assertEqual([p.name for p in self.s.scratch.iterdir() if p.name.startswith("edit-")], [])

    def test_export_id_with_trailing_newline_is_refused(self):
        with self.assertRaises(dx.DirectorError) as caught:
            dx.load_export_record(self.b, self.export_id + "\n")
        self.assertEqual(caught.exception.code, "E_EXPORT_UNKNOWN")


class FilesTests(ImportBase):
    def test_edit_files_are_written_into_a_new_folder(self):
        plan_id = self.imp(self.answer())["plan_id"]
        binding = self.open()
        manifest, _ = dx.compile_plan(binding, plan_id)
        folder = dx.write_edit_files(binding, plan_id, manifest, stamp="20261121-000000")
        self.assertEqual(folder.name, f"edit-{plan_id}-20261121-000000")
        self.assertTrue((folder / "manifest.json").is_file() and (folder / "report.md").is_file())
        again = dx.write_edit_files(binding, plan_id, manifest, stamp="20261121-000000")
        self.assertEqual(again.name, folder.name + "-2")  # a folder is never reused
        self.assertRegex(dx.new_project_name(), r"^AutoClipper DirectorA Test \d{8}-\d{6}$")

    def test_unknown_plan(self):
        with self.assertRaises(dx.DirectorError):
            dx.compile_plan(self.open(), "nope")


if __name__ == "__main__":
    unittest.main()
