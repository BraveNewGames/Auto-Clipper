from pathlib import Path
from unittest.mock import patch

from autoclipper.resolve_manifest import build_review_manifest


def confirmed_mics(source):
    return [{"part_id": part.part_id, "speaker": pov.name, "confirmed_by": "owner",
             "transcriber_track": 2} for pov, part in source.all_parts()]


def synthetic_probe(source, fps, playable_frames=None):
    frames = {
        Path(part.video_path).stem: (playable_frames or {}).get(part.part_id, round(part.duration * fps))
        for _, part in source.all_parts()
    }

    def probe(path):
        count = frames[path.stem]
        return [
            {"codec_type": "video", "r_frame_rate": f"{fps}/1", "nb_frames": str(count)},
            {"codec_type": "audio", "index": 1, "channels": 2, "tags": {"title": "Track1"}},
            {"codec_type": "audio", "index": 2, "channels": 2, "tags": {"title": "Track2"}},
        ]

    return patch("autoclipper.resolve_manifest.probe_streams", side_effect=probe)


def verified_review(source, fps):
    with synthetic_probe(source, fps):
        return build_review_manifest(source, fps=fps, confirmations=confirmed_mics(source))
