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


def _metadata(name, inferred=False):
    data = {
        "speaker_tracks": [{"track": 2, "speaker": name, "speaker_template": "{Recorder}",
                            "index": 2, "title": "Track2"}],
        "recorder": {"name": name, "source": "filename", "tracks": [2]},
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

    def make_session(self, inferred_beta=False):
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
            (dest / f"{stem}.json").write_text(json.dumps(_metadata(name, inferred)), encoding="utf-8")
        for sid, first, last in (("one", 1, 3), ("two", 5, 6), ("three", 8, 10)):
            session.suggestions.append(Suggestion(sid, "synthetic", event_id=f"EV-{sid}", title=f"Moment {sid}",
                                                  start=first, end=last))
            session.reviews[sid] = Review("keep")
        save_session(session)
        self.call("/api/session/open", {"folder": str(folder)})
        return folder

    def stub_builder(self, result=(0, json.dumps({"project": "stub", "reopened": True}), ""), gate=None):
        calls = []

        def run(manifest_path, project):
            calls.append((manifest_path, project))
            if gate is not None:
                gate.wait(10)
            return result

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
        self.assertEqual((beta["status"], beta["suggested_track"]), ("unverified", 2))
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
        self.assertEqual(calls, [(out / "manifest.json", job["project"])])
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
        self.stub_builder(result=(1, "", message + "\n"))
        self.call("/api/resolve/export", {})
        job = self.wait_job()
        self.assertEqual((job["state"], job["error"]), ("failed", message))
        self.assertFalse(Path(job["out_dir"], "readback.json").exists())

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
        with patch("autoclipper.server.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "{}", "")
            self.assertEqual(self.app.run_builder(manifest, "AutoClipper Export s 20260101-000000"), (0, "{}", ""))
        args, kwargs = run.call_args
        self.assertEqual(args[0], [sys.executable, "-m", "autoclipper.resolve_builder", str(manifest),
                                   "--execute", "--project", "AutoClipper Export s 20260101-000000"])
        self.assertEqual(Path(kwargs["cwd"]), ROOT)
        self.assertEqual(kwargs["timeout"], self.app.builder_timeout)


if __name__ == "__main__":
    unittest.main()
