"""Director catalog publication must stay inside its scratch directory."""
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from autoclipper.director_poc.__main__ import initialize_fkb, main
from autoclipper.fkb import FKBStore, FootageKnowledgeBase, Session


class DirectorWriteSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.scratch = self.base / "autoclipper-director-a-synthetic"
        self.scratch.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()
        self.outside_marker = self.outside / "preserve.txt"
        self.outside_marker.write_bytes(b"outside bytes")

    def link_directory(self, kind):
        link = self.scratch / ".autoclipper"
        if kind == "junction":
            if os.name != "nt":
                self.skipTest("Windows junctions only")
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(self.outside)],
                                    capture_output=True, text=True)
            if result.returncode:
                self.fail(result.stderr or result.stdout)
            self.addCleanup(lambda: os.rmdir(link) if os.path.lexists(link) else None)
        else:
            try:
                link.symlink_to(self.outside, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"Directory symlink unavailable: {exc}")
            self.addCleanup(lambda: link.unlink() if os.path.lexists(link) else None)
        return link

    def assert_outside_untouched(self):
        self.assertEqual([p.name for p in self.outside.iterdir()], ["preserve.txt"])
        self.assertEqual(self.outside_marker.read_bytes(), b"outside bytes")

    def test_fresh_index_cli_refuses_linked_directory(self):
        for kind in ("junction", "symlink"):
            with self.subTest(kind=kind):
                link = self.link_directory(kind)
                for name in ("manifest.json", "build.json", "session.json"):
                    (self.scratch / name).write_text("{}", encoding="utf-8")
                stderr = io.StringIO()
                with (patch.object(sys, "argv", ["director", "index-notes", str(self.scratch),
                                              str(self.scratch / "session.json")]),
                      patch("autoclipper.director_poc.__main__.validate_review_receipt",
                            return_value=SimpleNamespace(start_frame=0, session_origin=0)),
                      patch("autoclipper.director_poc.catalog_migration.persistent_mapping_digest"),
                      patch("autoclipper.director_poc.__main__.load_session"),
                      redirect_stderr(stderr), self.assertRaises(SystemExit) as error):
                    main()
                self.assertEqual(error.exception.code, 2)
                self.assertIn("real .autoclipper directory", stderr.getvalue())
                self.assert_outside_untouched()
                if kind == "junction":
                    os.rmdir(link)
                else:
                    link.unlink()

    def test_migration_refuses_linked_destination_and_preserves_source(self):
        old = self.base / "autoclipper-director-a-old"
        old.mkdir()
        original = FKBStore(old)
        original.initialize(FootageKnowledgeBase(Session("old", 24)))
        before = original.path.read_bytes()
        self.link_directory("junction" if os.name == "nt" else "symlink")
        with (patch("autoclipper.director_poc.__main__.validate_review_receipt",
                    return_value=SimpleNamespace(start_frame=0, session_origin=0)),
              patch("autoclipper.director_poc.catalog_migration.persistent_mapping_digest"),
              self.assertRaisesRegex(ValueError, "real .autoclipper directory")):
            initialize_fkb(self.scratch, object(), {"fps": 24},
                           {"project_id": "p", "timeline_id": "t"}, migrate_from=old)
        self.assertEqual(original.path.read_bytes(), before)
        self.assert_outside_untouched()

    def test_swapped_directory_blocks_update_and_backup(self):
        store = FKBStore(self.scratch)
        store.initialize(FootageKnowledgeBase(Session("test", 24)))
        original = store.path.read_bytes()
        real = self.scratch / "saved-real-directory"
        store.path.parent.rename(real)
        self.link_directory("junction" if os.name == "nt" else "symlink")
        with self.assertRaisesRegex(ValueError, "real .autoclipper directory"):
            store.update(lambda data: setattr(data, "mapping_stale", True))
        self.assertEqual((real / "director-fkb.json").read_bytes(), original)
        self.assertFalse((real / "director-fkb.json.bak").exists())
        self.assert_outside_untouched()

    def test_catalog_and_backup_symlinks_block_publication(self):
        store = FKBStore(self.scratch)
        store.path.parent.mkdir()
        for target in (store.path, store.backup):
            with self.subTest(target=target.name):
                try:
                    target.symlink_to(self.outside_marker)
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f"File symlink unavailable: {exc}")
                try:
                    with self.assertRaisesRegex(ValueError, "link or reparse point"):
                        store.initialize(FootageKnowledgeBase(Session("test", 24)))
                    self.assert_outside_untouched()
                finally:
                    target.unlink()

    def test_backup_symlink_after_initialization_blocks_update(self):
        store = FKBStore(self.scratch)
        store.initialize(FootageKnowledgeBase(Session("test", 24)))
        original = store.path.read_bytes()
        try:
            store.backup.symlink_to(self.outside_marker)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"File symlink unavailable: {exc}")
        with self.assertRaisesRegex(ValueError, "link or reparse point"):
            store.update(lambda data: setattr(data, "mapping_stale", True))
        self.assertEqual(store.path.read_bytes(), original)
        self.assert_outside_untouched()


if __name__ == "__main__":
    unittest.main()
