"""Fake-Resolve tests for adding a rough-cut timeline to the open Auto-Clipper project."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from autoclipper.resolve_builder import build_export, main
from test_resolve_builder import add_fixture_media_mapping, fake_resolve, make_manifest

OWNED = "AutoClipper Export synthetic-session 20260101-000000"


def rough_manifest(root, *, reference=False, segments=2):
    manifest = make_manifest(root)
    manifest["segments"] = manifest["segments"][:segments]
    for index, segment in enumerate(manifest["segments"]):
        segment.update(kind="media", session_start_frame=index * 60, session_end_frame=index * 60 + 60,
                       record_start_frame=index * 60)
        for angle in segment["angles"]:
            angle["source_frame_count"] = 600
            Path(angle["mp4_path"]).touch()
    manifest.update(mode="director_edit_plan", edit_plan={"plan_id": "p", "revision_id": "r"},
                    rough_cut={"title": "Best of", "brief": None})
    if reference:
        segment = manifest["segments"][0]
        angle = segment["angles"][0]
        track = {"status": "verified", "mp4_audio_stream_index": 1, "mp4_audio_ordinal": 1,
                 "mp4_audio_title": "Track1", "resolve_channels": [1, 2], "resolve_channels_status": "expected"}
        angle["reference_track"] = track
        segment["reference_audio"] = {
            "pov_id": angle["pov_id"], "pov_name": angle["pov_name"], "part_id": angle["part_id"],
            "mp4_path": angle["mp4_path"], "source_in_frame": angle["source_in_frame"],
            "source_out_frame": angle["source_out_frame"], "record_start_frame": 0,
            "duration_frames": 60, "track": track}
    add_fixture_media_mapping(manifest)
    return manifest


class Scene:
    """An already-open Auto-Clipper project with earlier timelines, built on the shared fake."""

    def __init__(self, name=OWNED, existing=("Old cut",), preload=(), frame_rate="60", open_project=True):
        self.sdk, self.manager, self.state, self.project, _ = fake_resolve()
        project, state = self.project, self.state
        self.pool = pool = project.GetMediaPool.return_value
        project.GetName.return_value = name
        project.GetSetting.side_effect = lambda key: frame_rate
        self.timelines = []
        for index, title in enumerate(existing):
            old = Mock()
            old.GetName.return_value = title
            old.GetUniqueId.return_value = f"old-{index}"
            self.timelines.append(old)
        self.old_timelines = list(self.timelines)
        new_timeline = self.new_timeline = pool.CreateEmptyTimeline.return_value

        def create_timeline(title, _timecode):
            new_timeline.GetName.return_value = title
            new_timeline.GetUniqueId.return_value = f"new-{len(self.timelines)}"
            self.timelines.append(new_timeline)
            return new_timeline

        pool.CreateEmptyTimeline.side_effect = create_timeline
        project.GetTimelineCount.side_effect = lambda: len(self.timelines)
        project.GetTimelineByIndex.side_effect = lambda index: self.timelines[index - 1]
        project.SetCurrentTimeline.return_value = True
        add_folder = pool.AddSubFolder.side_effect

        def add_folder_with_children(root, folder_name):
            folder = add_folder(root, folder_name)
            folder.GetSubFolderList.return_value = []
            return folder

        pool.AddSubFolder.side_effect = add_folder_with_children
        pool.GetRootFolder.return_value.GetSubFolderList.side_effect = (
            lambda: [state["reference_folder"]] if state["reference_folder"] else [])

        def delete_clips(clips):
            for clip in clips:
                for key in ("media", "reference_media"):
                    if clip in state[key]:
                        state[key].remove(clip)
            return True

        pool.DeleteClips.side_effect = delete_clips
        pool.DeleteTimelines.side_effect = lambda items: [self.timelines.remove(t) for t in items] and True
        pool.DeleteFolders.side_effect = lambda folders: state.update(reference_folder=None) or True
        self.manager.SaveProject.side_effect = lambda: state["current"] is project
        state["current"] = project if open_project else None
        self.preloaded = {}
        for path, pov, channels in preload:
            self.preload(path, pov, channels)

    def preload(self, path, pov, channels):
        clip, = self.pool.ImportMedia.side_effect([str(path)])
        clip.GetAudioMapping.return_value = json.dumps({"track_mapping": {
            "1": {"channel_idx": channels, "mute": False, "type": "stereo"}}})
        clip.SetName(pov)
        self.preloaded[path] = clip
        self.pool.ImportMedia.reset_mock()
        return clip

    def build(self, manifest, **kwargs):
        # The shared fake keeps one global item list; give each build its own timeline contents.
        for index, old in enumerate(self.timelines):  # freeze a finished timeline; the fake reuses one mock
            if old is self.new_timeline:
                frozen = Mock()
                frozen.GetName.return_value, frozen.GetUniqueId.return_value = old.GetName(), old.GetUniqueId()
                self.timelines[index] = frozen
        for key in ("video", "audio", "audio2"):
            self.state[key].clear()
        self.state["markers"].clear()
        with patch.dict(sys.modules, {"DaVinciResolveScript": self.sdk}):
            return build_export(manifest, add_to_open=True, timeline_name="Best of cut-1", **kwargs)


class AddTimelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def assert_untouched(self, scene):
        scene.manager.CreateProject.assert_not_called()
        scene.manager.CloseProject.assert_not_called()
        scene.manager.LoadProject.assert_not_called()
        scene.pool.CreateEmptyTimeline.assert_not_called()
        self.assertEqual(scene.timelines, scene.old_timelines)

    def test_refuses_without_an_open_project(self):
        scene = Scene(open_project=False)
        with self.assertRaisesRegex(RuntimeError, "No Resolve project is open"):
            scene.build(rough_manifest(self.root))
        self.assert_untouched(scene)

    def test_refuses_projects_not_created_by_auto_clipper(self):
        for name in ("Untitled Project", "My Game Edit", "AutoClipper Stage2 Test 20260101-000000",
                     "AutoClipper Export  20260101-000000", "AutoClipper Export synthetic-session"):
            with self.subTest(name=name):
                scene = Scene(name=name)
                with self.assertRaisesRegex(RuntimeError, "not created by Auto-Clipper"):
                    scene.build(rough_manifest(self.root))
                self.assert_untouched(scene)
                scene.pool.ImportMedia.assert_not_called()

    def test_only_rough_cuts_may_add_to_the_open_project(self):
        scene = Scene()
        plain = make_manifest(self.root)
        for segment in plain["segments"]:
            for angle in segment["angles"]:
                Path(angle["mp4_path"]).touch()
        with self.assertRaisesRegex(ValueError, "only available for AI rough cuts"):
            scene.build(plain)
        self.assert_untouched(scene)
        director = rough_manifest(self.root)
        director.pop("rough_cut")
        with self.assertRaisesRegex(ValueError, "only available for AI rough cuts"):
            scene.build(director)
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            scene.build(rough_manifest(self.root), project_name="AutoClipper Export synthetic-session 20260101-000000")

    def test_refuses_a_different_timeline_frame_rate(self):
        scene = Scene(frame_rate="30")
        with self.assertRaisesRegex(RuntimeError, "frame rate differs"):
            scene.build(rough_manifest(self.root))
        self.assert_untouched(scene)
        scene.pool.ImportMedia.assert_not_called()

    def test_adds_a_timeline_and_leaves_existing_work_untouched(self):
        scene = Scene(existing=("Old cut", "Another"))
        manifest = rough_manifest(self.root)
        result = scene.build(manifest)
        self.assertEqual((result["project"], result["timeline"], result["added_timeline"], result["reopened"]),
                         (OWNED, "Best of cut-1", True, False))
        self.assertEqual(result["preexisting_timelines"], ["Old cut", "Another"])
        self.assertEqual(scene.timelines[:2], scene.old_timelines)
        self.assertEqual([t.GetName() for t in scene.timelines], ["Old cut", "Another", "Best of cut-1"])
        for old in scene.old_timelines:
            self.assertEqual([c[0] for c in old.method_calls if not c[0].startswith("Get")], [])
        scene.manager.SaveProject.assert_called_once_with()
        for call in ("CreateProject", "CloseProject", "LoadProject"):
            getattr(scene.manager, call).assert_not_called()
        scene.pool.DeleteClips.assert_not_called()
        scene.pool.DeleteTimelines.assert_not_called()
        self.assertEqual(len(result["segments"]), 2)
        self.assertEqual([row["multicam"] for row in result["segments"]],
                         ["Best of cut-1 | 001 event-1-0", "Best of cut-1 | 002 event-1-1"])
        self.assertEqual(sorted(result["imported_sources"]), sorted(s["path"] for s in result["sources"]))
        self.assertTrue(any("not closed or reopened" in text for text in result["limitations"]))

    def test_timeline_name_is_made_unique_on_collision(self):
        scene = Scene(existing=("Old cut", "best of cut-1", "Best of cut-1 (2)"))
        result = scene.build(rough_manifest(self.root))
        self.assertEqual(result["timeline"], "Best of cut-1 (3)")
        self.assertEqual([t.GetName() for t in scene.timelines][:3], ["Old cut", "best of cut-1", "Best of cut-1 (2)"])

    def test_multicam_names_stay_unique_across_two_builds(self):
        scene = Scene()
        first = scene.build(rough_manifest(self.root))
        multicam_names = {row["multicam"] for row in first["segments"]}
        # A stale multicam from an earlier cut already uses the next timeline's name pattern.
        stale = Mock()
        stale.GetUniqueId.return_value = "stale"
        stale.GetName.return_value = "Best of cut-1 (2) | 001 event-1-0"
        scene.state["media"].append(stale)
        second = scene.build(rough_manifest(self.root))
        self.assertEqual(second["timeline"], "Best of cut-1 (3)")
        self.assertFalse(multicam_names & {row["multicam"] for row in second["segments"]})

    def test_existing_source_items_are_reused_not_imported_again(self):
        manifest = rough_manifest(self.root)
        scene = Scene(preload=[(self.root / "a1.mp4", "A", [3, 4]), (self.root / "b1.mp4", "B", [5, 6])])
        before = list(scene.state["media"])
        result = scene.build(manifest)
        imported = [c.args[0][0] for c in scene.pool.ImportMedia.call_args_list]
        self.assertEqual([Path(p).name for p in imported], ["a2.mp4"])
        self.assertEqual(sorted(Path(p).name for p in result["reused_sources"]), ["a1.mp4", "b1.mp4"])
        self.assertEqual(scene.state["media"][:2], before)
        for clip in before:  # reused items keep their name and exact mic mapping and were never remapped
            clip.SetAudioMapping.assert_not_called()
            clip.SetName.assert_called_once()  # only the preload's own naming call
        self.assertEqual(before[0].GetMarkInOut(), {"video": {"in": 9, "out": 20}, "audio": {}})  # restored

    def test_unsafe_existing_item_is_refused_before_anything_is_created(self):
        scene = Scene(preload=[(self.root / "a1.mp4", "SOMEONE ELSE", [3, 4])])
        with self.assertRaisesRegex(RuntimeError, "left untouched and nothing was added"):
            scene.build(rough_manifest(self.root))
        self.assert_untouched(scene)
        scene.pool.ImportMedia.assert_not_called()

    def test_reference_bin_and_items_are_reused(self):
        manifest = rough_manifest(self.root, reference=True)
        scene = Scene(preload=[(self.root / "a1.mp4", "A", [3, 4])])
        first = scene.build(manifest)
        self.assertEqual(len(scene.state["reference_media"]), 1)
        self.assertEqual(first["reused_reference_sources"], [])
        reference = scene.state["reference_media"][0]
        second = scene.build(manifest)
        self.assertEqual(second["timeline"], "Best of cut-1 (2)")
        self.assertEqual(scene.state["reference_media"], [reference])  # no second reference import
        self.assertEqual(len(second["reused_reference_sources"]), 1)
        self.assertEqual(scene.pool.AddSubFolder.call_count, 1)

    def test_failure_removes_only_what_this_run_created(self):
        scene = Scene(preload=[(self.root / "a1.mp4", "A", [3, 4])])
        kept = list(scene.state["media"])
        multicam = scene.pool.CreateMulticamClip.side_effect
        calls = []

        def fail_second(sources, options):
            calls.append(options["name"])
            if len(calls) == 2:
                raise RuntimeError("Resolve refused")
            made = multicam(sources, options)
            scene.state["media"].extend(made)
            return made

        scene.pool.CreateMulticamClip.side_effect = fail_second
        with self.assertRaisesRegex(RuntimeError, "Resolve refused") as caught:
            scene.build(rough_manifest(self.root))
        self.assertEqual(scene.timelines, scene.old_timelines)
        self.assertEqual(scene.state["media"], kept)
        for clip in kept:
            self.assertIn(clip, scene.state["media"])
        self.assertEqual(getattr(caught.exception, "cleanup_failures", []), [])
        deleted = scene.pool.DeleteClips.call_args.args[0]
        self.assertNotIn(kept[0], deleted)

    def test_rollback_failure_is_reported_not_hidden(self):
        scene = Scene()
        scene.pool.CreateMulticamClip.side_effect = RuntimeError("boom")
        scene.pool.DeleteTimelines.side_effect = lambda items: False
        with self.assertRaisesRegex(RuntimeError, "boom") as caught:
            scene.build(rough_manifest(self.root))
        self.assertIn("new timeline", caught.exception.cleanup_failures[0])
        self.assertIn("left in the project", caught.exception.cleanup_failures[0])

    def test_changed_existing_timelines_fail_readback_and_trigger_rollback(self):
        scene = Scene()
        save = scene.manager.SaveProject.side_effect

        def save_and_rename():
            scene.old_timelines[0].GetName.return_value = "Renamed by someone"
            return save()

        scene.manager.SaveProject.side_effect = save_and_rename
        with self.assertRaisesRegex(RuntimeError, "Existing timelines changed"):
            scene.build(rough_manifest(self.root))
        self.assertEqual(scene.timelines, scene.old_timelines)

    def test_cli_flags_and_default_behaviour(self):
        manifest_path = self.root / "m.json"
        manifest_path.write_text(json.dumps(rough_manifest(self.root)), encoding="utf-8")
        with patch("autoclipper.resolve_builder.build_export", return_value={"ok": 1}) as build:
            main([str(manifest_path), "--execute", "--add-to-open", "--timeline-name", "T"])
            main([str(manifest_path), "--execute", "--project", "AutoClipper Export x 20260101-000000"])
        self.assertEqual(build.call_args_list[0].kwargs["add_to_open"], True)
        self.assertEqual(build.call_args_list[0].kwargs["timeline_name"], "T")
        self.assertEqual(build.call_args_list[1].kwargs["add_to_open"], False)


class NewProjectDefaultsUnchanged(unittest.TestCase):
    def test_default_build_still_creates_a_new_project_and_reopens_it(self):
        sdk, manager, state, project, reopened = fake_resolve()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = make_manifest(root)
            for name in ("a1", "a2", "b1"):
                (root / f"{name}.mp4").touch()
            with (patch.dict(sys.modules, {"DaVinciResolveScript": sdk}),
                  patch("autoclipper.resolve_builder.mkdtemp", return_value=folder)):
                result = build_export(manifest)
        self.assertTrue(result["reopened"])
        self.assertNotIn("added_timeline", result)
        manager.CreateProject.assert_called_once()
        manager.CloseProject.assert_called_once_with(project)
        manager.LoadProject.assert_called_once()
        self.assertIs(state["current"], reopened)


if __name__ == "__main__":
    unittest.main()
