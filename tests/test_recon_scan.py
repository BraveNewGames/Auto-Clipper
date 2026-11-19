"""Folder scan with the user's real filename styles (mixed across POVs)."""
import shutil
import tempfile
import unittest
from pathlib import Path

from autoclipper.scan import scan_folder

SRT = "1\n00:00:01,000 --> 00:00:02,000\nX: hi\n"


class ScanNamingTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="recon_scan_"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def scan(self, names):
        for n in names:
            (self.dir / n).write_text(SRT, encoding="utf-8")
        return {pov.pov_id: pov for pov in scan_folder(self.dir).povs}

    def test_real_sampledata_names(self):
        povs = self.scan(["Andres__2026-11-16 17-09-26.srt", "Andres__2026-11-16 16-39-24.srt",
                          "Cindy__1.srt", "Cindy__2.srt", "Cindy__3.srt", "Didi__1.srt",
                          "Rika__2026-11-16 19-39-26.srt"])
        self.assertEqual(sorted(povs), ["andres", "cindy", "didi", "rika"])
        self.assertEqual({k: v.name for k, v in povs.items()},
                         {"andres": "Andres", "cindy": "Cindy", "didi": "Didi", "rika": "Rika"})
        self.assertEqual([p.srt_path for p in povs["andres"].parts],          # OBS timestamp order
                         ["Andres__2026-11-16 16-39-24.srt", "Andres__2026-11-16 17-09-26.srt"])
        self.assertEqual([p.part_id for p in povs["andres"].parts], ["andres-p1", "andres-p2"])
        self.assertEqual([(p.index, p.srt_path) for p in povs["cindy"].parts],
                         [(1, "Cindy__1.srt"), (2, "Cindy__2.srt"), (3, "Cindy__3.srt")])
        self.assertEqual(len(povs["didi"].parts), 1)
        self.assertEqual(len(povs["rika"].parts), 1)

    def test_mixed_suffix_styles(self):
        povs = self.scan(["Bob - Part 2.srt", "Bob - Part 1.srt", "Eve p1.srt", "Eve p2.srt",
                          "Zed_part1.srt", "Zed_part2.srt", "Kim 1.srt", "Kim 2.srt", "Ann_1.srt",
                          "Cindy - 2026-11-16 20-14-03.srt"])
        self.assertEqual({k: len(v.parts) for k, v in povs.items()},
                         {"bob": 2, "eve": 2, "zed": 2, "kim": 2, "ann": 1, "cindy": 1})
        self.assertEqual(povs["bob"].parts[0].srt_path, "Bob - Part 1.srt")
        self.assertEqual(povs["cindy"].name, "Cindy")      # timestamp seconds are not a part number

    def test_video_and_srt_pair_by_stem(self):
        for n in ("Cindy__1.mkv", "Cindy__2.mp4"):
            (self.dir / n).write_bytes(b"")
        povs = self.scan(["Cindy__1.srt", "Cindy__2.srt"])
        self.assertEqual([(p.video_path, p.srt_path) for p in povs["cindy"].parts],
                         [("Cindy__1.mkv", "Cindy__1.srt"), ("Cindy__2.mp4", "Cindy__2.srt")])

    def test_sample_style_still_works(self):
        povs = self.scan(["Cindy_part1.srt", "Cindy_part2.srt", "Andres_part1.srt",
                          "Didi 2026-11-16 20-00-45.srt", "Rika_part1.srt"])
        self.assertEqual(sorted(povs), ["andres", "cindy", "didi", "rika"])
        self.assertEqual(len(povs["cindy"].parts), 2)


if __name__ == "__main__":
    unittest.main()
