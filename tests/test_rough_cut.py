"""AI rough cut: prompt export, strict import validation, storage, builder and API (synthetic only)."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from autoclipper import rough_cut as rc
from autoclipper.models import Part, POV, Review, Session, Suggestion
from autoclipper.resolve_builder import build_export
from autoclipper.server import make_server
from autoclipper.store import save_session

from director_fixtures import confirmed_mics, synthetic_probe
from test_resolve_builder import fake_resolve

SRT = {
    "alpha-1": [(1, 3, "Alpha: Throw the crate down the slide"), (5, 7, "Alpha: No way that is going to work"),
                (9, 10.5, "Alpha: Watch this it is so good"), (16, 18, "Alpha: The whole bridge just collapsed")],
    "bravo-1": [(6.5, 8, "Bravo: Who left the door open again")],
}


def _ts(seconds):
    whole = int(seconds)
    return f"00:00:{whole:02d},{round((seconds - whole) * 1000):03d}"


def make_fixture(folder, *, charlie=False):
    """Alpha covers 0-20 s, Bravo 5-15 s; Charlie (optional) has no MP4 and stays excluded."""
    folder.mkdir(parents=True, exist_ok=True)
    povs = [POV("alpha", "Alpha", [Part("alpha-1", 1, "alpha-1.mp4", "alpha-1.srt", duration=20, offset=0)]),
            POV("bravo", "Bravo", [Part("bravo-1", 1, "bravo-1.mp4", "bravo-1.srt", duration=10, offset=5)])]
    if charlie:
        povs.append(POV("charlie", "Charlie", [Part("charlie-1", 1, "charlie-1.mp4", None, duration=20, offset=0)]))
    session = Session(session_id="synthetic-cut", folder=str(folder), povs=povs)
    for stem, cues in SRT.items():
        (folder / f"{stem}.mp4").write_bytes(b"synthetic " + stem.encode())
        (folder / f"{stem}.srt").write_text("".join(
            f"{n}\n{_ts(a)} --> {_ts(b)}\n{text}\n\n" for n, (a, b, text) in enumerate(cues, 1)), encoding="utf-8")
    session.suggestions.append(Suggestion("claude-01", "claude", "Claude", "EV001", "Door chaos", start=11, end=13.5))
    session.reviews["claude-01"] = Review("keep", approved_start=10.5, approved_end=14)
    return session


def answer(export_id, clips, **extra):
    return json.dumps({"schema": rc.SCHEMA, "export_id": export_id, "title": "Synthetic cut", "clips": clips, **extra})


GOOD_CLIPS = [
    {"id": "C01", "kind": "source", "title": "Door", "start_line": "L0004", "end_line": "L0004",
     "pad_before": 0.5, "pad_after": 0.5, "pov": "bravo", "audio_pov": None,
     "quotes": [{"line": "L0004", "text": "who left the door open"}]},
    {"id": "P01", "kind": "placeholder", "title": "Title card", "seconds": 2},
    {"id": "C02", "kind": "source", "title": "Crate", "start_line": "L0001", "end_line": "L0002",
     "pov": None, "audio_pov": "alpha", "quotes": [{"line": "L0002", "text": "No way that is going to work"}]},
    {"id": "C03", "kind": "source", "title": "Again", "suggestion_id": "claude-01"},
]


class RoughCutCoreTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.session = make_fixture(self.root / "session", charlie=True)
        self.confirmations = confirmed_mics(self.session)
        probe = synthetic_probe(replace(self.session, povs=self.session.povs[:2]), 60)
        probe.start()
        self.addCleanup(probe.stop)

    def context(self, session=None):
        return rc.prepare(session or self.session, confirmations=self.confirmations)

    def test_prompt_is_deterministic_and_lists_transcript_coverage_and_suggestions(self):
        context = self.context()
        prompt = rc.build_prompt(context, brief="Short and funny", target_seconds=30)
        self.assertEqual(prompt, rc.build_prompt(self.context(), brief="Short and funny", target_seconds=30))
        for text in (rc.prompt_export_id(context, brief="Short and funny", target_seconds=30),
                     "L0004 00:00:11 Bravo: Who left the door open again",
                     '"bravo" (Bravo): video + own mic verified during 00:00:05.000-00:00:15.000',
                     "claude-01 [keep] 00:00:10.500-00:00:14.000", "charlie-1 (mp4_missing)", "Short and funny"):
            self.assertIn(text, prompt)
        self.assertRegex(context["export_id"], r"^rc-[0-9a-f]{16}$")
        self.assertFalse((self.root / "session" / ".autoclipper").exists())

    def test_valid_answer_builds_ordered_multisegment_plan_with_continuous_reference(self):
        context = self.context()
        result = rc.validate_response(context, "Sure! Here it is:\n```json\n" + answer(context["export_id"], GOOD_CLIPS)
                                      + "\n```\nEnjoy.")
        self.assertTrue(result["ok"], result["errors"])
        entries = result["plan"]["entries"]
        self.assertEqual([e["entry_id"] for e in entries], ["C01", "P01", "C02", "C03"])
        self.assertEqual((entries[0]["session_in"], entries[0]["session_out_exclusive"]), (660, 810))
        self.assertEqual((entries[2]["session_in"], entries[2]["session_out_exclusive"]), (60, 420))
        self.assertEqual((entries[3]["session_in"], entries[3]["session_out_exclusive"]), (630, 840))
        self.assertEqual(entries[3]["candidate_ids"], ["claude-01"])
        self.assertEqual(entries[1]["provisional_frames"], 120)
        manifest = result["manifest"]
        self.assertEqual(manifest["mode"], "director_edit_plan")
        self.assertEqual(manifest["blockers"], [])
        self.assertIn("part_excluded", {w["code"] for w in manifest["warnings"]})
        segments = manifest["segments"]
        # C02 splits where Bravo's coverage starts (5 s); the clip order is the AI's, not session order.
        self.assertEqual([s["event_id"] for s in segments], ["C01", "P01", "C02", "C02", "C03"])
        self.assertEqual([s["record_start_frame"] for s in segments], [0, 150, 270, 510, 630])
        self.assertEqual([a["pov_id"] for a in segments[0]["angles"]], ["bravo"])
        self.assertEqual([a["pov_id"] for a in segments[2]["angles"]], ["alpha"])
        self.assertEqual([a["pov_id"] for a in segments[3]["angles"]], ["alpha", "bravo"])
        self.assertEqual([segments[i]["audio_angle"]["source_in_frame"] for i in (2, 3)], [60, 300])
        self.assertEqual([a["pov_id"] for a in segments[4]["angles"]], ["alpha", "bravo"])
        # One group mix POV for the whole cut, taken from a covering angle even when Bravo is shown alone.
        refs = [s["reference_audio"] for s in segments if s["kind"] == "media"]
        self.assertEqual({r["pov_id"] for r in refs}, {"alpha"})
        self.assertEqual(segments[0]["reference_angle"]["pov_id"], "alpha")
        self.assertEqual((refs[0]["source_in_frame"], refs[0]["record_start_frame"]), (660, 0))
        self.assertNotIn("reference_audio", segments[1])
        self.assertIn("GROUP REFERENCE AUDIO", result["report"])
        self.assertIn("| 1 | C01 | Door | lines |", result["report"])

    def test_errors_are_collected_into_a_repair_prompt(self):
        context = self.context()
        clips = [
            {"id": "C01", "kind": "source", "title": "Wrong cam", "start_line": "L0001", "end_line": "L0001",
             "pov": "bravo", "quotes": [{"line": "L0001", "text": "Throw the crate down the slide"}]},
            {"id": "C01", "kind": "source", "title": "Dup", "start_line": "L0009", "end_line": "L0002",
             "quotes": [{"line": "L0002", "text": "invented words"}]},
            {"id": "C03", "kind": "source", "title": "Bad mic", "start_line": "L0003", "end_line": "L0003",
             "audio_pov": "delta", "pad_after": 30, "start": "00:00:01",
             "quotes": [{"line": "L0003", "text": "Watch this"}]},
            {"id": "C04", "kind": "source", "title": "Quote elsewhere", "start_line": "L0003", "end_line": "L0003",
             "quotes": [{"line": "L0005", "text": "The whole bridge just collapsed"}]},
            {"id": "C05", "kind": "source", "title": "No quote", "start_line": "L0005", "end_line": "L0005"},
            {"id": "C06", "kind": "source", "title": "Excluded", "start_line": "L0005", "end_line": "L0005",
             "pov": "charlie", "quotes": [{"line": "L0005", "text": "bridge just collapsed"}]},
            {"id": "P01", "kind": "placeholder", "title": "Long", "seconds": 90},
        ]
        result = rc.validate_response(context, answer(context["export_id"], clips))
        self.assertFalse(result["ok"])
        text = "\n".join(result["errors"])
        for needle in ("Bravo (\"bravo\") has no verified video during 00:00:01.000-00:00:03.000",
                       "duplicate id", "\"start_line\" 'L0009' is not a transcript line id",
                       "text is not verbatim from L0002", "\"audio_pov\" must be null or one of",
                       "\"pad_after\" must be a number", "unsupported keys start",
                       "L0005 (00:00:16.000) is outside the clip", "give at least one verbatim quote",
                       "Charlie (\"charlie\") has no verified video", "placeholder \"seconds\""):
            self.assertIn(needle, text)
        self.assertIn(context["export_id"], result["repair_prompt"])
        self.assertIn("PROBLEMS", result["repair_prompt"])
        self.assertNotIn("plan", result)

    def test_limits_anchor_rules_and_frame_timecode_cross_check(self):
        context = self.context()
        one = [GOOD_CLIPS[0]]
        self.assertIn("at least 2 source clips", "\n".join(rc.validate_response(
            context, answer(context["export_id"], one))["errors"]))
        frames = {"id": "F1", "kind": "source", "title": "Frames", "session_in_frame": 60, "session_out_frame": 180,
                  "start_time": "00:00:01.000", "end_time": "00:00:03.000",
                  "quotes": [{"line": "L0001", "text": "Throw the crate"}]}
        ok = rc.validate_response(context, answer(context["export_id"], [frames, GOOD_CLIPS[0]]))
        self.assertTrue(ok["ok"], ok["errors"])
        self.assertEqual(ok["plan"]["entries"][0]["session_in"], 60)
        wrong = {**frames, "end_time": "00:00:04.000"}
        mixed = {**frames, "id": "F2", "start_line": "L0001", "end_line": "L0001"}
        short = {**frames, "id": "F3", "session_out_frame": 70, "end_time": "00:00:01.167"}
        outside = {**frames, "id": "F4", "session_in_frame": 1150, "session_out_frame": 1300,
                   "start_time": "00:00:19.167", "end_time": "00:00:21.667"}
        result = rc.validate_response(context, answer(context["export_id"], [wrong, mixed, short, outside]))
        text = "\n".join(result["errors"])
        for needle in ("is frame 240, which disagrees with frame 180", "not both", "clips must be 0.5-300 s",
                       "outside the session window"):
            self.assertIn(needle, text)

    def test_stale_or_missing_export_id(self):
        context = self.context()
        moved = replace(self.session, povs=[self.session.povs[0], replace(self.session.povs[1], parts=[
            replace(self.session.povs[1].parts[0], offset=5.5)]), self.session.povs[2]])
        stale = rc.validate_response(self.context(moved), answer(context["export_id"], GOOD_CLIPS))
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["repair_prompt"], "")
        self.assertIn("Copy a new prompt", stale["errors"][0])
        missing = rc.validate_response(context, answer("from the prompt", GOOD_CLIPS))
        self.assertFalse(missing["stale"])
        self.assertIn("must be copied exactly", missing["errors"][0])
        self.assertIn(context["export_id"], missing["repair_prompt"])

    def test_prompt_options_are_bound_to_export_id(self):
        context = self.context()
        alternate = rc.prompt_export_id(context, brief="A different brief", target_seconds=45)
        self.assertNotEqual(alternate, context["export_id"])
        self.assertIn(alternate, rc.build_prompt(context, brief="A different brief", target_seconds=45))
        stale = rc.validate_response(context, answer(context["export_id"], GOOD_CLIPS),
                                     brief="A different brief", target_seconds=45)
        self.assertTrue(stale["stale"])
        self.assertIn("prompt options", stale["errors"][0])
        accepted = rc.validate_response(context, answer(alternate, GOOD_CLIPS),
                                        brief="A different brief", target_seconds=45)
        self.assertTrue(accepted["ok"], accepted["errors"])
        self.assertEqual(accepted["manifest"]["rough_cut"]["brief"], "A different brief")

    def test_nonfinite_suggestion_is_untimed_in_prompt_and_import(self):
        self.session.reviews["claude-01"].approved_end = float("inf")
        context = self.context()
        self.assertNotIn("claude-01 [keep]", rc.build_prompt(context))
        result = rc.validate_response(context, answer(context["export_id"], GOOD_CLIPS))
        self.assertFalse(result["ok"])
        self.assertIn("has no timed range", "\n".join(result["errors"]))

    def test_one_character_quote_is_not_verbatim_evidence(self):
        context = self.context()
        clips = [{**GOOD_CLIPS[0], "quotes": [{"line": "L0004", "text": "o"}]}, GOOD_CLIPS[2]]
        result = rc.validate_response(context, answer(context["export_id"], clips))
        self.assertFalse(result["ok"])
        self.assertIn("not verbatim", "\n".join(result["errors"]))

    def test_line_anchor_keeps_later_start_line_end(self):
        context = self.context()
        context["line_index"]["L0001"] = replace(context["line_index"]["L0001"], session_end=8)
        clips = [{**GOOD_CLIPS[2], "start_line": "L0001", "end_line": "L0002"}, GOOD_CLIPS[0]]
        result = rc.validate_response(context, answer(context["export_id"], clips))
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["plan"]["entries"][0]["session_out_exclusive"], 480)

    def test_nonpart_compile_blocker_remains_fatal(self):
        from autoclipper import resolve_manifest as rm
        context = self.context()
        compile_plan = rm.compile_edit_plan

        def with_blocker(*args, **kwargs):
            manifest = compile_plan(*args, **kwargs)
            manifest["blockers"].append({"code": "fatal", "part_id": None, "message": "Fatal mapping error"})
            return manifest

        with patch.object(rm, "compile_edit_plan", side_effect=with_blocker):
            result = rc.validate_response(context, answer(context["export_id"], GOOD_CLIPS))
        self.assertFalse(result["ok"])
        self.assertIn("Fatal mapping error", "\n".join(result["errors"]))

    def test_pov_names_are_not_accepted_as_source_ids(self):
        context = self.context()
        clips = [{**GOOD_CLIPS[0], "pov": "Bravo"}, GOOD_CLIPS[2]]
        result = rc.validate_response(context, answer(context["export_id"], clips))
        self.assertFalse(result["ok"])
        self.assertIn('"pov" must be null or one of the pov ids', "\n".join(result["errors"]))

    def test_reference_audio_gap_rejects_entire_cut(self):
        from autoclipper import resolve_manifest as rm

        context = self.context()
        assign_reference = rm._edit_reference_audio

        def leave_gap(manifest, covered):
            assign_reference(manifest, covered)
            covered[0][0]["reference_audio"] = None

        with patch.object(rm, "_edit_reference_audio", side_effect=leave_gap):
            result = rc.validate_response(context, answer(context["export_id"], GOOD_CLIPS))
        self.assertFalse(result["ok"])
        self.assertNotIn("manifest", result)
        self.assertIn("No verified GROUP REFERENCE AUDIO", "\n".join(result["errors"]))
        self.assertTrue(result["repair_prompt"])

    def test_tolerant_extraction_and_unparseable_answer(self):
        context = self.context()
        smart = answer(context["export_id"], GOOD_CLIPS).replace('"title": "Synthetic cut"', "“title”: “Synthetic cut”")
        smart = smart.replace('"clips": [', '"clips": [').rstrip("}") + ",}"
        self.assertTrue(rc.validate_response(context, "Here you go\n" + smart)["ok"])
        bad = rc.validate_response(context, "I could not do it.")
        self.assertFalse(bad["ok"])
        self.assertIn('"clips" list', bad["errors"][0])
        with self.assertRaisesRegex(rc.RoughCutError, "Target length"):
            rc.validate_response(context, "{}", target_seconds="lots")

    def test_save_is_atomic_and_load_detects_tampering_and_links(self):
        context = self.context()
        text = answer(context["export_id"], GOOD_CLIPS)
        result = rc.validate_response(context, text)
        save_session(self.session)
        before = (self.root / "session" / ".autoclipper" / "session.json").read_bytes()
        cuts = rc.session_cuts_root(self.session)
        self.assertEqual(cuts, self.root / "session" / "autoclipper_exports" / "rough-cuts")
        folder = rc.save_cut(cuts, result, text)
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ["manifest.json", "plan.json", "prompt.txt", "report.md", "response.txt", "validation.json"])
        self.assertEqual([p.name for p in cuts.iterdir()], [folder.name])
        self.assertEqual((folder / "response.txt").read_text(encoding="utf-8"), text)
        _, manifest, record = rc.load_cut(cuts, folder.name)
        self.assertEqual(manifest["rough_cut"]["export_id"], record["export_id"])
        self.assertEqual(before, (self.root / "session" / ".autoclipper" / "session.json").read_bytes())
        second = rc.save_cut(cuts, result, text)
        self.assertNotEqual(second, folder)
        with patch("autoclipper.rough_cut.os.rename", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            rc.save_cut(cuts, result, text)
        self.assertEqual(len(list(cuts.iterdir())), 2)  # the failed save left nothing behind
        with self.assertRaisesRegex(rc.RoughCutError, "Only a validated"):
            rc.save_cut(cuts, {"ok": False}, text)
        (folder / "manifest.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(rc.RoughCutError, "changed after validation"):
            rc.load_cut(cuts, folder.name)
        for bad in ("..", "../x", "nope", None):
            with self.assertRaisesRegex(rc.RoughCutError, "Unknown"):
                rc.load_cut(cuts, bad)
        with patch("autoclipper.rough_cut._is_link", side_effect=lambda p: Path(p).name == "rough-cuts"):
            with self.assertRaisesRegex(rc.RoughCutError, "link or junction"):
                rc.session_cuts_root(self.session)

    def test_reference_audio_build_reads_back_after_reopen(self):
        context = self.context()
        clips = [{**GOOD_CLIPS[0], "pad_before": 0, "pad_after": 0}, GOOD_CLIPS[2]]
        result = rc.validate_response(context, answer(context["export_id"], clips))
        self.assertTrue(result["ok"], result["errors"])
        sdk, _, state, project, reopened = fake_resolve()
        pool = project.GetMediaPool.return_value
        for timeline in (pool.CreateEmptyTimeline.return_value, reopened.GetTimelineByIndex.return_value):
            timeline.GetName.return_value = "synthetic-cut"
        import_media = pool.ImportMedia.side_effect

        def import_long(paths):  # the fake reports 600 frames; the synthetic Alpha MP4 has 1200
            clip, = import_media(paths)
            properties = clip.GetClipProperty.side_effect
            clip.GetClipProperty.side_effect = lambda key: "1200" if key == "Frames" else properties(key)
            return [clip]

        pool.ImportMedia.side_effect = import_long
        with patch.dict(sys.modules, {"DaVinciResolveScript": sdk}), \
                patch("autoclipper.resolve_builder.mkdtemp", return_value=str(self.root)):
            readback = build_export(result["manifest"])
        self.assertEqual(state["track_name"], "GROUP REFERENCE AUDIO")
        self.assertEqual(len(state["audio2"]), 3)
        self.assertEqual(len(state["reference_media"]), 1)  # one Alpha group-mix item for the whole cut
        rows = readback["segments"]
        self.assertEqual([r["reference_audio"]["source_start"] for r in rows], [690, 60, 300])
        self.assertEqual([r["reference_audio"]["record_start"] for r in rows], [216000, 216090, 216330])
        self.assertEqual(rows[1]["audio"]["path"], str((self.root / "session" / "alpha-1.mp4").resolve()))
        self.assertEqual(len(readback["sources"]), 2)
        self.assertEqual(readback["edit_plan"]["plan_id"], f"rough-cut-{context['export_id']}")

    def test_director_edit_plans_are_unchanged_without_reference_flag(self):
        from autoclipper.resolve_manifest import _review_coverage, compile_edit_plan
        plan = {"plan_id": "p", "revision_id": "r", "revision_history": [], "session_id": "synthetic-cut",
                "fps": 60, "entries": [{"entry_id": "a", "kind": "source", "title": "A",
                                        "session_in": 60, "session_out_exclusive": 120}]}
        manifest = compile_edit_plan(self.session, plan, confirmations=self.confirmations)
        self.assertFalse(any("reference_audio" in s or "reference_angle" in s for s in manifest["segments"]))
        self.assertNotIn("reference_track", manifest["segments"][0]["angles"][0])
        plain, *_ = _review_coverage(self.session, None, self.confirmations, 60)
        self.assertEqual(manifest["source_mapping_digest"], plain["source_mapping_digest"])
        self.assertNotEqual(plain["source_mapping_digest"], self.context()["source_mapping_digest"])

    def test_builder_rejects_unmatched_or_orphaned_reference(self):
        from autoclipper.resolve_builder import validate_manifest
        context = self.context()
        manifest = rc.validate_response(context, answer(context["export_id"], GOOD_CLIPS))["manifest"]
        segment = manifest["segments"][0]
        segment["reference_audio"]["source_in_frame"] += 1
        with self.assertRaisesRegex(ValueError, "Reference audio must match"):
            validate_manifest(manifest)
        segment["reference_audio"]["source_in_frame"] -= 1
        del segment["reference_audio"]
        with self.assertRaisesRegex(ValueError, "needs its GROUP REFERENCE AUDIO"):
            validate_manifest(manifest)


class RoughCutApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ac_rough_ui_"))
        self._env = {k: os.environ.get(k) for k in ("APPDATA", "AUTOCLIPPER_NO_STARTFILE")}
        os.environ["APPDATA"] = str(self.tmp / "appdata")
        os.environ["AUTOCLIPPER_NO_STARTFILE"] = "1"
        self.folder = self.tmp / "session"
        session = make_fixture(self.folder)
        save_session(session)
        (self.folder / ".autoclipper" / "mic_confirmations.json").write_text(
            json.dumps({"schema_version": 1, "confirmations": confirmed_mics(session)}), encoding="utf-8")
        self.probe = synthetic_probe(session, 60)
        self.probe.start()
        self.httpd = make_server(0)
        self.app = self.httpd.app
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.call("/api/session/open", {"folder": str(self.folder)})
        self.session_json = (self.folder / ".autoclipper" / "session.json").read_bytes()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.probe.stop()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, path, body=None, expect=200):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                status, payload = res.status, res.read()
        except urllib.error.HTTPError as err:
            status, payload = err.code, err.read()
        self.assertEqual(status, expect, payload[:800])
        return json.loads(payload.decode("utf-8"))

    def stub_builder(self, gate=None):
        calls = []

        def run(manifest_path, project, result_path):
            calls.append((manifest_path, project, result_path))
            if gate is not None:
                gate.wait(10)
            result_path.write_text(json.dumps({"project": project, "reopened": True}), encoding="utf-8")
            return 0, "", ""

        self.app.run_builder = run
        return calls

    def wait_job(self):
        for _ in range(250):
            job = self.call("/api/resolve/status")
            if job["state"] != "running":
                return job
            time.sleep(0.02)
        self.fail("rough-cut job did not finish")

    def test_prompt_import_and_build_flow_never_writes_session_json(self):
        info = self.call("/api/rough-cut/prompt", {"brief": "Quick", "target_seconds": "20"})
        self.assertIn(info["export_id"], info["prompt"])
        self.assertEqual(info["excluded"], [])
        self.assertEqual(info["filename"], "synthetic-cut_rough_cut_prompt.txt")
        cuts = self.folder / "autoclipper_exports" / "rough-cuts"

        rejected = self.call("/api/rough-cut/import", {"text": answer(info["export_id"], GOOD_CLIPS[:1]),
                                                       "brief": "Quick", "target_seconds": "20"})
        self.assertFalse(rejected["ok"])
        self.assertTrue(rejected["repair_prompt"])
        self.assertFalse(cuts.exists())

        saved = self.call("/api/rough-cut/import", {"text": answer(info["export_id"], GOOD_CLIPS),
                                                    "brief": "Quick", "target_seconds": "20"})
        self.assertTrue(saved["ok"], saved["errors"])
        self.assertEqual(saved["segments"], 5)
        self.assertEqual(saved["reference_gaps"], 0)
        self.assertTrue((cuts / saved["cut_id"] / "manifest.json").is_file())
        prompt = (cuts / saved["cut_id"] / "prompt.txt").read_text(encoding="utf-8")
        self.assertEqual(prompt, info["prompt"])

        gate = threading.Event()
        calls = self.stub_builder(gate)
        job = self.call("/api/rough-cut/build", {"cut_id": saved["cut_id"]})
        self.assertEqual(job["kind"], "rough_cut")
        self.assertRegex(job["project"], r"^AutoClipper Export synthetic-cut \d{8}-\d{6}$")
        busy = self.call("/api/rough-cut/build", {"cut_id": saved["cut_id"]}, expect=409)
        self.assertIn("already running", busy["error"])
        self.call("/api/resolve/export", {}, expect=409)  # the one Resolve job slot is shared
        gate.set()
        done = self.wait_job()
        self.assertEqual(done["state"], "done")
        manifest_path, project, result_path = calls[0]
        self.assertEqual(manifest_path, cuts / saved["cut_id"] / "manifest.json")
        self.assertEqual(result_path.parent.parent, cuts / saved["cut_id"])
        self.assertEqual(self.session_json, (self.folder / ".autoclipper" / "session.json").read_bytes())

    def test_add_timeline_mode_passes_builder_flags_and_default_does_not(self):
        info = self.call("/api/rough-cut/prompt", {})
        saved = self.call("/api/rough-cut/import", {"text": answer(info["export_id"], GOOD_CLIPS)})
        seen = []

        def run(manifest_path, project, result_path, extra_args=None):
            seen.append((project, extra_args))
            result_path.write_text(json.dumps({"project": "open", "added_timeline": True}), encoding="utf-8")
            return 0, "", ""

        self.app.run_builder = run
        self.call("/api/rough-cut/build", {"cut_id": saved["cut_id"], "mode": "bogus"}, expect=400)
        self.assertEqual(self.call("/api/resolve/status")["state"], "idle")
        job = self.call("/api/rough-cut/build", {"cut_id": saved["cut_id"], "mode": "add_timeline"})
        self.assertEqual(job["mode"], "add_timeline")
        self.assertTrue(job["timeline_name"].endswith(saved["cut_id"]))
        for ch in ':,/*?"<>|;':
            self.assertNotIn(ch, job["timeline_name"])
        self.assertEqual(self.wait_job()["state"], "done")
        time.sleep(1.1)  # build folders are stamped to the second
        self.call("/api/rough-cut/build", {"cut_id": saved["cut_id"]})
        self.assertEqual(self.wait_job()["state"], "done")
        self.assertEqual(seen[0][1][:2], ("--add-to-open", "--timeline-name"))
        self.assertEqual(seen[0][1][2], job["timeline_name"])
        self.assertRegex(seen[1][0], r"^AutoClipper Export synthetic-cut \d{8}-\d{6}$")
        self.assertIsNone(seen[1][1])

    def test_run_builder_add_timeline_command_line_has_no_project_argument(self):
        with patch("autoclipper.server.subprocess.Popen") as popen:
            popen.return_value.communicate.return_value = ("{}", "")
            popen.return_value.returncode = 0
            self.app.run_builder(Path("m.json"), "ignored", Path("r.json"),
                                 extra_args=("--add-to-open", "--timeline-name", "T"))
        command = popen.call_args.args[0]
        self.assertNotIn("--project", command)
        self.assertEqual(command[-6:], ["--execute", "--add-to-open", "--timeline-name", "T",
                                        "--result-file", "r.json"])

    # ------------------------------------------------------------------ bulk import and batch build
    CUT_B = [GOOD_CLIPS[0], GOOD_CLIPS[2]]
    CUT_C = [GOOD_CLIPS[2], GOOD_CLIPS[3]]

    def cut(self, export_id, clips, title):
        return {"schema": rc.SCHEMA, "export_id": export_id, "title": title, "clips": clips}

    def test_several_pasted_objects_save_one_cut_each(self):
        info = self.call("/api/rough-cut/prompt", {})
        text = ("Here are the cuts:\n```json\n" + json.dumps(self.cut(info["export_id"], GOOD_CLIPS, "One"))
                + "\n```\nand\n```json\n" + json.dumps(self.cut(info["export_id"], self.CUT_B, "Two")) + "\n```")
        out = self.call("/api/rough-cut/import", {"text": text})
        self.assertTrue(out["bulk"] and out["ok"], out)
        self.assertEqual([c["title"] for c in out["saved"]], ["One", "Two"])
        self.assertEqual(len({c["cut_id"] for c in out["saved"]}), 2)
        self.assertEqual(out["rejected"], [])
        cuts = self.folder / "autoclipper_exports" / "rough-cuts"
        for saved in out["saved"]:
            self.assertTrue((cuts / saved["cut_id"] / "manifest.json").is_file())
        self.assertEqual(self.session_json, (self.folder / ".autoclipper" / "session.json").read_bytes())

    def test_wrapper_object_with_cuts(self):
        info = self.call("/api/rough-cut/prompt", {})
        wrapper = {"cuts": [self.cut(info["export_id"], GOOD_CLIPS, "A"), self.cut(info["export_id"], self.CUT_B, "B"),
                            self.cut(info["export_id"], self.CUT_C, "C")]}
        out = self.call("/api/rough-cut/import", {"text": json.dumps(wrapper)})
        self.assertEqual([c["title"] for c in out["saved"]], ["A", "B", "C"])
        self.assertEqual(len({c["cut_id"] for c in out["saved"]}), 3)

    def test_one_bad_cut_does_not_block_the_good_ones(self):
        info = self.call("/api/rough-cut/prompt", {})
        bad = self.cut(info["export_id"], [{**GOOD_CLIPS[0], "quotes": [{"line": "L0004", "text": "invented"}]},
                                          GOOD_CLIPS[2]], "Bad")
        stale = self.cut("rc-0123456789abcdef", GOOD_CLIPS, "Stale")
        wrapper = {"cuts": [self.cut(info["export_id"], GOOD_CLIPS, "Good"), bad, stale, "nonsense",
                            self.cut(info["export_id"], self.CUT_B, "Good2")]}
        out = self.call("/api/rough-cut/import", {"text": json.dumps(wrapper)})
        self.assertTrue(out["ok"])
        self.assertEqual([c["title"] for c in out["saved"]], ["Good", "Good2"])
        self.assertEqual([(r["number"], r["stale"]) for r in out["rejected"]],
                         [(2, False), (3, True), (4, False)])
        self.assertIn("Cut #2 (Bad)", out["repair_prompt"])
        self.assertNotIn("Stale", out["repair_prompt"])
        self.assertIn('{"cuts": [...]}', out["repair_prompt"])
        self.assertIn("other cuts were saved", out["repair_prompt"])
        cuts = self.folder / "autoclipper_exports" / "rough-cuts"
        self.assertEqual(len([p for p in cuts.iterdir() if not p.name.startswith(".")]), 2)

    def test_all_bad_cuts_save_nothing(self):
        info = self.call("/api/rough-cut/prompt", {})
        wrapper = {"cuts": [self.cut(info["export_id"], [GOOD_CLIPS[0]], "Short"),
                            self.cut(info["export_id"], [GOOD_CLIPS[2]], "Short2")]}
        out = self.call("/api/rough-cut/import", {"text": json.dumps(wrapper)})
        self.assertFalse(out["ok"])
        self.assertEqual(out["saved"], [])
        self.assertEqual(len(out["rejected"]), 2)
        self.assertFalse((self.folder / "autoclipper_exports" / "rough-cuts").exists()
                         and any((self.folder / "autoclipper_exports" / "rough-cuts").iterdir()))

    def test_unparseable_pasted_answer_is_rejected_not_dropped(self):
        info = self.call("/api/rough-cut/prompt", {})
        good = json.dumps(self.cut(info["export_id"], GOOD_CLIPS, "Good"))
        broken = json.dumps(self.cut(info["export_id"], self.CUT_B, "Broken"))[:-20]
        out = self.call("/api/rough-cut/import", {"text": good + "\n" + good.replace("Good", "Good2") + "\n" + broken})
        self.assertEqual(len(out["saved"]), 2)
        self.assertEqual((out["found"], out["parsed"]), (3, 2))
        self.assertEqual([r["label"] for r in out["rejected"]], ["Unparsed answer 1 of 1"])
        self.assertIn("only 2 could be parsed", out["rejected"][0]["errors"][0])
        self.assertIn("Unparsed answer 1 of 1", out["repair_prompt"])

    def test_each_pasted_answer_gets_its_own_tolerant_fixups(self):
        info = self.call("/api/rough-cut/prompt", {})
        eid = info["export_id"]
        trailing = json.dumps(self.cut(eid, GOOD_CLIPS, "Trailing comma"))[:-1] + ",}"
        smart = json.dumps(self.cut(eid, self.CUT_B, "Smart quotes")).replace('"', "”")
        clean = json.dumps(self.cut(eid, self.CUT_C, "Clean"))
        for order in ((clean, trailing), (trailing, clean), (smart, clean), (clean, smart), (trailing, smart, clean)):
            out = self.call("/api/rough-cut/import", {"text": "\n\n".join(order)})
            self.assertTrue(out["bulk"], order)
            self.assertEqual(len(out["saved"]), len(order), out["rejected"])
            self.assertEqual(out["rejected"], [])
            self.assertEqual(out["found"], out["parsed"])
            time.sleep(0.02)

    def test_a_paste_of_two_answers_never_falls_back_to_one(self):
        info = self.call("/api/rough-cut/prompt", {})
        eid = info["export_id"]
        good = json.dumps(self.cut(eid, GOOD_CLIPS, "Good"))
        hopeless = '{"schema": "x", "export_id": "' + eid + '", "clips": [ {"id": '
        out = self.call("/api/rough-cut/import", {"text": good + "\n" + hopeless})
        self.assertTrue(out["bulk"])
        self.assertEqual([c["title"] for c in out["saved"]], ["Good"])
        self.assertEqual((out["found"], out["parsed"], len(out["rejected"])), (2, 1, 1))

    def test_single_answer_path_is_unchanged(self):
        info = self.call("/api/rough-cut/prompt", {})
        out = self.call("/api/rough-cut/import", {"text": answer(info["export_id"], GOOD_CLIPS)})
        self.assertNotIn("bulk", out)
        self.assertTrue(out["ok"])
        self.assertIn("cut_id", out)
        bad = self.call("/api/rough-cut/import", {"text": answer(info["export_id"], [GOOD_CLIPS[0]])})
        self.assertNotIn("bulk", bad)
        self.assertFalse(bad["ok"])
        self.assertIsNone(rc.split_answers(answer(info["export_id"], GOOD_CLIPS)))
        self.assertIsNone(rc.split_answers("no json here"))

    def test_prompt_mentions_the_cuts_wrapper_once(self):
        prompt = self.call("/api/rough-cut/prompt", {})["prompt"]
        self.assertEqual(prompt.count('{"cuts": ['), 1)

    def stub_batch_builder(self, fail=(), gate=None):
        calls = []

        def run(manifest_path, project, result_path, extra_args=None):
            if gate is not None:
                gate.wait(10)
            calls.append((manifest_path.parent.name, extra_args))
            if len(calls) in fail:
                return 1, "", "Resolve refused this cut"
            result_path.write_text(json.dumps({"project": "open", "timeline": extra_args[2],
                                               "added_timeline": True}), encoding="utf-8")
            return 0, "", ""

        self.app.run_builder = run
        return calls

    def import_three(self):
        info = self.call("/api/rough-cut/prompt", {})
        wrapper = {"cuts": [self.cut(info["export_id"], GOOD_CLIPS, "Same title"),
                            self.cut(info["export_id"], self.CUT_B, "Same title"),
                            self.cut(info["export_id"], self.CUT_C, "Same title")]}
        return [c["cut_id"] for c in self.call("/api/rough-cut/import", {"text": json.dumps(wrapper)})["saved"]]

    def wait_batch(self):
        for _ in range(500):
            job = self.call("/api/resolve/status")
            if job["state"] not in ("running", "preparing"):
                return job
            time.sleep(0.02)
        self.fail("batch did not finish")

    def test_batch_builds_each_cut_sequentially_with_unique_timeline_names(self):
        ids = self.import_three()
        gate = threading.Event()
        calls = self.stub_batch_builder(gate=gate)
        job = self.call("/api/rough-cut/build-batch", {"cut_ids": ids})
        self.assertEqual(job["kind"], "rough_cut_batch")
        self.call("/api/rough-cut/build", {"cut_id": ids[0]}, expect=409)  # one job slot
        gate.set()
        done = self.wait_batch()
        self.assertEqual(done["state"], "done", done)
        self.assertEqual([c["state"] for c in done["cuts"]], ["done"] * 3)
        self.assertEqual([c[0] for c in calls], ids)  # in the order chosen, one at a time
        names = [c["timeline"] for c in done["cuts"]]
        self.assertEqual(len(set(n.casefold() for n in names)), 3)
        self.assertTrue(all(c[1][0] == "--add-to-open" for c in calls))
        self.assertTrue(all(c["readback_path"] for c in done["cuts"]))

    def test_batch_continues_after_a_failed_cut_and_reports_it(self):
        ids = self.import_three()
        calls = self.stub_batch_builder(fail={2})
        self.call("/api/rough-cut/build-batch", {"cut_ids": ids})
        done = self.wait_batch()
        self.assertEqual(len(calls), 3)
        self.assertEqual([c["state"] for c in done["cuts"]], ["done", "failed", "done"])
        self.assertIn("Resolve refused this cut", done["cuts"][1]["error"])
        self.assertEqual(done["state"], "failed")
        self.assertEqual(done["summary"], "Batch: 2 added, 1 failed.")

    def test_a_builder_timeout_stops_the_batch_and_marks_the_rest_not_started(self):
        import subprocess
        ids = self.import_three()
        calls = []

        def run(manifest_path, project, result_path, extra_args=None):
            calls.append(manifest_path.parent.name)
            if len(calls) == 2:
                raise subprocess.TimeoutExpired("builder", 1)
            result_path.write_text(json.dumps({"timeline": extra_args[2]}), encoding="utf-8")
            return 0, "", ""

        self.app.run_builder = run
        self.call("/api/rough-cut/build-batch", {"cut_ids": ids})
        done = self.wait_batch()
        self.assertEqual(calls, ids[:2])
        self.assertEqual([c["state"] for c in done["cuts"]], ["done", "failed", "not_started"])
        self.assertIn("timed out", done["cuts"][1]["error"])
        self.assertIn("stopped after a timeout", done["cuts"][2]["error"])
        self.assertEqual(done["state"], "failed")
        self.assertTrue(done["summary"].startswith("Batch: 1 added, 1 failed, 1 not started."), done["summary"])

    def test_ordinary_failures_still_continue_and_summary_counts_a_clean_batch(self):
        ids = self.import_three()
        self.stub_batch_builder()
        self.call("/api/rough-cut/build-batch", {"cut_ids": ids})
        self.assertEqual(self.wait_batch()["summary"], "Batch: 3 added, 0 failed.")

    def test_batch_rejects_bad_requests_and_stale_cuts(self):
        ids = self.import_three()
        calls = self.stub_batch_builder()
        for body in ({}, {"cut_ids": []}, {"cut_ids": [ids[0], ids[0]]}, {"cut_ids": [1]}, {"cut_ids": ["x"] * 13}):
            self.call("/api/rough-cut/build-batch", body, expect=400)
        self.call("/api/rough-cut/build-batch", {"cut_ids": ["../../x"]}, expect=409)
        self.assertEqual(self.call("/api/resolve/status")["state"], "idle")
        self.call("/api/session/meta", {"notes": "changed after validation"})
        self.call("/api/rough-cut/build-batch", {"cut_ids": ids})
        done = self.wait_batch()
        self.assertEqual([c["state"] for c in done["cuts"]], ["failed"] * 3)
        self.assertIn("changed since this rough cut was validated", done["cuts"][0]["error"])
        self.assertEqual(calls, [])

    def test_build_refuses_unknown_tampered_or_stale_cuts(self):
        info = self.call("/api/rough-cut/prompt", {})
        saved = self.call("/api/rough-cut/import", {"text": answer(info["export_id"], GOOD_CLIPS)})
        calls = self.stub_builder()
        self.call("/api/rough-cut/build", {"cut_id": "../../x"}, expect=409)
        folder = self.folder / "autoclipper_exports" / "rough-cuts" / saved["cut_id"]
        original = (folder / "manifest.json").read_bytes()
        (folder / "manifest.json").write_bytes(original.replace(b'"fps": 60', b'"fps": 30', 1))
        self.assertIn("changed after validation",
                      self.call("/api/rough-cut/build", {"cut_id": saved["cut_id"]}, expect=409)["error"])
        (folder / "manifest.json").write_bytes(original)
        validation = (folder / "validation.json").read_bytes()
        (folder / "validation.json").write_text("[]", encoding="utf-8")
        self.assertIn("validation record is malformed",
                      self.call("/api/rough-cut/build", {"cut_id": saved["cut_id"]}, expect=409)["error"])
        (folder / "validation.json").write_bytes(validation)
        self.call("/api/session/meta", {"notes": "edited after validation"})
        self.assertIn("changed since this rough cut was validated",
                      self.call("/api/rough-cut/build", {"cut_id": saved["cut_id"]}, expect=409)["error"])
        self.assertEqual(calls, [])
        self.assertEqual(self.call("/api/resolve/status")["state"], "idle")
        self.assertEqual(self.call("/api/rough-cut/import", {"text": " "}, expect=400)["error"],
                         "Paste the AI's JSON answer")

    def test_prompt_options_and_compile_io_fail_cleanly(self):
        info = self.call("/api/rough-cut/prompt", {"brief": "Original", "target_seconds": 30})
        self.assertIn(info["export_id"], info["prompt"])
        altered = self.call("/api/rough-cut/import", {"text": answer(info["export_id"], GOOD_CLIPS),
                                                      "brief": "Changed", "target_seconds": 30})
        self.assertTrue(altered["stale"])
        with patch("autoclipper.resolve_manifest.compile_edit_plan", side_effect=OSError("probe unavailable")):
            failed = self.call("/api/rough-cut/import", {"text": answer(info["export_id"], GOOD_CLIPS),
                                                       "brief": "Original", "target_seconds": 30}, expect=409)
        self.assertIn("Could not compile", failed["error"])
        self.assertFalse((self.folder / "autoclipper_exports" / "rough-cuts").exists())

    def test_nonfinite_review_range_returns_clean_prompt_and_import(self):
        self.app.session.reviews["claude-01"].approved_end = float("inf")
        info = self.call("/api/rough-cut/prompt", {})
        self.assertNotIn("claude-01 [keep]", info["prompt"])
        rejected = self.call("/api/rough-cut/import", {"text": answer(info["export_id"], GOOD_CLIPS)})
        self.assertFalse(rejected["ok"])
        self.assertIn("has no timed range", "\n".join(rejected["errors"]))


if __name__ == "__main__":
    unittest.main()
