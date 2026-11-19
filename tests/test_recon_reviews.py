import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from autoclipper import reviews
from autoclipper.reviews import (REVIEW_JSON_SCHEMA, build_review_packet, parse_review, import_review,
                                 assign_events, reassign_event, remove_reviewer)

from autoclipper.models import fmt_ts

from recon_util import make_session, cue, synthetic_session


def sug(id_, start, end, title="T", **kw):
    d = {"id": id_, "title": title, "start": start, "end": end, "priority": "high", "kind": "clip_now",
         "what_happens": "x", "why": "y"}
    d.update(kw)
    return d


def base_session():
    return make_session([("Cindy", [("cindy-p1", 0.0, 600.0)]), ("Andres", [("andres-p1", 10.0, 300.0),
                                                                           ("andres-p2", 330.0, 270.0)])])


class ParseTests(unittest.TestCase):
    def test_schema_is_json(self):
        json.dumps(REVIEW_JSON_SCHEMA)
        self.assertIn("suggestions", REVIEW_JSON_SCHEMA["properties"])

    def test_code_fence_and_prose(self):
        text = ("Sure! Here is my review:\n```json\n" + json.dumps({"reviewer": "Claude", "suggestions": [
            sug("C-01", "00:06:35", "00:06:50", "Zombie Slide",
                quotes=[{"time": "00:06:37.2", "speaker": "Cindy", "text": "Throw it!", "source": "cindy-p2"}])]})
                + "\n```\nHope this helps.")
        out = parse_review(text, "Claude")
        self.assertEqual(len(out), 1)
        s = out[0]
        self.assertEqual((s.suggestion_id, s.reviewer, s.reviewer_name), ("claude-01", "claude", "Claude"))
        self.assertEqual((s.start, s.end), (395.0, 410.0))
        self.assertAlmostEqual(s.quotes[0]["time"], 397.2)
        self.assertEqual(s.raw["id"], "C-01")
        self.assertTrue(s.imported_at)

    def test_bare_list_trailing_commas_smart_quotes_seconds(self):
        text = '[{“title”: "A", "start": 12, "end": "45.5s", "priority": "P1",},]'
        s = parse_review(text, "ChatGPT")[0]
        self.assertEqual((s.start, s.end, s.priority, s.reviewer), (12.0, 45.5, "high", "chatgpt"))

    def test_aliases_and_defaults(self):
        obj = {"clips": [{"name": "Bridge", "time_range": "00:01:00 - 00:01:20", "type": "Needs filming idea",
                          "povs": "Cindy, Andres", "quotes": ["[00:01:05] Cindy: wow", "plain quote"]},
                         {"title": "Only start", "start": "2:00"}],
               "ideas": [{"title": "Stage a catapult race"}]}
        out = parse_review(obj, "Claude")
        self.assertEqual(len(out), 3)
        a, b, c = out
        self.assertEqual((a.start, a.end, a.kind, a.povs), (60.0, 80.0, "needs_filming", ["Cindy", "Andres"]))
        self.assertEqual(a.quotes[0], {"text": "wow", "speaker": "Cindy", "time": 65.0, "source": ""})
        self.assertEqual(a.quotes[1]["text"], "plain quote")
        # clip_now without a valid range: kept, untimed, flagged (never silently dropped)
        self.assertEqual((b.start, b.end, b.priority, b.kind), (0.0, 0.0, "medium", "clip_now"))
        self.assertIn("no valid session time range", b.uncertainty)
        self.assertEqual(len(out.warnings), 1)
        self.assertIn("Only start", out.warnings[0])
        self.assertEqual((c.kind, c.start, c.end), ("needs_filming", 0.0, 0.0))
        self.assertIn("no session time", c.uncertainty)

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            parse_review("I could not find anything interesting.", "Claude")
        with self.assertRaises(ValueError):
            parse_review({"suggestions": []}, "Claude")


class EventTests(unittest.TestCase):
    def test_shared_and_distinct_events(self):
        s = base_session()
        s.event_prefix = "NB"
        c = import_review(s, {"suggestions": [sug("1", 100, 120, "Slide"), sug("2", 110, 125, "Slide alt"),
                                              sug("3", 300, 320, "Gap")]}, "Claude")
        self.assertEqual([x.event_id for x in c], ["NB-001", "NB-002", "NB-003"])
        g = import_review(s, {"suggestions": [sug("1", 98, 118, "Zombie slide"), sug("2", 500, 510, "Other")]},
                          "ChatGPT")
        self.assertEqual(g[0].event_id, "NB-001")      # same moment, different window -> shared ID
        self.assertEqual(g[1].event_id, "NB-004")
        ev = {e.event_id: e for e in s.events}
        self.assertEqual(ev["NB-001"].suggestion_ids, ["claude-01", "chatgpt-01"])
        self.assertEqual(ev["NB-001"].title, "Slide")

    def test_reimport_gets_unique_ids_and_never_renumbers(self):
        s = base_session()
        s.event_prefix = "NB"
        import_review(s, [sug("1", 100, 120)], "Claude")
        again = import_review(s, [sug("1", 100, 120)], "Claude")
        self.assertEqual(again[0].suggestion_id, "claude-02")
        self.assertEqual(again[0].event_id, "NB-002")  # same reviewer -> not merged automatically
        self.assertEqual(len({x.suggestion_id for x in s.suggestions}), 2)

    def test_low_overlap_not_merged(self):
        s = base_session()
        import_review(s, [sug("1", 100, 120)], "Claude")
        g = import_review(s, [sug("1", 115, 140)], "ChatGPT")   # 5 s overlap / 20 s shorter = 25%
        self.assertEqual(g[0].event_id, "NB-002")

    def test_reassign(self):
        s = base_session()
        s.event_prefix = "NB"
        import_review(s, [sug("1", 100, 120), sug("2", 200, 220)], "Claude")
        import_review(s, [sug("1", 100, 120)], "ChatGPT")
        self.assertEqual(reassign_event(s, "chatgpt-01", "NB-002"), "NB-002")
        self.assertEqual({e.event_id: e.suggestion_ids for e in s.events},
                         {"NB-001": ["claude-01"], "NB-002": ["claude-02", "chatgpt-01"]})
        new = reassign_event(s, "claude-01", None)
        self.assertEqual(new, "NB-003")                 # emptied NB-001 removed; no reuse of numbers
        self.assertEqual(sorted(e.event_id for e in s.events), ["NB-002", "NB-003"])
        self.assertEqual(reassign_event(s, "claude-02", "NB-099"), "NB-099")
        with self.assertRaises(KeyError):
            reassign_event(s, "nope", None)

    def test_remove_reviewer(self):
        s = base_session()
        import_review(s, [sug("1", 100, 120)], "Claude")
        import_review(s, [sug("1", 100, 120)], "ChatGPT")
        self.assertEqual(remove_reviewer(s, "Claude"), 1)
        self.assertEqual([e.suggestion_ids for e in s.events], [["chatgpt-01"]])

    def test_assign_events_batch_with_two_reviewers(self):
        s = base_session()
        a = parse_review([sug("1", 10, 30)], "Claude")
        b = parse_review([sug("1", 12, 28)], "ChatGPT")
        s.suggestions.extend(a + b)
        assign_events(s, a + b)
        self.assertEqual(a[0].event_id, b[0].event_id)


class UntimedTests(unittest.TestCase):
    def test_needs_filming_without_times_is_clean(self):
        out = parse_review({"suggestions": [{"title": "Stage it", "kind": "needs_filming", "start": None,
                                             "end": None, "what_happens": "x", "why": "y"}]}, "Claude")
        self.assertEqual((out[0].start, out[0].end, out[0].kind), (0.0, 0.0, "needs_filming"))
        self.assertEqual(out.warnings, [])

    def test_clip_now_invalid_ranges_flagged_not_dropped(self):
        out = parse_review({"suggestions": [
            sug("1", "00:01:10", "00:01:00", "Backwards"),
            sug("2", None, None, "No times"),
            sug("3", "soon", "later", "Garbage"),
            sug("4", "00:01:00", "00:01:20", "Fine")]}, "ChatGPT")
        self.assertEqual(len(out), 4)
        self.assertEqual([(x.start, x.end) for x in out], [(0.0, 0.0)] * 3 + [(60.0, 80.0)])
        self.assertEqual(len(out.warnings), 3)
        self.assertTrue(all("untimed" in w for w in out.warnings))

    def test_import_returns_warnings_and_keeps_untimed_in_session(self):
        s = base_session()
        res = import_review(s, [sug("1", "00:02:00", None, "Half"), sug("2", 10, 20, "Ok")], "Claude")
        self.assertEqual(len(s.suggestions), 2)
        self.assertEqual(len(res.warnings), 1)
        self.assertIn("claude-01", res.warnings[0])
        self.assertTrue(all(x.event_id for x in s.suggestions))

    def test_schema_requires_times_only_for_clip_now(self):
        items = REVIEW_JSON_SCHEMA["properties"]["suggestions"]["items"]
        self.assertNotIn("start", items["required"])
        self.assertEqual(items["if"]["properties"]["kind"]["const"], "clip_now")
        self.assertEqual(items["then"]["required"], ["start", "end"])
        self.assertIn("null", items["properties"]["start"]["type"])
        self.assertIn("line", items["properties"]["quotes"]["items"]["properties"])


class ResolutionRankingTests(unittest.TestCase):
    def test_stale_id_without_time_prefers_named_line_in_window(self):
        s = make_session([("Cindy", [("cindy-p1", 0.0, 900.0)]), ("Andres", [("andres-p1", 0.0, 900.0)])])
        cues = {"cindy-p1": [cue("cindy-p1", "cindy", 1, 400.0, 402.0, "named", "The whole cart landed on the roof.", "Cindy"),
                             cue("cindy-p1", "cindy", 2, 20.0, 22.0, "named", "Something else entirely here", "Cindy")],
                "andres-p1": [cue("andres-p1", "andres", 1, 5.0, 7.0, "team", "the whole cart landed on roof")]}
        plines = reviews.session_packet_lines(s, cues)
        res = parse_review([sug("1", 395, 410, "Roof", quotes=[{"line": "L0003", "text": "The whole cart landed on the roof."}])],
                           "Claude")
        reviews.resolve_quote_lines(s, res, [], plines=plines)
        q = res[0].quotes[0]
        self.assertEqual(q["speaker"], "Cindy")
        self.assertEqual(q["source"], "cindy-p1 00:06:40,000 Named")

    def test_untimed_suggestions_numbered_after_timed(self):
        s = base_session()
        import_review(s, {"suggestions": [{"title": "Idea", "kind": "needs_filming"}, sug("2", 100, 120, "Clip")]},
                      "Claude")
        by = {x.title: x.event_id for x in s.suggestions}
        self.assertEqual((by["Clip"], by["Idea"]), ("NB-001", "NB-002"))


class PacketTests(unittest.TestCase):
    def test_packet_contents(self):
        session, cues, truth = synthetic_session(seed=1)
        session.session_id = "NB-2026-11-16"
        text = build_review_packet(session, "Claude", cues)
        for needle in ("Return ONLY a JSON object", "needs_filming", "confirmed_by_transcript",
                       "NB-2026-11-16", "Andres: part 1 00:00:12.500-00:05:12.500",
                       "NO footage during gap 00:05:12.500-00:05:30.000",
                       "Team?:", '"reviewer": "Claude"', reviews.DEFAULT_GAME_CONTEXT,
                       "self-contained", "setups and payoffs", "Repeatable formats"):
            self.assertIn(needle, text)
        transcript = text[text.index("TRANSCRIPT ("):]
        self.assertNotIn("src:", transcript)
        self.assertNotIn("Named", transcript)
        self.assertRegex(transcript, r"\nL0001 \d\d:\d\d:\d\d [^:]+: ")

    def test_example_is_neutral_and_has_untimed_idea(self):
        session, cues, truth = synthetic_session(seed=1)
        text = build_review_packet(session, "Claude", cues)
        start = text.index("{", text.index("OUTPUT"))
        ex = json.JSONDecoder().raw_decode(text, start)[0]
        clip, idea = ex["suggestions"]
        self.assertTrue(clip["title"].startswith("<") and clip["start"].startswith("<"))
        self.assertEqual(idea["kind"], "needs_filming")
        self.assertNotIn("start", idea)
        example_text = json.dumps(ex).lower()
        for real in ("zombie", "slide", "cindy", "andres"):
            self.assertNotIn(real, example_text)

    def test_game_context_used_and_editable(self):
        session, cues, truth = synthetic_session(seed=1)
        session.game_context = "A cozy farming game about competitive goats."
        text = build_review_packet(session, "Claude", cues)
        self.assertIn("competitive goats", text)
        self.assertNotIn(reviews.DEFAULT_GAME_CONTEXT, text)

    def test_packet_is_independent_of_imported_reviews(self):
        session, cues, truth = synthetic_session(seed=1)
        before = build_review_packet(session, "ChatGPT", cues)
        import_review(session, [sug("1", 10, 20, "Purple Platypus Parade", why="unique-why-xyzzy",
                                    quotes=[{"text": "quote-plugh"}])], "Claude")
        after = build_review_packet(session, "ChatGPT", cues)
        self.assertEqual(before, after)
        for leak in ("Purple Platypus", "xyzzy", "plugh", "claude-01"):
            self.assertNotIn(leak, after)

    def test_quote_line_ids_resolve_to_sources(self):
        session, cues, truth = synthetic_session(seed=1)
        plines = reviews.session_packet_lines(session, cues)
        target = next(p for p in plines if p.speaker and len(p.sources) >= 2)
        stale = next(p for p in plines if p is not target and p.text != target.text)
        res = parse_review([sug("1", 10, 20, "A", quotes=[
            {"line": target.packet_id, "text": target.text},
            {"line": stale.packet_id, "text": target.text, "time": fmt_ts(target.session_start)},
            {"line": "L9999", "text": "words nobody said here at all"},
            {"time": "00:00:05", "text": "no line id"}])], "Claude")
        warnings = []
        n = reviews.resolve_quote_lines(session, res, warnings, plines=plines)
        q = res[0].quotes
        self.assertEqual(n, 2)
        self.assertEqual(q[0]["line"], target.packet_id)
        self.assertEqual(q[0]["speaker"], target.speaker)
        self.assertEqual(len(q[0]["sources"]), len(target.sources))
        self.assertEqual(q[0]["sources"][0]["channel"], "named")
        self.assertTrue(q[0]["source"].endswith("Named"))
        # stale id: the quoted text wins, original citation kept
        self.assertEqual((q[1]["line"], q[1]["line_cited"]), (target.packet_id, stale.packet_id))
        self.assertTrue(q[2]["line_unresolved"])
        self.assertEqual(len(warnings), 1)
        self.assertEqual(q[3], {"text": "no line id", "speaker": None, "time": 5.0, "source": ""})


if __name__ == "__main__":
    unittest.main()
