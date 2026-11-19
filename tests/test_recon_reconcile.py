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

    def test_own_voice_echo_in_own_team_channel_is_a_source_not_a_line(self):
        # Real data: the owner's voice leaks into their own Team channel at the same moment.
        s = three_povs()
        cues = {"cindy-p1": [cue("cindy-p1", "cindy", 1, 50.0, 52.0, "named", "Watch this it is so good", "Cindy"),
                             cue("cindy-p1", "cindy", 2, 50.1, 52.0, "team", "watch this, it is so good!")],
                "andres-p1": [], "rika-p1": []}
        lines = build_session_transcript(s, cues)
        self.assertEqual(len(lines), 1)
        self.assertEqual([x["match"] for x in lines[0].sources], [None, "echo"])
        self.assertIn("echoed", lines[0].note)

    def test_own_file_team_line_never_given_to_owner(self):
        # A different sentence in Cindy's Team channel, even right next to her own line, stays
        # unattributed: file ownership is not evidence.
        s = three_povs()
        cues = {"cindy-p1": [cue("cindy-p1", "cindy", 1, 50.0, 52.0, "named", "Watch this it is so good", "Cindy"),
                             cue("cindy-p1", "cindy", 2, 50.4, 52.0, "team", "the lamp fell on my head again"),
                             cue("cindy-p1", "cindy", 3, 60.0, 61.0, "team", "Watch this it is so good")],
                "andres-p1": [], "rika-p1": []}
        lines = build_session_transcript(s, cues)
        self.assertEqual(len(lines), 3)
        self.assertEqual(sum(1 for l in lines if l.speaker == "Cindy"), 1)
        self.assertTrue(all(l.speaker is None for l in lines if l.channel == "team"))

    def test_substring_fragment_attached(self):
        s = three_povs()
        cues = {"cindy-p1": [cue("cindy-p1", "cindy", 1, 100.0, 105.0, "named",
                                 "okay so we're at the mansion guys okay so here's the game", "Cindy")],
                "andres-p1": [cue("andres-p1", "andres", 1, 90.2, 92.0, "team", "Okay, so we're at the mansion, guys.")],
                "rika-p1": []}
        lines = build_session_transcript(s, cues)
        self.assertEqual(len(lines), 1)
        self.assertIn(lines[0].sources[1]["match"], ("rendition", "fragment"))

    def test_split_named_line_two_team_fragments_same_part(self):
        s = three_povs()
        cues = {"cindy-p1": [cue("cindy-p1", "cindy", 1, 100.0, 106.0, "named",
                                 "bring the rope over here and tie it to the red truck please", "Cindy")],
                "andres-p1": [cue("andres-p1", "andres", 1, 90.1, 93.0, "team", "Bring the rope over here"),
                              cue("andres-p1", "andres", 2, 93.0, 96.0, "team", "and tie it to the red truck, please.")],
                "rika-p1": []}
        lines = build_session_transcript(s, cues)
        self.assertEqual(len(lines), 1)
        self.assertEqual(len(lines[0].sources), 3)

    def test_one_team_cue_spanning_two_named_lines(self):
        s = three_povs()
        cues = {"cindy-p1": [cue("cindy-p1", "cindy", 1, 100.0, 101.5, "named", "Is the generator on?", "Cindy")],
                "andres-p1": [cue("andres-p1", "andres", 1, 92.0, 94.0, "named", "No, the fuse blew up again.", "Andres")],
                "rika-p1": [cue("rika-p1", "rika", 1, 105.0, 109.0, "team",
                                "is the generator on no the fuse blew up again")]}
        lines = build_session_transcript(s, cues)
        self.assertEqual([l.speaker for l in lines], ["Cindy", "Andres"])
        self.assertEqual(sum(len(l.sources) for l in lines), 3)

    def test_wider_tolerance_for_alignment_error(self):
        s = three_povs()
        cues = {"cindy-p1": [cue("cindy-p1", "cindy", 1, 100.0, 102.0, "named", "The whole bridge just collapsed", "Cindy")],
                "andres-p1": [cue("andres-p1", "andres", 1, 93.6, 95.6, "team", "the whole bridge just collapsed")],
                "rika-p1": []}      # Andres is 3.6 s off
        lines = build_session_transcript(s, cues)
        self.assertEqual(len(lines), 1)

    def test_unique_and_ambiguous_team_lines_kept(self):
        s = three_povs()
        cues = {"cindy-p1": [cue("cindy-p1", "cindy", 1, 100.0, 102.0, "named", "I have a mustache now", "Cindy")],
                "andres-p1": [cue("andres-p1", "andres", 1, 91.0, 93.0, "team", "I have one dash, should we mute?"),
                              cue("andres-p1", "andres", 2, 130.0, 132.0, "team", "Who parked the ambulance in the pool?")],
                "rika-p1": [cue("rika-p1", "rika", 1, 145.2, 147.0, "team", "Who parked the ambulance in the pool")]}
        lines = build_session_transcript(s, cues)
        self.assertEqual(len(lines), 3)
        team = [l for l in lines if l.channel == "team"]
        self.assertEqual(len(team), 2)
        self.assertTrue(all(l.speaker is None for l in team))
        self.assertEqual(sorted(len(l.sources) for l in team), [1, 2])

    def test_generic_piece_far_away_kept(self):
        s = three_povs()
        cues = {"cindy-p1": [cue("cindy-p1", "cindy", 1, 100.0, 108.0, "named",
                                 "oh my god the cart is on the roof and nobody saw it happen", "Cindy")],
                "andres-p1": [cue("andres-p1", "andres", 1, 106.0, 107.0, "team", "Oh my god!")],
                "rika-p1": []}  # session 116 s: 8 s after Cindy's line ended
        lines = build_session_transcript(s, cues)
        self.assertEqual(len(lines), 2)

    # ---- follow-up patterns from the real session (all four players recording)
    def four_povs(self):
        return make_session([("Cindy", [("cindy-p1", 0.0, 1800.0)]), ("Andres", [("andres-p1", 0.0, 1800.0)]),
                             ("Didi", [("didi-p1", 0.0, 1800.0)]), ("Rika", [("rika-p1", 0.0, 1800.0)])])

    def test_followup_substring_short_team(self):
        s = self.four_povs()
        cues = {"cindy-p1": [cue("cindy-p1", "cindy", 1, 1239.0, 1242.0, "named",
                                 "Oh, I'm floating. I'm flying. Sorry.", "Cindy")],
                "andres-p1": [cue("andres-p1", "andres", 1, 1241.0, 1242.0, "team", "I'm flying.")],
                "didi-p1": [], "rika-p1": []}
        lines = build_session_transcript(s, cues)
        self.assertEqual([l.speaker for l in lines], ["Cindy"])
        self.assertEqual(len(lines[0].sources), 2)

    def test_followup_one_team_line_two_named_lines(self):
        s = self.four_povs()
        cues = {"cindy-p1": [cue("cindy-p1", "cindy", 1, 1248.0, 1249.5, "named", "Guys, I can't. Oh.", "Cindy"),
                             cue("cindy-p1", "cindy", 2, 1252.0, 1253.0, "named", "Take me back.", "Cindy")],
                "andres-p1": [cue("andres-p1", "andres", 1, 1248.2, 1252.5, "team",
                                  "Guys, I can't- Oh, oh, oh, oh Just take me back!")],
                "didi-p1": [], "rika-p1": []}
        lines = build_session_transcript(s, cues)
        self.assertEqual([l.speaker for l in lines], ["Cindy", "Cindy"])
        self.assertEqual(sum(len(l.sources) for l in lines), 3)

    def test_followup_reordered_fragment(self):
        s = self.four_povs()
        cues = {"didi-p1": [cue("didi-p1", "didi", 1, 672.0, 674.0, "named", "We're like in raccoon city.", "Didi")],
                "rika-p1": [cue("rika-p1", "rika", 1, 672.5, 673.5, "team", "like raccoon city")],
                "cindy-p1": [], "andres-p1": []}
        lines = build_session_transcript(s, cues)
        self.assertEqual([l.speaker for l in lines], ["Didi"])

    def test_followup_team_repeat_across_povs_collapsed(self):
        s = self.four_povs()
        cues = {"cindy-p1": [cue("cindy-p1", "cindy", 1, 500.0, 504.0, "team",
                                 "The way to revive is you grab with one hand and slap with the other.")],
                "didi-p1": [cue("didi-p1", "didi", 1, 502.5, 505.0, "team",
                                "you grab with one hand, and you slap him awake.")],
                "andres-p1": [], "rika-p1": []}
        lines = build_session_transcript(s, cues)
        self.assertEqual(len(lines), 1)
        self.assertIsNone(lines[0].speaker)
        self.assertEqual(len(lines[0].sources), 2)

    def test_followup_new_content_kept_and_gap_is_conservative(self):
        s = self.four_povs()
        cues = {"cindy-p1": [cue("cindy-p1", "cindy", 1, 300.0, 302.0, "named", "Grab the zombie by the legs.", "Cindy")],
                "andres-p1": [cue("andres-p1", "andres", 1, 300.5, 302.0, "team", "Who took the ambulance keys from the garage")],
                "didi-p1": [], "rika-p1": []}
        lines = build_session_transcript(s, cues)
        self.assertEqual(len(lines), 2)                     # "ambulance keys", "garage" are new content
        # Rika not recording -> an interjection is not absorbed, a one-word line needs 2 content words
        s2 = make_session([("Cindy", [("cindy-p1", 0.0, 900.0)]), ("Andres", [("andres-p1", 0.0, 900.0)]),
                           ("Rika", [("rika-p1", 600.0, 300.0)])])
        cues2 = {"cindy-p1": [cue("cindy-p1", "cindy", 1, 300.0, 302.0, "named", "Okay grab the zombie", "Cindy")],
                 "andres-p1": [cue("andres-p1", "andres", 1, 301.0, 302.0, "team", "Okay."),
                               cue("andres-p1", "andres", 2, 303.0, 304.0, "team", "Zombie!")],
                 "rika-p1": []}
        lines2 = build_session_transcript(s2, cues2)
        self.assertEqual(sum(1 for l in lines2 if l.speaker is None), 2)

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
