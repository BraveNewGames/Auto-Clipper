"""Recon modules against CORE's generated sample session (samples/make_sample.py)."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from autoclipper.autoalign import suggest_offsets, apply_suggested_offsets
from autoclipper.reconcile import build_session_transcript, load_cues, similarity
from autoclipper.reviews import build_review_packet, import_review, parse_review
from autoclipper.models import parse_ts
from autoclipper.scan import scan_folder

ROOT = Path(__file__).resolve().parents[1]


class SampleSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="recon_sample_")
        subprocess.run([sys.executable, "-m", "samples.make_sample", cls.tmp, "--no-video"],
                       cwd=ROOT, check=True, capture_output=True)
        found = list(Path(cls.tmp).rglob("ground_truth.json"))
        assert found, "sample generator produced no ground_truth.json"
        cls.folder = found[0].parent
        cls.truth = json.loads(found[0].read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def fresh(self):
        s = scan_folder(self.folder)
        s.event_prefix = self.truth["event_prefix"]
        return s

    def aligned(self):
        s = self.fresh()
        for _, pt in s.all_parts():
            pt.offset = float(self.truth["offsets"][pt.part_id])
            pt.offset_source = "manual"
            pt.duration = float(self.truth["durations"][pt.part_id])
            pt.duration_source = "manual"
        return s

    # ---------------------------------------------------------------- alignment
    def test_auto_alignment_recovers_ground_truth(self):
        s = self.fresh()
        self.assertTrue(all(pt.offset == 0.0 for _, pt in s.all_parts()))
        sugg = suggest_offsets(s)
        self.assertEqual({x["part_id"] for x in sugg}, set(self.truth["offsets"]))
        for x in sugg:
            true = self.truth["offsets"][x["part_id"]]
            self.assertIsNotNone(x["offset"], x)
            self.assertLess(abs(x["offset"] - true), 0.5, x)
            self.assertGreaterEqual(x["confidence"], 0.5, x)
        apply_suggested_offsets(s, sugg)
        for _, pt in s.all_parts():
            self.assertAlmostEqual(pt.offset, self.truth["offsets"][pt.part_id], delta=0.5)

    def test_alignment_with_manual_anchor_keeps_it(self):
        s = self.fresh()
        cindy1 = s.part_by_id("cindy-p1")
        cindy1.offset, cindy1.offset_source = 0.0, "manual"
        sugg = {x["part_id"]: x for x in suggest_offsets(s, reference_part_id="cindy-p1")}
        self.assertEqual(sugg["cindy-p1"]["offset"], 0.0)
        self.assertAlmostEqual(sugg["andres-p2"]["offset"], 330.0, delta=0.5)

    # ---------------------------------------------------------------- reconciliation
    def check_transcript(self, s):
        lines = build_session_transcript(s)
        correct = 0
        for t in self.truth["lines"]:
            cands = [l for l in lines if abs(l.session_start - t["time"]) <= 1.0
                     and similarity(l.text, t["text"]) > 0.5]
            self.assertLessEqual(len(cands), 1, ("duplicate lines for one utterance", t,
                                                 [(l.speaker, l.text) for l in cands]))
            if cands and cands[0].speaker == t["speaker"]:
                correct += 1
        self.assertGreaterEqual(correct / len(self.truth["lines"]), 0.9)
        povname = {pt.part_id: pov.name for pov, pt in s.all_parts()}
        for l in lines:
            if l.speaker is None:
                continue
            # attribution only ever comes from that speaker's own named mic, never file ownership
            named = [src for src in l.sources if src["channel"] == "named"]
            self.assertTrue(named, l)
            self.assertTrue(all(povname[src["part_id"]] == l.speaker for src in named), l)
            for src in l.sources:
                if src["channel"] == "team":
                    self.assertNotEqual(povname[src["part_id"]], l.speaker, l)
        return lines

    def test_reconciliation_with_true_offsets(self):
        lines = self.check_transcript(self.aligned())
        slide = [l for l in lines if "zombie down the slide" in l.text.lower()]
        self.assertEqual(len(slide), 1)
        self.assertEqual(slide[0].speaker, "Cindy")
        self.assertGreaterEqual(len(slide[0].sources), 2)

    def test_reconciliation_after_auto_alignment(self):
        s = self.fresh()
        apply_suggested_offsets(s, suggest_offsets(s))
        self.check_transcript(s)

    def test_team_only_lines_never_attributed_to_owner(self):
        s = self.aligned()
        cues = load_cues(s)
        # Drop every named cue: nothing may be attributed to anyone any more.
        team_only = {pid: [c for c in cs if c.channel != "named"] for pid, cs in cues.items()}
        lines = build_session_transcript(s, team_only)
        self.assertTrue(lines)
        self.assertTrue(all(l.speaker is None for l in lines))

    # ---------------------------------------------------------------- reviews
    def test_sample_reviews_import_backward_compatible(self):
        s = self.aligned()
        for name, reviewer in (("review_claude.json", "Claude"), ("review_chatgpt.json", "ChatGPT")):
            raw = json.loads((self.folder / name).read_text(encoding="utf-8"))["suggestions"]
            res = import_review(s, json.dumps({"suggestions": raw}), reviewer)
            self.assertEqual(res.warnings, [], name)
            for r, sg in zip(raw, res):
                if r["kind"] == "clip_now":
                    self.assertAlmostEqual(sg.start, parse_ts(r["start"]), places=3)
                    self.assertAlmostEqual(sg.end, parse_ts(r["end"]), places=3)
                self.assertEqual([q["text"] for q in sg.quotes], [q["text"] for q in r.get("quotes", [])])
                self.assertTrue(all("sources" not in q for q in sg.quotes))   # no line ids -> unchanged

    def test_quote_line_id_resolves_on_sample(self):
        s = self.aligned()
        packet = build_review_packet(s, "Claude")
        lid = next(l.split(" ")[0] for l in packet.splitlines() if "Throw the zombie down the slide" in l)
        res = import_review(s, {"suggestions": [{"title": "X", "start": "00:06:30", "end": "00:06:50",
                                                 "kind": "clip_now",
                                                 "quotes": [{"line": lid, "text": "Throw the zombie down the slide!"}]}]},
                            "Claude")
        q = res[0].quotes[0]
        self.assertEqual(q["speaker"], "Cindy")
        self.assertEqual(q["source"], "cindy-p2 00:03:17,200 Named")
        self.assertIn("andres-p2", {x["part_id"] for x in q["sources"]})

    def test_review_import_event_ids(self):
        s = self.aligned()
        c = import_review(s, (self.folder / "review_claude.json").read_text(encoding="utf-8"), "Claude")
        g = import_review(s, (self.folder / "review_chatgpt.json").read_text(encoding="utf-8"), "ChatGPT")
        by_title = lambda lst: {x.title: x for x in lst}
        c, g = by_title(c), by_title(g)
        self.assertEqual(c["Zombie Slide"].event_id, g["Zombie Slide"].event_id)
        ids = [x.event_id for x in c.values()] + [g["Four-Cart Race"].event_id]
        self.assertEqual(len(ids), len(set(ids)))          # everything else distinct
        self.assertTrue(all(x.event_id.startswith("NB-") for x in s.suggestions))
        self.assertEqual(g["Four-Cart Race"].kind, "needs_filming")
        self.assertEqual(len({x.suggestion_id for x in s.suggestions}), len(s.suggestions))

    def test_review_packet_roundtrip(self):
        s = self.aligned()
        packet = build_review_packet(s, "ChatGPT")
        self.assertRegex(packet, r"L\d{4} 00:06:37 Cindy: Throw the zombie down the slide!")
        self.assertNotIn("cindy-p2 00:03:17,200", packet)        # provenance stays internal
        self.assertIn("Andres: part 1 00:00:12.500-00:05:12.500; part 2 00:05:30.000-", packet)
        self.assertIn("NO footage during gap 00:05:12.500-00:05:30.000", packet)
        self.assertNotIn("Alignment not yet verified", packet)
        # unknown durations (no video) are estimated from the SRT, never left blank
        fresh = self.fresh()
        self.assertIn("Andres: part 1 00:00:00.000-~", build_review_packet(fresh, "Claude"))
        self.assertTrue(parse_review((self.folder / "review_chatgpt.json").read_text(encoding="utf-8"), "ChatGPT"))


if __name__ == "__main__":
    unittest.main()
