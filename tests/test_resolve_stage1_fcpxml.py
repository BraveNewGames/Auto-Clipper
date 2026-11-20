import unittest
from pathlib import Path
from xml.etree.ElementTree import fromstring

from autoclipper.resolve_stage1 import Placement
from autoclipper.resolve_stage1_fcpxml import Angle, build_fcpxml


class ResolveFcpxmlTests(unittest.TestCase):
    def test_multicam_uses_frame_exact_source_ranges_and_track_two(self):
        a = Angle("POV A", Path("POV A & mic.mp4").absolute(), 200, 2560, 1440,
                  Placement("a", "a1", 61, 121, 0, 60))
        b = Angle("POV B", Path("POV B.mp4").absolute(), 300, 1920, 1080,
                  Placement("b", "b1", 63, 123, 0, 60))
        root = fromstring(build_fcpxml((a, b), 60))
        self.assertEqual(root.attrib["version"], "1.10")
        angles = root.findall("./resources/media/multicam/mc-angle")
        self.assertEqual([angle.attrib["name"] for angle in angles], ["POV A", "POV B"])
        for index, (angle, source, start) in enumerate(zip(angles, (a, b), ("61/60s", "21/20s")), 1):
            video, audio = angle.find("clip/video"), angle.find("clip/audio")
            for item in (video, audio):
                self.assertEqual(item.attrib["ref"], f"asset{index}")
                self.assertEqual(item.attrib["start"], start)
                self.assertEqual(item.attrib["duration"], "1s")
            self.assertEqual((audio.attrib["srcID"], audio.attrib["srcCh"]), ("2", "1,2"))
            asset = root.find(f"./resources/asset[@id='asset{index}']/media-rep")
            self.assertEqual(asset.attrib["src"], source.path.as_uri())
        instance = root.find("./project/sequence/spine/mc-clip")
        self.assertEqual((instance.attrib["duration"], instance.attrib["ref"]), ("1s", "multicam"))
        self.assertEqual(instance.find("mc-source").attrib["angleID"], "angle1")
