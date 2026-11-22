"""Director Notes AI bridge: export a note-centred prompt, validate the pasted answer, save an EditPlan.

Manual only: the owner pastes the prompt into ChatGPT or Claude and pastes the answer back. There are
no provider calls. The core works on a *scratch* folder prepared by ``director_poc`` (it never uses the
Auto-Clipper session binding). It writes only below ``<scratch>/.autoclipper/director-ai/`` and, through
``FKBStore.update``, new EditPlans in the scratch catalog. The recording folder is only read.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from .fkb import FKBStore, FrameRange
from .rough_cut import _is_link, safe_directory

SCHEMA = "autoclipper.director_bridge.v1"
RECORD_SCHEMA = "autoclipper.director_bridge_export.v1"
SCRATCH_PREFIX = "autoclipper-director-a-"
AI_FOLDER = ("director-ai",)
DEFAULT_BUDGET_CHARS = 120_000
HARD_CAP_CHARS = 400_000
WINDOW_STEPS = (20, 15, 10, 5)
MAX_NOTES = 200
EXPORT_ID = re.compile(r"dx-[0-9a-f]{12}")
_PLAN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
_NOTE_ALIAS = re.compile(r"^N\d{2,4}$")
_SPAN_ALIAS = re.compile(r"^S\d{4,6}$")
_ID_LINE = "\x00EXPORT-ID-LINE"
_ID_TEXT = ('EXPORT ID: {} (copy into "export_id"; it proves the answer matches this prompt and catalog '
            'state)')
LEGACY = "Use the CLI with explicit confirmations."

# Content-free example so it cannot bias the editor towards any real moment.
_EXAMPLE = {
    "schema": SCHEMA, "export_id": "<EXPORT ID>", "title": "<working title>", "summary": "<one sentence>",
    "clips": [
        {"id": "C01", "kind": "source", "title": "<what this clip shows>", "note_ids": ["<note alias>"],
         "note": "<note alias>", "pad_before": 1.5, "pad_after": 2.0, "pov": None, "audio_pov": None,
         "quotes": [{"line": "<line alias>", "text": "<verbatim transcript text>"}],
         "why": "<editorial reason>"},
        {"id": "C02", "kind": "source", "title": "<title>", "note_ids": ["<note alias>"],
         "start_line": "<line alias>", "end_line": "<line alias>", "pad_before": 0, "pad_after": 1,
         "pov": "<pov id>", "audio_pov": "<pov id>", "quotes": [{"line": "<line alias>", "text": "<verbatim text>"}]},
        {"id": "P01", "kind": "placeholder", "title": "<missing beat or title card>", "seconds": 3},
    ],
}

RESPONSE_JSON_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "AutoClipper Director notes edit plan",
    "type": "object", "additionalProperties": False,
    "required": ["schema", "export_id", "title", "clips"],
    "properties": {
        "schema": {"const": SCHEMA}, "export_id": {"type": "string", "description": "Copy EXPORT ID exactly"},
        "title": {"type": "string", "maxLength": 120}, "summary": {"type": "string"},
        "clips": {"type": "array", "minItems": 1, "maxItems": 60, "items": {"oneOf": [
            {"type": "object", "additionalProperties": False, "required": ["id", "kind", "title", "note_ids"],
             "properties": {
                 "id": {"type": "string"}, "kind": {"const": "source"}, "title": {"type": "string"},
                 "note_ids": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                 "note": {"type": "string", "description": "Anchor: one of note_ids; the clip is that note's range"},
                 "start_line": {"type": "string"}, "end_line": {"type": "string"},
                 "session_in_frame": {"type": "integer"}, "session_out_frame": {"type": "integer"},
                 "in_tc": {"type": "string"}, "out_tc": {"type": "string"},
                 "pad_before": {"type": "number", "minimum": 0, "maximum": 10},
                 "pad_after": {"type": "number", "minimum": 0, "maximum": 10},
                 "pov": {"type": ["string", "null"]}, "audio_pov": {"type": ["string", "null"]},
                 "quotes": {"type": "array", "items": {
                     "type": "object", "additionalProperties": False, "required": ["line", "text"],
                     "properties": {"line": {"type": "string"}, "text": {"type": "string"},
                                    "speaker": {"type": "string"}}}},
                 "why": {"type": "string"}, "uncertainty": {"type": "string"}}},
            {"type": "object", "additionalProperties": False, "required": ["id", "kind", "title", "seconds"],
             "properties": {"id": {"type": "string"}, "kind": {"const": "placeholder"},
                            "title": {"type": "string"}, "seconds": {"type": "number", "minimum": 0.5, "maximum": 30},
                            "why": {"type": "string"}}},
        ]}},
    },
}


class DirectorError(ValueError):
    """A refusal with a stable ``code`` (``E_...``). The message always starts with the code."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass
class Binding:
    """A verified scratch folder. Rebuilt by ``open_scratch`` on every request; nothing is cached."""
    scratch: Path
    root: Path
    source: object
    manifest: dict
    built: dict
    kb: object
    store: FKBStore
    origin: int
    review_frames: tuple
    fps: int
    digest: str
    summary: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Line:
    """A transcript span shaped like the rough-cut packet lines the shared validator reads."""
    packet_id: str
    session_start: float
    session_end: float
    text: str
    label: str


# --------------------------------------------------------------------------- scratch binding

def _clean_path(value, what):
    if not isinstance(value, str) or not value.strip():
        raise DirectorError("E_PATH", f"{what} is required.")
    text = value.strip().strip('"').strip("'").strip()
    if not text or "\x00" in text:
        raise DirectorError("E_PATH", f"{what} is required.")
    return Path(text)


def _real_file(path, what):
    if _is_link(path) or not path.is_file():
        raise DirectorError("E_SCRATCH", f"{what} is missing or is a link.")


def open_scratch(scratch, session_root):
    """Apply every Director binding rule, in order, and return a Binding. Read-only."""
    from .director_poc.__main__ import SourceSessionError, read, source_input_identity, source_session, temp_copy
    from .director_poc.catalog_migration import persistent_mapping_digest
    from .director_poc.receipt import validate_review_receipt

    raw = _clean_path(scratch, "The scratch folder")
    if _is_link(raw):
        raise DirectorError("E_SCRATCH_LINK", "The scratch folder is a link or junction.")
    try:
        path = temp_copy(raw)
    except (OSError, ValueError, RuntimeError):
        raise DirectorError("E_SCRATCH_PATH", "The scratch folder must be an existing folder under system temp.") from None
    if not path.name.startswith(SCRATCH_PREFIX) or not path.is_dir():
        raise DirectorError("E_SCRATCH_PATH", f"Use a scratch folder named {SCRATCH_PREFIX}* printed by prepare.")
    state = path / ".autoclipper"
    if _is_link(state) or not state.is_dir():
        raise DirectorError("E_SCRATCH_LINK", "The scratch .autoclipper folder is missing or is a link.")
    for name in ("manifest.json", "build.json", "session.json"):
        _real_file(path / name, name)
    store = FKBStore(path)
    _real_file(store.path, "The Director catalog (FKB)")
    try:
        manifest, built = read(path / "manifest.json"), read(path / "build.json")
        binding = validate_review_receipt(manifest, built)
        digest = persistent_mapping_digest(manifest)
        kb = store.load()
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise DirectorError("E_RECEIPT", f"The scratch review receipt or catalog cannot be verified. {exc}") from None
    session = kb.session
    if (session.project_id, session.timeline_id, session.fps, session.review_session_start_frame,
            session.timeline_start_frame) != (built["project_id"], built["timeline_id"], manifest["fps"],
                                              binding.session_origin, binding.start_frame):
        raise DirectorError("E_CLOCK", "The catalog is bound to a different timeline or frame clock.")
    if kb.mapping_stale or kb.source_mapping_digest != digest:
        raise DirectorError("E_MAPPING_CHANGED", "The catalog has no matching verified source mapping. "
                            "Re-index or migrate the catalog.")
    if session_root is None:
        raise DirectorError("E_SOURCE", "The recording folder (the --session-root used at prepare) is required.")
    try:
        source = source_session(path / "session.json", _clean_path(session_root, "The recording folder"))
        pinned = (manifest.get("pinned_inputs") or {}).get("session_source")
        if pinned is None or kb.pinned_inputs is None:
            raise DirectorError("E_LEGACY", LEGACY)
        if pinned != source_input_identity(source):
            raise DirectorError("E_SOURCE", "The recording folder or its SRT files differ from prepare. "
                                "Use the same recording folder and unchanged SRTs.")
    except SourceSessionError as exc:
        raise DirectorError("E_SOURCE", str(exc)) from None
    except (OSError, ValueError) as exc:
        if isinstance(exc, DirectorError):
            raise
        raise DirectorError("E_SOURCE", f"The recording folder cannot be read. {exc}") from None
    if source.session_id != session.session_id:
        raise DirectorError("E_SESSION", "The scratch session.json belongs to another session than the catalog.")
    frames = manifest.get("review_session_frames")
    review = (frames[0], frames[1])
    notes = [n for n in kb.director_notes if not n.deleted]
    return Binding(path, Path(source.folder), source, manifest, built, kb, store,
                   session.timeline_start_frame - session.review_session_start_frame, review, session.fps, digest,
                   {"fps": session.fps, "session_id": session.session_id, "digest": digest[:12],
                    "project": built["project"], "timeline": built["timeline"], "notes": len(notes)})


# --------------------------------------------------------------------------- notes and coverage

def tc(binding, frame):
    """Review-clock timecode for a session frame (non-drop HH:MM:SS:FF)."""
    from .director_poc.core import frame_tc
    try:
        return frame_tc(binding.origin + frame, binding.fps)
    except ValueError:
        return f"f{frame}"


def tc_range(binding, start, end):
    return f"{tc(binding, start)}-{tc(binding, end)}"


def live_notes(binding):
    return sorted((n for n in binding.kb.director_notes if not n.deleted),
                  key=lambda n: (n.frames["start"], n.frames["end"], n.note_id))


def _one_line(text, limit=None):
    text = " ".join(str(text or "").split())
    return text if limit is None or len(text) <= limit else text[:limit - 1].rstrip() + "…"


def list_notes(binding):
    return [{"note_id": n.note_id, "rev": n.revision, "frames": dict(n.frames),
             "tc_in": tc(binding, n.frames["start"]), "tc_out": tc(binding, n.frames["end"]),
             "title": n.title, "tags": list(n.tags), "excerpt": _one_line(n.text, 90), "pov_ids": list(n.pov_ids)}
            for n in live_notes(binding)]


def video_intervals(kb):
    """Per POV, the merged verified playable session-frame intervals (video and its own mic, Medium 4)."""
    rows = {}
    for part in kb.recording_parts:
        rows.setdefault(part.pov_id, []).extend(list(i) for i in part.playable_intervals)
    out = {}
    for pov_id, intervals in rows.items():
        merged = []
        for left, right in sorted(intervals):
            if merged and left <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], right)
            else:
                merged.append([left, right])
        out[pov_id] = merged
    return out


def pov_names(kb):
    return {part.pov_id: part.player for part in kb.recording_parts}


def coverage_summary(binding):
    first, end = binding.review_frames
    names = pov_names(binding.kb)
    rows = []
    for pov_id, intervals in sorted(video_intervals(binding.kb).items()):
        gaps, cursor = [], first
        for left, right in intervals:
            if left > cursor:
                gaps.append([cursor, min(left, end)])
            cursor = max(cursor, right)
        if cursor < end:
            gaps.append([cursor, end])
        rows.append({"pov_id": pov_id, "name": names.get(pov_id, pov_id), "video": intervals, "mic": intervals,
                     "gaps": [g for g in gaps if g[0] < g[1]], "review_frames": [first, end]})
    return rows


def sorted_spans(kb):
    return sorted(kb.transcript_spans, key=lambda s: (s.frames["start"], s.frames["end"], s.span_id))


def span_aliases(kb):
    return {span.span_id: f"S{number:04d}" for number, span in enumerate(sorted_spans(kb), 1)}


def select_notes(binding, *, note_ids=None, tag=None, query=None):
    notes = live_notes(binding)
    if note_ids is not None:
        if not isinstance(note_ids, list) or not all(isinstance(i, str) for i in note_ids):
            raise DirectorError("E_SELECTION", "note_ids must be a list of note ids.")
        wanted = set(note_ids)
        missing = wanted - {n.note_id for n in notes}
        if missing:
            raise DirectorError("E_NOTE_UNKNOWN", f"{len(missing)} selected note(s) are unknown or deleted.")
        notes = [n for n in notes if n.note_id in wanted]
    if tag:
        notes = [n for n in notes if tag in n.tags]
    if query:
        terms = str(query).casefold().split()
        notes = [n for n in notes if all(t in " ".join((n.text, n.title, *n.tags)).casefold() for t in terms)]
    if not notes:
        raise DirectorError("E_NO_NOTES", "No live notes match the selection.")
    if len(notes) > MAX_NOTES:
        raise DirectorError("E_TOO_LARGE", f"{len(notes)} notes selected; select at most {MAX_NOTES}.")
    return notes


# --------------------------------------------------------------------------- export

def _windows(binding, notes, window_s, full):
    """Per-note windows (kept for anchoring) and the merged windows used for the transcript."""
    first, end = binding.review_frames
    pad = window_s * binding.fps
    own = {n.note_id: FrameRange(n.frames["start"] - pad, n.frames["end"] + pad) for n in notes}
    if full:
        return own, [FrameRange(min(first, *(w.start for w in own.values())),
                                max(end, *(w.end for w in own.values())))]
    merged = []
    for window in sorted(own.values(), key=lambda w: (w.start, w.end)):
        if merged and window.start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], window.end)
        else:
            merged.append([window.start, window.end])
    return own, [FrameRange(a, b) for a, b in merged]


def _coverage_lines(binding):
    lines = []
    for row in coverage_summary(binding):
        text = ", ".join(tc_range(binding, a, b) for a, b in row["video"]) or "no verified footage"
        gaps = ", ".join(tc_range(binding, a, b) for a, b in row["gaps"])
        lines.append(f'- pov id "{row["pov_id"]}" ({row["name"]}): VIDEO and own MIC verified during {text}'
                     + (f"; GAPS (cannot be used): {gaps}" if gaps else ""))
    return lines


def _render(binding, notes, aliases, spans, window_s, full):
    fps, kb = binding.fps, binding.kb
    own, merged = _windows(binding, notes, window_s, full)
    first, end = binding.review_frames
    rows = [
        f"You are the editor of a short highlight edit built from ONE multiplayer playtest session. A human\n"
        f"reviewer marked NOTES on moments while watching the review timeline. Choose clips that are anchored\n"
        f"to those notes and put them in the order they should PLAY. Auto-Clipper validates your answer\n"
        f"strictly and assembles it in a new DaVinci Resolve project; the human editor refines it afterwards.",
        "",
        "SESSION",
        f"Schema: {SCHEMA.replace('bridge.v1', 'bridge_prompt.v1')}",
        f"Session id: {kb.session.session_id}",
        f"Frame rate: {fps} fps (non-drop). Frames are integers; a range [In, Out) includes In and excludes Out.",
        f"Review window: {tc_range(binding, first, end)} (frames [{first}, {end}))",
        f"Source mapping digest: {binding.digest[:12]}",
        f"Transcript context: {'full transcript' if full else f'+/-{window_s} s around each note'}",
        _ID_LINE,
        "",
        "RULES",
        "- Clips play in the order you list them. They need not be chronological.",
        "- Every source clip cites at least one note in note_ids (use the aliases N01, N02, ... below).",
        "  Anchor each clip with exactly ONE of:",
        "  (a) note: one of its note_ids; the clip is that note's range, widened by pad_before/pad_after (0-10 s);",
        "  (b) start_line + end_line: transcript line aliases (S0001 ...); the clip runs from the start of",
        "      start_line to the end of end_line, widened by the pads;",
        "  (c) session_in_frame + session_out_frame (exclusive) plus in_tc + out_tc (the timecodes below);",
        "      no pads. Use only when you need exact frames.",
        ("  The clip must overlap the range of a note it cites (the transcript is complete, but a clip "
         "must still touch a cited note)." if full else
         "  The clip must overlap the transcript context shown for a note it cites."),
        "- quotes: for (b) and (c) give at least one verbatim quote {\"line\", \"text\"} from inside the clip.",
        "  Copy the text exactly. Never invent note aliases, line aliases, quotes or pov ids.",
        "- pov: null (recommended) = multicam of every camera covering the clip; or one pov id with verified",
        "  video for the WHOLE clip. audio_pov: null (recommended) = each angle's own mic; or one pov id whose",
        "  own mic covers the WHOLE clip. A range outside COVERAGE is rejected, not replaced by a slate.",
        "- Clips are 0.5-300 s each. Use a placeholder (0.5-30 s) only for a beat that must be filmed or a",
        "  title card; it becomes a labelled slate. ids are short and unique (C01, C02, P01).",
        "- Provide the COMPLETE answer in one JSON object. Any problem rejects the whole answer.",
        "",
        "OUTPUT",
        "Return ONLY one JSON object (no prose before or after) shaped like this (<...> are placeholders):",
        json.dumps(_EXAMPLE, indent=1),
        "",
        f"JSON schema: {json.dumps(RESPONSE_JSON_SCHEMA, separators=(',', ':'))}",
        "",
        "COVERAGE (timecodes are the review timeline)",
        *_coverage_lines(binding),
        "",
        f"NOTES ({len(notes)})",
    ]
    for note in notes:
        alias = aliases[note.note_id]
        frames = note.frames
        head = (f'{alias} rev {note.revision} | {tc_range(binding, frames["start"], frames["end"])} | '
                f'frames [{frames["start"]}, {frames["end"]}) | "{_one_line(note.title, 120)}"')
        if note.tags:
            head += " | tags: " + ", ".join(_one_line(t, 40) for t in note.tags)
        if note.pov_ids:
            head += " | povs: " + ", ".join(note.pov_ids)
        rows.append(head)
        text = str(note.text or "").strip().replace("\r\n", "\n").replace("\r", "\n")
        rows.append("  text: " + text.replace("\n", "\n        "))
    rows += ["", "TRANSCRIPT", 'Format: <line alias> [In, Out) <In timecode> <speaker>: <text>',
             'Speaker names come from that player\'s own microphone (reliable). "Team?" = heard over voice chat, '
             'speaker unknown (never guess who). "(?)" = wording uncertain. Cite line aliases in quotes.']
    listed = set()
    count = 0
    for number, window in enumerate(merged, 1):
        cited = [aliases[n.note_id] for n in notes if own[n.note_id].overlaps(window)]
        rows += ["", f"WINDOW {number}: {tc_range(binding, window.start, window.end)} (notes {', '.join(cited)})"]
        for span in spans:
            frames = FrameRange.from_dict(span.frames)
            if span.span_id in listed or not frames.overlaps(window):
                continue
            listed.add(span.span_id)
            count += 1
            rows.append(f'{aliases[span.span_id]} [{frames.start}, {frames.end}) {tc(binding, frames.start)} '
                        f'{span.speaker or "Team?"}: {_one_line(span.text)}'
                        + (" (?)" if span.uncertain else ""))
    return "\n".join(rows) + "\n", own, listed, count


def _write_new(binding, names, filename, text):
    """Create a new file below the scratch folder through checked folders; never overwrite."""
    folder = safe_directory(binding.scratch, ".autoclipper", *AI_FOLDER, *names)
    path = folder / filename
    if _is_link(path):
        raise DirectorError("E_WRITE", f"Refusing to write through a link: {filename}")
    try:
        with open(path, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except FileExistsError:
        if path.read_text(encoding="utf-8") != text:
            raise DirectorError("E_WRITE", f"{filename} already exists with different content.") from None
    return path


def export_packet(binding, *, note_ids=None, tag=None, query=None, full_transcript=False, save_prompt=True):
    """Build the prompt and write its export record (no transcript text) under director-ai/exports/."""
    notes = select_notes(binding, note_ids=note_ids, tag=tag, query=query)
    aliases = {n.note_id: f"N{number:02d}" for number, n in enumerate(notes, 1)}
    span_alias = span_aliases(binding.kb)
    aliases.update(span_alias)
    spans = sorted_spans(binding.kb)
    attempts = [None] if full_transcript else list(WINDOW_STEPS)
    chosen = None
    for step in attempts:
        text, own, listed, count = _render(binding, notes, aliases, spans, step or 0, bool(full_transcript))
        chosen = (step, text, own, listed, count)
        size = len(text) - len(_ID_LINE) + len(_ID_TEXT.format("dx-" + "0" * 12))
        if size <= DEFAULT_BUDGET_CHARS or (step == attempts[-1] and size <= HARD_CAP_CHARS):
            break
    step, template, own, listed, count = chosen
    size = len(template) - len(_ID_LINE) + len(_ID_TEXT.format("dx-" + "0" * 12))
    if size > HARD_CAP_CHARS:
        raise DirectorError("E_TOO_LARGE", f"The prompt would be {size:,} characters; the limit is "
                            f"{HARD_CAP_CHARS:,}. Select fewer notes.")
    hashed = template.replace(_ID_LINE + "\n", "")
    prompt_sha = sha256(hashed.encode("utf-8")).hexdigest()
    export_id = "dx-" + prompt_sha[:12]
    prompt = template.replace(_ID_LINE, _ID_TEXT.format(export_id))
    session = binding.kb.session
    record = {
        "schema": RECORD_SCHEMA, "export_id": export_id, "session_id": session.session_id, "fps": session.fps,
        "source_mapping_digest": binding.kb.source_mapping_digest, "clock_origin": binding.origin,
        "review_session_frames": list(binding.review_frames),
        "options": {"window_s": step, "full_transcript": bool(full_transcript)},
        "prompt_sha256": prompt_sha, "chars": len(prompt),
        "notes": {aliases[n.note_id]: {"note_id": n.note_id, "revision": n.revision, "frames": dict(n.frames),
                                       "window": own[n.note_id].to_dict()} for n in notes},
        "spans": {span_alias[s.span_id]: {"span_id": s.span_id, "frames": dict(s.frames)}
                  for s in spans if s.span_id in listed},
    }
    _write_new(binding, ("exports",), f"{export_id}.json", json.dumps(record, indent=2, sort_keys=True) + "\n")
    if save_prompt:
        _write_new(binding, ("prompts",), f"{export_id}.txt", prompt)
    return {"export_id": export_id, "prompt": prompt, "prompt_sha256": prompt_sha, "chars": len(prompt),
            "window_s": step, "counts": {"notes": len(notes), "spans": count, "windows": len(
                _windows(binding, notes, step or 0, bool(full_transcript))[1])},
            "saved_prompt": bool(save_prompt)}


def load_export_record(binding, export_id):
    if not isinstance(export_id, str) or not EXPORT_ID.fullmatch(export_id):
        raise DirectorError("E_EXPORT_UNKNOWN", f'"export_id" must be copied exactly from the prompt (dx-...).')
    folder = binding.scratch / ".autoclipper" / AI_FOLDER[0] / "exports"
    path = folder / f"{export_id}.json"
    if _is_link(folder) or _is_link(path) or not path.is_file():
        raise DirectorError("E_EXPORT_UNKNOWN", f"No export {export_id} was made from this scratch folder. "
                            "Copy a new prompt.")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DirectorError("E_EXPORT_UNKNOWN", f"The export record for {export_id} is unreadable.") from None
    if not isinstance(record, dict) or record.get("schema") != RECORD_SCHEMA or record.get("export_id") != export_id:
        raise DirectorError("E_EXPORT_UNKNOWN", f"The export record for {export_id} is malformed.")
    return record


# --------------------------------------------------------------------------- import: validation context

REMINDERS = """- Valid pov ids: {povs}. pov and audio_pov may be null (recommended).
- Every source clip needs note_ids (aliases from NOTES) and exactly one anchor: note, OR start_line +
  end_line, OR session_in_frame + session_out_frame with in_tc + out_tc. Pads are 0-10 s.
- Quotes must be copied verbatim with the line alias of a line inside the clip.
- The clip must overlap the transcript context shown for a note it cites (that note's own range when
  the full transcript was exported).
- Only use ranges inside COVERAGE from the original prompt; never invent aliases or ids."""

_SOURCE_KEYS = {"id", "kind", "title", "note", "note_ids", "start_line", "end_line", "session_in_frame",
                "session_out_frame", "in_tc", "out_tc", "pad_before", "pad_after", "pov", "audio_pov",
                "quotes", "why", "uncertainty"}
_TOP_KEYS = {"schema", "export_id", "title", "summary", "clips"}
# Convention, not a security boundary: only import_response is meant to hold this token.
_TOKEN = object()


class _NoteAnchors:
    """The rough-cut validator's extra-anchor hook: note anchors, TC-checked frames and note citations."""
    keys = _SOURCE_KEYS
    hint = "anchor the clip with note, start_line/end_line or session frames with timecodes."

    def __init__(self, binding, record):
        self.binding, self.record = binding, record

    def _frame_of(self, value, key, where, errors):
        from .director_poc.core import tc_frame
        try:
            return tc_frame(value, self.binding.fps) - self.binding.origin
        except ValueError:
            errors.append(f"{where}: \"{key}\" must be a timecode HH:MM:SS:FF on the review timeline.")
            return None

    def resolve(self, clip, where, errors, pads, rate):
        from fractions import Fraction
        from .resolve_manifest import _record_frame
        aliases = clip.get("note_ids")
        known = self.record["notes"]
        cited = []
        if not isinstance(aliases, list) or not aliases or len(aliases) > 20 or not all(
                isinstance(a, str) and _NOTE_ALIAS.match(a) for a in aliases):
            errors.append(f"{where}: E_UNANCHORED: \"note_ids\" must cite at least one note alias such as N01.")
        else:
            for alias in dict.fromkeys(aliases):
                if alias not in known:
                    errors.append(f"{where}: E_NOTE_UNKNOWN: {alias} was not exported in this prompt.")
                else:
                    cited.append(alias)
        extra = {"_note_aliases": cited}
        kinds = [name for name, present in (
            ("note", "note" in clip),
            ("frames", any(k in clip for k in ("session_in_frame", "session_out_frame", "in_tc", "out_tc")))) if present]
        if len(kinds) > 1:
            errors.append(f"{where}: use exactly one anchor kind.")
            return None, None, None, extra
        if kinds == ["note"]:
            alias = clip["note"]
            if not isinstance(alias, str) or not isinstance(aliases, list) or alias not in aliases:
                errors.append(f"{where}: \"note\" must be one of this clip's note_ids.")
                return "note", None, None, extra
            if alias not in known:
                return "note", None, None, extra
            frames = known[alias]["frames"]
            return ("note", frames["start"] - _record_frame(pads["pad_before"], 0, rate),
                    frames["end"] + _record_frame(pads["pad_after"], 0, rate), extra)
        if kinds == ["frames"]:
            first, end = clip.get("session_in_frame"), clip.get("session_out_frame")
            if any(clip.get(k, 0) not in (0, None) for k in ("pad_before", "pad_after")):
                errors.append(f"{where}: frame-anchored clips cannot use pads; widen the frames instead.")
            if type(first) is not int or type(end) is not int:
                errors.append(f"{where}: session_in_frame and session_out_frame must be integers.")
                return "frames", None, None, extra
            for key, frame in (("in_tc", first), ("out_tc", end)):
                claimed = self._frame_of(clip.get(key), key, where, errors) if isinstance(clip.get(key), str) else None
                if claimed is None:
                    if not isinstance(clip.get(key), str):
                        errors.append(f"{where}: frame anchors need \"{key}\" as a review-timeline timecode.")
                    return "frames", None, None, extra
                if claimed != frame:
                    errors.append(f"{where}: \"{key}\" {clip[key]} is frame {claimed}, which disagrees with "
                                  f"frame {frame}. Recheck the timecode or the frame number.")
                    return "frames", None, None, extra
            return "frames", first, end, extra
        return None, None, None, extra


def _context(binding, record):
    """The shared validator's context, built from the verified catalog (never from the main session)."""
    from types import SimpleNamespace
    from .resolve_manifest import _CoveredPart
    kb, fps = binding.kb, binding.fps
    names = pov_names(kb)
    povs = [SimpleNamespace(pov_id=pid, name=names[pid]) for pid in dict.fromkeys(
        part.pov_id for part in kb.recording_parts)]
    coverage = {pid: [] for pid in names}
    for pid, intervals in video_intervals(kb).items():
        coverage[pid] = [_CoveredPart(f"{pid}-{n}", n, a, b, {}) for n, (a, b) in enumerate(intervals, 1)]
    current = {span.span_id: span for span in kb.transcript_spans}
    lines = {}
    for alias, row in record["spans"].items():
        span = current.get(row["span_id"])
        if span is not None and span.frames == row["frames"]:
            lines[alias] = Line(alias, row["frames"]["start"] / fps, row["frames"]["end"] / fps, span.text,
                                span.speaker or "Team?")
    first, end = binding.review_frames
    return {"session": SimpleNamespace(session_id=kb.session.session_id, povs=povs, suggestions=[]),
            "fps": fps, "first": first, "end": end, "coverage": coverage, "line_index": lines,
            "mapping": {"blockers": []}, "source_mapping_digest": kb.source_mapping_digest,
            "transcripts_dir": None, "confirmations": None, "export_id": record["export_id"]}


# --------------------------------------------------------------------------- import: gate and save

class GatedPlan:
    """A plan that passed every check. Only ``import_response`` can create one (it holds the token)."""
    __slots__ = ("plan", "snapshot", "export_id", "response_sha256", "provider", "title")

    def __init__(self, token, **fields):
        if token is not _TOKEN:
            raise TypeError("GatedPlan can only be created by a validated import.")
        for name in self.__slots__:
            object.__setattr__(self, name, fields[name])

    def __setattr__(self, name, value):
        raise AttributeError("GatedPlan is immutable")


def _provider(value):
    text = " ".join(str(value or "").split())[:40]
    return text or "unspecified"


def _entry_projection(entry, note_uuid):
    from .rough_cut import _CONTROL
    if entry["kind"] == "placeholder":
        return {"entry_id": entry["entry_id"], "kind": "placeholder", "title": entry["title"],
                "provisional_frames": entry["provisional_frames"]}
    return {"entry_id": entry["entry_id"], "kind": "source", "title": entry["title"],
            "frames": {"start": entry["session_in"], "end": entry["session_out_exclusive"]},
            "pov_id": entry["pov_id"], "audio_pov_id": entry["audio_pov_id"],
            "note_ids": [note_uuid[a] for a in entry["_note_aliases"]]}


def _save_validated(binding, gated, response_text, save_response):
    """The only function that persists an AI plan. Requires a GatedPlan from ``import_response``."""
    if not isinstance(gated, GatedPlan):
        raise TypeError("Only a validated plan can be saved.")
    from .fkb import EditPlan

    plan = EditPlan(gated.plan["plan_id"], list(gated.plan["entries"]), revisions=list(gated.plan["revisions"]))

    def change(data):
        # Optimistic check: nothing may have changed the catalog since this plan was validated.
        if data.to_dict() != gated.snapshot:
            raise DirectorError("E_FKB_CHANGED", "The Director catalog changed while the answer was being "
                                "validated (is the companion still running?). Nothing was saved; try again.")
        data.edit_plans.append(plan)

    binding.store.update(change)
    warning = None
    if save_response:
        try:
            _write_new(binding, ("responses",), f"{gated.export_id}-{gated.response_sha256[:12]}.txt", response_text)
        except (OSError, ValueError) as exc:
            warning = f"The plan was saved, but the response copy could not be written: {exc}"
    return warning


def import_response(binding, text, *, provider, save_response=True, companion_closed=False):
    """Validate a pasted answer completely and, only if it passes, save it as a new AI EditPlan."""
    from fractions import Fraction
    from . import resolve_manifest as rm
    from . import rough_cut as rc
    from .fkb import EditPlan, Provenance
    from .resolve_builder import validate_manifest

    if companion_closed is not True:
        raise DirectorError("E_COMPANION", "Confirm that the Director companion for this scratch folder is closed; "
                            "it is the only other writer of the catalog.")
    provider = _provider(provider)
    result = {"ok": False, "export_id": None, "errors": [], "warnings": [], "stale": False,
              "repair_prompt": "", "report": ""}
    errors = result["errors"]

    def finish():
        result["report"] = _failure_report(result)
        return result

    try:
        data = rc.extract_json(text)
    except ValueError as exc:
        message = str(exc)
        if '"suggestions"' in text and '"clips"' not in text:
            message = "This looks like an AI Review answer (suggestions), not a Director plan."
        errors.append(f"E_SCHEMA: {message}")
        return finish()
    try:
        record = load_export_record(binding, data.get("export_id"))
    except DirectorError as exc:
        errors.append(str(exc))
        return finish()
    export_id = result["export_id"] = record["export_id"]
    kb = binding.kb
    if (record["session_id"], record["fps"]) != (kb.session.session_id, kb.session.fps) or \
            record["clock_origin"] != binding.origin:
        errors.append("E_SESSION: This export belongs to another session or frame clock.")
        return finish()
    if kb.mapping_stale or record["source_mapping_digest"] != kb.source_mapping_digest:
        result["stale"] = True
        errors.append("E_MAPPING_CHANGED: The verified source mapping changed since this prompt was copied. "
                      "Copy a new prompt.")
        return finish()

    context = _context(binding, record)
    fps = binding.fps
    repair = lambda: rc._repair_prompt(context, errors, export_id=export_id, schema=SCHEMA, label="Director plan",
                                       reminders=REMINDERS.format(povs=", ".join(
                                           f'"{p.pov_id}" ({p.name})' for p in context["session"].povs)))
    if data.get("schema") != SCHEMA:
        errors.append(f'E_SCHEMA: "schema" must be "{SCHEMA}".')
    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        errors.append(f"Unsupported top-level keys {', '.join(unknown)}; allowed: {', '.join(sorted(_TOP_KEYS))}.")
    title = rc._text_field(data, "title", "Answer", errors)
    if "summary" in data and not isinstance(data["summary"], str):
        errors.append('"summary" must be text.')
    clips = data.get("clips")
    if not isinstance(clips, list) or not clips:
        errors.append('"clips" must be a nonempty list.')
        clips = []
    elif len(clips) > rc.MAX_CLIPS:
        errors.append(f'"clips" has {len(clips)} entries; the limit is {rc.MAX_CLIPS}.')
        clips = []

    hook = _NoteAnchors(binding, record)
    notes_now = {n.note_id: n for n in kb.director_notes}
    entries, seen = [], set()
    for number, clip in enumerate(clips, 1):
        if not isinstance(clip, dict):
            errors.append(f"Clip #{number}: must be an object.")
            continue
        clip_id = clip.get("id")
        where = f"Clip #{number} ({clip_id})" if isinstance(clip_id, str) else f"Clip #{number}"
        if not isinstance(clip_id, str) or not rc._ID.match(clip_id):
            errors.append(f'{where}: "id" must be a short id such as C01 (letters, digits, _ . -).')
        elif clip_id in seen:
            errors.append(f"{where}: E_DUPLICATE: duplicate id.")
        seen.add(clip_id)
        entry = rc._validate_clip(context, clip, where, errors, anchors=hook)
        if entry is None:
            continue
        if entry["kind"] == "source":
            before = len(errors)
            window_hit = False
            for alias in entry["_note_aliases"]:
                row = record["notes"][alias]
                current = notes_now.get(row["note_id"])
                if current is None:
                    errors.append(f"{where}: E_NOTE_UNKNOWN: {alias} no longer exists in the catalog.")
                elif current.deleted:
                    errors.append(f"{where}: E_NOTE_DELETED: {alias} was deleted after the prompt was copied.")
                elif current.revision != row["revision"] or current.frames != row["frames"]:
                    errors.append(f"{where}: E_NOTE_STALE: {alias} was exported at revision {row['revision']} "
                                  f"but is now revision {current.revision}. Copy a new prompt.")
                elif current.source_mapping_digest != kb.source_mapping_digest:
                    errors.append(f"{where}: E_NOTE_UNVERIFIED: {alias} is not bound to the verified source mapping.")
                else:
                    window = row["window"]
                    window_hit |= entry["session_in"] < window["end"] and window["start"] < entry["session_out_exclusive"]
            if len(errors) == before and not window_hit:
                errors.append(f"{where}: E_UNANCHORED: the clip does not overlap the context window of any note "
                              "it cites.")
            if len(errors) > before:
                continue
        entries.append({"entry_id": clip_id, **entry})

    sources = [e for e in entries if e["kind"] == "source"]
    if clips and not any(isinstance(c, dict) and c.get("kind") == "source" for c in clips):
        errors.append("A plan needs at least one source clip.")
    if clips and not errors:
        total = sum(e["session_out_exclusive"] - e["session_in"] for e in sources) + \
            sum(e["provisional_frames"] for e in entries if e["kind"] == "placeholder")
        if total > rc.MAX_TOTAL_SECONDS * fps:
            errors.append(f"The plan is {total / fps:.1f} s; the limit is {rc.MAX_TOTAL_SECONDS} s.")
    if errors:
        result["repair_prompt"] = repair()
        return finish()

    warnings = []
    ordered = sorted(sources, key=lambda e: e["session_in"])
    for left, right in zip(ordered, ordered[1:]):
        if right["session_in"] < left["session_out_exclusive"]:
            warnings.append({"code": "W_OVERLAP", "event_id": right["entry_id"],
                             "message": f"W_OVERLAP: {right['entry_id']} overlaps {left['entry_id']} in session time."})
    for entry in sources:
        if entry["session_out_exclusive"] - entry["session_in"] < fps:
            warnings.append({"code": "W_SHORT_CLIP", "event_id": entry["entry_id"],
                             "message": f"W_SHORT_CLIP: {entry['entry_id']} is under one second."})
    positions = [e["session_in"] for e in sources]
    if positions != sorted(positions):
        warnings.append({"code": "W_ORDER", "message": "W_ORDER: clips are not in session order (allowed)."})
    total_s = sum(e["session_out_exclusive"] - e["session_in"] for e in sources) / fps
    if total_s > 900:
        warnings.append({"code": "W_LONG_PLAN", "message": f"W_LONG_PLAN: source clips total {total_s:.0f} s."})

    note_uuid = {alias: row["note_id"] for alias, row in record["notes"].items()}
    response_sha = sha256(text.encode("utf-8")).hexdigest()
    taken = sum(1 for p in kb.edit_plans if p.plan_id.startswith(f"ai-{export_id}-"))
    plan_id = f"ai-{export_id}-{taken + 1}"
    revision = {"revision_id": response_sha[:16], "provenance": Provenance.AI_INFERENCE.value,
                "source": "director_ai_bridge", "provider": provider, "schema": SCHEMA, "export_id": export_id,
                "prompt_sha256": record["prompt_sha256"], "response_sha256": response_sha, "title": title,
                "summary": data.get("summary") if isinstance(data.get("summary"), str) else "",
                "warnings": [w["message"] for w in warnings],
                "created_at": datetime.now(timezone.utc).isoformat()}
    projected = [_entry_projection(e, note_uuid) for e in entries]
    plan = EditPlan(plan_id, projected, revisions=[revision])
    snapshot = kb.to_dict()

    def compile_fn(_context, proposed):
        import copy
        candidate = copy.deepcopy(kb)
        candidate.edit_plans.append(copy.deepcopy(proposed))
        candidate.bind_edit_plan(candidate.edit_plans[-1])
        compiled = rm.compile_fkb_edit_plan(binding.source, candidate, plan_id, revision_id=revision["revision_id"])
        # gate_plan refuses a clip that still uses a part the compile itself excluded as unverified.
        context["mapping"]["blockers"] = [dict(b) for b in compiled["blockers"] if b.get("part_id")]
        return compiled

    manifest = rc.gate_plan(context, plan, result, compile_fn=compile_fn, repair=repair)
    if manifest is None:
        return finish()
    try:
        validate_manifest(manifest)
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"Resolve builder preflight failed: {exc}")
        return finish()
    manifest["warnings"].extend(warnings)
    gated = GatedPlan(_TOKEN, plan={"plan_id": plan_id, "entries": projected, "revisions": [revision]},
                      snapshot=snapshot, export_id=export_id, response_sha256=response_sha, provider=provider,
                      title=title)
    save_warning = _save_validated(binding, gated, text, bool(save_response))
    if save_warning:
        manifest["warnings"].append({"code": "W_RESPONSE_NOT_SAVED", "message": save_warning})
    all_warnings = [*manifest["warnings"]]
    result.update(ok=True, plan_id=plan_id, title=title, provider=provider, warnings=all_warnings,
                  segments=len(manifest["segments"]),
                  clips=[{"id": e["entry_id"], "kind": e["kind"], "title": e["title"],
                          **({"anchor": e["_anchor"], "notes": e["_note_aliases"], "quotes": e["_quotes"],
                              "pov_id": e["pov_id"], "audio_pov_id": e["audio_pov_id"],
                              "tc_in": tc(binding, e["session_in"]), "tc_out": tc(binding, e["session_out_exclusive"]),
                              "session_start": e["session_in"] / fps, "session_end": e["session_out_exclusive"] / fps}
                             if e["kind"] == "source" else {"seconds": e["provisional_frames"] / fps})}
                         for e in entries])
    result["report"] = _success_report(result, manifest)
    return result


def _failure_report(result):
    if result["ok"] or not result["errors"]:
        return ""
    rows = [f"Rejected: {len(result['errors'])} problem(s). Nothing was saved."]
    rows += [f"{number}. {error}" for number, error in enumerate(result["errors"], 1)]
    return "\n".join(rows)


def _success_report(result, manifest):
    from . import resolve_manifest as rm
    rows = [f"# Director AI plan: {result['title']}", "",
            f"Plan {result['plan_id']} from export {result['export_id']} ({result['provider']}), "
            f"{len(result['clips'])} entries, {len(manifest['segments'])} timeline segments.", "",
            "| # | Clip | Title | Anchor | Review TC | POV | Mic | Notes |",
            "| ---: | --- | --- | --- | --- | --- | --- | --- |"]
    for number, clip in enumerate(result["clips"], 1):
        title = clip["title"].replace("|", r"\|")
        if clip["kind"] == "placeholder":
            rows.append(f"| {number} | {clip['id']} | {title} | placeholder | {clip['seconds']:.2f} s slate | — | — | — |")
            continue
        rows.append(f"| {number} | {clip['id']} | {title} | {clip['anchor']} | {clip['tc_in']}-{clip['tc_out']} | "
                    f"{clip['pov_id'] or 'multicam'} | {clip['audio_pov_id'] or 'per angle'} | "
                    f"{', '.join(clip['notes'])} |")
    return "\n".join(rows) + "\n\n" + rm.render_report(manifest)


# --------------------------------------------------------------------------- plans, preview, build files

def _ai_revision(plan):
    return next((r for r in plan.revisions if isinstance(r, dict) and r.get("provenance") == "ai_inference"), None)


def list_plans(binding):
    rows = []
    for plan in binding.kb.edit_plans:
        revision = _ai_revision(plan)
        sources = sum(1 for e in plan.entries if e.get("kind") == "source")
        rows.append({"plan_id": plan.plan_id, "ai": revision is not None,
                     "title": (revision or {}).get("title"), "provider": (revision or {}).get("provider"),
                     "export_id": (revision or {}).get("export_id"), "clips": sources,
                     "placeholders": len(plan.entries) - sources,
                     "warnings": len((revision or {}).get("warnings", [])),
                     "created_at": (revision or {}).get("created_at")})
    return rows


def compile_plan(binding, plan_id):
    """Dry-run compile of a saved plan from the pinned inputs. Never contacts Resolve."""
    import copy
    from . import resolve_manifest as rm
    from .resolve_builder import validate_manifest
    if not isinstance(plan_id, str) or not _PLAN_ID.fullmatch(plan_id):
        raise DirectorError("E_PLAN_UNKNOWN", "Unknown plan.")
    plans = [p for p in binding.kb.edit_plans if p.plan_id == plan_id]
    if len(plans) != 1:
        raise DirectorError("E_PLAN_UNKNOWN", "Unknown plan.")
    plan, = plans
    if not plan.revisions:
        raise DirectorError("E_PLAN_UNKNOWN", "The plan has no revision to compile.")
    try:
        manifest = rm.compile_fkb_edit_plan(binding.source, copy.deepcopy(binding.kb), plan_id,
                                            revision_id=str(plan.revisions[-1]["revision_id"]))
        validate_manifest(manifest)
    except (KeyError, TypeError, ValueError) as exc:
        raise DirectorError("E_COMPILE", str(exc)) from None
    segments = manifest["segments"]
    return manifest, {"plan_id": plan_id, "segments": len(segments),
                      "slates": sum(1 for s in segments if s.get("kind") == "placeholder"),
                      "blockers": len(manifest["blockers"]), "warnings": manifest["warnings"],
                      "blocker_rows": manifest["blockers"]}


def new_project_name(now=None):
    from .resolve_builder import DIRECTOR_TEST_PREFIX
    return f"{DIRECTOR_TEST_PREFIX}{(now or datetime.now()):%Y%m%d-%H%M%S}"


def write_edit_files(binding, plan_id, manifest, stamp=None):
    """Write edit-<plan>-<stamp>/{manifest.json,report.md} in a NEW folder inside scratch; never reused."""
    from . import resolve_manifest as rm
    if not isinstance(plan_id, str) or not _PLAN_ID.fullmatch(plan_id):
        raise DirectorError("E_PLAN_UNKNOWN", "Unknown plan.")
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    if not re.fullmatch(r"\d{8}-\d{6}", stamp):
        raise DirectorError("E_WRITE", "Invalid edit folder stamp.")
    for suffix in ["", *(f"-{n}" for n in range(2, 100))]:
        name = f"edit-{plan_id}-{stamp}{suffix}"
        if not (binding.scratch / name).exists() and not _is_link(binding.scratch / name):
            break
    else:
        raise DirectorError("E_WRITE", "Could not choose a new edit folder name.")
    folder = safe_directory(binding.scratch, name)
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    for filename, content in (("manifest.json", manifest_text), ("report.md", rm.render_report(manifest))):
        with open(folder / filename, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    return folder


# --------------------------------------------------------------------------- CLI (testing and troubleshooting)

def _copy_to_clipboard(text):
    import tkinter
    root = tkinter.Tk()
    root.withdraw()
    try:
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
    finally:
        root.destroy()


def _write_user_file(path, text):
    with open(path, "x", encoding="utf-8", newline="\n") as handle:  # never overwrite
        handle.write(text)


def main(argv=None):
    """``python -m autoclipper.director_ai {export,import,plan-build} SCRATCH --session-root FOLDER ...``"""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="director_ai", description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("export", "import", "plan-build"):
        cmd = commands.add_parser(name)
        cmd.add_argument("scratch", type=Path, help="Scratch folder printed by director_poc prepare")
        cmd.add_argument("--session-root", type=Path, required=True,
                         help="Recording folder used as --session-root at prepare (read only)")
        if name == "export":
            cmd.add_argument("--notes", nargs="+", help="Note ids to include (default: all live notes)")
            cmd.add_argument("--tag")
            cmd.add_argument("--query")
            cmd.add_argument("--full-transcript", action="store_true")
            cmd.add_argument("--no-save-prompt", action="store_true", help="Keep hashes only in scratch")
            cmd.add_argument("--out", type=Path, help="New file that receives the prompt")
            cmd.add_argument("--copy", action="store_true", help="Copy the prompt to the clipboard")
        elif name == "import":
            cmd.add_argument("response", type=Path, help="Text file with the AI's answer")
            cmd.add_argument("--provider", default="unspecified")
            cmd.add_argument("--companion-closed", action="store_true",
                             help="Confirm the Director companion for this scratch is closed")
            cmd.add_argument("--no-save-response", action="store_true")
            cmd.add_argument("--repair-out", type=Path, help="New file that receives the repair prompt")
        else:
            cmd.add_argument("plan_id")
            cmd.add_argument("--dry-run", action="store_true", help="Compile and write files; do not contact Resolve")
            cmd.add_argument("--confirm-project-manager", action="store_true",
                             help="Resolve is open on the Project Manager and the current project is saved and closed")
    args = parser.parse_args(argv)
    try:
        binding = open_scratch(str(args.scratch), str(args.session_root))
        if args.command == "export":
            out = export_packet(binding, note_ids=args.notes, tag=args.tag, query=args.query,
                                full_transcript=args.full_transcript, save_prompt=not args.no_save_prompt)
            if args.out:
                _write_user_file(args.out, out["prompt"])
            if args.copy:
                _copy_to_clipboard(out["prompt"])
            print(json.dumps({k: v for k, v in out.items() if k != "prompt"}, indent=2))
            return 0
        if args.command == "import":
            text = args.response.read_text(encoding="utf-8-sig")
            result = import_response(binding, text, provider=args.provider, save_response=not args.no_save_response,
                                     companion_closed=args.companion_closed)
            if not result["ok"]:
                sys.stdout.write(result["report"] + "\n")
                if result["repair_prompt"]:
                    if args.repair_out:
                        _write_user_file(args.repair_out, result["repair_prompt"])
                        print(f"Repair prompt written to {args.repair_out}")
                    else:
                        sys.stdout.write("\n" + result["repair_prompt"])
                return 1
            print(json.dumps({"plan_id": result["plan_id"], "export_id": result["export_id"],
                              "segments": result["segments"], "warnings": len(result["warnings"])}, indent=2))
            return 0
        if not (args.dry_run or args.confirm_project_manager):
            raise DirectorError("E_CONFIRM", "Pass --dry-run, or --confirm-project-manager to build in Resolve.")
        manifest, summary = compile_plan(binding, args.plan_id)
        if summary["blockers"]:
            raise DirectorError("E_COMPILE", "The plan has blockers: " + "; ".join(
                b["message"] for b in summary["blocker_rows"]))
        folder = write_edit_files(binding, args.plan_id, manifest)
        if args.dry_run:
            print(json.dumps({"edit_dir": str(folder), "segments": summary["segments"], "slates": summary["slates"]},
                             indent=2))
            return 0
        from .resolve_builder import build_export
        name = new_project_name()
        built = build_export(manifest, project_name=name)
        with open(folder / "build.json", "x", encoding="utf-8", newline="\n") as handle:
            json.dump(built, handle, indent=2, ensure_ascii=False)
        print(f"Reopened {name} / {built.get('timeline')}")
        return 0
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        parser.exit(2, f"{parser.prog}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
