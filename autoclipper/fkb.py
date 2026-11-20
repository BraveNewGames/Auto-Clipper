"""Session-local Director Mode records on a zero-based, exclusive-Out clock."""
from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from json import loads
from math import ceil, floor
from pathlib import Path
from threading import RLock
from uuid import NAMESPACE_URL, uuid5

from .models import _Model
from .reconcile import build_session_transcript
from .store import _write_json


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

    def __post_init__(self):
        if type(self.fps) is not int or not 1 <= self.fps <= 100:
            raise ValueError("Expected Phase A integer non-drop fps")

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


@dataclass
class EditPlan(_Model):
    plan_id: str
    # Ordered entries carry source part/range, POV, audio and evidence choices.
    entries: list[dict]
    timeline_id: str | None = None
    revisions: list[dict] = field(default_factory=list)


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
        """Return each known covered part separately, preserving gaps and overlaps."""
        return [part for part in self.recording_parts
                if part.media_online and part.session_range
                and FrameRange.from_dict(part.session_range).overlaps(frames)]


class FKBStore:
    """Use one writer process per session. In-process updates share a lock."""
    _lock = RLock()

    def __init__(self, folder):
        self.path = Path(folder).resolve() / ".autoclipper" / "director-fkb.json"
        self.backup = self.path.with_suffix(".json.bak")

    def load(self):
        return FootageKnowledgeBase.from_dict(loads(self.path.read_text(encoding="utf-8")))

    def initialize(self, database):
        with self._lock:
            if self.path.exists():
                raise FileExistsError(self.path)
            _write_json(self.path, database.to_dict())

    def update(self, change):
        with self._lock:
            data = self.load()
            previous = data.to_dict()
            result = change(data)
            current = data.to_dict()
            if current != previous:
                _write_json(self.backup, previous)
                _write_json(self.path, current)
            return result

    def ingest(self, source, *, fps, timeline_start_frame=0, project_id=None,
               timeline_id=None, cues_by_part=None, audio_by_part=None):
        incoming = ingest_session(source, fps=fps, timeline_start_frame=timeline_start_frame,
                                  project_id=project_id, timeline_id=timeline_id,
                                  cues_by_part=cues_by_part, audio_by_part=audio_by_part)
        with self._lock:
            if not self.path.exists():
                self.initialize(incoming)
                return

            def refresh(data):
                if data.session.session_id != incoming.session.session_id:
                    raise ValueError("Director FKB belongs to another session")
                if (data.session.fps, data.session.timeline_start_frame,
                    data.session.project_id, data.session.timeline_id) != (
                    incoming.session.fps, incoming.session.timeline_start_frame,
                    incoming.session.project_id, incoming.session.timeline_id):
                    raise ValueError("Director FKB clock or Resolve binding changed")
                has_references = data.director_notes or data.candidate_moments or data.edit_plans
                if has_references:
                    # Freeze the evidence previously referenced by editorial records.
                    data.mapping_stale = (data.recording_parts != incoming.recording_parts
                                          or data.transcript_spans != incoming.transcript_spans)
                    return
                data.recording_parts = incoming.recording_parts
                data.transcript_spans = incoming.transcript_spans
                data.session.source_manifest = incoming.session.source_manifest
                data.mapping_stale = False

            self.update(refresh)


def ingest_session(source, *, fps, timeline_start_frame=0, project_id=None,
                   timeline_id=None, cues_by_part=None, audio_by_part=None):
    if not source.session_id:
        raise ValueError("Auto-Clipper session requires a stable session ID")
    session = Session(source.session_id, fps, timeline_start_frame, project_id, timeline_id)
    parts = []
    by_id = {}
    for pov, part in source.all_parts():
        key = stable_id(source.session_id, "part", pov.pov_id, part.part_id)
        if not part.part_id or part.part_id in by_id:
            raise ValueError("Auto-Clipper recording part IDs must be unique and nonempty")
        by_id[part.part_id] = key, part.srt_path
        end = part.duration
        local = session.frames(0, end).to_dict() if end and end > 0 else None
        # Include full media frames only. Do not claim a partial frame as footage.
        first = ceil(Fraction(str(part.offset)) * fps)
        last = floor((Fraction(str(part.offset)) + Fraction(str(end))) * fps) if local else first
        coverage = FrameRange(first, last).to_dict() if last > first else None
        online = bool(part.video_path and (Path(source.folder) / part.video_path).is_file())
        parts.append(RecordingPart(
            key, pov.pov_id, pov.name, part.video_path, part.srt_path, end,
            part.duration_source, part.offset, part.offset_source, part.offset_confidence,
            local, coverage, (audio_by_part or {}).get(part.part_id), online))
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
    return FootageKnowledgeBase(session, parts, spans)

