import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from autoclipper import reconcile
from autoclipper.reconcile import build_session_transcript, transcript_text, similarity

from recon_util import make_session, cue, synthetic_session


def three_povs():
    return make_session([("Cindy", [("cindy-p1", 0.0, 600.0)]),
                         ("Andres", [("andres-p1", 10.0, 600.0)]),
                         ("Rika", [("rika-p1", -5.0, 600.0)])])


class SimilarityTests(unittest.TestCase):
    def test_punctuation_and_case_ignored(self):
        self.assertEqual(similarity("Throw the zombie, down the SLIDE!", "throw the zombie down the slide"), 1.0)

    def test_rough_rendition_still_similar(self):
        self.assertGreater(similarity("Throw the zombie down the slide!", "Throw the body down the slide"), 0.6)
        self.assertGreater(similarity("Throw the zombie down the slide!", "Throw ... down the slide!"), 0.6)

    def test_unrelated_is_low(self):
        self.assertLess(similarity("Throw the zombie down the slide", "Who left the door open again"), 0.5)


class ReconcileTests(unittest.TestCase):
    def test_spec_example_one_line_named_preferred(self):
        s = three_povs()
        cues = {
            # session time 395 everywhere
            "cindy-p1": [cue("cindy-p1", "cindy", 1, 395.0, 397.0, "named", "Throw the zombie down the slide!", "Cindy")],
            "andres-p1": [cue("andres-p1", "andres", 1, 385.2, 387.0, "team", "Throw the body down the slide!")],
            "rika-p1": [cue("rika-p1", "rika", 1, 399.8, 401.9, "team", "Throw ... down the slide!")],
        }
        lines = build_session_transcript(s, cues)
        self.assertEqual(len(lines), 1)
        l = lines[0]
        self.assertEqual(l.speaker, "Cindy")
        self.assertEqual(l.text, "Throw the zombie down the slide!")
        self.assertEqual(l.confidence, "high")
        self.assertEqual({src["part_id"] for src in l.sources}, {"cindy-p1", "andres-p1", "rika-p1"})
        self.assertEqual(l.sources[0]["channel"], "named")
        self.assertIn("[00:06:35.000] Cindy: Throw the zombie down the slide!", transcript_text(lines))

    def test_majority_team_does_not_outvote_named(self):
        s = three_povs()
        cues = {
            "cindy-p1": [cue("cindy-p1", "cindy", 1, 100.0, 102.0, "named", "Grab the ladder before it falls", "Cindy")],
            "andres-p1": [cue("andres-p1", "andres", 1, 90.1, 92.0, "team", "grab the latter before it calls")],
            "rika-p1": [cue("rika-p1", "rika", 1, 104.9, 107.0, "team", "grab the latter before it calls")],
        }
        lines = build_session_transcript(s, cues)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].text, "Grab the ladder before it falls")

    def test_team_never_attributed_to_file_owner(self):
        s = three_povs()
        cues = {
            "cindy-p1": [cue("cindy-p1", "cindy", 1, 50.0, 52.0, "team", "Who left the door open again")],
            "andres-p1": [cue("andres-p1", "andres", 1, 40.2, 42.0, "team", "who left the door open again?")],
            "rika-p1": [],
        }
        lines = build_session_transcript(s, cues)
        self.assertEqual(len(lines), 1)
        l = lines[0]
        self.assertIsNone(l.speaker)
        self.assertEqual(l.channel, "team")
        self.assertEqual(l.confidence, "medium")
        self.assertFalse(l.uncertain)
        self.assertEqual(len(l.sources), 2)
        self.assertIn("Team (unattributed)", transcript_text(lines))

    def test_team_in_own_file_not_matched_to_own_named(self):
        # Cindy's own Team channel can't contain Cindy's voice.
        s = three_povs()
        cues = {"cindy-p1": [cue("cindy-p1", "cindy", 1, 50.0, 52.0, "named", "Watch this it is so good", "Cindy"),
                             cue("cindy-p1", "cindy", 2, 50.1, 52.0, "team", "watch this it is so good")],
                "andres-p1": [], "rika-p1": []}
        lines = build_session_transcript(s, cues)
        self.assertEqual(len(lines), 2)
        team = [l for l in lines if l.channel == "team"][0]
        self.assertIsNone(team.speaker)

    def test_disagreeing_team_renditions_flagged(self):
        s = three_povs()
        cues = {"cindy-p1": [cue("cindy-p1", "cindy", 1, 50.0, 52.0, "team", "the whole bridge just collapsed now")],
                "andres-p1": [cue("andres-p1", "andres", 1, 40.0, 42.0, "team", "the hole fridge just collapsed")],
                "rika-p1": [cue("rika-p1", "rika", 1, 55.0, 57.0, "team", "whole bridge collapsed")]}
        lines = build_session_transcript(s, cues)
        self.assertEqual(len(lines), 1)
        self.assertIsNone(lines[0].speaker)
        self.assertEqual(len(lines[0].sources), 3)

    def test_far_apart_in_time_not_merged(self):
        s = three_povs()
        cues = {"cindy-p1": [cue("cindy-p1", "cindy", 1, 50.0, 52.0, "named", "Can we do that again please", "Cindy")],
                "andres-p1": [cue("andres-p1", "andres", 1, 80.0, 82.0, "team", "can we do that again please")],
                "rika-p1": []}
        lines = build_session_transcript(s, cues)
        self.assertEqual(len(lines), 2)
        self.assertEqual([l.speaker for l in lines], ["Cindy", None])

    def test_local_only_and_unlabeled_kept(self):
        s = three_povs()
        cues = {"cindy-p1": [cue("cindy-p1", "cindy", 1, 5.0, 6.0, "named", "hmm", "Cindy"),
                             cue("cindy-p1", "cindy", 2, 8.0, 9.0, "unlabeled", "music playing")],
                "andres-p1": [], "rika-p1": []}
        lines = build_session_transcript(s, cues)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].speaker, "Cindy")
        self.assertEqual(lines[1].channel, "unlabeled")

    def test_two_speakers_same_time_kept_separate(self):
        s = three_povs()
        cues = {"cindy-p1": [cue("cindy-p1", "cindy", 1, 50.0, 52.0, "named", "Get in the truck right now", "Cindy"),
                             cue("cindy-p1", "cindy", 2, 50.3, 52.0, "team", "the car is rolling down the hill")],
                "andres-p1": [cue("andres-p1", "andres", 1, 40.3, 42.0, "named", "The car is rolling down the hill", "Andres"),
                              cue("andres-p1", "andres", 2, 40.0, 42.0, "team", "get in the truck right now")],
                "rika-p1": [cue("rika-p1", "rika", 1, 55.0, 57.0, "team", "get in the truck right now"),
                            cue("rika-p1", "rika", 2, 55.3, 57.0, "team", "car is rolling down the hill")]}
        lines = build_session_transcript(s, cues)
        self.assertEqual(len(lines), 2)
        by = {l.speaker: l for l in lines}
        self.assertEqual({src["part_id"] for src in by["Cindy"].sources}, {"cindy-p1", "andres-p1", "rika-p1"})
        self.assertEqual({src["part_id"] for src in by["Andres"].sources}, {"cindy-p1", "andres-p1", "rika-p1"})

    def test_overlapping_parts_same_pov_dedup(self):
        s = make_session([("Cindy", [("cindy-p1", 0.0, 210.0), ("cindy-p2", 200.0, 200.0)])])
        cues = {"cindy-p1": [cue("cindy-p1", "cindy", 1, 205.0, 207.0, "named", "Stack the crates higher", "Cindy")],
                "cindy-p2": [cue("cindy-p2", "cindy", 1, 5.1, 7.0, "named", "Stack the crates higher.", "Cindy")]}
        lines = build_session_transcript(s, cues)
        self.assertEqual(len(lines), 1)
        self.assertEqual(len(lines[0].sources), 2)

    def test_overrides_and_sources_text(self):
        s = three_povs()
        cues = {"cindy-p1": [], "rika-p1": [],
                "andres-p1": [cue("andres-p1", "andres", 3, 40.0, 42.0, "team", "pass me the shovel")]}
        s.transcript_overrides = {"andres-p1:3": {"speaker": "Didi"}}
        lines = build_session_transcript(s, cues)
        self.assertEqual(lines[0].speaker, "Didi")
        txt = transcript_text(lines, include_sources=True)
        self.assertIn("andres-p1 00:00:40,000 Team", txt)

    def test_synthetic_session_quality(self):
        session, cues, truth = synthetic_session(seed=3)
        lines = build_session_transcript(session, cues)
        dialogue = [l for l in lines if reconcile.normalize(l.text) != "hmm"]
        correct = 0
        for t in truth:
            cands = [l for l in dialogue if abs(l.session_start - t["time"]) <= 0.6
                     and similarity(l.text, t["text"]) > 0.6]
            # one utterance -> exactly one line (no duplicates)
            self.assertLessEqual(len(cands), 1, (t, [(l.speaker, l.text) for l in cands]))
            if cands and cands[0].speaker == t["speaker"]:
                correct += 1
        self.assertGreaterEqual(correct / len(truth), 0.9)
        # never attribute a Team-only line to its file owner
        for l in lines:
            if l.speaker and l.channel == "named":
                self.assertEqual(l.sources[0]["channel"], "named")
                self.assertEqual(l.sources[0]["pov_name"], l.speaker)
                for src in l.sources[1:]:
                    if src["channel"] == "team":
                        self.assertNotEqual(src["pov_name"], l.speaker)


if __name__ == "__main__":
    unittest.main()
