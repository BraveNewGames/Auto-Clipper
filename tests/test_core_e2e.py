import csv
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from autoclipper.alignment import set_offset
from autoclipper.losslesscut import expected_filenames
from autoclipper.store import load_session, save_session

# Independently calculated, unpadded local windows in milliseconds.
EXPECTED = {
    ("claude", "Zombie Slide"): {
        "andres-p2": (60125, 80250), "cindy-p2": (190125, 200000),
        "cindy-p3": (0, 10250), "didi-p1": (345125, 365250), "rika-p1": (360125, 380250),
    },
    ("claude", "Cart Landing Roof"): {
        "andres-p2": (72125, 84875), "cindy-p3": (2125, 14875),
        "didi-p1": (357125, 369875), "rika-p1": (372125, 384875),
    },
    ("claude", "Fountain Drop"): {
        "cindy-p2": (115000, 128000), "didi-p1": (270000, 283000), "rika-p1": (285000, 298000),
    },
    ("chatgpt", "Zombie Slide"): {
        "andres-p2": (64250, 75500), "cindy-p2": (194250, 200000),
        "cindy-p3": (0, 5500), "didi-p1": (349250, 360500), "rika-p1": (364250, 375500),
    },
}


class PipelineE2ETest(unittest.TestCase):
    def cli(self, command, folder, *args):
        return json.loads(subprocess.check_output(
            [sys.executable, "-m", "autoclipper.cli", command, str(folder), *args],
            encoding="utf-8",
        ))

    def test_sample_reviews_export_every_covered_piece(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "NB-2026-11-16"
            subprocess.run([sys.executable, "-m", "samples.make_sample", str(folder), "--no-video"],
                           check=True)
            self.assertEqual(len(list(folder.glob("*.srt"))), 7)
            self.assertEqual(list(folder.glob("*.mkv")), [])
            truth = json.loads((folder / "ground_truth.json").read_text(encoding="utf-8"))
            self.cli("scan", folder)
            session = load_session(folder)
            for part_id, offset in truth["offsets"].items():
                if part_id not in {"cindy-p2", "cindy-p3"}:
                    set_offset(session, part_id, offset)
            save_session(session)
            coverage = self.cli("chain", folder)
            session = load_session(folder)
            self.assertEqual({part.part_id: part.offset for _, part in session.all_parts()},
                             truth["offsets"])
            self.assertEqual({part.part_id: part.duration for _, part in session.all_parts()},
                             truth["durations"])
            self.assertEqual(session.part_by_id("cindy-p3").offset_source, "chained")
            self.assertEqual(coverage["povs"]["andres"]["gaps"], [[312.5, 330]])

            # Editorial padding would deliberately reach across the edges of this gap.
            mapped = self.cli("map", folder, "--pad-before", "0", "--pad-after", "0")
            self.assertEqual(len(mapped), 5)
            session = load_session(folder)
            zombie = [s for s in session.suggestions if s.title == "Zombie Slide"]
            self.assertEqual(len(zombie), 2)
            self.assertEqual(len({s.event_id for s in zombie}), 1)

            exports = self.cli("export", folder)
            seen = set()
            labels = {"claude": set(), "chatgpt": set()}
            expected_sets = {
                reviewer: {name.casefold() for name in expected_filenames(session, reviewer)}
                for reviewer in labels
            }
            unsafe = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
            for export in exports:
                reviewer = export["reviewer"]
                part_id = export["part_id"]
                csv_path = Path(export["csv_path"])
                self.assertFalse(unsafe.search(csv_path.name))
                self.assertIn(csv_path.name, (csv_path.parent / "README.txt").read_text(encoding="utf-8"))
                with csv_path.open(encoding="utf-8", newline="") as handle:
                    rows = list(csv.reader(handle))
                self.assertEqual(len(rows), len(export["rows"]))
                previous_end = 0
                for raw_start, raw_end, label in rows:
                    self.assertRegex(raw_start, r"^\d+\.\d{3}$")
                    self.assertRegex(raw_end, r"^\d+\.\d{3}$")
                    start = round(float(raw_start) * 1000)
                    end = round(float(raw_end) * 1000)
                    self.assertGreaterEqual(start, previous_end)
                    self.assertGreater(end, start)
                    previous_end = end
                    self.assertFalse(unsafe.search(label))
                    self.assertLessEqual(len(label), 120)
                    self.assertEqual(label.rstrip(". "), label)
                    name = (label + ".mkv").casefold()
                    self.assertNotIn(name, labels[reviewer])
                    labels[reviewer].add(name)
                    title = label.split(" - ")[1]
                    self.assertEqual((start, end), EXPECTED[reviewer, title][part_id])
                    key = reviewer, title, part_id
                    self.assertNotIn(key, seen)
                    seen.add(key)
                    if title == "Zombie Slide":
                        if part_id == "cindy-p2":
                            self.assertIn("Cindy (1 of 2)", label)
                        if part_id == "cindy-p3":
                            self.assertIn("Cindy (2 of 2)", label)
                        tag = {"claude": "Claude", "chatgpt": "ChatGPT"}[reviewer]
                        self.assertTrue(label.endswith(f"[{tag}]"))

            self.assertEqual(seen, {(reviewer, title, part)
                                    for (reviewer, title), parts in EXPECTED.items()
                                    for part in parts})
            self.assertEqual(labels, expected_sets)
            self.assertEqual({e["pass_no"] for e in exports
                              if e["reviewer"] == "claude" and e["part_id"] == "cindy-p3"}, {1, 2})
