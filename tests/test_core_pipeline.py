import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autoclipper.alignment import chain_parts, coverage_report, set_offset
from autoclipper.losslesscut import export_reviewer, expected_filenames
from autoclipper.mapping import map_range
from autoclipper.models import Part, POV, Review, Session, Suggestion, fmt_ts, parse_ts
from autoclipper.scan import scan_folder
from autoclipper.srt import parse_srt, split_label
from autoclipper.store import load_session, save_session


class CoreTests(unittest.TestCase):
    def test_time_and_srt(self):
        self.assertEqual(fmt_ts(parse_ts("-01:02,345")), "-00:01:02.345")
        self.assertEqual(split_label("(tEaM) Line"), ("(tEaM)", "team", None, "Line"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.srt"
            path.write_bytes(b"1\n00:00:00,500 --> 00:00:02,000\nCindy: caf\xe9\n\n"
                             b"2\n00:00:01,000 --> 00:00:03,000\n[Team]: Nice\n")
            first, second = parse_srt(path, "cindy-p1", "cindy")
            self.assertEqual((first.channel, first.speaker, first.text), ("named", "Cindy", "caf\u00e9"))
            self.assertEqual((second.channel, second.speaker, second.part_id), ("team", None, "cindy-p1"))
            self.assertGreater(first.local_end, second.local_start)

    def test_scan_and_reopen(self):
        with tempfile.TemporaryDirectory() as directory, patch("autoclipper.scan.probe_duration", return_value=20):
            root = Path(directory)
            for name in ("Cindy - Part 2.mp4", "cindy - part 2.SRT",
                         "Didi 2026-11-16 20-01-00.srt", "Didi 2026-11-16 20-00-00.srt"):
                (root / name).touch()
            session = scan_folder(root)
            cindy = session.part_by_id("cindy-p2")
            self.assertEqual((cindy.srt_path, cindy.duration, cindy.duration_source),
                             ("cindy - part 2.SRT", 20, "ffprobe"))
            self.assertEqual(session.part_by_id("didi-p1").srt_path, "Didi 2026-11-16 20-00-00.srt")
            cindy.video_path = str(root / cindy.video_path)
            session.reviews["s1"] = Review("keep", approved_start=4)
            path = save_session(session)
            loaded = load_session(root)
            self.assertEqual(path, root / ".autoclipper" / "session.json")
            self.assertEqual(loaded.part_by_id("cindy-p2").video_path, "Cindy - Part 2.mp4")
            self.assertEqual(loaded.reviews, session.reviews)

    def test_chain_and_intersect(self):
        parts = [Part("p1", 1, duration=20), Part("p2", 2, duration=10),
                 Part("p3", 3, duration=20)]
        session = Session(povs=[POV("p", "Player", parts)])
        set_offset(session, "p1", -5)
        set_offset(session, "p3", 30)
        chain_parts(session)
        self.assertEqual([part.offset for part in parts], [-5, 15, 30])
        self.assertEqual(coverage_report(session)["povs"]["p"]["gaps"], [[25, 30]])
        self.assertEqual(map_range(session, 25.1, 29, 0, 0), [])
        ranges = map_range(session, 14, 32, 0, 0)
        self.assertEqual([(r.local_start, r.local_end, r.piece, r.pieces) for r in ranges],
                         [(19, 20, 1, 3), (0, 10, 2, 3), (0, 2, 3, 3)])

    def test_padding_does_not_fill_andres_gap(self):
        parts = [Part("andres-p1", 1, duration=300, offset=12.5),
                 Part("andres-p2", 2, duration=270, offset=330)]
        session = Session(povs=[POV("andres", "Andres", parts)])
        self.assertEqual(map_range(session, 315, 328), [])

    def test_padding_does_not_add_a_third_piece_to_a_boundary_crossing(self):
        parts = [Part("cindy-p1", 1, duration=10, offset=0),
                 Part("cindy-p2", 2, duration=10, offset=10),
                 Part("cindy-p3", 3, duration=10, offset=20)]
        session = Session(povs=[POV("cindy", "Cindy", parts)])
        ranges = map_range(session, 9, 18)
        self.assertEqual(
            [(r.part_id, r.local_start, r.local_end, r.piece, r.pieces) for r in ranges],
            [("cindy-p1", 4, 10, 1, 2), ("cindy-p2", 0, 10, 2, 2)],
        )

    def test_unknown_duration_uses_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "p.srt").write_text(
                "1\n00:00:00,000 --> 00:00:03,500\nHello\n", encoding="utf-8")
            session = Session(folder=directory, povs=[POV("p", "Player", [Part("p1", srt_path="p.srt")])])
            item, = map_range(session, 3, 4, 0, 0)
            self.assertTrue(item.duration_unknown)
            self.assertEqual(item.local_end, 3.5)

    def test_export_approved_and_partition(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session(folder=directory, pad_before=0, pad_after=0,
                              povs=[POV("p", "Player", [Part("p1", video_path="a.mkv", duration=20)])])
            session.suggestions.extend([
                Suggestion("one", "claude", "Claude", "E-001", 'A: B? C, D', 0, 9),
                Suggestion("two", "claude", "Claude", "E-001", 'A: B? C, D', 3, 6),
                Suggestion("three", "chatgpt", "ChatGPT", "E-001", "Other", 0, 5),
            ])
            session.reviews["one"] = Review("keep", approved_start=2, approved_end=5)
            first, second = export_reviewer(session, "claude")
            self.assertEqual([first.rows[0][:2], second.rows[0][:2]], [(2, 5), (3, 6)])
            with Path(first.csv_path).open(encoding="utf-8", newline="") as handle:
                self.assertEqual(list(csv.reader(handle)),
                                 [["2.000", "5.000", "E-001 - A B C, D - Player [Claude]"]])
            names = expected_filenames(session, "claude")
            self.assertEqual(len(set(names)), 2)
            self.assertEqual(len(export_reviewer(session, "claude", statuses=["keep"])), 1)

