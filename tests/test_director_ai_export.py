import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

from autoclipper import director_ai as dx
from director_scratch import make_scratch


class OpenScratchTests(unittest.TestCase):
    def setUp(self):
        self.s = make_scratch(self)

    def test_opens_a_verified_scratch(self):
        b = dx.open_scratch(str(self.s.scratch), str(self.s.recordings))
        self.assertEqual((b.fps, b.summary["notes"], b.review_frames), (60, 3, (0, 3600)))
        self.assertEqual(b.origin, 216000)

    def refuse(self, code, scratch=None, root=None):
        with self.assertRaises(dx.DirectorError) as caught:
            dx.open_scratch(str(scratch or self.s.scratch), str(root or self.s.recordings))
        self.assertEqual(caught.exception.code, code)

    def test_refusals(self):
        self.refuse("E_SCRATCH_PATH", scratch=self.s.temp / "missing")
        self.refuse("E_SCRATCH_PATH", scratch=Path(os.environ.get("SystemRoot", "/")))
        self.refuse("E_SCRATCH_PATH", scratch=self.s.recordings)  # wrong prefix, still under temp
        (self.s.scratch / "build.json").rename(self.s.scratch / "build.bak")
        self.refuse("E_SCRATCH")
        (self.s.scratch / "build.bak").rename(self.s.scratch / "build.json")
        with self.assertRaises(dx.DirectorError) as caught:
            dx.open_scratch(str(self.s.scratch), None)
        self.assertEqual(caught.exception.code, "E_SOURCE")

    def test_recording_root_and_srt_must_match_prepare(self):
        other = self.s.temp / "other"
        other.mkdir()
        self.refuse("E_SOURCE", root=other)
        (self.s.recordings / "a1.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nAda: Changed.\n", encoding="utf-8")
        self.refuse("E_SOURCE")

    def test_stale_or_legacy_catalog_is_refused(self):
        def mark_stale(data):
            data.mapping_stale = True
        self.s.store.update(mark_stale)
        self.refuse("E_MAPPING_CHANGED")

    def test_legacy_manifest_without_pins_is_refused(self):
        manifest = json.loads((self.s.scratch / "manifest.json").read_text(encoding="utf-8"))
        del manifest["pinned_inputs"]["session_source"]
        # Rewriting the manifest would break its receipt; refusal must still come from the binding rules.
        (self.s.scratch / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(dx.DirectorError):
            dx.open_scratch(str(self.s.scratch), str(self.s.recordings))

    @unittest.skipUnless(sys.platform == "win32", "Windows junction test")
    def test_junctions_are_refused(self):
        junction = self.s.temp / "autoclipper-director-a-link"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(self.s.scratch)],
                                capture_output=True, text=True)
        if result.returncode:
            self.skipTest("cannot create junction")
        try:
            self.refuse("E_SCRATCH_LINK", scratch=junction)
        finally:
            os.rmdir(junction)
        state = self.s.scratch / ".autoclipper"
        moved = self.s.scratch / "real-state"
        state.rename(moved)
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(state), str(moved)], capture_output=True, text=True)
        if result.returncode:
            state_back = moved.rename(state)
            self.skipTest("cannot create junction")
        try:
            self.refuse("E_SCRATCH_LINK")
        finally:
            os.rmdir(state)
            moved.rename(state)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.s = make_scratch(self)
        self.b = dx.open_scratch(str(self.s.scratch), str(self.s.recordings))

    def export(self, **kw):
        return dx.export_packet(self.b, **kw)

    def test_prompt_content_hash_and_record(self):
        out = self.export()
        prompt = out["prompt"]
        self.assertRegex(out["export_id"], r"^dx-[0-9a-f]{12}$")
        self.assertEqual(out["counts"]["notes"], 3)
        # No file paths, part ids or source resolution leak into the prompt.
        for forbidden in (str(self.s.temp), "a1.mp4", "a1.srt", "b1", "recordings", "source_path", "mp4"):
            self.assertNotIn(forbidden, prompt)
        self.assertIn("Team?", prompt)
        self.assertIn("N01 rev 0 | 01:00:20:00-01:00:23:00 | frames [1200, 1380)", prompt)
        self.assertIn("pov id \"bob\" (Bob): VIDEO and own MIC verified during 01:00:10:00-01:00:40:00", prompt)
        self.assertIn("GAPS (cannot be used): 01:00:00:00-01:00:10:00, 01:00:40:00-01:01:00:00", prompt)
        # The hash excludes only the line containing the ID and is reproducible.
        body = "".join(line for line in prompt.splitlines(keepends=True) if "EXPORT ID:" not in line)
        import hashlib
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(), out["prompt_sha256"])
        self.assertEqual(self.export()["export_id"], out["export_id"])
        record = json.loads((self.s.scratch / ".autoclipper" / "director-ai" / "exports" /
                             f"{out['export_id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(record["notes"]), ["N01", "N02", "N03"])
        self.assertEqual(record["notes"]["N01"]["revision"], 0)
        raw = json.dumps(record)
        for text in ("Truck staging", "flip that truck", "Truck flip near the bridge"):
            self.assertNotIn(text, raw)  # the record holds no note or transcript text
        saved = self.s.scratch / ".autoclipper" / "director-ai" / "prompts" / f"{out['export_id']}.txt"
        self.assertEqual(saved.read_text(encoding="utf-8"), prompt)

    def test_golden_prompt(self):
        golden = Path(__file__).parent / "golden" / "director_bridge_prompt.txt"
        masked = re.sub(r"^(Source mapping digest|EXPORT ID): .*$", lambda m: m.group(1) + ": <masked>",
                        self.export()["prompt"], flags=re.M)
        if os.environ.get("AUTOCLIPPER_UPDATE_GOLDEN"):
            golden.write_bytes(masked.encode("utf-8"))
        self.assertEqual(masked.splitlines(), golden.read_bytes().decode("utf-8").splitlines())

    def test_save_prompt_false_writes_record_only(self):
        out = self.export(save_prompt=False)
        base = self.s.scratch / ".autoclipper" / "director-ai"
        self.assertTrue((base / "exports" / f"{out['export_id']}.json").is_file())
        self.assertFalse((base / "prompts").exists())

    def test_selection_and_deleted_notes(self):
        keep = [self.s.notes[1].note_id]
        out = self.export(note_ids=keep)
        self.assertEqual(out["counts"]["notes"], 1)
        self.assertNotIn("flip that truck", out["prompt"].split("TRANSCRIPT", 1)[1].split("WINDOW 1")[0])
        from autoclipper.director_notes import NotesCatalog
        NotesCatalog(self.s.store).delete(self.s.notes[0].note_id)
        b = dx.open_scratch(str(self.s.scratch), str(self.s.recordings))
        self.assertEqual(dx.export_packet(b)["counts"]["notes"], 2)
        with self.assertRaises(dx.DirectorError) as caught:
            dx.export_packet(b, note_ids=[self.s.notes[0].note_id])
        self.assertEqual(caught.exception.code, "E_NOTE_UNKNOWN")
        with self.assertRaises(dx.DirectorError) as caught:
            dx.export_packet(b, query="no such words anywhere")
        self.assertEqual(caught.exception.code, "E_NO_NOTES")

    def test_windows_merge_and_shrink_to_budget(self):
        out = self.export()
        # At +/-20 s all three notes' windows overlap and merge into one.
        self.assertEqual(out["counts"]["windows"], 1)
        self.assertEqual(out["window_s"], 20)
        original = dx.DEFAULT_BUDGET_CHARS
        try:
            dx.DEFAULT_BUDGET_CHARS = len(out["prompt"]) - 1
            smaller = self.export()
        finally:
            dx.DEFAULT_BUDGET_CHARS = original
        self.assertLess(smaller["window_s"], 20)
        try:
            dx.DEFAULT_BUDGET_CHARS = 1  # nothing fits: the 5 s window is used while under the hard cap
            smallest = self.export()
        finally:
            dx.DEFAULT_BUDGET_CHARS = original
        self.assertEqual((smallest["window_s"], smallest["counts"]["windows"]), (5, 2))

    def test_refuses_above_hard_cap(self):
        original = (dx.DEFAULT_BUDGET_CHARS, dx.HARD_CAP_CHARS)
        try:
            dx.DEFAULT_BUDGET_CHARS = dx.HARD_CAP_CHARS = 100
            with self.assertRaises(dx.DirectorError) as caught:
                self.export()
        finally:
            dx.DEFAULT_BUDGET_CHARS, dx.HARD_CAP_CHARS = original
        self.assertEqual(caught.exception.code, "E_TOO_LARGE")
        self.assertIn("fewer notes", str(caught.exception))

    def test_full_transcript_is_larger(self):
        self.assertGreater(self.export(full_transcript=True)["counts"]["spans"], 0)
        self.assertIn("full transcript", self.export(full_transcript=True)["prompt"])

    def test_writes_refuse_links(self):
        if sys.platform != "win32":
            self.skipTest("junction test is Windows only")
        base = self.s.scratch / ".autoclipper"
        target = self.s.temp / "elsewhere"
        target.mkdir()
        link = base / "director-ai"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
        if result.returncode:
            self.skipTest("cannot create junction")
        try:
            with self.assertRaises(ValueError):
                self.export()
            self.assertEqual(list(target.iterdir()), [])
        finally:
            os.rmdir(link)

    def test_coverage_summary_reports_gaps(self):
        rows = {row["pov_id"]: row for row in dx.coverage_summary(self.b)}
        self.assertEqual(rows["bob"]["video"], [[600, 2400]])
        self.assertEqual(rows["bob"]["gaps"], [[0, 600], [2400, 3600]])
        self.assertEqual(rows["ada"]["gaps"], [])

    def test_notes_listing(self):
        rows = dx.list_notes(self.b)
        self.assertEqual([r["frames"]["start"] for r in rows], [1200, 1300, 2400])
        self.assertEqual(rows[0]["tc_in"], "01:00:20:00")


if __name__ == "__main__":
    unittest.main()
