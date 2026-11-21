"""Session-local Director Mode records on a zero-based, exclusive-Out clock."""
from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from json import loads
from math import ceil, floor
import json
import os
from pathlib import Path
import stat
from tempfile import NamedTemporaryFile
from threading import RLock
from uuid import NAMESPACE_URL, uuid5

from .models import _Model
from .reconcile import build_session_transcript
from .resolve_manifest import catalog_source_identity, source_mapping_digest


class Provenance(str, Enum):
    USER = "user"
    TRANSCRIPT = "transcript"
    AI_INFERENCE = "ai_inference"
    VERIFIED = "verified"


def stable_id(*parts):
    return str(uuid5(NAMESPACE_URL, repr(("autoclipper/fkb", *parts))))


@dataclass(frozen=True)
class FrameRange(_Model):
    start: int
    end: int

    def __post_init__(self):
        if type(self.start) is not int or type(self.end) is not int or self.end <= self.start:
            raise ValueError("Expected integer In and exclusive Out with positive duration")

    def overlaps(self, other):
        return self.start < other.end and other.start < self.end


@dataclass
class Session(_Model):
    session_id: str
    fps: int
    timeline_start_frame: int = 0
    project_id: str | None = None
    timeline_id: str | None = None
    source_manifest: list[str] = field(default_factory=list)
    review_session_start_frame: int = 0

    def __post_init__(self):
        if type(self.fps) is not int or not 1 <= self.fps <= 100:
            raise ValueError("Expected Phase A integer non-drop fps")
        if type(self.review_session_start_frame) is not int:
            raise ValueError("Expected an integer review session origin")

    def frames(self, start, end):
        return FrameRange(floor(Fraction(str(start)) * self.fps),
                          ceil(Fraction(str(end)) * self.fps))


@dataclass
class RecordingPart(_Model):
    part_id: str
    pov_id: str
    player: str
    video_path: str | None
    srt_path: str | None
    duration: float | None
    duration_source: str | None
    offset: float
    offset_source: str
    offset_confidence: float | None
    source_range: dict | None
    session_range: dict | None
    audio_stream_mapping: dict | None
    media_online: bool
    playable_intervals: list[list[int]] = field(default_factory=list)


@dataclass
class TranscriptSpan(_Model):
    span_id: str
    text: str
    speaker: str | None
    confidence: str
    uncertain: bool
    frames: dict
    sources: list[dict]
    provenance: str = Provenance.TRANSCRIPT


@dataclass
class DirectorNote(_Model):
    note_id: str
    frames: dict
    original_text: str
    text: str
    created_at: str
    updated_at: str
    capture: dict
    title: str = ""
    tags: list[str] = field(default_factory=list)
    pov_ids: list[str] = field(default_factory=list)
    provenance: str = Provenance.USER
    marker_id: str | None = None
    metadata_provider: str | None = None
    customized: list[str] = field(default_factory=list)
    revisions: list[dict] = field(default_factory=list)
    deleted: bool = False
    revision: int = 0
    source_mapping_digest: str | None = None


@dataclass
class CandidateMoment(_Model):
    candidate_id: str
    logline: str
    frames: dict
    evidence_ids: list[str]
    status: str = "maybe"
    verified: bool = False
    uncertainty: str = ""
    provenance: str = Provenance.AI_INFERENCE
    model: str | None = None
    run_id: str | None = None
    model_version: str | None = None
    revisions: list[dict] = field(default_factory=list)
    note_anchor_id: str | None = None
    note_anchor_revision: int | None = None

    def __post_init__(self):
        if not isinstance(self.frames, dict):
            raise ValueError("Candidate frames must be an empty object or a positive frame range")
        if self.frames:
            if set(self.frames) != {"start", "end"}:
                raise ValueError("Candidate frame range must contain start and end")
            FrameRange(**self.frames)


@dataclass
class EditPlan(_Model):
    plan_id: str
    # Ordered entries carry source part/range, POV, audio and evidence choices.
    entries: list[dict]
    timeline_id: str | None = None
    revisions: list[dict] = field(default_factory=list)
    source_mapping_digest: str | None = None
    note_revisions: dict[str, int] = field(default_factory=dict)
    candidate_note_anchors: dict[str, list] = field(default_factory=dict)


@dataclass
class FootageKnowledgeBase(_Model):
    session: Session
    recording_parts: list[RecordingPart] = field(default_factory=list)
    transcript_spans: list[TranscriptSpan] = field(default_factory=list)
    director_notes: list[DirectorNote] = field(default_factory=list)
    candidate_moments: list[CandidateMoment] = field(default_factory=list)
    edit_plans: list[EditPlan] = field(default_factory=list)
    mapping_stale: bool = False
    schema_version: int = 1
    source_mapping_digest: str | None = None
    source_resolution: dict | None = None
    pinned_inputs: dict | None = None

    @classmethod
    def from_dict(cls, data):
        if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise ValueError("Unsupported Director FKB schema")
        return cls(**{
            **data,
            "session": Session.from_dict(data["session"]),
            "recording_parts": [RecordingPart.from_dict(row) for row in data["recording_parts"]],
            "transcript_spans": [TranscriptSpan.from_dict(row) for row in data["transcript_spans"]],
            "director_notes": [DirectorNote.from_dict(row) for row in data["director_notes"]],
            "candidate_moments": [CandidateMoment.from_dict(row) for row in data["candidate_moments"]],
            "edit_plans": [EditPlan.from_dict(row) for row in data["edit_plans"]],
        })

    def coverage(self, frames):
        """Return the verified parts with at least one playable frame in the range."""
        if self.mapping_stale or not self.source_mapping_digest:
            return []
        return [part for part in self.recording_parts
                if any(FrameRange(*interval).overlaps(frames)
                       for interval in part.playable_intervals)]

    def bind_edit_plan(self, plan):
        """Bind a newly selected plan to this catalog snapshot."""
        if self.mapping_stale:
            raise ValueError("Reconcile the stale footage mapping before saving an edit plan.")
        plan.source_mapping_digest = self.source_mapping_digest
        refs = {note_id for entry in plan.entries if entry["kind"] != "placeholder"
                for note_id in entry.get("note_ids", ())}
        plan.note_revisions = {note.note_id: note.revision for note in self.director_notes
                               if note.note_id in refs and not note.deleted}
        candidates = {candidate.candidate_id: candidate for candidate in self.candidate_moments}
        plan.candidate_note_anchors = {}
        for entry in plan.entries:
            if entry["kind"] == "candidate":
                candidate = candidates.get(entry["candidate_id"])
                if candidate and candidate.note_anchor_id:
                    plan.candidate_note_anchors[candidate.candidate_id] = [
                        candidate.note_anchor_id, candidate.note_anchor_revision]
        return plan


class FKBStore:
    """Use one writer process per session. In-process updates share a lock."""
    _lock = RLock()

    def __init__(self, folder):
        self.root = Path(folder).resolve()
        self.path = self.root / ".autoclipper" / "director-fkb.json"
        self.backup = self.path.with_suffix(".json.bak")

    @staticmethod
    def _is_link(path):
        try:
            attributes = os.lstat(path)
        except FileNotFoundError:
            return False
        return (os.path.islink(path) or bool(getattr(attributes, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))

    def verify_writable(self):
        directory = self.path.parent
        if not self.root.is_dir():
            raise ValueError(f"Refusing Director catalog write: scratch folder {self.root} is missing.")
        if self._is_link(directory):
            raise ValueError(f"Refusing Director catalog write: {directory} is a link or reparse point. "
                             "Use a real .autoclipper directory inside the scratch folder.")
        try:
            directory.mkdir(exist_ok=True)
        except FileExistsError as exc:
            raise ValueError(f"Refusing Director catalog write: {directory} is not a real directory.") from exc
        if (self._is_link(directory) or not directory.is_dir()
                or not directory.resolve().is_relative_to(self.root)):
            raise ValueError(f"Refusing Director catalog write: {directory} must be a real directory "
                             "inside the scratch folder.")
        for path in (self.path, self.backup):
            if self._is_link(path):
                raise ValueError(f"Refusing Director catalog write: {path} is a link or reparse point. "
                                 "Remove the link from the scratch folder before retrying.")
        return directory

    @classmethod
    def _matches_open_file(cls, path, identity):
        if cls._is_link(path):
            return False
        try:
            current = os.lstat(path)
        except FileNotFoundError:
            return False
        return (current.st_dev, current.st_ino) == identity

    def _write(self, path, data):
        directory = self.verify_writable()
        temporary = None
        identity = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                    delete=False) as handle:
                temporary = Path(handle.name)
                opened = os.fstat(handle.fileno())
                identity = (opened.st_dev, opened.st_ino)
                self.verify_writable()
                if not self._matches_open_file(temporary, identity):
                    raise ValueError("Refusing Director catalog write: temporary file changed during publication.")
                json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
            # Check again before publishing bytes that may contain Director notes.
            self.verify_writable()
            if not self._matches_open_file(temporary, identity):
                raise ValueError("Refusing Director catalog write: temporary file changed during publication.")
            os.replace(temporary, path)
        finally:
            # Unlink only the file opened above, even if its parent became a link.
            if temporary is not None and identity is not None and self._matches_open_file(temporary, identity):
                temporary.unlink(missing_ok=True)

    def load(self):
        database = FootageKnowledgeBase.from_dict(loads(self.path.read_text(encoding="utf-8")))
        database._scratch_dir = self.path.parent.parent
        return database

    def initialize(self, database):
        with self._lock:
            self.verify_writable()
            if self.path.exists():
                raise FileExistsError(self.path)
            self._write(self.path, database.to_dict())

    def update(self, change):
        with self._lock:
            self.verify_writable()
            data = self.load()
            previous = data.to_dict()
            existing_plans = {plan.plan_id for plan in data.edit_plans}
            result = change(data)
            for plan in data.edit_plans:
                if plan.plan_id not in existing_plans and plan.source_mapping_digest is None:
                    data.bind_edit_plan(plan)
            current = data.to_dict()
            if current != previous:
                self.verify_writable()
                self._write(self.backup, previous)
                self._write(self.path, current)
            return result

    def ingest(self, source, *, fps, timeline_start_frame=0, review_session_start_frame=0, project_id=None,
               timeline_id=None, cues_by_part=None, audio_by_part=None, review_manifest=None):
        incoming = ingest_session(source, fps=fps, timeline_start_frame=timeline_start_frame,
                                  review_session_start_frame=review_session_start_frame,
                                  project_id=project_id, timeline_id=timeline_id,
                                  cues_by_part=cues_by_part, audio_by_part=audio_by_part,
                                  review_manifest=review_manifest)
        with self._lock:
            self.verify_writable()
            if not self.path.exists():
                self.initialize(incoming)
                return

            def refresh(data):
                if data.session.session_id != incoming.session.session_id:
                    raise ValueError("Director FKB belongs to another session")
                if (data.session.fps, data.session.timeline_start_frame, data.session.review_session_start_frame,
                    data.session.project_id, data.session.timeline_id) != (
                    incoming.session.fps, incoming.session.timeline_start_frame,
                    incoming.session.review_session_start_frame,
                    incoming.session.project_id, incoming.session.timeline_id):
                    raise ValueError("Director FKB clock or Resolve binding changed")
                has_references = data.director_notes or data.candidate_moments or data.edit_plans
                if has_references:
                    # Freeze the evidence previously referenced by editorial records.
                    data.mapping_stale = (data.recording_parts != incoming.recording_parts
                                          or data.transcript_spans != incoming.transcript_spans
                                          or data.source_mapping_digest != incoming.source_mapping_digest)
                    if not data.mapping_stale:
                        data.source_resolution = incoming.source_resolution
                        data.pinned_inputs = incoming.pinned_inputs
                    return
                data.recording_parts = incoming.recording_parts
                data.transcript_spans = incoming.transcript_spans
                data.session.source_manifest = incoming.session.source_manifest
                data.source_mapping_digest = incoming.source_mapping_digest
                data.source_resolution = incoming.source_resolution
                data.pinned_inputs = incoming.pinned_inputs
                data.mapping_stale = False

            self.update(refresh)


def ingest_session(source, *, fps, timeline_start_frame=0, review_session_start_frame=0, project_id=None,
                   timeline_id=None, cues_by_part=None, audio_by_part=None, review_manifest=None):
    if not source.session_id:
        raise ValueError("Auto-Clipper session requires a stable session ID")
    session = Session(source.session_id, fps, timeline_start_frame, project_id, timeline_id,
                      review_session_start_frame=review_session_start_frame)
    parts = []
    by_id = {}
    mapping_parts = {}
    digest = None
    if review_manifest is not None:
        digest = source_mapping_digest(review_manifest)
        mapping = review_manifest["source_mapping"]
        if (digest != review_manifest.get("source_mapping_digest") or mapping["fps"] != fps
                or review_manifest.get("catalog_source") != catalog_source_identity(source)):
            raise ValueError("Review source mapping disagrees with this session. Rebuild and reconcile the catalog.")
        mapping_parts = {row["part_id"]: row for row in mapping["parts"]}
    for pov, part in source.all_parts():
        key = stable_id(source.session_id, "part", pov.pov_id, part.part_id)
        if not part.part_id or part.part_id in by_id:
            raise ValueError("Auto-Clipper recording part IDs must be unique and nonempty")
        by_id[part.part_id] = key, part.srt_path
        row = mapping_parts.get(part.part_id, {})
        media = row.get("media")
        intervals = row.get("playable_intervals", [])
        local = FrameRange(0, media["source_frame_count"]).to_dict() if media else None
        placement = row.get("session_start_frame")
        bounds = FrameRange(placement, placement + local["end"]).to_dict() if local else None
        parts.append(RecordingPart(
            key, pov.pov_id, pov.name, part.video_path, part.srt_path, part.duration,
            part.duration_source, part.offset, part.offset_source, part.offset_confidence,
            local, bounds, media["mic"] if media else (audio_by_part or {}).get(part.part_id), bool(media),
            [list(interval) for interval in intervals]))
    session.source_manifest = [part.part_id for part in parts]
    spans = []
    for line in build_session_transcript(source, cues_by_part):
        sources = []
        for ref in line.sources:
            key, srt_path = by_id[ref["part_id"]]
            sources.append({**ref, "part_id": key, "srt_path": srt_path})
        identity = sorted({(ref["part_id"], ref["cue_index"], ref["local_start"],
                            ref["local_end"]) for ref in sources})
        spans.append(TranscriptSpan(
            stable_id(source.session_id, "span", identity), line.text, line.speaker,
            line.confidence, line.uncertain,
            session.frames(line.session_start, line.session_end).to_dict(), sources))
    resolution = None
    if digest:
        resolution = {
            "catalog_source": review_manifest["catalog_source"],
            "paths": {part_id: row["source_path"] for part_id, row in mapping_parts.items()},
        }
    return FootageKnowledgeBase(session, parts, spans, source_mapping_digest=digest,
                                source_resolution=resolution,
                                pinned_inputs=review_manifest.get("pinned_inputs") if digest else None)

