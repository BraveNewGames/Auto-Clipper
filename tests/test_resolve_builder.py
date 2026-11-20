import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from autoclipper.models import Part, POV, Review, Session, Suggestion
from autoclipper.resolve_builder import build_export, import_sources, main
from autoclipper.resolve_manifest import build_manifest


def make_manifest(root):
    def angle(pov, part, first, channels):
        return {
            "pov_id": pov, "pov_name": pov.upper(), "part_id": part, "part_index": 1,
            "mp4_path": str(root / f"{part}.mp4"),
            "source_in_frame": first, "source_out_frame": first + 60,
            "handle_before_frames": 12, "handle_after_frames": 30,
            "mic": {"status": "verified", "transcriber_track": 2 if pov == "a" else 3, "speaker": pov.upper(),
                    "mp4_audio_stream_index": 2, "resolve_channels": channels, "evidence": "synthetic"},
        }

    segments = []
    for index in range(4):
        event = max(1, index)
        segments.append({
            "segment_id": f"event-{event}-{index}", "event_id": f"event-{event}",
            "suggestion_id": f"pick-{event}", "reviewer": "synthetic", "title": f"Moment {event}",
            "range_source": "approved", "piece": index + 1 if index < 2 else 1,
            "pieces": 2 if index < 2 else 1, "session_start": float(index), "session_end": index + 1.0,
            "record_start_frame": index * 60, "duration_frames": 60,
            "angles": [
                angle("a", "a1" if index == 0 else "a2", 70 + index * 60, [3, 4]),
                angle("b", "b1", 120 + index * 60, [5, 6]),
            ],
            "unavailable": [{"pov_id": "c", "pov_name": "C", "reason": "recording_gap"}],
        })
    return {
        "schema_version": 1, "session_id": "synthetic-session", "session_folder": str(root),
        "created_at": "2026-01-01T00:00:00Z", "fps": 60, "blockers": [], "warnings": [],
        "segments": segments,
    }


def fake_resolve():
    sdk, manager = Mock(), Mock()
    resolve = sdk.scriptapp.return_value
    resolve.GetProjectManager.return_value = manager
    resolve.MULTICAM_ANGLE_SYNC_IN = "in"
    resolve.MULTICAM_ANGLE_NAME_CLIP = "clip"
    resolve.MULTICAM_AUDIO_SOURCE = "source"
    project, reopened = Mock(), Mock()
    pool = project.GetMediaPool.return_value
    timeline, reopened_timeline = pool.CreateEmptyTimeline.return_value, Mock()
    state = {"current": None, "media": [], "video": [], "audio": [], "markers": {}}
    manager.GetCurrentProject.side_effect = lambda: state["current"]
    manager.GetProjectListInCurrentFolder.return_value = []
    project.GetTimelineCount.return_value = 0
    reopened.GetTimelineCount.return_value = 1
    reopened.GetTimelineByIndex.return_value = reopened_timeline
    reopened.GetMediaPool.return_value.GetRootFolder.return_value.GetClipList.side_effect = (
        lambda: list(state["media"])
    )
    for item in (timeline, reopened_timeline):
        item.GetName.return_value = "synthetic-session"
        item.GetSettings.return_value = {"timelineFrameRate": "60"}
        item.GetStartFrame.return_value = 216000
        item.GetItemListInTrack.side_effect = lambda kind, index: list(state[kind])
        item.GetMarkerCustomData.side_effect = lambda offset: state["markers"][offset]

    def create_project(name, _):
        project.GetName.return_value = reopened.GetName.return_value = name
        state["current"] = project
        return project

    def close_project(closing):
        assert closing is project and state["current"] is project
        state["current"] = None
        return True

    def load_project(name):
        assert state["current"] is None and name == project.GetName.return_value
        state["current"] = reopened
        return reopened

    def import_media(paths):
        path, = paths
        clip = Mock()
        properties = {"File Path": path, "FPS": "60", "Frames": "600", "Online Status": "Online"}
        clip.GetClipProperty.side_effect = properties.get
        clip.GetAudioMapping.return_value = json.dumps({"track_mapping": {
            "1": {"channel_idx": [1, 2], "mute": False, "type": "stereo"},
            "2": {"channel_idx": [3, 4], "mute": False, "type": "stereo"},
            "3": {"channel_idx": [5, 6], "mute": False, "type": "stereo"},
        }})
        clip.GetMarkInOut.return_value = {"video": {"in": 9, "out": 20}, "audio": {}} if path.endswith(
            "a1.mp4",
        ) else {"video": {}, "audio": {}}

        def set_name(name):
            clip.GetName.return_value = name
            return True

        def set_mapping(mapping):
            clip.GetAudioMapping.return_value = mapping
            return True

        def set_marks(first, last, kind="all"):
            marks = dict(clip.GetMarkInOut.return_value)
            for key in ("video", "audio") if kind == "all" else (kind,):
                marks[key] = {"in": first, "out": last}
            clip.GetMarkInOut.return_value = marks
            return True

        def clear_marks():
            clip.GetMarkInOut.return_value = {"video": {}, "audio": {}}
            return True

        clip.SetName.side_effect = set_name
        clip.SetAudioMapping.side_effect = set_mapping
        clip.SetMarkInOut.side_effect = set_marks
        clip.ClearMarkInOut.side_effect = clear_marks
        state["media"].append(clip)
        return [clip]

    def create_multicam(sources, options):
        clip = Mock()
        clip.GetName.return_value = options["name"]
        clip.GetTimeline.return_value = None
        marks = sources[0].GetMarkInOut()["video"]
        props = {"Type": "Multicam", "File Path": "", "Frames": str(marks["out"] - marks["in"] + 1)}
        clip.GetClipProperty.side_effect = props.get
        return [clip]

    def append(infos):
        info, = infos
        assert "mediaType" not in info
        start = info["recordFrame"]
        length = info["endFrame"] - info["startFrame"]
        for kind in ("video", "audio"):
            item = Mock()
            item.GetMediaPoolItem.return_value = info["mediaPoolItem"]
            item.GetStart.return_value = start
            item.GetEnd.return_value = start + length
            item.GetDuration.return_value = length
            item.GetSourceStartFrame.return_value = 0
            item.GetSourceEndFrame.return_value = length - 2
            item.GetSourceAudioChannelMapping.return_value = json.dumps({
                "track_mapping": {"1": {"channel_idx": [1, 2], "mute": False, "type": "stereo"}},
            })
            state[kind].append(item)
        return [state["video"][-1]]

    def mark(frame, _color, _name, _note, _duration, data):
        state["markers"][frame] = data
        return True

    manager.CreateProject.side_effect = create_project
    manager.SaveProject.side_effect = lambda: state["current"] is project
    manager.CloseProject.side_effect = close_project
    manager.LoadProject.side_effect = load_project
    pool.ImportMedia.side_effect = import_media
    pool.CreateMulticamClip.side_effect = create_multicam
    pool.AppendToTimeline.side_effect = append
    timeline.AddMarker.side_effect = mark
    return sdk, manager, state, project, reopened


class ResolveBuilderTests(unittest.TestCase):
    def test_session_to_manifest_to_builder_dry_run(self):
        sdk = Mock()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            transcripts = root / "_Transcripts"
            session = Session(session_id="synthetic-integration", folder=str(root), povs=[
                POV("alpha", "Alpha", [Part("a1", 1, "a1.mkv", duration=2, offset=0),
                                       Part("a2", 2, "a2.mkv", duration=5, offset=2)]),
                POV("beta", "Beta", [Part("b1", 1, "b1.mkv", duration=2, offset=0)]),
            ])
            for part, name in (("a1", "Alpha"), ("a2", "Alpha"), ("b1", "Beta")):
                (root / f"{part}.mp4").touch()
                dest = transcripts / part
                dest.mkdir(parents=True)
                (dest / f"{part}.json").write_text(
                    json.dumps({
                        "speaker_tracks": [{"track": 2, "speaker": name, "speaker_template": "{Recorder}",
                                            "index": 7, "title": "Track2"}],
                        "recorder": {"name": name, "source": "filename", "tracks": [2]},
                    }),
                    encoding="utf-8",
                )
            for sid, first, last in (("later", 4, 5), ("split", 1, 3), ("early", 0.25, 0.75)):
                session.suggestions.append(Suggestion(sid, "synthetic", event_id=f"ev-{sid}",
                                                       title=sid, start=first, end=last))
                session.reviews[sid] = Review("keep")
            inventory = [
                {"index": 0, "codec_type": "video", "r_frame_rate": "60/1"},
                {"index": 1, "codec_type": "audio", "channels": 1, "tags": {"title": "Track1"}},
                {"index": 7, "codec_type": "audio", "channels": 2, "tags": {"title": "Track2"}},
            ]
            path = root / "manifest.json"
            output = io.StringIO()
            def probe(path):
                duration = "5" if path.stem == "a2" else "2"
                return [{**inventory[0], "duration": duration}, *inventory[1:]]

            with (patch("autoclipper.resolve_manifest.probe_streams", side_effect=probe),
                  patch.dict(sys.modules, {"DaVinciResolveScript": sdk}), redirect_stdout(output)):
                manifest = build_manifest(session)
                self.assertEqual(manifest["blockers"], [])
                path.write_text(json.dumps(manifest), encoding="utf-8")
                main([str(path), "--dry-run"])
                segments = json.loads(output.getvalue())["manifest"]["segments"]
                self.assertEqual([s["suggestion_id"] for s in segments], ["early", "split", "split", "later"])
                self.assertEqual([(s["record_start_frame"], s["duration_frames"]) for s in segments],
                                 [(0, 30), (30, 60), (90, 60), (150, 60)])
                self.assertEqual([{a["part_id"] for a in s["angles"]} for s in segments],
                                 [{"a1", "b1"}, {"a1", "b1"}, {"a2"}, {"a2"}])
                self.assertEqual({v["pov_id"] for v in segments[2]["unavailable"]}, {"beta"})
                self.assertTrue(all(a["mic"]["resolve_channels"] == [2, 3]
                                    for s in segments for a in s["angles"]))

                (root / "a2.mp4").unlink()
                blocked = build_manifest(session)
                self.assertIn("mp4_missing", {item["code"] for item in blocked["blockers"]})
                path.write_text(json.dumps(blocked), encoding="utf-8")
                with self.assertRaises(SystemExit) as stopped, patch("sys.stderr", new_callable=io.StringIO) as err:
                    main([str(path), "--dry-run"])
                self.assertEqual(stopped.exception.code, 1)
                self.assertIn("Manifest has blockers", err.getvalue())
        sdk.scriptapp.assert_not_called()

    def test_dry_run_and_blockers_never_contact_resolve(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = make_manifest(root)
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            output = io.StringIO()
            with patch.dict(sys.modules, {"DaVinciResolveScript": None}), redirect_stdout(output):
                main([str(path), "--dry-run"])
                self.assertEqual(json.loads(output.getvalue())["manifest"], manifest)
                manifest["blockers"] = [{"code": "mic_unmapped", "message": "Missing confirmed mic"}]
                with self.assertRaisesRegex(ValueError, "Missing confirmed mic"):
                    build_export(manifest)

    def test_multicam_compilation_saves_and_verifies_after_reopen(self):
        sdk, manager, state, project, reopened = fake_resolve()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = make_manifest(root)
            short = manifest["segments"][1]
            short["duration_frames"] = 6
            short["angles"] = short["angles"][1:]
            short["angles"][0]["source_out_frame"] = short["angles"][0]["source_in_frame"] + 6
            short["unavailable"].append({"pov_id": "a", "pov_name": "A", "reason": "recording_gap"})
            for later in manifest["segments"][2:]:
                later["record_start_frame"] -= 54
            for name in ("a1", "a2", "b1"):
                (root / f"{name}.mp4").touch()
            with (patch.dict(sys.modules, {"DaVinciResolveScript": sdk}),
                  patch("autoclipper.resolve_builder.mkdtemp", return_value=folder)):
                report = build_export(manifest)
            self.assertTrue(report["reopened"])
            self.assertIs(state["current"], reopened)
            manager.SaveProject.assert_called_once_with()
            manager.CloseProject.assert_called_once_with(project)
            manager.LoadProject.assert_called_once_with(report["project"])
            self.assertRegex(report["project"], r"^AutoClipper Export synthetic-session \d{8}-\d{6}$")
            self.assertEqual([row["record_start_frame"] for row in report["segments"]],
                             [216000, 216060, 216066, 216126])
            self.assertEqual([row["suggestion_id"] for row in report["segments"]],
                             ["pick-1", "pick-1", "pick-2", "pick-3"])
            self.assertEqual(report["segments"][0]["unavailable"][0]["reason"], "recording_gap")
            self.assertEqual(report["segments"][1]["duration_frames"], 6)
            self.assertEqual(len(report["segments"][1]["planned_angles"]), 1)
            self.assertIn("short_segment", {warning["code"] for warning in report["warnings"]})
            pool = project.GetMediaPool.return_value
            self.assertEqual(pool.ImportMedia.call_count, 3)
            self.assertEqual(pool.CreateEmptyTimeline.call_count, 1)
            self.assertEqual(pool.CreateMulticamClip.call_count, 4)
            options = pool.CreateMulticamClip.call_args.args[1]
            self.assertEqual([options[key] for key in ("angleSyncMode", "angleNameMode", "multicamAudioMode")],
                             ["in", "clip", "source"])
            self.assertEqual([clip.SetAudioMapping.call_count for clip in state["media"]], [1, 1, 1])
            self.assertEqual([clip.SetMarkInOut.call_count for clip in state["media"]], [2, 4, 2])
            self.assertEqual([clip.ClearMarkInOut.call_count for clip in state["media"]], [1, 4, 2])
            self.assertEqual(state["media"][0].GetMarkInOut(), {"video": {"in": 9, "out": 20}, "audio": {}})
            self.assertEqual(state["media"][1].GetMarkInOut(), {"video": {}, "audio": {}})

    def test_mic_inventory_must_match_expected_track_before_remapping(self):
        pool = Mock()
        path = Path("source.mp4").resolve()
        clip = Mock()
        pool.ImportMedia.return_value = [clip]
        clip.GetClipProperty.side_effect = {"File Path": str(path), "FPS": "60"}.__getitem__
        expected = {"type": "stereo", "channel_idx": [3, 4]}
        for inventory in (
            {"1": expected},
            {"2": expected, "3": expected},
            {"2": {**expected, "title": "Track3"}},
        ):
            with self.subTest(inventory=inventory):
                clip.GetAudioMapping.return_value = json.dumps({"track_mapping": inventory})
                with self.assertRaisesRegex(RuntimeError, "expected Resolve track 2"):
                    import_sources(pool, {path: ("pov", "Player", 2, (3, 4))}, 60)
        clip.SetAudioMapping.assert_not_called()

    def test_multicam_creation_failure_restores_marks(self):
        sdk, manager, state, project, _ = fake_resolve()
        project.GetMediaPool.return_value.CreateMulticamClip.side_effect = RuntimeError("create failed")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = make_manifest(root)
            for name in ("a1", "a2", "b1"):
                (root / f"{name}.mp4").touch()
            with (patch.dict(sys.modules, {"DaVinciResolveScript": sdk}),
                  patch("autoclipper.resolve_builder.mkdtemp", return_value=folder)):
                with self.assertRaisesRegex(RuntimeError, "create failed"):
                    build_export(manifest)
        self.assertEqual(state["media"][0].GetMarkInOut(), {"video": {"in": 9, "out": 20}, "audio": {}})
        self.assertEqual(state["media"][1].GetMarkInOut(), {"video": {}, "audio": {}})
        manager.SaveProject.assert_not_called()

    def test_owner_checkout_and_failed_reopen(self):
        sdk, manager, state, _, _ = fake_resolve()
        with tempfile.TemporaryDirectory() as folder:
            manifest = make_manifest(Path(folder))
            for name in ("a1", "a2", "b1"):
                (Path(folder) / f"{name}.mp4").touch()
            owner = Mock()
            owner.GetName.return_value = "Owner Edit"
            state["current"] = owner
            with (patch.dict(sys.modules, {"DaVinciResolveScript": sdk}),
                  patch("autoclipper.resolve_builder.mkdtemp", return_value=folder)):
                with self.assertRaisesRegex(RuntimeError, "close the active owner project"):
                    build_export(manifest)
                manager.CreateProject.assert_not_called()
                manager.SaveProject.assert_not_called()
                state["current"] = None
                manager.LoadProject.side_effect = None
                manager.LoadProject.return_value = None
                with self.assertRaisesRegex(RuntimeError, "could not reopen"):
                    build_export(manifest)
            manager.SaveProject.assert_called_once_with()
            manager.CloseProject.assert_called_once()

    def test_existing_export_name_is_refused(self):
        sdk, manager, _, _, _ = fake_resolve()
        manager.GetProjectListInCurrentFolder.return_value = [
            "autoclipper export synthetic-session 20260101-000000",
        ]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = make_manifest(root)
            for name in ("a1", "a2", "b1"):
                (root / f"{name}.mp4").touch()
            with (patch.dict(sys.modules, {"DaVinciResolveScript": sdk}),
                  patch("autoclipper.resolve_builder.datetime") as clock):
                clock.now.return_value = datetime(2026, 1, 1)
                with self.assertRaisesRegex(RuntimeError, "already exists"):
                    build_export(manifest)
        manager.CreateProject.assert_not_called()
        manager.SaveProject.assert_not_called()
        manager.LoadProject.assert_not_called()
