import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from autoclipper.autoalign import suggest_offsets, apply_suggested_offsets
from autoclipper.reconcile import build_session_transcript

from recon_util import synthetic_session, make_session, cue


class AutoAlignTests(unittest.TestCase):
    def scrambled(self, seed):
        session, cues, truth = synthetic_session(seed=seed)
        true = {pt.part_id: pt.offset for _, pt in session.all_parts()}
        for _, pt in session.all_parts():
            pt.offset = 0.0
        return session, cues, true

    def test_recovers_offsets_within_half_second(self):
        for seed in (1, 2, 7):
            session, cues, true = self.scrambled(seed)
            sugg = suggest_offsets(session, cues)
            self.assertEqual(len(sugg), 7)
            by = {s["part_id"]: s for s in sugg}
            self.assertEqual(by["cindy-p1"]["method"], "reference")
            for pid, off in true.items():
                self.assertIsNotNone(by[pid]["offset"], (seed, pid))
                self.assertLess(abs(by[pid]["offset"] - off), 0.5, (seed, pid, by[pid]))
                if pid != "cindy-p1":
                    self.assertGreaterEqual(by[pid]["confidence"], 0.5, (seed, pid, by[pid]))
            # never applied automatically
            self.assertTrue(all(pt.offset == 0.0 for _, pt in session.all_parts()))

    def test_apply_respects_manual_and_threshold(self):
        session, cues, true = self.scrambled(1)
        session.part_by_id("rika-p1").offset_source = "manual"
        session.part_by_id("rika-p1").offset = 29.0
        sugg = suggest_offsets(session, cues)
        apply_suggested_offsets(session, sugg)
        self.assertEqual(session.part_by_id("rika-p1").offset, 29.0)
        self.assertEqual(session.part_by_id("rika-p1").offset_source, "manual")
        p = session.part_by_id("andres-p2")
        self.assertAlmostEqual(p.offset, 330.0, delta=0.5)
        self.assertEqual(p.offset_source, "transcript")
        self.assertIsNotNone(p.offset_confidence)
        # reconciliation after alignment attributes speakers again
        lines = build_session_transcript(session, cues)
        self.assertGreater(sum(1 for l in lines if l.speaker), 100)

    def test_min_confidence_blocks_apply(self):
        session, cues, true = self.scrambled(2)
        sugg = suggest_offsets(session, cues)
        apply_suggested_offsets(session, sugg, min_confidence=1.01)
        self.assertTrue(all(pt.offset == 0.0 for _, pt in session.all_parts()))

    def test_reference_keeps_its_offset(self):
        session, cues, true = self.scrambled(1)
        session.part_by_id("didi-p1").offset = 100.0
        sugg = {s["part_id"]: s for s in suggest_offsets(session, cues, reference_part_id="didi-p1")}
        self.assertEqual(sugg["didi-p1"]["offset"], 100.0)
        self.assertAlmostEqual(sugg["cindy-p1"]["offset"], 55.0, delta=0.5)
        self.assertAlmostEqual(sugg["andres-p1"]["offset"], 67.5, delta=0.5)

    def test_unmatched_part_reports_none(self):
        s = make_session([("Cindy", [("cindy-p1", 0.0, 100.0)]), ("Bob", [("bob-p1", 0.0, 100.0)])])
        cues = {"cindy-p1": [cue("cindy-p1", "cindy", 1, 5, 7, "named", "something entirely unique here", "Cindy")],
                "bob-p1": [cue("bob-p1", "bob", 1, 5, 7, "named", "a different sentence altogether", "Bob")]}
        sugg = {x["part_id"]: x for x in suggest_offsets(s, cues)}
        self.assertEqual(sugg["bob-p1"]["method"], "none")
        self.assertIsNone(sugg["bob-p1"]["offset"])
        apply_suggested_offsets(s, list(sugg.values()))
        self.assertEqual(s.part_by_id("bob-p1").offset_source, "default")

    def test_simple_pair_named_team(self):
        s = make_session([("Cindy", [("cindy-p1", 0.0, 100.0)]), ("Andres", [("andres-p1", 0.0, 100.0)])])
        lines = ["Throw the zombie down the slide", "Grab the ladder before it falls",
                 "The whole bridge just collapsed", "Bring the rope over here please"]
        cc, ac = [], []
        for i, t in enumerate(lines):
            cc.append(cue("cindy-p1", "cindy", i + 1, 20 + 10 * i, 22 + 10 * i, "named", t, "Cindy"))
            ac.append(cue("andres-p1", "andres", i + 1, 8 + 10 * i + (0.2 if i % 2 else -0.2), 10 + 10 * i, "team", t.lower()))
        sugg = {x["part_id"]: x for x in suggest_offsets(s, {"cindy-p1": cc, "andres-p1": ac})}
        self.assertAlmostEqual(sugg["andres-p1"]["offset"], 12.0, delta=0.3)
        self.assertEqual(sugg["andres-p1"]["support"], 4)


if __name__ == "__main__":
    unittest.main()
