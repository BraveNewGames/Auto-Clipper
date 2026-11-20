import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from autoclipper.models import Part, POV, Session
from autoclipper.resolve_stage1 import Placement, append_clip, assemble, isolate_mic, main, plan_placements


class ResolvePlacementTests(unittest.TestCase):
    def test_offsets_and_source_phase_use_common_record_length(self):
        session = Session(pad_before=4, pad_after=4, povs=[
            POV("a", parts=[Part("a1", offset=-1, duration=10)]),
            POV("b", parts=[Part("b1", offset=0.125, duration=10)]),
        ])
        self.assertEqual(plan_placements(session, 1.05, 2.15, 20), [
            Placement("a", "a1", 41, 63, 0, 22),
            Placement("b", "b1", 19, 41, 0, 22),
        ])

    def test_part_boundary_and_gap_stay_on_the_session_clock(self):
        session = Session(povs=[POV("a", parts=[
            Part("a3", index=3, offset=2.5, duration=2),
            Part("a2", index=2, offset=1, duration=1),
            Part("a1", offset=0, duration=1),
        ])])
        self.assertEqual(plan_placements(session, 0.56, 3.05, 10), [
            Placement("a", "a1", 6, 10, 0, 4),
            Placement("a", "a2", 0, 10, 4, 14),
            Placement("a", "a3", 0, 6, 19, 25),
        ])

    def test_strict_quantized_bounds_and_unknown_duration(self):
        part = Part("a1", duration=1.02)
        session = Session(povs=[POV("a", parts=[part])])
        self.assertEqual(plan_placements(session, -1, 2, 10), [
            Placement("a", "a1", 0, 10, 10, 20),
        ])
        self.assertEqual(plan_placements(session, 0.55, 1.02, 10), [
            Placement("a", "a1", 5, 10, 0, 5),
        ])
        part.duration = 1.06
        self.assertEqual(plan_placements(session, 0, 1.06, 10), [
            Placement("a", "a1", 0, 11, 0, 11),
        ])
        part.duration = None
        with self.assertRaisesRegex(ValueError, "Unknown media duration for a1"):
            plan_placements(session, 0, 1, 10)
        part.duration = 1.02
        part.duration_source = "srt_estimate"
        with self.assertRaisesRegex(ValueError, "Unverified media duration"):
            plan_placements(session, 0, 1, 10)

    def test_dry_run_needs_neither_mp4s_nor_resolve_module(self):
        session = Session(povs=[
            POV("a", parts=[Part("a1", video_path="one.mkv", duration=2)]),
            POV("b", parts=[Part("b1", video_path="two.mkv", duration=2)]),
        ])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            copy = root / "session.json"
            copy.write_text(json.dumps(session.to_dict()), encoding="utf-8")
            output = io.StringIO()
            with patch.dict(sys.modules, {"DaVinciResolveScript": None}), contextlib.redirect_stdout(output):
                main([str(copy), "0", "1", "--fps", "60", "--dry-run",
                      "--source", "a1", str(root / "one.mp4"),
                      "--source", "b1", str(root / "two.mp4"),
                      "--mic", "b1", "3", "5,6", "--mic", "a1", "2", "3,4"])
        placements = json.loads(output.getvalue())["placements"]
        self.assertEqual([p["source_out"] for p in placements], [60, 60])
        self.assertEqual([p["mic"] for p in placements], [
            {"track": 2, "channels": [3, 4]}, {"track": 3, "channels": [5, 6]},
        ])

    def test_existing_project_name_is_refused_before_creation(self):
        sdk = Mock()
        manager = sdk.scriptapp.return_value.GetProjectManager.return_value
        manager.GetCurrentProject.return_value = None
        manager.GetProjectListInCurrentFolder.return_value = ["AutoClipper Stage1 Test"]
        with patch.dict(sys.modules, {"DaVinciResolveScript": sdk}):
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                assemble("AutoClipper Stage1 test", [], 60, False)
        manager.CreateProject.assert_not_called()
        manager.SaveProject.assert_not_called()

    def test_project_name_prefix_is_required_before_connecting(self):
        sdk = Mock()
        with patch.dict(sys.modules, {"DaVinciResolveScript": sdk}):
            for name in (None, "Owner Project", "AutoClipper Stage1 "):
                with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "fresh --project"):
                    assemble(name, [], 60, True)
        sdk.scriptapp.assert_not_called()

    def test_open_owner_project_or_nonempty_untitled_is_refused(self):
        sdk = Mock()
        manager = sdk.scriptapp.return_value.GetProjectManager.return_value
        current = manager.GetCurrentProject.return_value
        current.GetTimelineCount.return_value = 1
        with patch.dict(sys.modules, {"DaVinciResolveScript": sdk}):
            for name in ("Owner Project", "Untitled Project"):
                with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "(?i)close"):
                    current.GetName.return_value = name
                    assemble("AutoClipper Stage1 Test", [], 60, True)
        manager.CreateProject.assert_not_called()
        manager.SaveProject.assert_not_called()

    def test_assemble_stacked_saves_only_the_active_scratch_project(self):
        sdk = Mock()
        manager = sdk.scriptapp.return_value.GetProjectManager.return_value
        owner_placeholder, scratch = Mock(), manager.CreateProject.return_value
        owner_placeholder.GetName.return_value = "Untitled Project"
        owner_placeholder.GetTimelineCount.return_value = 0
        root = owner_placeholder.GetMediaPool.return_value.GetRootFolder.return_value
        root.GetClipList.return_value = root.GetSubFolderList.return_value = []
        manager.GetCurrentProject.side_effect = [owner_placeholder, scratch, scratch]
        manager.GetProjectListInCurrentFolder.return_value = []
        scratch.GetTimelineCount.return_value = 0
        pool = scratch.GetMediaPool.return_value
        timeline = pool.CreateEmptyTimeline.return_value
        timeline.GetSettings.return_value = {"timelineFrameRate": "60"}
        timeline.GetStartFrame.return_value = 216000
        timeline.GetTrackCount.return_value = 2
        inputs = []
        clips, items = [], []
        for track in (1, 2):
            path = Path(f"synthetic-{track}.mp4").resolve()
            inputs.append((Placement(str(track), f"p{track}", 30, 90, 0, 60), path))
            clip, item = Mock(), Mock()
            props = {"File Path": str(path), "FPS": "60", "Frames": "120", "Start TC": "00:00:00:00"}
            clip.GetName.return_value = path.name
            clip.GetClipProperty.side_effect = props.__getitem__
            clip.GetAudioMapping.return_value = "{}"
            item.GetMediaPoolItem.return_value = clip
            item.GetSourceStartFrame.return_value, item.GetSourceEndFrame.return_value = 30, 90
            item.GetStart.return_value, item.GetEnd.return_value = 216000, 216060
            item.GetDuration.return_value = 60
            clips.append([clip])
            items.append([item])
        pool.ImportMedia.side_effect = clips
        timeline.GetItemListInTrack.side_effect = items

        def save():
            self.assertEqual(manager.GetCurrentProject.call_count, 3)
            return True

        manager.SaveProject.side_effect = save
        with (patch.dict(sys.modules, {"DaVinciResolveScript": sdk}),
              patch("autoclipper.resolve_stage1.mkdtemp", return_value="scratch"),
              contextlib.redirect_stdout(io.StringIO())):
            assemble("AutoClipper Stage1 Test", inputs, 60, True)
        manager.CreateProject.assert_called_once_with("AutoClipper Stage1 Test", "scratch")
        scratch.SetCurrentTimeline.assert_called_once_with(timeline)
        owner_placeholder.SetSettings.assert_not_called()
        owner_placeholder.SetCurrentTimeline.assert_not_called()
        manager.SaveProject.assert_called_once_with()

    def test_append_passes_the_exclusive_source_end(self):
        pool, clip, item = Mock(), Mock(), Mock()
        pool.AppendToTimeline.return_value = [item]
        append_clip(pool, clip, 100, 120, 500, 2)
        pool.AppendToTimeline.assert_called_once_with([{
            "mediaPoolItem": clip,
            "startFrame": 100,
            "endFrame": 120,
            "recordFrame": 500,
            "trackIndex": 2,
            "mediaType": 1,
        }])
        append_clip(pool, clip, 100, 120, 500, 2, with_audio=True)
        pool.AppendToTimeline.assert_called_with([{
            "mediaPoolItem": clip,
            "startFrame": 100,
            "endFrame": 120,
            "recordFrame": 500,
            "trackIndex": 2,
        }])

    def test_mic_mapping_selects_the_requested_track_and_checks_readback(self):
        mic = {"channel_idx": [5, 6], "mute": False, "type": "stereo"}
        other = {"channel_idx": [1, 2], "mute": False, "type": "stereo"}
        original = {"embedded_audio_channels": 6, "linked_audio": {},
                    "track_mapping": {"1": other, "3": mic}}
        reduced = {**original, "track_mapping": {"1": mic}}
        clip = Mock()
        clip.GetAudioMapping.side_effect = [json.dumps(original), json.dumps(reduced)]
        self.assertEqual(isolate_mic(clip, 3, [5, 6]), reduced)
        self.assertEqual(json.loads(clip.SetAudioMapping.call_args.args[0]), reduced)
        clip.GetAudioMapping.side_effect = [json.dumps(original)] * 2
        with self.assertRaisesRegex(RuntimeError, "readback differs"):
            isolate_mic(clip, 3, [5, 6])
        clip.SetAudioMapping.reset_mock()
        clip.GetAudioMapping.side_effect = [json.dumps(original)]
        with self.assertRaisesRegex(RuntimeError, "do not match"):
            isolate_mic(clip, 3, [3, 4])
        clip.SetAudioMapping.assert_not_called()
