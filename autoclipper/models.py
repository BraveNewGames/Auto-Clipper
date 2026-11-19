"""Persisted session data and shared timestamp conversion."""
from dataclasses import asdict, dataclass, field
import math


def fmt_ts(sec: float) -> str:
    sign = "-" if sec < 0 else ""
    minutes, millis = divmod(round(abs(sec) * 1000), 60000)
    hours, minutes = divmod(minutes, 60)
    seconds, millis = divmod(millis, 1000)
    return f"{sign}{hours:02}:{minutes:02}:{seconds:02}.{millis:03}"


def parse_ts(s: str | float) -> float:
    value = str(s).strip().replace(",", ".")
    sign = -1 if value.startswith("-") else 1
    fields = value.lstrip("+-").split(":")
    if len(fields) == 1:
        result = float(value)
    else:
        if len(fields) > 3 or any(not f.isdecimal() for f in fields[:-1]):
            raise ValueError(f"Invalid timestamp: {s!r}")
        seconds = float(fields[-1])
        if not 0 <= seconds < 60 or (len(fields) == 3 and int(fields[1]) >= 60):
            raise ValueError(f"Invalid timestamp: {s!r}")
        result = sign * sum(float(f) * 60 ** i for i, f in enumerate(reversed(fields)))
    if not math.isfinite(result):
        raise ValueError(f"Invalid timestamp: {s!r}")
    return result


class _Model:
    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


@dataclass
class Part(_Model):
    part_id: str = ""
    index: int = 1
    video_path: str | None = None
    srt_path: str | None = None
    duration: float | None = None
    duration_source: str | None = None
    offset: float = 0.0
    offset_source: str = "default"
    offset_confidence: float | None = None
    notes: str = ""


@dataclass
class POV(_Model):
    pov_id: str = ""
    name: str = ""
    parts: list[Part] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data):
        return cls(**{**data, "parts": [Part.from_dict(p) for p in data.get("parts", [])]})


@dataclass
class Suggestion(_Model):
    suggestion_id: str = ""
    reviewer: str = ""
    reviewer_name: str = ""
    event_id: str = ""
    title: str = ""
    start: float = 0.0
    end: float = 0.0
    priority: str = "medium"
    kind: str = "clip_now"
    what_happens: str = ""
    why: str = ""
    hook: str = ""
    structure: str = ""
    quotes: list[dict] = field(default_factory=list)
    povs: list[str] = field(default_factory=list)
    pov_notes: str = ""
    confirmed_by_transcript: str = ""
    needs_visual_check: str = ""
    uncertainty: str = ""
    imported_at: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class Event(_Model):
    event_id: str = ""
    title: str = ""
    suggestion_ids: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class Review(_Model):
    status: str | None = None
    notes: str = ""
    approved_start: float | None = None
    approved_end: float | None = None
    updated_at: str = ""


@dataclass
class Session(_Model):
    schema_version: int = 1
    session_id: str = ""
    event_prefix: str = ""
    game: str = ""
    notes: str = ""
    folder: str = ""
    pad_before: float = 5.0
    pad_after: float = 5.0
    povs: list[POV] = field(default_factory=list)
    suggestions: list[Suggestion] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    reviews: dict[str, Review] = field(default_factory=dict)
    transcript_overrides: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data):
        return cls(**{
            **data,
            "povs": [POV.from_dict(p) for p in data.get("povs", [])],
            "suggestions": [Suggestion.from_dict(s) for s in data.get("suggestions", [])],
            "events": [Event.from_dict(e) for e in data.get("events", [])],
            "reviews": {sid: Review.from_dict(r) for sid, r in data.get("reviews", {}).items()},
        })

    def all_parts(self) -> list[tuple[POV, Part]]:
        return [(pov, part) for pov in self.povs for part in pov.parts]

    def part_by_id(self, part_id):
        for _, part in self.all_parts():
            if part.part_id == part_id:
                return part
        raise KeyError(part_id)

