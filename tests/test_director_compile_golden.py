"""C2: an edit-plan compile without reference audio is unchanged by the rough-cut work.

The golden file was produced from this branch and cross-checked against the pre-rough-cut tree (13329ac)
with the same synthetic inputs: the outputs differed only in ``created_at``.
Regenerate with AUTOCLIPPER_UPDATE_GOLDEN=1 only for an intentional manifest change.
"""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autoclipper import resolve_manifest as rm
from autoclipper.models import Part, POV, Session

from director_fixtures import confirmed_mics, synthetic_probe

GOLDEN = Path(__file__).parent / "golden" / "bridge_compile_no_reference.json"
PLAN = {"plan_id": "p", "session_id": "synthetic", "fps": 60, "revision_id": "r1", "revision_history": [],
        "entries": [
            {"entry_id": "C01", "kind": "source", "title": "Flip", "session_in": 1140, "session_out_exclusive": 1440,
             "pov_id": None, "audio_pov_id": None, "note_ids": ["n1"]},
            {"entry_id": "C02", "kind": "source", "title": "Bob", "session_in": 1320, "session_out_exclusive": 1500,
             "pov_id": "bob", "audio_pov_id": "bob", "note_ids": ["n2"]},
            {"entry_id": "P01", "kind": "placeholder", "title": "Card", "provisional_frames": 180},
            {"entry_id": "C03", "kind": "source", "title": "Edge", "session_in": 300, "session_out_exclusive": 900,
             "pov_id": "ada", "audio_pov_id": None, "note_ids": ["n3"]},
        ]}


def scrub(value, root):
    if isinstance(value, dict):
        # The digest covers absolute source paths, so it differs per temp folder.
        return {key: ("<DIGEST>" if key == "source_mapping_digest" else scrub(item, root))
                for key, item in value.items() if key != "created_at"}
    if isinstance(value, list):
        return [scrub(item, root) for item in value]
    if isinstance(value, str):
        return value.replace(root, "<ROOT>").replace("\\", "/")
    return value


class CompileGoldenTests(unittest.TestCase):
    def test_manifest_without_reference_audio_matches_golden(self):
        with TemporaryDirectory(prefix="autoclipper-golden-") as directory:
            root = Path(directory).resolve()
            for name in ("a1.mp4", "b1.mp4"):
                (root / name).write_bytes(b"synthetic")
                os.utime(root / name, ns=(1_700_000_000_000_000_000,) * 2)
            source = Session(session_id="synthetic", folder=str(root), povs=[
                POV("ada", "Ada", [Part("a1", video_path="a1.mp4", duration=60)]),
                POV("bob", "Bob", [Part("b1", video_path="b1.mp4", duration=30, offset=10.0)]),
            ])
            with synthetic_probe(source, 60):
                manifest = rm.compile_edit_plan(source, json.loads(json.dumps(PLAN)), None, confirmed_mics(source))
            text = json.dumps(scrub(json.loads(json.dumps(manifest, default=str)), str(root)),
                              indent=1, sort_keys=True)
        self.assertFalse(any(key in segment for segment in manifest["segments"]
                             for key in ("reference_audio", "reference_angle")))
        self.assertNotIn("reference_track", text)
        if os.environ.get("AUTOCLIPPER_UPDATE_GOLDEN"):
            GOLDEN.write_bytes(text.encode("utf-8"))
        self.assertEqual(text.splitlines(), GOLDEN.read_bytes().decode("utf-8").splitlines())


if __name__ == "__main__":
    unittest.main()
