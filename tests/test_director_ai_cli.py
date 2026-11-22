from contextlib import redirect_stderr, redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from autoclipper import director_ai as dx
from test_director_ai_import import GOOD, ImportBase


class CliTests(ImportBase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = dx.main([argv[0], str(self.s.scratch), "--session-root", str(self.s.recordings), *argv[1:]])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def response_file(self, mutate=None):
        data = json.loads(json.dumps(GOOD))
        data["export_id"] = self.export_id
        if mutate:
            mutate(data)
        path = self.s.temp / "response.txt"
        path.write_text("```json\n" + json.dumps(data) + "\n```", encoding="utf-8")
        return path

    def test_export_prints_no_text_and_writes_new_file_only(self):
        target = self.s.temp / "prompt.txt"
        with patch("autoclipper.director_ai._copy_to_clipboard") as copy:
            code, out, _ = self.run_cli("export", "--out", str(target), "--copy")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["counts"]["notes"], 3)
        for secret in ("Truck", "flip", "recovery", str(self.s.temp)):
            self.assertNotIn(secret.casefold(), out.casefold())
        self.assertIn("EXPORT ID", target.read_text(encoding="utf-8"))
        copy.assert_called_once()
        code, _, err = self.run_cli("export", "--out", str(target))
        self.assertEqual(code, 2)  # never overwrites
        self.assertIn("exists", err)

    def test_import_success_and_failure(self):
        path = self.response_file()
        code, out, _ = self.run_cli("import", str(path), "--provider", "Claude")
        self.assertEqual(code, 2)  # --companion-closed is required
        code, out, _ = self.run_cli("import", str(path), "--provider", "Claude", "--companion-closed")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["plan_id"], f"ai-{self.export_id}-1")
        repair = self.s.temp / "repair.txt"
        bad = self.response_file(lambda d: d["clips"][3].update(in_tc="01:00:41:00"))
        before = self.catalog_bytes()
        code, out, _ = self.run_cli("import", str(bad), "--companion-closed", "--repair-out", str(repair))
        self.assertEqual(code, 1)
        self.assertIn("disagrees", out)
        self.assertEqual(self.catalog_bytes(), before)
        self.assertIn(self.export_id, repair.read_text(encoding="utf-8"))

    def test_plan_build(self):
        plan_id = dx.import_response(self.open(), self.response_file().read_text(encoding="utf-8"), provider="x",
                                     companion_closed=True)["plan_id"]
        code, _, err = self.run_cli("plan-build", plan_id)
        self.assertEqual(code, 2)
        self.assertIn("--dry-run", err)
        code, out, _ = self.run_cli("plan-build", plan_id, "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["segments"], 4)
        built = {"timeline": "Review"}
        with patch("autoclipper.resolve_builder.build_export", return_value=built) as build:
            code, out, _ = self.run_cli("plan-build", plan_id, "--confirm-project-manager")
        self.assertEqual(code, 0, out)
        project = build.call_args.kwargs["project_name"]
        self.assertRegex(project, r"^AutoClipper DirectorA Test \d{8}-\d{6}$")
        code, _, err = self.run_cli("plan-build", "unknown", "--dry-run")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
