"""Server tests for the Director tab endpoints. The builder is stubbed: Resolve is never contacted."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from pathlib import Path

from autoclipper import director_ai as dx
from autoclipper.director_notes import NotesCatalog
from autoclipper.server import make_server
from autoclipper.store import save_session

from test_director_ai_import import GOOD, ImportBase


class DirectorUiCase(ImportBase):
    def setUp(self):
        super().setUp()
        self.appdata = Path(tempfile.mkdtemp(prefix="ac_director_ui_"))
        self._env = {k: os.environ.get(k) for k in ("APPDATA", "AUTOCLIPPER_NO_STARTFILE")}
        os.environ["APPDATA"] = str(self.appdata)
        os.environ["AUTOCLIPPER_NO_STARTFILE"] = "1"
        self.httpd = make_server(0)
        self.app = self.httpd.app
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.cleanup_server)

    def cleanup_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.appdata, ignore_errors=True)

    def call(self, path, body=None, expect=200, headers=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, headers={
            **({"Content-Type": "application/json"} if data else {}), **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=60) as res:
                status, payload = res.status, res.read()
        except urllib.error.HTTPError as err:
            status, payload = err.code, err.read()
        self.assertEqual(status, expect, payload[:600])
        return json.loads(payload.decode("utf-8"))

    def bind(self):
        return self.call("/api/director/open", {"scratch": str(self.s.scratch), "session_root": str(self.s.recordings)})

    def stub_builder(self, result=(0, {"project": "stub", "reopened": True}, ""), gate=None):
        calls = []

        def run(manifest_path, project, result_path):
            calls.append((manifest_path, project, result_path))
            if gate is not None:
                gate.wait(10)
            code, readback, stderr = result
            if readback is not None:
                result_path.write_text(json.dumps(readback), encoding="utf-8")
            return code, "progress text\n", stderr

        self.app.run_builder = run
        return calls

    def wait_job(self):
        for _ in range(300):
            job = self.call("/api/resolve/status")
            if job["state"] not in ("running", "preparing"):
                return job
            time.sleep(0.02)
        self.fail("job did not finish")

    def export_id_for_api(self):
        self.bind()
        return self.call("/api/director/export", {})["export_id"]

    def answer_text(self, export_id, mutate=None):
        data = json.loads(json.dumps(GOOD))
        data["export_id"] = export_id
        if mutate:
            mutate(data)
        return "Here you go:\n```json\n" + json.dumps(data) + "\n```"


class BindingTests(DirectorUiCase):
    def test_scratch_list_and_open_summary(self):
        # Scratch folders live directly under system temp; point the scan at the fixture's temp folder.
        with patch("tempfile.gettempdir", return_value=str(self.s.temp)):
            rows = self.call("/api/director/scratches")
        mine = [r for r in rows if r["path"] == str(self.s.scratch)]
        self.assertEqual([(r["built"], r["fkb"], r["notes"]) for r in mine], [(True, True, 3)])
        summary = self.bind()
        self.assertEqual((summary["fps"], summary["notes"], summary["session_id"]), (60, 3, "synthetic"))
        state = self.call("/api/director/state")
        self.assertEqual(len(state["notes"]), 3)
        self.assertEqual(state["plans"], [])
        self.assertEqual({row["pov_id"] for row in state["coverage"]}, {"ada", "bob"})

    def test_web_assets_have_the_director_tab(self):
        with urllib.request.urlopen(self.base + "/") as res:
            self.assertIn('data-tab="director"', res.read().decode("utf-8"))
        with urllib.request.urlopen(self.base + "/app.js") as res:
            script = res.read().decode("utf-8")
        for needle in ("/api/director/import", "/api/director/build", "confirm_project_manager", "companion_closed"):
            self.assertIn(needle, script)
        # Prompt and answer text never go to browser storage.
        self.assertNotIn("localStorage", script)
        self.assertNotIn("sessionStorage", script)

    def test_binding_is_independent_of_the_session(self):
        before = (self.call("/api/recent"), (self.s.scratch / ".autoclipper").exists())
        self.bind()
        self.call("/api/director/state")
        self.call("/api/session", expect=409)  # still no open Auto-Clipper session
        self.assertIsNone(self.app.session)
        self.assertEqual(self.call("/api/recent"), before[0])
        self.assertFalse((self.s.scratch / ".autoclipper" / "session.json").exists())

    def test_refusals(self):
        def refuse(scratch, root=None, expect=400):
            body = self.call("/api/director/open", {"scratch": str(scratch), "session_root": str(root or self.s.recordings)},
                             expect=expect)
            self.assertIn("error", body)
            return body

        refuse(Path(os.environ.get("SystemRoot", "/")))
        refuse(self.s.recordings)  # under temp but not a scratch
        refuse(self.s.temp / "autoclipper-director-a-missing")
        refuse(self.s.scratch, root=self.s.temp)
        self.assertEqual(refuse(self.s.scratch, root=self.s.temp)["code"], "E_SOURCE")
        self.call("/api/director/open", {"scratch": 5, "session_root": "x"}, expect=400)
        self.call("/api/director/open", {}, expect=400)
        self.call("/api/director/state", expect=409)  # never bound
        (self.s.scratch / "build.json").rename(self.s.scratch / "build.moved")
        refuse(self.s.scratch)
        (self.s.scratch / "build.moved").rename(self.s.scratch / "build.json")

    @unittest.skipUnless(sys.platform == "win32", "Windows junction test")
    def test_scratch_listing_skips_junctions(self):
        junction = self.s.temp / "autoclipper-director-a-jct"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(self.s.scratch)],
                                capture_output=True, text=True)
        if result.returncode:
            self.skipTest("cannot create junction")
        try:
            with patch("tempfile.gettempdir", return_value=str(self.s.temp)):
                names = [row["name"] for row in self.call("/api/director/scratches")]
        finally:
            os.rmdir(junction)
        self.assertIn(self.s.scratch.name, names)
        self.assertNotIn("autoclipper-director-a-jct", names)

    def test_stale_catalog_is_refused(self):
        self.s.store.update(lambda data: setattr(data, "mapping_stale", True))
        body = self.call("/api/director/open", {"scratch": str(self.s.scratch), "session_root": str(self.s.recordings)},
                         expect=400)
        self.assertEqual(body["code"], "E_MAPPING_CHANGED")

    def test_query_string_paths_are_ignored(self):
        self.call("/api/director/state?scratch=" + str(self.s.scratch) + "&session_root=" + str(self.s.recordings),
                  expect=409)

    def test_cross_origin_writes_are_refused(self):
        for path, body in (("/api/director/open", {"scratch": str(self.s.scratch)}),
                           ("/api/director/export", {}), ("/api/director/import", {"text": "x"}),
                           ("/api/director/build", {"plan_id": "x", "confirm_project_manager": True})):
            self.call(path, body, expect=403, headers={"Origin": "https://evil.example"})
        self.call("/api/director/state", expect=403, headers={"Origin": "https://evil.example"})

    @unittest.skipUnless(sys.platform == "win32", "Windows junction test")
    def test_junction_scratch_is_refused(self):
        junction = self.s.temp / "autoclipper-director-a-junction"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(self.s.scratch)],
                                capture_output=True, text=True)
        if result.returncode:
            self.skipTest("cannot create junction")
        try:
            body = self.call("/api/director/open", {"scratch": str(junction), "session_root": str(self.s.recordings)},
                             expect=400)
            self.assertEqual(body["code"], "E_SCRATCH_LINK")
        finally:
            os.rmdir(junction)

    def test_existing_endpoints_unchanged_while_a_scratch_is_bound(self):
        session_file = save_session(self.s.source)
        self.call("/api/session/open", {"folder": str(self.s.recordings)})
        first = self.call("/api/resolve/preview")
        self.bind()
        second = self.call("/api/resolve/preview")

        def strip(value):
            value["manifest"].pop("created_at", None)
            return value

        self.assertEqual(strip(first), strip(second))
        self.assertTrue(Path(session_file).is_file())


class ExportImportTests(DirectorUiCase):
    def test_export_is_private_and_stable(self):
        self.bind()
        first = self.call("/api/director/export", {})
        again = self.call("/api/director/export", {"save_prompt": True})
        self.assertEqual(first["export_id"], again["export_id"])
        self.assertEqual(first["counts"]["notes"], 3)
        for forbidden in (str(self.s.temp), "a1.srt", "mp4"):
            self.assertNotIn(forbidden, first["prompt"])
        one = self.call("/api/director/export", {"note_ids": [self.s.notes[1].note_id], "save_prompt": False})
        self.assertEqual(one["counts"]["notes"], 1)
        self.assertFalse(one["saved_prompt"])
        self.call("/api/director/export", {"note_ids": ["no-such-note"]}, expect=400)

    def test_export_over_the_cap_returns_409(self):
        self.bind()
        original = dx.DEFAULT_BUDGET_CHARS, dx.HARD_CAP_CHARS
        dx.DEFAULT_BUDGET_CHARS = dx.HARD_CAP_CHARS = 200
        try:
            body = self.call("/api/director/export", {}, expect=409)
        finally:
            dx.DEFAULT_BUDGET_CHARS, dx.HARD_CAP_CHARS = original
        self.assertIn("select fewer notes", body["error"].lower())

    def test_valid_import_saves_and_lists_the_plan(self):
        export_id = self.export_id_for_api()
        result = self.call("/api/director/import", {
            "text": self.answer_text(export_id), "provider": "Claude", "companion_closed": True})
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan_id"], f"ai-{export_id}-1")
        plans = self.call("/api/director/state")["plans"]
        self.assertEqual([(p["plan_id"], p["provider"], p["clips"], p["placeholders"]) for p in plans],
                         [(result["plan_id"], "Claude", 3, 1)])
        preview = self.call("/api/director/plan-preview?plan_id=" + result["plan_id"])
        self.assertEqual((preview["segments"], preview["slates"], preview["blockers"]), (4, 1, 0))
        self.call("/api/director/plan-preview?plan_id=nope", expect=409)

    def test_invalid_import_is_422_and_changes_nothing(self):
        export_id = self.export_id_for_api()
        before = self.catalog_bytes()
        body = self.call("/api/director/import", {
            "text": self.answer_text(export_id, lambda d: d["clips"][3].update(in_tc="01:00:41:00")),
            "provider": "ChatGPT", "companion_closed": True}, expect=422)
        self.assertIn("disagrees", body["report"])
        self.assertTrue(body["repair_prompt"])
        self.assertEqual(body["export_id"], export_id)
        self.assertEqual(self.catalog_bytes(), before)
        ai_review = self.call("/api/director/import", {
            "text": json.dumps({"suggestions": [{"title": "x"}]}), "companion_closed": True}, expect=422)
        self.assertIn("E_SCHEMA", ai_review["errors"][0])
        self.assertEqual(self.catalog_bytes(), before)

    def test_import_requires_closed_companion(self):
        export_id = self.export_id_for_api()
        before = self.catalog_bytes()
        for flag in ({}, {"companion_closed": False}, {"companion_closed": "yes"}):
            body = self.call("/api/director/import", {"text": self.answer_text(export_id), **flag}, expect=409)
            self.assertEqual(body["code"], "E_COMPANION")
        self.call("/api/director/import", {"text": "  "}, expect=400)
        self.assertEqual(self.catalog_bytes(), before)

    def test_stale_note_revision_is_refused(self):
        export_id = self.export_id_for_api()
        NotesCatalog(self.s.store).edit_text(self.s.notes[0].note_id, "Changed after export")
        body = self.call("/api/director/import", {"text": self.answer_text(export_id), "companion_closed": True},
                         expect=422)
        self.assertIn("E_NOTE_STALE", body["report"])


class BuildTests(DirectorUiCase):
    def saved_plan(self):
        export_id = self.export_id_for_api()
        return self.call("/api/director/import", {
            "text": self.answer_text(export_id), "provider": "Claude", "companion_closed": True})["plan_id"]

    def test_build_runs_the_unchanged_job_with_a_new_director_project(self):
        plan_id = self.saved_plan()
        calls = self.stub_builder()
        self.call("/api/director/build", {"plan_id": plan_id}, expect=409)  # needs confirm_project_manager
        self.assertEqual(calls, [])
        job = self.call("/api/director/build", {"plan_id": plan_id, "confirm_project_manager": True})
        self.assertEqual((job["kind"], job["plan_id"]), ("director_plan", plan_id))
        self.assertRegex(job["project"], r"^AutoClipper DirectorA Test \d{8}-\d{6}$")
        done = self.wait_job()
        self.assertEqual((done["state"], done["kind"]), ("done", "director_plan"))
        manifest_path, project, result_path = calls[0]
        self.assertEqual(project, job["project"])
        folder = Path(job["out_dir"])
        self.assertEqual(folder.parent, self.s.scratch)
        self.assertTrue(folder.name.startswith(f"edit-{plan_id}-"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["mode"], "director_edit_plan")
        self.assertEqual(len(manifest["segments"]), 4)
        self.assertEqual(result_path.parent, folder)

    def test_shared_job_slot_refuses_in_both_directions(self):
        plan_id = self.saved_plan()
        gate = threading.Event()
        self.stub_builder(gate=gate)
        self.call("/api/director/build", {"plan_id": plan_id, "confirm_project_manager": True})
        try:
            self.call("/api/resolve/export", {}, expect=409)
            self.call("/api/rough-cut/build", {"cut_id": "x"}, expect=409)
            body = self.call("/api/director/build", {"plan_id": plan_id, "confirm_project_manager": True}, expect=409)
            self.assertIn("job", body)
        finally:
            gate.set()
        self.wait_job()
        with self.app._resolve_lock:
            self.app._resolve_job = {"state": "running"}  # an Export to Resolve job is running
        self.call("/api/director/build", {"plan_id": plan_id, "confirm_project_manager": True}, expect=409)
        with self.app._resolve_lock:
            self.app._resolve_job = {"state": "idle"}

    def test_failures_are_surfaced(self):
        plan_id = self.saved_plan()
        for result, needle in (((1, None, "builder exploded"), "builder exploded"),
                               ((0, None, ""), "without writing its result file")):
            self.stub_builder(result=result)
            self.call("/api/director/build", {"plan_id": plan_id, "confirm_project_manager": True})
            job = self.wait_job()
            self.assertEqual(job["state"], "failed")
            self.assertIn(needle, job["error"])

    def test_unknown_plan_and_unbound_scratch(self):
        self.bind()
        self.stub_builder()
        self.call("/api/director/build", {"plan_id": "nope", "confirm_project_manager": True}, expect=409)
        self.assertEqual(self.call("/api/resolve/status")["state"], "idle")  # slot restored after a refusal


if __name__ == "__main__":
    unittest.main()
