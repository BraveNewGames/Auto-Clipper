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
from .rough_cut import _clock, _digest, _intervals, _is_link, safe_directory

SCHEMA = "autoclipper.director_bridge.v1"
RECORD_SCHEMA = "autoclipper.director_bridge_export.v1"
SCRATCH_PREFIX = "autoclipper-director-a-"
AI_FOLDER = ("director-ai",)
DEFAULT_BUDGET_CHARS = 120_000
HARD_CAP_CHARS = 400_000
WINDOW_STEPS = (20, 15, 10, 5)
MAX_NOTES = 200
EXPORT_ID = re.compile(r"^dx-[0-9a-f]{12}$")
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
        "  The clip must overlap the transcript context shown for a note it cites.",
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
    if not isinstance(export_id, str) or not EXPORT_ID.match(export_id):
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
