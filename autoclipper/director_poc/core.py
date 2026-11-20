"""Offline-testable timing, capture and persistence for the disposable Phase A POC."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from fractions import Fraction
import json
from math import floor
from pathlib import Path
import re
from uuid import uuid4

from ..resolve_builder import DIRECTOR_TEST_PREFIX
from ..store import _write_json


def now():
    return datetime.now(timezone.utc).isoformat()


def tc_frame(value, fps):
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}:\d{2}:\d{2}", value):
        raise ValueError(f"Expected non-drop timecode, got {value!r}")
    hours, minutes, seconds, frame = map(int, value.split(":"))
    if hours >= 24 or minutes >= 60 or seconds >= 60 or frame >= fps:
        raise ValueError(f"Invalid timecode {value}")
    return ((hours * 60 + minutes) * 60 + seconds) * fps + frame


def frame_tc(frame, fps):
    if not 0 <= frame < 86400 * fps:
        raise ValueError("Timecode outside a single day")
    seconds, frame = divmod(frame, fps)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}:{frame:02}"


class Clock:
    def __init__(self, manifest, start_tc, start_frame):
        self.fps = manifest["fps"]
        if type(self.fps) is not int or not 1 <= self.fps <= 100:
            raise ValueError("Expected integer non-drop fps")
        self.origin = tc_frame(start_tc, self.fps)
        if self.origin != start_frame:
            raise ValueError("Resolve start frame and timecode disagree")
        self.segments = manifest["segments"]

    def timecode(self, relative):
        return frame_tc(self.origin + relative, self.fps)

    def relative(self, timecode):
        return tc_frame(timecode, self.fps) - self.origin

    def intervals(self, segment):
        record = segment["record_start_frame"]
        session = segment.get("session_start_frame")
        if session is None:
            session = floor(Fraction(str(segment["session_start"])) * self.fps + Fraction(1, 2))
        return record, session, segment["duration_frames"]

    def session_range(self, first, end):
        cursor = first
        session_first = None
        for segment in self.segments:
            record, session, length = self.intervals(segment)
            if not record <= cursor < record + length:
                continue
            mapped = session + cursor - record
            if session_first is None:
                session_first = mapped
            if mapped != session_first + cursor - first or not segment["angles"]:
                break
            cursor = min(record + length, end)
            if cursor == end:
                return session_first, session_first + end - first
        raise ValueError("Range crosses a gap or lies outside the review timeline")

    def timeline_frame(self, session_frame):
        for segment in self.segments:
            record, session, length = self.intervals(segment)
            if session <= session_frame < session + length and segment["angles"]:
                return record + session_frame - session
        raise ValueError("Session frame has no review footage")

    def sources(self, session_frame):
        for segment in self.segments:
            _, session, length = self.intervals(segment)
            if session <= session_frame < session + length:
                return {a["pov_id"]: (a["part_id"], a["source_in_frame"] + session_frame - session)
                        for a in segment["angles"]}
        return {}


@dataclass(frozen=True)
class Capture:
    project: str
    project_id: str
    timeline: str
    timeline_id: str
    timeline_in: int
    timeline_out_exclusive: int
    in_timecode: str
    out_timecode_exclusive: str
    session_in: int
    session_out_exclusive: int
    fps: int
    playhead: str
    captured_at: str


class Review:
    def __init__(self, resolve, result, manifest, *, out_inclusive=True):
        self.resolve = resolve
        self.result = result
        self.out_inclusive = out_inclusive
        if not re.fullmatch(re.escape(DIRECTOR_TEST_PREFIX) + r"\d{8}-\d{6}", result["project"]):
            raise ValueError("Expected a timestamped DirectorA scratch project")
        if not all(result[k] for k in ("project_id", "timeline", "timeline_id")):
            raise ValueError("Missing scratch identity")
        timeline = self.active()
        self.clock = Clock(manifest, timeline.GetStartTimecode(), timeline.GetStartFrame())
        if Fraction(timeline.GetSettings()["timelineFrameRate"]) != self.clock.fps:
            raise ValueError("Unexpected timeline frame rate")

    def active(self):
        manager = self.resolve.GetProjectManager()
        if not manager:
            raise RuntimeError("Resolve project manager unavailable")
        project = manager.GetCurrentProject()
        if not project or (project.GetName(), project.GetUniqueId()) != (
                self.result["project"], self.result["project_id"]):
            raise RuntimeError("Open the exact DirectorA scratch project")
        timeline = project.GetCurrentTimeline()
        if not timeline or (timeline.GetName(), timeline.GetUniqueId()) != (
                self.result["timeline"], self.result["timeline_id"]):
            raise RuntimeError("Activate the exact DirectorA review timeline")
        return timeline

    def sample(self):
        timecode = self.active().GetCurrentTimecode()
        return timecode, self.clock.relative(timecode)

    def capture(self):
        timeline = self.active()
        # Extract values before returning to the UI. No API setter belongs here.
        marks = (timeline.GetMarkInOut() or {}).get("video") or {}
        first, last = marks.get("in"), marks.get("out")
        playhead = timeline.GetCurrentTimecode()
        self.clock.relative(playhead)
        self.active()
        if type(first) is not int or type(last) is not int or first < 0 or last <= first:
            raise ValueError(f"Set two distinct video In/Out marks (read {first!r}, {last!r})")
        end = last + int(self.out_inclusive)
        session_in, session_end = self.clock.session_range(first, end)
        return Capture(self.result["project"], self.result["project_id"], self.result["timeline"],
                       self.result["timeline_id"], first, end, self.clock.timecode(first),
                       self.clock.timecode(end), session_in, session_end, self.clock.fps,
                       playhead, now())

    def note_frame(self, note):
        if any(note[key] != self.result[key] for key in ("project", "project_id", "timeline", "timeline_id", "fps")):
            raise ValueError("Note belongs to a different review timeline")
        return self.clock.timeline_frame(note["session_in"])

    def jump(self, note):
        target = self.clock.timecode(self.note_frame(note))
        if not self.active().SetCurrentTimecode(target):
            raise RuntimeError("Resolve rejected the seek")
        actual, _ = self.sample()
        return f"Target {target}, read back {actual}"

    def marker(self, note, operation):
        frame = self.note_frame(note)
        timeline = self.active()
        key = note["note_id"]
        if operation == "add":
            if frame in (timeline.GetMarkers() or {}):
                raise ValueError("This frame already has a marker. The note is still saved.")
            duration = note["session_out_exclusive"] - note["session_in"]
            if not timeline.AddMarker(frame, "Blue", "DirectorA note", "", duration, key):
                raise RuntimeError("Resolve rejected the marker")
        if operation == "delete" and not timeline.DeleteMarkerByCustomData(key):
            raise RuntimeError("Resolve could not delete the note marker")
        return timeline.GetMarkerByCustomData(key)


def connect_review(result, manifest, out_inclusive):
    from DaVinciResolveScript import scriptapp

    resolve = scriptapp("Resolve")
    if not resolve:
        raise RuntimeError("Resolve unavailable. Verify Local scripting and SDK environment.")
    return Review(resolve, result, manifest, out_inclusive=out_inclusive)


class NoteStore:
    """One companion process per scratch directory."""

    def __init__(self, path):
        self.path = Path(path)

    def _load(self):
        if not self.path.exists():
            return {"schema_version": 1, "notes": []}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1 or not isinstance(data.get("notes"), list):
            raise ValueError("Unsupported note store")
        return data

    def list(self):
        return self._load()["notes"]

    def add(self, capture, text):
        if (not isinstance(capture, Capture) or capture.session_out_exclusive <= capture.session_in
                or capture.timeline_out_exclusive <= capture.timeline_in or not text.strip()):
            raise ValueError("A timed capture and note text are required")
        data = self._load()
        note = {**asdict(capture), "note_id": str(uuid4()), "original_text": text,
                "text": text, "created_at": now(), "revisions": []}
        data["notes"].append(note)
        _write_json(self.path, data)
        return note

    def revise(self, note_id, text):
        if not text.strip():
            raise ValueError("Note text is required")
        data = self._load()
        for note in data["notes"]:
            if note["note_id"] == note_id:
                note["revisions"].append({"text": note["text"], "replaced_at": now()})
                note["text"] = text
                _write_json(self.path, data)
                return
        raise ValueError("Unknown note ID")
