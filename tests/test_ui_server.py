"""API-level tests: real server on an ephemeral port, driven with urllib against the sample session."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from autoclipper.server import make_server

ROOT = Path(__file__).resolve().parents[1]
HAS_REVIEWS = importlib.util.find_spec("autoclipper.reviews") is not None


class ServerTestCase(unittest.TestCase):
    """Starts a server + a fresh copy of the sample session for each test class."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ac_ui_")
        cls._env = {k: os.environ.get(k) for k in ("APPDATA", "AUTOCLIPPER_NO_STARTFILE")}
        os.environ["APPDATA"] = os.path.join(cls.tmp, "appdata")
        os.environ["AUTOCLIPPER_NO_STARTFILE"] = "1"
        cls.folder = Path(cls.tmp) / "NB-2026-11-16"
        subprocess.run([sys.executable, "-m", "samples.make_sample", str(cls.folder), "--no-video"],
                       cwd=ROOT, check=True, capture_output=True)
        cls.durations = json.loads((cls.folder / "durations.json").read_text(encoding="utf-8"))
        cls.truth = json.loads((cls.folder / "ground_truth.json").read_text(encoding="utf-8"))
        cls.start_server()

    @classmethod
    def start_server(cls):
        cls.httpd = make_server(0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def stop_server(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    @classmethod
    def tearDownClass(cls):
        cls.stop_server()
        for k, v in cls._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- helpers
    def call(self, path, body=None, expect=200, raw=False):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                status, payload, headers = res.status, res.read(), res.headers
        except urllib.error.HTTPError as err:
            status, payload, headers = err.code, err.read(), err.headers
        self.assertEqual(status, expect, payload[:500])
        if raw:
            return payload.decode("utf-8"), headers
        return json.loads(payload.decode("utf-8"))

    def create_session(self):
        r = self.call("/api/session/open", {"folder": str(self.folder)})
        if r.get("needs_create"):
            r = self.call("/api/session/create", {"folder": str(self.folder), "session_id": "NB-2026-11-16",
                                                  "event_prefix": "NB"})
        return r

    def parts(self, payload):
        return {p["part_id"]: p for pov in payload["session"]["povs"] for p in pov["parts"]}


class TestStaticAndSafety(ServerTestCase):
    def test_static_files_served(self):
        html, headers = self.call("/", raw=True)
        self.assertIn("Auto-Clipper", html)
        self.assertIn("text/html", headers["Content-Type"])
        js, _ = self.call("/app.js", raw=True)
        self.assertIn("renderGuide", js)
        self.call("/style.css", raw=True)
        self.call("/../server.py", expect=404, raw=True)
        self.call("/api/nope", expect=404)

    def test_bound_to_localhost(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")

    def test_requires_session_then_open_path_guard(self):
        self.call("/api/guide", expect=409)
        self.call("/api/session/open", {"folder": str(Path(self.tmp) / "missing")}, expect=404)
        self.create_session()
        # inside: ok (dry run); outside: refused
        self.assertTrue(self.call("/api/open-path", {"path": str(self.folder)})["ok"])
        self.assertTrue(self.call("/api/open-path", {"path": "Cindy_part1.srt"})["ok"])
        self.call("/api/open-path", {"path": self.tmp}, expect=403)
        self.call("/api/open-path", {"path": "..\\"}, expect=403)
        self.call("/api/open-path", {"path": "C:\\Windows"}, expect=403)

    def test_cross_origin_post_refused(self):
        req = urllib.request.Request(self.base + "/api/session/meta", data=b"{}",
                                     headers={"Content-Type": "application/json", "Origin": "http://evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 403)


class TestEndToEnd(ServerTestCase):
    """The whole workflow through the HTTP API, in order."""

    def test_full_workflow(self):
        # 1. create session ----------------------------------------------------
        r = self.call("/api/session/open", {"folder": str(self.folder)})
        self.assertTrue(r.get("needs_create"))
        r = self.call("/api/session/create", {"folder": str(self.folder), "session_id": "NB-2026-11-16", "event_prefix": "NB"})
        self.assertEqual(r["session"]["session_id"], "NB-2026-11-16")
        self.assertEqual({p["name"] for p in r["session"]["povs"]}, {"Cindy", "Andres", "Didi", "Rika"})
        self.assertTrue((self.folder / ".autoclipper" / "session.json").exists())
        recent = self.call("/api/recent")["recent"]
        self.assertTrue(any(Path(x["folder"]) == self.folder.resolve() for x in recent))

        r = self.call("/api/session/meta", {"session_id": "NB-2026-11-16", "event_prefix": "NB", "game": "Test game",
                                            "notes": "n", "pad_before": "3", "pad_after": 4})
        self.assertEqual((r["session"]["pad_before"], r["session"]["pad_after"]), (3.0, 4.0))
        self.call("/api/session/meta", {"pad_before": "-1"}, expect=400)

        # 2. durations (no video in sample) + manual edit validation ------------
        for pid, dur in self.durations.items():
            r = self.call("/api/parts/update", {"part_id": pid, "duration": dur})
        parts = self.parts(r)
        self.assertEqual(parts["cindy-p2"]["duration_source"], "manual")
        self.call("/api/parts/update", {"part_id": "cindy-p1", "offset": "abc"}, expect=400)
        self.call("/api/parts/update", {"part_id": "nope", "offset": 1}, expect=404)

        # 3. chain + suggest + apply offsets -----------------------------------
        r = self.call("/api/align/chain", {})
        parts = self.parts(r)
        self.assertAlmostEqual(parts["cindy-p2"]["offset"], 200.0)
        self.assertAlmostEqual(parts["cindy-p3"]["offset"], 400.0)
        self.assertEqual(parts["cindy-p3"]["offset_source"], "chained")

        self.call("/api/align/suggest", {"reference_part_id": "nope"}, expect=404)
        sugg = self.call("/api/align/suggest", {"reference_part_id": "cindy-p1"})["suggestions"]
        self.assertTrue(sugg)
        chosen = [x for x in sugg if x["method"] == "transcript" and x["offset"] is not None and x["confidence"] >= 0.5]
        self.assertTrue(chosen)
        self.assertTrue(all("label" in x for x in sugg))
        r = self.call("/api/align/apply", {"suggestions": chosen})
        self.assertEqual(r["applied"], len(chosen))
        parts = self.parts(r)
        truth = {"cindy-p1": 0, "cindy-p2": 200, "cindy-p3": 400, "andres-p1": 12.5, "andres-p2": 330,
                 "didi-p1": 45, "rika-p1": 30}
        for pid, off in truth.items():
            self.assertAlmostEqual(parts[pid]["offset"], off, delta=1.0, msg=pid)
        # manual offset wins and is persisted
        r = self.call("/api/parts/update", {"part_id": "andres-p2", "offset": "00:05:30"})
        self.assertEqual(self.parts(r)["andres-p2"]["offset_source"], "manual")
        self.assertAlmostEqual(self.parts(r)["andres-p2"]["offset"], 330.0)
        self.assertIn("povs", r["coverage"])
        gaps = r["coverage"]["povs"]["andres"]["gaps"]
        self.assertEqual(len(gaps), 1)
        self.assertAlmostEqual(gaps[0][0], 312.5, delta=0.5)
        self.assertAlmostEqual(gaps[0][1], 330.0, delta=0.01)

        # 4. transcript ------------------------------------------------------
        tr = self.call("/api/transcript")
        self.assertGreater(tr["total"], 10)
        hit = self.call("/api/transcript?q=slide")
        self.assertTrue(hit["lines"])
        self.assertLess(len(hit["lines"]), tr["total"])
        self.assertTrue(all(ln["sources"] for ln in hit["lines"]))

        if not HAS_REVIEWS:
            self.skipTest("autoclipper.reviews not merged yet")

        # 5. reviews ---------------------------------------------------------
        packet, headers = self.call("/api/review-packet?reviewer=Claude", raw=True)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn("slide", packet.lower())
        for name, fname in (("Claude", "review_claude.json"), ("ChatGPT", "review_chatgpt.json")):
            text = (self.folder / fname).read_text(encoding="utf-8")
            r = self.call("/api/reviews/import", {"reviewer_name": name, "text": text})
            self.assertGreater(r["imported"], 0)
        self.call("/api/reviews/import", {"reviewer_name": "X", "text": "   "}, expect=400)
        revs = {x["reviewer"]: x for x in r["reviewers"]}
        self.assertEqual(set(revs), {"claude", "chatgpt"})

        # 6. guide -----------------------------------------------------------
        g = self.call("/api/guide")
        self.assertEqual(len(g["suggestions"]), sum(x["count"] for x in revs.values()))
        self.assertTrue(all(s["event_id"] for s in g["suggestions"]))
        self.assertTrue(all(s["event_id"].startswith("NB-") for s in g["suggestions"]))
        multi = [e for e in g["events"]
                 if len({s["reviewer"] for s in g["suggestions"] if s["suggestion_id"] in e["suggestion_ids"]}) > 1]
        self.assertTrue(multi, "same event from both reviewers should share an event")
        # boundary-crossing event: Cindy gets two pieces
        boundary = [s for s in g["suggestions"]
                    if any(rg["pov_name"] == "Cindy" and rg["pieces"] == 2 for rg in s["ranges"])]
        self.assertTrue(boundary)
        # gap event: Andres absent
        gap = [s for s in g["suggestions"] if s["kind"] == "clip_now" and 315 <= s["start"] and s["end"] <= 329]
        self.assertTrue(gap)
        g0 = gap[0]
        self.assertFalse([rg for rg in g0["ranges"] if rg["pov_name"] == "Andres"])

        # 7. review decisions ----------------------------------------------
        sids = [s["suggestion_id"] for s in g["suggestions"] if s["kind"] == "clip_now"]
        keep, reject = sids[0], sids[1]
        r = self.call("/api/review", {"suggestion_id": keep, "status": "keep", "notes": "great",
                                      "approved_start": "00:06:31", "approved_end": "00:06:45.5"})
        self.assertEqual(r["review"]["approved_end"], 405.5)
        self.call("/api/review", {"suggestion_id": reject, "status": "reject", "notes": "meh"})
        self.call("/api/review", {"suggestion_id": keep, "status": "bogus"}, expect=400)
        self.call("/api/review", {"suggestion_id": keep, "status": "keep", "approved_start": 10, "approved_end": 5}, expect=400)
        g = self.call("/api/guide")
        sg = next(s for s in g["suggestions"] if s["suggestion_id"] == keep)
        self.assertEqual(sg["review"]["status"], "keep")
        self.assertTrue(sg["approved_ranges"])

        # reassign to a new event and back
        old_event = sg["event_id"]
        new_event = self.call("/api/events/reassign", {"suggestion_id": keep, "event_id": None})["event_id"]
        self.assertNotEqual(new_event, old_event)
        self.assertEqual(self.call("/api/events/reassign", {"suggestion_id": keep, "event_id": old_event})["event_id"], old_event)

        # 8. export -----------------------------------------------------------
        info = self.call("/api/export/info")
        self.assertIn("SEG_LABEL", info["setup"])
        ex_all = self.call("/api/export", {"statuses": ["keep", "maybe", "reject", None]})
        ex_def = self.call("/api/export", {"statuses": ["keep", "maybe", None]})
        ex_keep = self.call("/api/export", {"statuses": ["keep"]})
        rows = lambda ex: sum(f["rows"] for f in ex["files"])
        self.assertGreater(rows(ex_all), rows(ex_def))
        self.assertGreater(rows(ex_def), rows(ex_keep))
        self.assertGreater(rows(ex_keep), 0)
        self.assertEqual({f["reviewer"] for f in ex_keep["files"]}, {"claude"} if keep.startswith("claude") else {"chatgpt"})
        # a narrower re-export must not leave CSVs from the wider one behind
        exports = self.folder / "autoclipper_exports"
        (exports / "notes.txt").write_text("user file", encoding="utf-8")
        on_disk = {str(p.resolve()) for p in exports.glob("*/*.csv")}
        self.assertEqual(on_disk, {str(Path(f["csv_path"]).resolve()) for f in ex_keep["files"]})
        self.assertTrue((exports / "notes.txt").exists())
        ex = self.call("/api/export", {"statuses": ["keep", "maybe", None]})
        for f in ex["files"]:
            p = Path(f["csv_path"])
            self.assertTrue(p.exists(), p)
            self.assertTrue(p.resolve().is_relative_to((self.folder / "autoclipper_exports").resolve()))
            self.assertEqual(len([ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]), f["rows"])
            self.assertTrue(self.call("/api/open-path", {"path": f["folder"]})["ok"])
        self.assertTrue(any(f["pass_no"] > 1 for f in ex_all["files"]), "overlapping suggestions need a 2nd pass")
        names = [n for lst in ex["expected"].values() for n in lst]
        self.assertTrue(any(n.startswith("NB-") and n.endswith(" - Cindy.mkv") or "Cindy (1 of 2)" in n for n in names))

        # 9. delete a reviewer -------------------------------------------------
        r = self.call("/api/reviews/delete", {"reviewer": "chatgpt"})
        self.assertGreater(r["deleted"], 0)
        self.assertEqual([x["reviewer"] for x in r["reviewers"]], ["claude"])
        self.assertFalse([s for s in r["session"]["suggestions"] if s["reviewer"] == "chatgpt"])
        self.assertTrue(all(sid.startswith("claude") for e in r["session"]["events"] for sid in e["suggestion_ids"]))

        # 10. persisted state reloads in a brand-new server ------------------
        before = self.call("/api/session")["session"]
        type(self).stop_server()
        type(self).start_server()
        self.call("/api/session", expect=409)
        r = self.call("/api/session/open", {"folder": str(self.folder)})
        after = r["session"]
        self.assertEqual(after["suggestions"], before["suggestions"])
        self.assertEqual(after["events"], before["events"])
        self.assertEqual(after["reviews"][keep]["status"], "keep")
        self.assertEqual(after["reviews"][keep]["notes"], "great")
        self.assertEqual(self.parts(r)["andres-p2"]["offset_source"], "manual")
        self.assertEqual(after["game"], "Test game")


class TestUntimedSuggestions(ServerTestCase):
    @unittest.skipUnless(HAS_REVIEWS, "autoclipper.reviews not merged yet")
    def test_untimed_ideas_are_flagged_and_never_exported(self):
        self.create_session()
        for pid, dur in self.durations.items():
            self.call("/api/parts/update", {"part_id": pid, "duration": dur})
        self.call("/api/align/chain", {})
        review = {"reviewer": "Tester", "suggestions": [
            {"id": "T-1", "title": "Timed clip", "start": "00:01:00", "end": "00:01:10", "kind": "clip_now"},
            {"id": "T-2", "title": "Idea without time", "kind": "clip_now", "what_happens": "stage a race"},
            {"id": "T-3", "title": "Filming idea", "kind": "needs_filming"}]}
        self.call("/api/reviews/import", {"reviewer_name": "Tester", "text": json.dumps(review)})
        g = self.call("/api/guide")
        by_title = {s["title"]: s for s in g["suggestions"]}
        self.assertFalse(by_title["Timed clip"]["untimed"])
        self.assertTrue(by_title["Timed clip"]["ranges"])
        for title in ("Idea without time", "Filming idea"):
            self.assertTrue(by_title[title]["untimed"], title)
            self.assertEqual(by_title[title]["ranges"], [])
        ex = self.call("/api/export", {"statuses": ["keep", "maybe", "reject", None]})
        labels = [lbl for f in ex["files"] for _, _, lbl in
                  (ln.rsplit(",", 2) for ln in Path(f["csv_path"]).read_text(encoding="utf-8").splitlines() if ln)]
        self.assertTrue(any("Timed clip" in lbl for lbl in labels))
        self.assertFalse([lbl for lbl in labels if "Idea without time" in lbl or "Filming idea" in lbl])
        for f in ex["files"]:
            for ln in Path(f["csv_path"]).read_text(encoding="utf-8").splitlines():
                start, end, _ = ln.split(",", 2)
                self.assertLess(float(start), float(end), ln)
        names = [n for lst in ex["expected"].values() for n in lst]
        self.assertFalse([n for n in names if "Idea without time" in n])


class TestPartsEditing(ServerTestCase):
    def test_move_part_between_povs(self):
        self.create_session()
        r = self.call("/api/parts/move", {"part_id": "rika-p1", "pov_name": "Guest"})
        povs = {p["name"]: p for p in r["session"]["povs"]}
        self.assertIn("Guest", povs)
        self.assertNotIn("Rika", povs)  # empty POV removed
        r = self.call("/api/parts/update", {"part_id": "rika-p1", "pov_name": "Cindy"})
        cindy = next(p for p in r["session"]["povs"] if p["name"] == "Cindy")
        self.assertEqual([p["index"] for p in cindy["parts"]], [1, 2, 3, 4])
        r = self.call("/api/parts/update", {"part_id": "rika-p1", "pov_name": "Rika", "index": 1})
        self.assertEqual(len(next(p for p in r["session"]["povs"] if p["name"] == "Cindy")["parts"]), 3)
        r = self.call("/api/parts/update", {"part_id": "rika-p1", "duration": None})
        self.assertIsNone(self.parts(r)["rika-p1"]["duration"])
        self.assertIsNotNone(self.parts(r)["rika-p1"].get("duration_estimate"))


if __name__ == "__main__":
    unittest.main()
