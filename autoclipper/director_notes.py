"""Persistent note lifecycle and local metadata for Director Mode."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import re
from typing import Protocol
from uuid import uuid4

from .director_poc.core import Capture, tc_frame
from .fkb import DirectorNote, FrameRange


def _now():
    return datetime.now(timezone.utc).isoformat()


class MetadataProvider(Protocol):
    name: str

    def generate(self, text: str) -> tuple[str, list[str]]:
        ...


class LocalMetadata:
    name = "local-keywords-v1"

    def generate(self, text):
        words = re.findall(r"[^\W\d_]+", text.casefold())
        keywords = dict.fromkeys(word for word in words if len(word) >= 4)
        return " ".join(text.split()[:8])[:72], list(keywords)[:5]


@dataclass(frozen=True)
class RangeAdjustment:
    note_id: str
    revision: int
    old: FrameRange
    new: FrameRange
    capture: dict


class NotesCatalog:
    def __init__(self, store, provider=None):
        self.store = store
        self.provider = provider if provider is not None else LocalMetadata()

    @staticmethod
    def _capture(data, capture, playback, delayed):
        if playback != "paused" or delayed:
            raise ValueError("Pause playback and capture a fresh range")
        session = data.session
        if data.mapping_stale:
            raise ValueError("Resolve the stale footage mapping before capturing")
        if not session.project_id or not session.timeline_id or (
            capture.project_id, capture.timeline_id, capture.fps
        ) != (session.project_id, session.timeline_id, session.fps):
            raise ValueError("Capture must match the session timeline and frame clock")
        frames = FrameRange(capture.session_in, capture.session_out_exclusive)
        timeline = FrameRange(capture.timeline_in, capture.timeline_out_exclusive)
        if timeline.start < 0 or frames.end - frames.start != timeline.end - timeline.start:
            raise ValueError("Capture range disagrees with the session frame clock")
        if (tc_frame(capture.in_timecode, session.fps) != session.timeline_start_frame + timeline.start
                or tc_frame(capture.out_timecode_exclusive, session.fps)
                != session.timeline_start_frame + timeline.end):
            raise ValueError("Capture timecodes disagree with the session timeline origin")
        # A persistent catalog is bound to a full-session timeline. Its Resolve
        # start frame represents session frame zero, regardless of the manifest.
        if frames != timeline:
            raise ValueError("Capture session range disagrees with the bound session frame origin")
        return frames, {**asdict(capture), "playback": "operator_confirmed_paused",
                        "playback_api_verified": False, "delayed": False}

    def create(self, capture, text, *, playback=None, delayed=False):
        if not text.strip():
            raise ValueError("Note text is required")

        def add(data):
            frames, evidence = self._capture(data, capture, playback, delayed)
            timestamp = _now()
            note = DirectorNote(str(uuid4()), frames.to_dict(), text, text,
                                timestamp, timestamp, evidence)
            note.marker_id = note.note_id
            note.pov_ids = sorted({part.pov_id for part in data.coverage(frames)})
            data.director_notes.append(note)
            return note

        return self.store.update(add)

    @staticmethod
    def _find(data, note_id, *, deleted=False):
        for note in data.director_notes:
            if note.note_id == note_id and note.deleted == deleted:
                return note
        raise KeyError("Note is unavailable")

    @staticmethod
    def _revise(note, **changes):
        if any(getattr(note, key) != value for key, value in changes.items()):
            snapshot = note.to_dict()
            del snapshot["revisions"]
            note.revisions.append(snapshot)
            for key, value in changes.items():
                setattr(note, key, value)
            note.updated_at = _now()
            note.revision += 1
        return note

    def edit_text(self, note_id, text, *, append=True):
        if not text.strip():
            raise ValueError("Note text is required")

        def edit(data):
            note = self._find(data, note_id)
            updated = f"{note.text}\n{text}" if append else text
            changes = {"text": updated}
            if updated != note.text:
                for key, empty in (("title", ""), ("tags", [])):
                    if key not in note.customized:
                        changes[key] = empty
                changes["metadata_provider"] = None
            return self._revise(note, **changes)

        return self.store.update(edit)

    def edit_metadata(self, note_id, *, title=None, tags=None):
        changes = {}
        if title is not None:
            changes["title"] = title
        if tags is not None:
            changes["tags"] = list(tags)

        def edit(data):
            note = self._find(data, note_id)
            return self._revise(note, **changes, customized=sorted(set(note.customized) | changes.keys()))

        return self.store.update(edit)

    def refresh_metadata(self, note_id):
        """Run off the UI thread. Discard results if the source text changed in flight."""
        note = self._find(self.store.load(), note_id)
        title, tags = self.provider.generate(note.text)

        def apply(data):
            current = self._find(data, note_id)
            if current.text != note.text:
                return current
            changes = {key: value for key, value in (("title", title), ("tags", tags))
                       if key not in current.customized}
            if changes:
                changes["metadata_provider"] = self.provider.name
            return self._revise(current, **changes)

        return self.store.update(apply)

    def preview_range(self, note_id, capture, *, playback=None, delayed=False):
        data = self.store.load()
        note = self._find(data, note_id)
        frames, evidence = self._capture(data, capture, playback, delayed)
        return RangeAdjustment(note_id, note.revision, FrameRange.from_dict(note.frames),
                               frames, evidence)

    def apply_range(self, adjustment):
        def apply(data):
            note = self._find(data, adjustment.note_id)
            if (note.revision != adjustment.revision or note.frames != adjustment.old.to_dict()
                    or data.mapping_stale):
                raise ValueError("Note or mapping changed. Capture and preview the range again.")
            capture = Capture(**{key: adjustment.capture[key] for key in Capture.__dataclass_fields__})
            frames, evidence = self._capture(
                data, capture,
                "paused" if adjustment.capture.get("playback") == "operator_confirmed_paused" else None,
                adjustment.capture.get("delayed", True))
            if frames != adjustment.new:
                raise ValueError("Capture range disagrees with the preview")
            return self._revise(note, frames=frames.to_dict(), capture=evidence,
                                pov_ids=sorted({part.pov_id for part in data.coverage(frames)}))

        return self.store.update(apply)

    def delete(self, note_id):
        return self.store.update(
            lambda data: self._revise(self._find(data, note_id), deleted=True))

    def recover(self, note_id):
        return self.store.update(
            lambda data: self._revise(self._find(data, note_id, deleted=True), deleted=False))

    def restore_revision(self, note_id, revision):
        def restore(data):
            note = self._find(data, note_id)
            snapshot = next((r for r in note.revisions if r["revision"] == revision), None)
            if snapshot is None:
                raise KeyError("Unknown note revision")
            if data.mapping_stale and snapshot["frames"] != note.frames:
                raise ValueError("Resolve the stale footage mapping before restoring this range")
            fields = ("frames", "capture", "text", "pov_ids", "title", "tags",
                      "customized", "metadata_provider")
            return self._revise(note, **{key: snapshot[key] for key in fields})

        return self.store.update(restore)

    def search(self, query="", *, deleted=False):
        data = self.store.load()
        terms = query.casefold().split()
        matches = []
        for note in data.director_notes:
            if note.deleted != deleted:
                continue
            direct = " ".join((note.text, note.title, *note.tags)).casefold()
            if not all(term in direct for term in terms):
                frames = FrameRange.from_dict(note.frames)
                context = " ".join(span.text for span in data.transcript_spans
                                   if frames.overlaps(FrameRange.from_dict(span.frames))).casefold()
                if not all(term in direct or term in context for term in terms):
                    continue
            matches.append(note)
        return matches


def review_payload(note):
    """Supply only marker/navigation identity and timing to the Phase A process."""
    keys = ("project", "project_id", "timeline", "timeline_id", "fps")
    return {**{key: note.capture[key] for key in keys}, "note_id": note.note_id,
            "session_in": note.frames["start"], "session_out_exclusive": note.frames["end"]}
