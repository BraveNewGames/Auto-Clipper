import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from autoclipper.models import Part, POV, Review, Session, Suggestion
from autoclipper.resolve_builder import build_export, describe_failure, import_sources, main
from autoclipper.resolve_manifest import build_manifest, build_review_manifest, compile_edit_plan, source_mapping_digest

from director_fixtures import confirmed_mics, synthetic_probe


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


def add_fixture_media_mapping(manifest):
    paths = {angle["mp4_path"] for segment in manifest["segments"]
             for angle in (*segment["angles"], *([segment["audio_angle"]] if segment.get("audio_angle") else []))}
    rows = []
    for path in sorted(paths):
        stat = Path(path).stat()
        rows.append({"media": {"mp4_path": path}, "file_identity": [stat.st_size, stat.st_mtime_ns]})
    manifest["source_mapping"] = {"version": 1, "parts": rows}
    manifest["source_mapping_digest"] = source_mapping_digest(manifest)


def fake_resolve():
    sdk, manager = Mock(), Mock()
    resolve = sdk.scriptapp.return_value
    resolve.GetProjectManager.return_value = manager
    resolve.MULTICAM_ANGLE_SYNC_IN = "in"
    resolve.MULTICAM_ANGLE_NAME_CLIP = "clip"
    resolve.MULTICAM_AUDIO_SOURCE = "source"
    project, reopened = Mock(), Mock()
    project.GetUniqueId.return_value = reopened.GetUniqueId.return_value = "scratch-project-id"
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
        item.GetUniqueId.return_value = "review-timeline-id"
        item.GetSettings.return_value = {"timelineFrameRate": "60"}
        item.GetStartFrame.return_value = 216000
        item.GetStartTimecode.return_value = "01:00:00:00"
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
        start = info["recordFrame"]
        length = info["endFrame"] - info["startFrame"]
        source_start = info["startFrame"]
        streams = {1: ("video",), 2: ("audio",)}.get(info.get("mediaType"), ("video", "audio"))
        items = []
        for kind in streams:
            item = Mock()
            item.GetMediaPoolItem.return_value = info["mediaPoolItem"]
            item.GetStart.return_value = start
            item.GetEnd.return_value = start + length
            item.GetDuration.return_value = length
            item.GetSourceStartFrame.return_value = source_start
            item.GetSourceEndFrame.return_value = source_start + length - 2
            item.GetSourceAudioChannelMapping.return_value = json.dumps({
                "track_mapping": {"1": {"channel_idx": [1, 2], "mute": False, "type": "stereo"}},
            })
            state[kind].append(item)
            items.append(item)
        return items

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

    def test_result_file_holds_only_the_json_result(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = make_manifest(root)
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            result = root / "readback.json"
            with patch.dict(sys.modules, {"DaVinciResolveScript": None}), redirect_stdout(io.StringIO()):
                main([str(path), "--dry-run", "--result-file", str(result)])
                self.assertEqual(json.loads(result.read_text(encoding="utf-8"))["manifest"], manifest)
                with self.assertRaises(SystemExit), patch("sys.stderr", new_callable=io.StringIO) as err:
                    main([str(path), "--dry-run", "--result-file", str(result)])
                self.assertIn("Result file already exists", err.getvalue())
                manifest["blockers"] = [{"code": "mic_unmapped", "message": "Missing confirmed mic"}]
                path.write_text(json.dumps(manifest), encoding="utf-8")
                failed = root / "failed.json"
                with self.assertRaises(SystemExit), patch("sys.stderr", new_callable=io.StringIO):
                    main([str(path), "--dry-run", "--result-file", str(failed)])
                self.assertFalse(failed.exists())

    def test_director_edit_build_keeps_selected_mic_and_silent_slate_after_reopen(self):
        sdk, _, state, project, _ = fake_resolve()
        pool = project.GetMediaPool.return_value
        import_media = pool.ImportMedia.side_effect
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = make_manifest(root)
            for name in ("a1", "a2", "b1", "placeholder"):
                (root / f"{name}.mp4").touch()
            segments = manifest["segments"]
            manifest.update(mode="director_edit_plan", edit_plan={"plan_id": "demo", "revision_id": "r1"})
            for segment in segments:
                start = segment["record_start_frame"]
                segment.update(kind="media", session_start_frame=start, session_end_frame=start + 60)
                for angle in segment["angles"]:
                    angle["source_frame_count"] = 600
            video, audio = segments[0]["angles"]
            segments[0].update(angles=[video], audio_angle=audio, note_ids=["note-1"])
            slate = segments[1]
            for field in ("session_start", "session_end", "session_start_frame", "session_end_frame"):
                del slate[field]
            slate.update(kind="placeholder", angles=[], placeholder_reason="planned_missing_beat", candidate_ids=[])
            add_fixture_media_mapping(manifest)

            def import_with_slate(paths):
                clips = import_media(paths)
                if Path(paths[0]).name == "placeholder.mp4":
                    clip, = clips
                    properties = clip.GetClipProperty.side_effect
                    clip.GetClipProperty.side_effect = lambda key: "60" if key == "Frames" else properties(key)
                return clips

            pool.ImportMedia.side_effect = import_with_slate
            with (patch.dict(sys.modules, {"DaVinciResolveScript": sdk}),
                  patch("autoclipper.resolve_builder.mkdtemp", return_value=folder),
                  patch("autoclipper.resolve_builder.render_placeholder", return_value=root / "placeholder.mp4")
                  as render):
                result = build_export(manifest)
            self.assertEqual(result["edit_plan"]["revision_id"], "r1")
            self.assertEqual(result["segments"][0]["audio"]["path"], str(root / "b1.mp4"))
            self.assertEqual(result["segments"][0]["audio"]["source_start"], 120)
            self.assertEqual(result["segments"][0]["note_ids"], ["note-1"])
            self.assertEqual(result["segments"][1]["video"]["record_start"], 216060)
            self.assertEqual(result["segments"][1]["video"]["name"], "MISSING FOOTAGE: Moment 1")
            self.assertEqual(len(state["video"]), 4)
            self.assertEqual(len(state["audio"]), 3)
            render.assert_called_once_with(root, slate, 60)

    def test_director_review_preserves_blank_interior_frames_after_reopen(self):
        sdk, _, _, project, _ = fake_resolve()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = make_manifest(root)
            for name in ("a1", "a2", "b1"):
                (root / f"{name}.mp4").touch()
            segments = manifest["segments"]
            manifest.update(mode="director_review", review_session_frames=[120, 360], uncovered_edges=[])
            for segment in segments:
                start = 120 + segment["record_start_frame"]
                segment.update(kind="media", session_start_frame=start,
                               session_end_frame=start + segment["duration_frames"])
                for angle in segment["angles"]:
                    angle["source_frame_count"] = 600
            segments[1].update(kind="coverage_gap", angles=[])
            add_fixture_media_mapping(manifest)
            with (patch.dict(sys.modules, {"DaVinciResolveScript": sdk}),
                  patch("autoclipper.resolve_builder.mkdtemp", return_value=folder)):
                result = build_export(manifest)
            self.assertEqual(result["review_session_frames"], [120, 360])
            self.assertEqual([r["record_start_frame"] for r in result["segments"]],
                             [216000, 216060, 216120, 216180])
            self.assertEqual(result["segments"][1]["kind"], "coverage_gap")
            self.assertEqual(project.GetMediaPool.return_value.AppendToTimeline.call_count, 3)

    def test_director_build_records_reopened_scratch_identity(self):
        sdk, _, _, _, reopened = fake_resolve()
        reopened.GetUniqueId.return_value = "scratch-project-id"
        reopened.GetTimelineByIndex.return_value.GetUniqueId.return_value = "review-timeline-id"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = make_manifest(root)
            for name in ("a1", "a2", "b1"):
                (root / f"{name}.mp4").touch()
            with (patch.dict(sys.modules, {"DaVinciResolveScript": sdk}),
                  patch("autoclipper.resolve_builder.mkdtemp", return_value=folder)):
                result = build_export(manifest, project_name="AutoClipper DirectorA Test 20261120-010203")
        self.assertEqual(result["project_id"], "scratch-project-id")
        self.assertEqual(result["timeline_id"], "review-timeline-id")

    def test_director_media_size_or_mtime_change_stops_both_build_modes_before_sdk_import(self):
        for mode in ("review", "edit"):
            for change in ("size", "mtime"):
                with self.subTest(mode=mode, change=change), tempfile.TemporaryDirectory() as folder:
                    root = Path(folder)
                    mp4 = root / "a1.mp4"
                    mp4.write_bytes(b"fixture")
                    source = Session(session_id="synthetic-session", folder=folder, povs=[
                        POV("alpha", "Alpha", [Part("a1", video_path=mp4.name, duration=2)])
                    ])
                    confirmations = confirmed_mics(source)
                    with synthetic_probe(source, 60):
                        if mode == "review":
                            manifest = build_review_manifest(source, confirmations=confirmations)
                        else:
                            manifest = compile_edit_plan(source, {
                                "plan_id": "draft", "session_id": source.session_id, "fps": 60,
                                "revision_id": "r1", "revision_history": [],
                                "entries": [{"entry_id": "cut", "kind": "source", "title": "Fixture",
                                             "session_in": 12, "session_out_exclusive": 24}],
                            }, confirmations=confirmations)
                    previous = mp4.stat()
                    if change == "size":
                        mp4.write_bytes(b"different size")
                        os.utime(mp4, ns=(previous.st_atime_ns, previous.st_mtime_ns))
                    else:
                        os.utime(mp4, ns=(previous.st_atime_ns, previous.st_mtime_ns + 2_000_000_000))
                    sdk = Mock()
                    with (patch.dict(sys.modules, {"DaVinciResolveScript": sdk}),
                          patch("builtins.__import__", wraps=__import__) as imports,
                          self.assertRaisesRegex(ValueError, "source media changed.*Rebuild.*recompile")):
                        build_export(manifest)
                    self.assertNotIn("DaVinciResolveScript", [call.args[0] for call in imports.call_args_list])
                    sdk.scriptapp.assert_not_called()

    def test_director_refuses_same_name_reopened_project_with_another_id(self):
        sdk, manager, _, project, reopened = fake_resolve()
        reopened.GetUniqueId.return_value = "different-project-id"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = make_manifest(root)
            for stem in ("a1", "a2", "b1"):
                (root / f"{stem}.mp4").touch()
            name = "AutoClipper DirectorA Test 20261120-010203"
            with (patch.dict(sys.modules, {"DaVinciResolveScript": sdk}),
                  patch("autoclipper.resolve_builder.mkdtemp", return_value=folder),
                  patch("autoclipper.director_poc.receipt.record_review_receipt") as record_receipt,
                  self.assertRaisesRegex(RuntimeError, "Reopened Director project ID differs")):
                build_export(manifest, project_name=name)
        self.assertEqual(reopened.GetName(), name)
        self.assertNotEqual(reopened.GetUniqueId(), project.GetUniqueId())
        manager.LoadProject.assert_called_once_with(name)
        reopened.GetTimelineByIndex.assert_not_called()
        reopened.SetCurrentTimeline.assert_not_called()
        record_receipt.assert_not_called()

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
                report = build_export(manifest, project_name="AutoClipper Stage2 Test 20260101-000000")
            self.assertTrue(report["reopened"])
            self.assertIs(state["current"], reopened)
            manager.SaveProject.assert_called_once_with()
            manager.CloseProject.assert_called_once_with(project)
            manager.LoadProject.assert_called_once_with(report["project"])
            self.assertEqual(report["project"], "AutoClipper Stage2 Test 20260101-000000")
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
                    import_sources(pool, {path: ("pov", "Player", 2, 2, "Track2", (3, 4))}, 60)
        clip.SetAudioMapping.assert_not_called()

    def test_mic_inventory_uses_mp4_ordinal_and_checks_owner_track_title(self):
        pool = Mock()
        path = Path("source.mp4").resolve()
        clip = Mock()
        pool.ImportMedia.return_value = [clip]
        clip.GetClipProperty.side_effect = {"File Path": str(path), "FPS": "60"}.__getitem__
        clip.GetName.return_value = "Player"
        track = {"type": "stereo", "channel_idx": [3, 4], "mute": False, "title": "Track4"}
        original = {"track_mapping": {"2": track}}
        remapped = {"track_mapping": {"1": track}}
        clip.GetAudioMapping.side_effect = list(map(json.dumps, (original, original, remapped)))
        self.assertEqual(import_sources(pool, {path: ("pov", "Player", 4, 2, "Track4", (3, 4))}, 60),
                         {path: clip})
        self.assertEqual(json.loads(clip.SetAudioMapping.call_args.args[0]), remapped)

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

    def test_restore_failure_preserves_original_build_error(self):
        sdk, manager, _, project, _ = fake_resolve()
        failure = RuntimeError("create failed")
        cleanup = RuntimeError("restore failed")
        project.GetMediaPool.return_value.CreateMulticamClip.side_effect = failure
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = make_manifest(root)
            for name in ("a1", "a2", "b1"):
                (root / f"{name}.mp4").touch()
            with (patch.dict(sys.modules, {"DaVinciResolveScript": sdk}),
                  patch("autoclipper.resolve_builder.mkdtemp", return_value=folder),
                  patch("autoclipper.resolve_builder.restore_source_marks", side_effect=cleanup),
                  self.assertRaises(RuntimeError) as raised):
                build_export(manifest)
        self.assertIs(raised.exception, failure)
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(raised.exception.cleanup_failures, [cleanup])
        self.assertEqual(describe_failure(raised.exception),
                         "Resolve export stopped: create failed\nCleanup also failed: restore failed\n")
        manager.SaveProject.assert_not_called()

    def test_cli_failure_labels_causes_in_order(self):
        root = RuntimeError("root cause")
        middle = RuntimeError("middle")
        middle.__cause__ = root
        top = ValueError("top")
        top.__cause__ = middle
        self.assertEqual(describe_failure(top), "Resolve export stopped: top\nCaused by: middle\n"
                                                "Caused by: root cause\n")
        self.assertEqual(describe_failure(RuntimeError("plain")), "Resolve export stopped: plain\n")

    def test_mic_title_accepts_equivalent_track_forms(self):
        path = Path("source.mp4").resolve()
        for label in ("Track2", "Track 2", "track 2", "TRACK2", "Track 02"):
            with self.subTest(label=label):
                pool = Mock()
                clip = Mock()
                pool.ImportMedia.return_value = [clip]
                clip.GetClipProperty.side_effect = {"File Path": str(path), "FPS": "60"}.__getitem__
                clip.GetName.return_value = "Player"
                track = {"type": "stereo", "channel_idx": [3, 4], "mute": False, "title": label}
                original = {"track_mapping": {"2": track}}
                remapped = {"track_mapping": {"1": track}}
                clip.GetAudioMapping.side_effect = list(map(json.dumps, (original, original, remapped)))
                self.assertEqual(import_sources(pool, {path: ("pov", "Player", 2, 2, "Track2", (3, 4))}, 60),
                                 {path: clip})
        for label in ("Track 3", "track3", "Game", "Track 2b"):
            with self.subTest(mismatch=label):
                pool = Mock()
                clip = Mock()
                pool.ImportMedia.return_value = [clip]
                clip.GetClipProperty.side_effect = {"File Path": str(path), "FPS": "60"}.__getitem__
                clip.GetAudioMapping.return_value = json.dumps(
                    {"track_mapping": {"2": {"type": "stereo", "channel_idx": [3, 4], "title": label}}})
                with self.assertRaisesRegex(RuntimeError, "expected Resolve track 2"):
                    import_sources(pool, {path: ("pov", "Player", 2, 2, "Track2", (3, 4))}, 60)
                clip.SetAudioMapping.assert_not_called()

    def test_explicit_export_project_name_must_match_session(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = make_manifest(Path(folder))
            session = manifest["session_id"]
            plan = build_export(manifest, dry_run=True, project_name=f"AutoClipper Export {session} 20260101-000000")
            self.assertEqual(plan["mode"], "dry-run")
            for name in ("AutoClipper Export other 20260101-000000", f"AutoClipper Export {session} now",
                         "Owner Edit"):
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Explicit project name"):
                    build_export(manifest, dry_run=True, project_name=name)

    def test_director_requires_project_manager_or_verified_default(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = make_manifest(root)
            for name in ("a1", "a2", "b1"):
                (root / f"{name}.mp4").touch()
            project_name = "AutoClipper DirectorA Test 20261120-010203"
            for active_name in ("AutoClipper Export Owner", "AutoClipper Stage2 Test Owner",
                                "AutoClipper Stage1 Owner", "Untitled Project", None):
                with self.subTest(active_name=active_name):
                    sdk, manager, state, _, _ = fake_resolve()
                    if active_name is not None:
                        owner = Mock()
                        owner.GetName.return_value = active_name
                        owner.GetUniqueId.return_value = "owner-project-id"
                        owner.GetTimelineCount.return_value = 0
                        pool_root = owner.GetMediaPool.return_value.GetRootFolder.return_value
                        pool_root.GetClipList.return_value = []
                        pool_root.GetSubFolderList.return_value = []
                        state["current"] = owner
                    with (patch.dict(sys.modules, {"DaVinciResolveScript": sdk}),
                          patch("autoclipper.resolve_builder.mkdtemp", return_value=folder)):
                        if active_name not in ("Untitled Project", None):
                            with self.assertRaisesRegex(RuntimeError, "Director requires Project Manager"):
                                build_export(manifest, project_name=project_name)
                            manager.CreateProject.assert_not_called()
                            manager.SaveProject.assert_not_called()
                        else:
                            if active_name == "Untitled Project":
                                manager.GetProjectListInCurrentFolder.return_value = [active_name]
                                with self.assertRaisesRegex(RuntimeError, "Director requires Project Manager"):
                                    build_export(manifest, project_name=project_name)
                                manager.CreateProject.assert_not_called()
                                manager.GetProjectListInCurrentFolder.return_value = []
                            self.assertTrue(build_export(manifest, project_name=project_name)["reopened"])
                            manager.CreateProject.assert_called_once()

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
