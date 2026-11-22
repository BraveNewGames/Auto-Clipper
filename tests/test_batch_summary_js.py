"""The Export tab must summarize a rough-cut batch as a batch, not as a single export."""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "autoclipper" / "web" / "app.js"


class BatchSummaryTests(unittest.TestCase):
    def setUp(self):
        self.source = APP.read_text(encoding="utf-8")

    def test_export_tab_routes_a_batch_job_to_the_batch_box_before_the_single_export_text(self):
        body = self.source[self.source.index("function resolveJobBox"):]
        batch = body.index('job.kind === "rough_cut_batch"')
        self.assertLess(batch, body.index("Export complete."))
        self.assertLess(batch, body.index("segment(s)"))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_summary_counts(self):
        match = re.search(r"function batchSummary\(job\) \{.*?\n\}\n", self.source, re.S)
        self.assertTrue(match)
        script = match.group(0) + """
const cuts = (...states) => states.map((state) => ({ state }));
console.log(JSON.stringify([
  batchSummary({ state: "done", cuts: cuts("done", "done", "done") }),
  batchSummary({ state: "failed", cuts: cuts("done", "failed", "done") }),
  batchSummary({ state: "failed", cuts: cuts("done", "failed", "not_started") }),
  batchSummary({ state: "running", cuts: cuts("done", "running", "queued") }),
]));"""
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out), [
            "Batch: 3 added, 0 failed", "Batch: 2 added, 1 failed",
            "Batch: 1 added, 1 failed, 1 not started", "Batch: 1 of 3 cut(s) finished"])


if __name__ == "__main__":
    unittest.main()
