"""Offline verification for a companion's reopened scratch review receipt."""

from dataclasses import dataclass
from fractions import Fraction
from math import floor
from pathlib import Path
import re

from ..resolve_builder import DIRECTOR_TEST_PREFIX, START_TIMECODE, marker_data, validate_manifest
from .core import tc_frame


_IDENTITY_KEYS = ("project", "project_id", "timeline", "timeline_id", "fps")
_REBUILD = "Build a new disposable review with the current builder and keep its manifest and build.json together."


@dataclass(frozen=True)
class ReviewBinding:
    session_origin: int
    start_timecode: str
    start_frame: int


def _fail(reason):
    raise ValueError(f"{reason}. {_REBUILD}")


def _canonical_digest(key, data):
    from ..resolve_manifest import source_mapping_digest

    return source_mapping_digest({"source_mapping": {"version": 1, key: data}})


def _manifest_digest(manifest):
    # The receipt also needs the ordered record map. The FKB source mapping
    # alone does not encode clip order, blank spans, or the legacy review form.
    keys = ("schema_version", "session_id", "session_folder", "mode", "fps", "requested_session_frames",
            "review_session_frames", "uncovered_edges", "catalog_source", "source_mapping",
            "source_mapping_digest", "segments")
    return _canonical_digest("review_manifest", {key: manifest[key] for key in keys if key in manifest})


def _session_origin(manifest, built):
    mode = manifest.get("mode")
    bounds = manifest.get("review_session_frames")
    if mode == "director_review":
        if (built.get("mode") != mode or bounds != built.get("review_session_frames")
                or not isinstance(bounds, list) or len(bounds) != 2
                or any(type(frame) is not int for frame in bounds) or bounds[0] >= bounds[1]):
            _fail("Review session origin is missing or disagrees with the verified build")
        return bounds[0]
    if mode is not None or built.get("mode") is not None or bounds is not None or built.get("review_session_frames") is not None:
        _fail("The companion requires a review manifest")

    # Older manifest schemas can be used only with a new receipt and a flat
    # zero-based review. Ordered edit compilations can skip or repeat time.
    fps = manifest["fps"]
    for part in manifest["segments"]:
        first, end = (floor(Fraction(str(part[key])) * fps + Fraction(1, 2))
                      for key in ("session_start", "session_end"))
        if (first, end) != (part["record_start_frame"], part["record_start_frame"] + part["duration_frames"]):
            _fail("Legacy review is not on the supported zero-origin session clock")
    return 0


def _check_segments(manifest, built, start):
    planned = manifest["segments"]
    rows = built.get("segments")
    if not isinstance(rows, list) or len(rows) != len(planned):
        _fail("Reopened segment map differs from the manifest")
    for segment, row in zip(planned, rows):
        record = start + segment["record_start_frame"]
        duration = segment["duration_frames"]
        if (not isinstance(row, dict) or row.get("record_start_frame") != record
                or row.get("duration_frames") != duration
                or any(row.get(key) != value for key, value in marker_data(segment).items())
                or any(row.get(key) != segment.get(key) for key in
                       ("kind", "session_start_frame", "session_end_frame", "unavailable"))):
            _fail("Reopened segment map differs from the manifest")
        if not segment["angles"]:
            if row.get("angles") != [] or "video" in row or "audio" in row:
                _fail("Reopened blank span differs from the manifest")
            continue
        if row.get("planned_angles") != segment["angles"]:
            _fail("Reopened planned angle map differs from the manifest")
        for track in ("video", "audio"):
            item = row.get(track)
            if not isinstance(item, dict) or (
                    item.get("record_start"), item.get("record_end"), item.get("duration"), item.get("source_start")
            ) != (record, record + duration, duration, 0):
                _fail("Reopened track placement differs from the manifest")
        if "angles" not in row:
            _fail("Reopened native angle readback is missing")
        if row["angles"] is not None:
            expected = {Path(angle["mp4_path"]).resolve(): angle for angle in segment["angles"]}
            for native in row["angles"]:
                angle = expected.pop(Path(native["path"]).resolve(), None)
                if (angle is None or native.get("source_start") != angle["source_in_frame"]
                        or native.get("source_end") not in (angle["source_out_frame"] - 1, angle["source_out_frame"])
                        or native.get("duration") != duration or native.get("angle_name") != angle["pov_name"]):
                    _fail("Reopened native angle readback differs from the manifest")
            if expected:
                _fail("Reopened native angle readback is incomplete")


def record_review_receipt(manifest, built, timeline):
    """Attach provenance only after a completed save/reopen and media readback."""
    built["review_receipt"] = {
        "version": 1,
        "identity": {key: built[key] for key in _IDENTITY_KEYS},
        "manifest_digest": _manifest_digest(manifest),
        "segments_digest": _canonical_digest("reopened_segments", built["segments"]),
        "start_timecode": timeline.GetStartTimecode(),
        "start_frame": timeline.GetStartFrame(),
    }
    return validate_review_receipt(manifest, built)


def validate_review_receipt(manifest, built):
    """Return the verified binding without importing or contacting Resolve."""
    if not isinstance(manifest, dict) or not isinstance(built, dict):
        _fail("Invalid Director review manifest or build receipt")
    receipt = built.get("review_receipt")
    if not isinstance(receipt, dict) or type(receipt.get("version")) is not int or receipt["version"] != 1:
        _fail("Missing or unsupported Director review build receipt")
    identity = {key: built.get(key) for key in _IDENTITY_KEYS}
    if (built.get("reopened") is not True or receipt.get("identity") != identity
            or any(not isinstance(identity[key], str) or not identity[key].strip() for key in _IDENTITY_KEYS[:-1])
            or not re.fullmatch(re.escape(DIRECTOR_TEST_PREFIX) + r"\d{8}-\d{6}", identity["project"])
            or identity["timeline"] != manifest["session_id"]
            or type(identity["fps"]) is not int or identity["fps"] != manifest["fps"]):
        _fail("Review build identity differs from the manifest or receipt")
    origin = _session_origin(manifest, built)
    validate_manifest(manifest)
    if receipt.get("manifest_digest") != _manifest_digest(manifest):
        _fail("Review manifest timing or coverage differs from the build receipt")
    start = tc_frame(START_TIMECODE, manifest["fps"])
    if receipt.get("start_timecode") != START_TIMECODE or receipt.get("start_frame") != start:
        _fail("Reopened timeline start disagrees with the requested 01:00:00:00 clock")
    _check_segments(manifest, built, start)
    if receipt.get("segments_digest") != _canonical_digest("reopened_segments", built["segments"]):
        _fail("Reopened segment map differs from the recorded receipt")
    return ReviewBinding(origin, START_TIMECODE, start)
