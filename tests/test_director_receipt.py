import builtins
from contextlib import redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autoclipper.director_poc.__main__ import main
from autoclipper.director_poc.core import connect_review
from autoclipper.director_poc.receipt import validate_review_receipt
from autoclipper.fkb import FKBStore, FootageKnowledgeBase, Session
from autoclipper.resolve_builder import build_export

from test_resolve_builder import add_fixture_media_mapping, fake_resolve, make_manifest


PROJECT = "AutoClipper DirectorA Test 20261120-010203"


class DirectorReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="autoclipper-director-a-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = make_manifest(self.root)
        for name in ("a1", "a2", "b1"):
            (self.root / f"{name}.mp4").touch()
        self.sdk, _, _, project, self.reopened = fake_resolve()
        project.GetUniqueId.return_value = self.reopened.GetUniqueId.return_value = "fixture-project"
        self.timeline = self.reopened.GetTimelineByIndex.return_value
        self.timeline.GetUniqueId.return_value = "fixture-timeline"

    def rebased_review(self):
        manifest = self.manifest
        manifest.update(mode="director_review", review_session_frames=[60, 300])
        for segment in manifest["segments"]:
            first = 60 + segment["record_start_frame"]
            segment.update(kind="media", session_start_frame=first, session_end_frame=first + 60)
            for angle in segment["angles"]:
                angle["source_frame_count"] = 600
        manifest["segments"][1].update(kind="coverage_gap", angles=[])
        add_fixture_media_mapping(manifest)
        return manifest

    def build(self, manifest):
        with (patch.dict(sys.modules, {"DaVinciResolveScript": self.sdk}),
              patch("autoclipper.resolve_builder.mkdtemp", return_value=str(self.root))):
            return build_export(manifest, project_name=PROJECT)

    def assert_preflight_refuses(self, manifest, receipt, message):
        (self.root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        if receipt is not None:
            (self.root / "build.json").write_text(json.dumps(receipt), encoding="utf-8")
        for extra in ([], ["--persistent-notes"]):
            with self.subTest(flags=extra):
                stderr = io.StringIO()
                with (patch.object(sys, "argv", ["director", "companion", str(self.root), *extra]),
                      patch("builtins.__import__", wraps=builtins.__import__) as imports,
                      patch("autoclipper.director_poc.__main__.connect_review") as connect,
                      redirect_stderr(stderr), self.assertRaises(SystemExit)):
                    main()
                self.assertIn(message, stderr.getvalue())
                self.assertNotIn("DaVinciResolveScript", [call.args[0] for call in imports.call_args_list])
                connect.assert_not_called()

    def test_missing_build_file_or_receipt_blocks_both_modes(self):
        self.assert_preflight_refuses(self.manifest, None, "Cannot verify the scratch review build receipt")
        receipt = self.build(self.manifest)
        del receipt["review_receipt"]
        self.assert_preflight_refuses(self.manifest, receipt, "Missing or unsupported")

    def test_legacy_index_notes_refusal_preserves_catalog_and_has_cli_recovery_message(self):
        session = self.root / "session.json"
        session.write_text(json.dumps({"session_id": "synthetic-session"}), encoding="utf-8")
        (self.root / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        (self.root / "build.json").write_text('{"reopened": true}', encoding="utf-8")
        catalog = self.root / ".autoclipper" / "director-fkb.json"
        catalog.parent.mkdir()
        catalog.write_bytes(b'{"legacy": true}')
        stderr = io.StringIO()
        with (patch.object(sys, "argv", ["director", "index-notes", str(self.root), str(session)]),
              redirect_stderr(stderr), self.assertRaises(SystemExit) as exit_status):
            main()
        self.assertEqual(exit_status.exception.code, 2)
        self.assertIn("Build a new disposable review", stderr.getvalue())
        self.assertIn("Keep the existing catalog and its backup", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertEqual(catalog.read_bytes(), b'{"legacy": true}')

    def test_replacing_a_blank_with_footage_at_same_bounds_blocks_both_modes(self):
        manifest = self.rebased_review()
        receipt = self.build(manifest)
        replacement = deepcopy(manifest)
        gap = replacement["segments"][1]
        gap.update(kind="media", angles=deepcopy(replacement["segments"][0]["angles"]))
        self.assert_preflight_refuses(replacement, receipt, "timing or coverage differs")

    def test_readback_map_and_identity_swaps_block_both_modes(self):
        manifest = self.rebased_review()
        receipt = self.build(manifest)
        self.assertEqual(validate_review_receipt(manifest, receipt).session_origin, 60)
        wrong_map = deepcopy(receipt)
        wrong_map["segments"][2]["record_start_frame"] += 1
        self.assert_preflight_refuses(manifest, wrong_map, "Reopened segment map differs")
        wrong_readback = deepcopy(receipt)
        wrong_readback["segments"][0]["video"]["source_end"] += 1
        self.assert_preflight_refuses(manifest, wrong_readback, "recorded receipt")
        self.assert_preflight_refuses(manifest, {**receipt, "timeline_id": "another"}, "identity differs")

    def test_wrong_reopened_start_cannot_issue_receipt(self):
        self.timeline.GetStartTimecode.return_value = "00:59:59:59"
        with self.assertRaisesRegex(ValueError, "Reopened timeline start disagrees"):
            self.build(self.manifest)

    def test_wrong_receipt_start_blocks_both_modes_and_direct_sdk_entry(self):
        receipt = self.build(self.manifest)
        receipt["review_receipt"]["start_frame"] += 1
        self.assert_preflight_refuses(self.manifest, receipt, "Reopened timeline start disagrees")
        with patch.dict(sys.modules, {"DaVinciResolveScript": self.sdk}):
            self.sdk.scriptapp.reset_mock()
            with self.assertRaisesRegex(ValueError, "Reopened timeline start disagrees"):
                connect_review(receipt, self.manifest, True)
        self.sdk.scriptapp.assert_not_called()

    def test_legacy_zero_origin_accepts_new_verified_receipt(self):
        receipt = self.build(self.manifest)
        self.assertEqual(validate_review_receipt(self.manifest, receipt).session_origin, 0)

    def test_legacy_receipt_with_matching_catalog_clock_is_disposable_only(self):
        receipt = self.build(self.manifest)
        store = FKBStore(self.root)
        store.initialize(FootageKnowledgeBase(Session(
            self.manifest["session_id"], 60, 216000, receipt["project_id"], receipt["timeline_id"])))
        previous = store.path.read_bytes()
        for name, value in (("manifest.json", self.manifest), ("build.json", receipt)):
            (self.root / name).write_text(json.dumps(value), encoding="utf-8")
        args = ["director", "companion", str(self.root)]
        with patch("autoclipper.director_poc.window.run_window") as window:
            with patch.object(sys, "argv", args):
                main()
            self.assertIsNone(window.call_args.kwargs["persistent"])
            window.reset_mock()
            stderr = io.StringIO()
            with (patch.object(sys, "argv", [*args, "--persistent-notes"]),
                  redirect_stderr(stderr), self.assertRaises(SystemExit)):
                main()
            self.assertIn("Persistent notes require verified source mapping", stderr.getvalue())
            window.assert_not_called()
        self.assertEqual(store.path.read_bytes(), previous)

    def test_legacy_nonzero_origin_cannot_issue_receipt(self):
        shifted = deepcopy(self.manifest)
        for segment in shifted["segments"]:
            segment["session_start"] += 1
            segment["session_end"] += 1
        with self.assertRaisesRegex(ValueError, "supported zero-origin"):
            self.build(shifted)
