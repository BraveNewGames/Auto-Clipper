"""Verify and copy a legacy catalog onto a newly receipted Director review."""

from pathlib import Path

from ..fkb import FrameRange
from ..resolve_manifest import source_mapping_digest
from .core import Clock
from .receipt import validate_review_receipt


REINDEX = ("Persistent notes require verified source mapping. Prepare and build a new Director review, "
           "then run index-notes, using --migrate-from OLD_SCRATCH to preserve an existing legacy catalog.")


def persistent_mapping_digest(manifest):
    if manifest.get("mode") != "director_review":
        raise ValueError(REINDEX)
    try:
        digest = source_mapping_digest(manifest)
    except ValueError as exc:
        raise ValueError(REINDEX) from exc
    mapping = manifest["source_mapping"]
    if (manifest.get("source_mapping_digest") != digest or mapping.get("fps") != manifest["fps"]
            or not isinstance(mapping.get("parts"), list) or not mapping["parts"]):
        raise ValueError(REINDEX)
    return digest


def _coverage(clock, frames):
    """Compare source placement independently of how reviews split their clips."""
    spans = []
    for segment in clock.segments:
        _, start, length = clock.intervals(segment)
        left, right = max(start, frames.start), min(start + length, frames.end)
        if left >= right:
            continue
        sources = sorted((angle["pov_id"], angle["part_id"], str(Path(angle["mp4_path"]).resolve()),
                          angle["source_in_frame"] - start) for angle in segment["angles"])
        if spans and spans[-1][1] == left and spans[-1][2] == sources:
            spans[-1] = (spans[-1][0], right, sources)
        else:
            spans.append((left, right, sources))
    return spans


def migrate_legacy_catalog(previous, old_manifest, old_built, incoming, manifest, built, binding):
    """Return the new catalog after verifying every recorded range and identity."""
    old_binding = validate_review_receipt(old_manifest, old_built)
    old, new = previous.session, incoming.session
    if (previous.source_mapping_digest is not None or old_manifest.get("mode") is not None
            or (old.session_id, old.fps) != (new.session_id, new.fps)
            or (old.project_id, old.timeline_id, old.fps, old.timeline_start_frame,
                old.review_session_start_frame) != (
                    old_built["project_id"], old_built["timeline_id"], old_manifest["fps"],
                    old_binding.start_frame, old_binding.session_origin)):
        raise ValueError("The old catalog does not match the receipted legacy review and source session.")

    old_clock = Clock(old_manifest, old_binding.start_timecode, old_binding.start_frame)
    new_clock = Clock(manifest, binding.start_timecode, binding.start_frame)
    identity = ("project", "project_id", "timeline", "timeline_id", "fps")
    for note in previous.director_notes:
        states = [note.to_dict(), *note.revisions]
        migrated = []
        for state in states:
            frames = FrameRange.from_dict(state["frames"])
            capture = state["capture"]
            first, end = frames.start - old_binding.session_origin, frames.end - old_binding.session_origin
            new_first, new_end = frames.start - binding.session_origin, frames.end - binding.session_origin
            if (any(capture.get(key) != old_built[key] for key in identity)
                    or (capture["session_in"], capture["session_out_exclusive"]) != (frames.start, frames.end)
                    or (capture["timeline_in"], capture["timeline_out_exclusive"]) != (first, end)
                    or (old_clock.relative(capture["in_timecode"]),
                        old_clock.relative(capture["out_timecode_exclusive"])) != (first, end)
                    or old_clock.session_range(first, end) != (frames.start, frames.end)
                    or new_clock.session_range(new_first, new_end) != (frames.start, frames.end)
                    or _coverage(old_clock, frames) != _coverage(new_clock, frames)):
                raise ValueError(f"Cannot verify the same footage for note {note.note_id} revision "
                                 f"{state['revision']}.")
            playhead_session = old_binding.session_origin + old_clock.relative(capture["playhead"])
            state["capture"] = {
                **capture, **{key: built[key] for key in identity}, "timeline_in": new_first,
                "timeline_out_exclusive": new_end, "in_timecode": new_clock.timecode(new_first),
                "out_timecode_exclusive": new_clock.timecode(new_end),
                "playhead": new_clock.timecode(playhead_session - binding.session_origin),
                "migrated_from": capture,
            }
            state["source_mapping_digest"] = incoming.source_mapping_digest
            state["pov_ids"] = sorted({part.pov_id for part in incoming.coverage(frames)})
            migrated.append(state)
        current, *history = migrated
        incoming.director_notes.append(type(note).from_dict({**current, "revisions": history}))

    incoming.candidate_moments = previous.candidate_moments
    incoming.edit_plans = previous.edit_plans
    return incoming
