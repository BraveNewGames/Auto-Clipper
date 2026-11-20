"""API tests for the Export to Resolve endpoints. The builder is stubbed: no Resolve is contacted."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from autoclipper.models import Part, POV, Review, Session, Suggestion
from autoclipper.server import make_server
from autoclipper.store import save_session

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = [
    {"index": 0, "codec_type": "video", "r_frame_rate": "60/1", "duration": "20"},
    {"index": 1, "codec_type": "audio", "channels": 2, "tags": {"title": "Track1"}},
    {"index": 2, "codec_type": "audio", "channels": 2, "tags": {"title": "Track2"}},
]


def _metadata(name, inferred=False, track=2, index=None):
    data = {
        "speaker_tracks": [{"track": track, "speaker": name, "speaker_template": "{Recorder}",
                            "index": track if index is None else index, "title": f"Track{track}"}],
        "recorder": {"name": name, "source": "filename", "tracks": [track]},
    }
    if inferred:
        data["original_speaker_tracks"] = [{"track": 2, "speaker": "Speaker 2"}]
        data["relabel_history"] = [{"provenance": "inferred_from_labels"}]
    return data


class ResolveApiCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ac_resolve_ui_"))
        self._env = {k: os.environ.get(k) for k in ("APPDATA", "AUTOCLIPPER_NO_STARTFILE")}
        os.environ["APPDATA"] = str(self.tmp / "appdata")
        os.environ["AUTOCLIPPER_NO_STARTFILE"] = "1"
        self.probe = patch("autoclipper.resolve_manifest.probe_streams", return_value=INVENTORY)
        self.probe.start()
        self.httpd = make_server(0)
        self.app = self.httpd.app
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

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

    def make_session(self, inferred_beta=False, beta_track=2, beta_index=None):
        folder = self.tmp / "session"
        session = Session(session_id="synthetic-ui", folder=str(folder), povs=[
            POV("alpha", "Alpha", [Part("alpha-p1", 1, "alpha1.mkv", duration=20, offset=0)]),
            POV("beta", "Beta", [Part("beta-p1", 1, "beta1.mp4", duration=20, offset=0)]),
        ])
        for stem, name, inferred in (("alpha1", "Alpha", False), ("beta1", "Beta", inferred_beta)):
            (folder / f"{stem}.mp4").parent.mkdir(parents=True, exist_ok=True)
            (folder / f"{stem}.mp4").touch()
            dest = folder.parent / "_Transcripts" / stem
            dest.mkdir(parents=True)
            (dest / f"{stem}.json").write_text(
                json.dumps(_metadata(name, inferred, beta_track if name == "Beta" else 2,
                                     beta_index if name == "Beta" else None)), encoding="utf-8")
        for sid, first, last in (("one", 1, 3), ("two", 5, 6), ("three", 8, 10)):
            session.suggestions.append(Suggestion(sid, "synthetic", event_id=f"EV-{sid}", title=f"Moment {sid}",
                                                  start=first, end=last))
            session.reviews[sid] = Review("keep")
        save_session(session)
        self.call("/api/session/open", {"folder": str(folder)})
        return folder

    def stub_builder(self, result=(0, {"project": "stub", "reopened": True}, ""), gate=None):
        calls = []

        def run(manifest_path, project, result_path):
            calls.append((manifest_path, project, result_path))
            if gate is not None:
                gate.wait(10)
            code, readback, stderr = result
            if readback is not None:
                result_path.write_text(json.dumps(readback), encoding="utf-8")
            # Noise on stdout must not matter: the result comes from the result file.
            return code, "progress text, not JSON\n", stderr

        self.app.run_builder = run
        return calls

    def wait_job(self):
        for _ in range(200):
            job = self.call("/api/resolve/status")
            if job["state"] != "running":
                return job
            time.sleep(0.02)
        self.fail("export job did not finish")

    def snapshot(self, folder):
        return {p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


class TestResolveExportApi(ResolveApiCase):
    def test_sample_session_preview_reports_blockers_and_export_refuses(self):
        folder = self.tmp / "NB-2026-11-16"
        subprocess.run([sys.executable, "-m", "samples.make_sample", str(folder), "--no-video"],
                       cwd=ROOT, check=True, capture_output=True)
        self.call("/api/session/create", {"folder": str(folder), "session_id": "NB-2026-11-16",
                                          "event_prefix": "NB"})
        self.call("/api/align/chain", {})
        text = (folder / "review_claude.json").read_text(encoding="utf-8")
        self.call("/api/reviews/import", {"reviewer_name": "Claude", "text": text})
        guide = self.call("/api/guide")
        for s in [s for s in guide["suggestions"] if s["kind"] == "clip_now"][:3]:
            self.call("/api/review", {"suggestion_id": s["suggestion_id"], "status": "keep"})
        before = self.snapshot(folder)

        preview = self.call("/api/resolve/preview")
        self.assertFalse(preview["can_export"])
        # The --no-video sample has SRT-only parts: no MP4 means no verifiable source.
        self.assertIn("no_angles", {b["code"] for b in preview["manifest"]["blockers"]})
        self.assertEqual(len({s["suggestion_id"] for s in preview["manifest"]["segments"]}), 3)
        self.assertIn("# Resolve timeline manifest", preview["report"])
        self.assertEqual(self.snapshot(folder), before, "preview must not write")

        calls = self.stub_builder()
        refused = self.call("/api/resolve/export", {}, expect=409)
        self.assertIn("Export blocked", refused["error"])
        self.assertTrue(refused["blockers"])
        self.assertEqual(calls, [])
        self.assertFalse((folder / "autoclipper_exports" / "resolve").exists())

    def test_inferred_mic_blocks_until_owner_confirms(self):
        folder = self.make_session(inferred_beta=True)
        preview = self.call("/api/resolve/preview")
        self.assertFalse(preview["can_export"])
        self.assertEqual({(b["code"], b["part_id"]) for b in preview["manifest"]["blockers"]},
                         {("mic_unverified", "beta-p1")})
        mics = {m["part_id"]: m for m in preview["mics"]}
        self.assertEqual(mics["alpha-p1"]["status"], "verified")
        self.assertEqual(mics["alpha-p1"]["resolve_channels"], [3, 4])
        beta = mics["beta-p1"]
        self.assertEqual((beta["status"], beta["expected_track"], beta["default_track"]), ("unverified", 2, 2))
        self.assertEqual(beta["relabel_provenance"], ["inferred_from_labels"])
        self.assertEqual(beta["metadata_tracks"], [{"track": 2, "speaker": "Beta", "title": "Track2"}])
        self.call("/api/resolve/export", {}, expect=409)

        self.call("/api/resolve/confirm-mic", {"part_id": "beta-p1", "transcriber_track": 2,
                                               "speaker": "Alpha"}, expect=400)
        self.call("/api/resolve/confirm-mic", {"part_id": "nope", "transcriber_track": 2}, expect=404)
        self.call("/api/resolve/confirm-mic", {"part_id": "beta-p1", "transcriber_track": 0}, expect=400)
        before = self.snapshot(folder)
        r = self.call("/api/resolve/confirm-mic", {"part_id": "beta-p1", "transcriber_track": 2})
        after = self.snapshot(folder)
        changed = {k for k in after if before.get(k) != after[k]} | (before.keys() - after.keys())
        self.assertEqual(changed, {Path(".autoclipper/mic_confirmations.json")})
        data = json.loads((folder / ".autoclipper" / "mic_confirmations.json").read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 1)
        row, = data["confirmations"]
        self.assertEqual((row["part_id"], row["transcriber_track"], row["speaker"], row["confirmed_by"]),
                         ("beta-p1", 2, "Beta", "owner"))
        self.assertEqual(r["confirmation"], row)

        preview = self.call("/api/resolve/preview")
        self.assertTrue(preview["can_export"], preview["manifest"]["blockers"])
        self.assertEqual({m["part_id"]: m["status"] for m in preview["mics"]},
                         {"alpha-p1": "verified", "beta-p1": "owner_confirmed"})

        # Re-confirming replaces the earlier choice for the part instead of creating a conflict.
        self.call("/api/resolve/confirm-mic", {"part_id": "beta-p1", "transcriber_track": "2"})
        data = json.loads((folder / ".autoclipper" / "mic_confirmations.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["confirmations"]), 1)

    def test_export_writes_manifest_and_report_and_invokes_builder(self):
        folder = self.make_session()
        calls = self.stub_builder()
        job = self.call("/api/resolve/export", {})
        self.assertEqual(job["state"], "running")
        out = Path(job["out_dir"])
        self.assertEqual(out.parent, (folder / "autoclipper_exports" / "resolve").resolve())
        self.assertRegex(out.name, r"^\d{8}-\d{6}$")
        self.assertEqual(job["project"], f"AutoClipper Export synthetic-ui {out.name}")
        done = self.wait_job()
        self.assertEqual(done["state"], "done", done)
        self.assertEqual(calls, [(out / "manifest.json", job["project"], out / "readback.json")])
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["blockers"], [])
        self.assertEqual([s["suggestion_id"] for s in manifest["segments"]], ["one", "two", "three"])
        self.assertIn("one", (out / "report.md").read_text(encoding="utf-8"))
        self.assertEqual(json.loads((out / "readback.json").read_text(encoding="utf-8")), done["readback"])

    def test_concurrent_export_refused(self):
        self.make_session()
        gate = threading.Event()
        calls = self.stub_builder(gate=gate)
        try:
            self.call("/api/resolve/export", {})
            refused = self.call("/api/resolve/export", {}, expect=409)
            self.assertIn("already running", refused["error"])
        finally:
            gate.set()
        self.assertEqual(self.wait_job()["state"], "done")
        self.assertEqual(len(calls), 1)

    def test_builder_failure_is_surfaced_verbatim(self):
        self.make_session()
        message = "Resolve export stopped: Save and close the active owner project before starting a Resolve export"
        self.stub_builder(result=(1, None, message + "\n"))
        self.call("/api/resolve/export", {})
        job = self.wait_job()
        self.assertEqual((job["state"], job["error"]), ("failed", message))
        self.assertFalse(Path(job["out_dir"], "readback.json").exists())

    def test_missing_result_file_is_a_failure(self):
        self.make_session()
        self.stub_builder(result=(0, None, ""))
        self.call("/api/resolve/export", {})
        job = self.wait_job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("without writing its result file", job["error"])

    def test_builder_timeout_is_reported(self):
        self.make_session()

        def run(*_):
            raise subprocess.TimeoutExpired("builder", 1)

        self.app.run_builder = run
        self.call("/api/resolve/export", {})
        job = self.wait_job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("timed out", job["error"])

    def test_run_builder_command_line(self):
        manifest = self.tmp / "manifest.json"
        result = self.tmp / "readback.json"
        with patch("autoclipper.server.subprocess.Popen") as popen:
            popen.return_value.communicate.return_value = ("{}", "")
            popen.return_value.returncode = 0
            self.assertEqual(self.app.run_builder(manifest, "AutoClipper Export s 20260101-000000", result),
                             (0, "{}", ""))
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [sys.executable, "-m", "autoclipper.resolve_builder", str(manifest),
                                   "--execute", "--project", "AutoClipper Export s 20260101-000000",
                                   "--result-file", str(result)])
        self.assertEqual(Path(kwargs["cwd"]), ROOT)
        popen.return_value.communicate.assert_called_once_with(timeout=self.app.builder_timeout)
        self.assertIsNone(self.app._builder_proc)

    def test_builder_child_is_killed_on_timeout_and_stopped_on_exit(self):
        self.app.builder_command = [sys.executable, "-c", "import time; time.sleep(60)"]
        self.app.builder_timeout = 1
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.app.run_builder(self.tmp / "m.json", "p", self.tmp / "r.json")
        self.assertLess(time.monotonic() - started, 30)

        self.app.builder_timeout = 60
        outcome = {}
        worker = threading.Thread(target=lambda: outcome.update(
            result=self.app.run_builder(self.tmp / "m.json", "p", self.tmp / "r.json")))
        worker.start()
        for _ in range(500):
            if self.app._builder_proc is not None:
                break
            time.sleep(0.01)
        proc = self.app._builder_proc
        self.assertIsNotNone(proc)
        self.app.stop_builder()  # what the atexit hook runs when the server stops
        worker.join(20)
        self.assertFalse(worker.is_alive())
        self.assertIsNotNone(proc.poll())
        self.assertNotEqual(outcome["result"][0], 0)


class TestMicChoices(ResolveApiCase):
    def test_confirm_rejects_tracks_missing_from_the_mp4(self):
        folder = self.make_session(inferred_beta=True)
        beta = {m["part_id"]: m for m in self.call("/api/resolve/preview")["mics"]}["beta-p1"]
        self.assertEqual(beta["expected_track"], 2)
        self.assertEqual(beta["default_track"], 2)
        self.assertEqual([(o["track"], o["mp4_audio_ordinal"], o["resolve_channels"]) for o in beta["options"]],
                         [(1, 1, [1, 2]), (2, 2, [3, 4])])
        refused = self.call("/api/resolve/confirm-mic", {"part_id": "beta-p1", "transcriber_track": 3}, expect=400)
        self.assertIn("Track 3 is not an audio track of beta1.mp4", refused["error"])
        self.assertIn("track 2 (MP4 audio 2", refused["error"])
        self.assertFalse((folder / ".autoclipper" / "mic_confirmations.json").exists())
        (folder / "beta1.mp4").unlink()
        refused = self.call("/api/resolve/confirm-mic", {"part_id": "beta-p1", "transcriber_track": 2}, expect=409)
        self.assertIn("No OBS-remuxed MP4", refused["error"])

    def test_unmapped_mic_can_be_cleared_and_a_wrong_pick_corrected(self):
        # Metadata names track 3, but the MP4 only has two audio streams.
        folder = self.make_session(beta_track=3)
        preview = self.call("/api/resolve/preview")
        self.assertEqual({(b["code"], b["part_id"]) for b in preview["manifest"]["blockers"]},
                         {("mic_unmapped", "beta-p1")})
        mics = {m["part_id"]: m for m in preview["mics"]}
        self.assertNotIn("options", mics["alpha-p1"])  # verified from recorder metadata
        beta = mics["beta-p1"]
        self.assertEqual((beta["status"], beta["expected_track"], beta["default_track"]), ("unmapped", 3, None))
        self.assertEqual([o["track"] for o in beta["options"]], [1, 2])

        self.call("/api/resolve/confirm-mic", {"part_id": "beta-p1", "transcriber_track": 3}, expect=400)
        self.call("/api/resolve/confirm-mic", {"part_id": "beta-p1", "transcriber_track": 1})
        preview = self.call("/api/resolve/preview")
        self.assertTrue(preview["can_export"])
        beta = {m["part_id"]: m for m in preview["mics"]}["beta-p1"]
        self.assertEqual((beta["status"], beta["transcriber_track"], beta["default_track"]),
                         ("owner_confirmed", 1, 1))
        self.assertEqual([o["track"] for o in beta["options"]], [1, 2])  # can still be corrected

        self.call("/api/resolve/confirm-mic", {"part_id": "beta-p1", "transcriber_track": 2})
        data = json.loads((folder / ".autoclipper" / "mic_confirmations.json").read_text(encoding="utf-8"))
        self.assertEqual([(r["part_id"], r["transcriber_track"]) for r in data["confirmations"]], [("beta-p1", 2)])
        beta = {m["part_id"]: m for m in self.call("/api/resolve/preview")["mics"]}["beta-p1"]
        self.assertEqual((beta["transcriber_track"], beta["resolve_channels"]), (2, [3, 4]))


class TestMicMetadataConflict(ResolveApiCase):
    def test_owner_confirmation_overrides_conflicting_metadata_with_a_warning(self):
        # Metadata says Beta's track 2 is stream index 7; the MP4 has Track2 at index 2.
        folder = self.make_session(beta_index=7)
        preview = self.call("/api/resolve/preview")
        self.assertEqual({(b["code"], b["part_id"]) for b in preview["manifest"]["blockers"]},
                         {("mic_unmapped", "beta-p1")})  # fail closed without a confirmation
        self.assertNotIn("mic_metadata_conflict", {w["code"] for w in preview["manifest"]["warnings"]})
        beta = {m["part_id"]: m for m in preview["mics"]}["beta-p1"]
        self.assertEqual((beta["expected_track"], beta["default_track"]), (2, 2))
        self.assertEqual([(o["track"], o["mp4_audio_stream_index"]) for o in beta["options"]], [(1, 1), (2, 2)])

        refused = self.call("/api/resolve/confirm-mic", {"part_id": "beta-p1", "transcriber_track": 5}, expect=400)
        self.assertIn("Track 5 is not an audio track", refused["error"])
        self.assertFalse((folder / ".autoclipper" / "mic_confirmations.json").exists())

        self.call("/api/resolve/confirm-mic", {"part_id": "beta-p1", "transcriber_track": 2})
        preview = self.call("/api/resolve/preview")
        manifest = preview["manifest"]
        self.assertEqual(manifest["blockers"], [])
        conflict, = [w for w in manifest["warnings"] if w["code"] == "mic_metadata_conflict"]
        self.assertEqual((conflict["pov_name"], conflict["part_id"]), ("Beta", "beta-p1"))
        self.assertIn("stream index 7", conflict["message"])
        self.assertIn("owner confirmed track 2 (MP4 stream #2)", conflict["message"])
        self.assertIn("mic_metadata_conflict", preview["report"])
        mic = next(a["mic"] for s in manifest["segments"] for a in s["angles"] if a["part_id"] == "beta-p1")
        self.assertEqual((mic["status"], mic["transcriber_track"], mic["mp4_audio_stream_index"],
                          mic["resolve_channels"]), ("owner_confirmed", 2, 2, [3, 4]))
        alpha = next(a["mic"] for s in manifest["segments"] for a in s["angles"] if a["part_id"] == "alpha-p1")
        self.assertEqual(alpha["status"], "verified")
        self.assertNotIn("metadata_conflict", alpha)

    def test_confirmation_without_conflict_keeps_metadata_mapping(self):
        self.make_session(inferred_beta=True)
        self.call("/api/resolve/confirm-mic", {"part_id": "beta-p1", "transcriber_track": 2})
        manifest = self.call("/api/resolve/preview")["manifest"]
        self.assertEqual(manifest["blockers"], [])
        self.assertNotIn("mic_metadata_conflict", {w["code"] for w in manifest["warnings"]})


class TestResolveLocking(ResolveApiCase):
    def test_probe_runs_outside_the_app_lock_and_session_changes_are_detected(self):
        self.make_session()
        seen = {}

        def probe(path):
            if "lock_free" not in seen:
                result = {}

                def try_lock():  # from another thread, as a concurrent request would
                    result["ok"] = self.app.lock.acquire(timeout=0.5)
                    if result["ok"]:
                        self.app.lock.release()

                t = threading.Thread(target=try_lock)
                t.start()
                t.join()
                seen["lock_free"] = result["ok"]
                # Another request edits the session while the MP4s are being probed.
                self.call("/api/review", {"suggestion_id": "two", "status": seen.get("status", "reject")})
            return INVENTORY

        with patch("autoclipper.resolve_manifest.probe_streams", side_effect=probe):
            refused = self.call("/api/resolve/preview", expect=409)
        self.assertTrue(seen["lock_free"])
        self.assertIn("session changed", refused["error"])
        preview = self.call("/api/resolve/preview")
        self.assertEqual([s["suggestion_id"] for s in preview["manifest"]["segments"]], ["one", "three"])

        seen.clear()
        seen["status"] = "maybe"  # a real change again for the export attempt
        calls = self.stub_builder()
        with patch("autoclipper.resolve_manifest.probe_streams", side_effect=probe):
            self.call("/api/resolve/export", {}, expect=409)
        self.assertEqual(calls, [])
        self.assertEqual(self.call("/api/resolve/status")["state"], "idle")
        self.assertFalse((self.tmp / "session" / "autoclipper_exports").exists())

if __name__ == "__main__":
    unittest.main()
