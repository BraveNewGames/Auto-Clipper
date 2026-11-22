"""AI rough cut: export a prompt, validate the pasted answer, store a buildable edit plan.

Workflow: build_prompt() -> the owner pastes the prompt into ChatGPT or Claude -> pastes the
answer back -> validate_response() extracts the JSON tolerantly, validates it strictly against
the current session, transcript and verified source mapping, compiles it with the existing
edit-plan compiler and preflights it with the Resolve builder. save_cut() writes the accepted
prompt, response, plan, manifest and report atomically under rough-cuts/<timestamp>/. Any
validation error saves nothing and returns a repair prompt instead. session.json is never written.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
from datetime import datetime, timezone
from fractions import Fraction
from hashlib import sha256
from math import isfinite
from pathlib import Path
from tempfile import mkdtemp

from . import resolve_manifest as rm
from .models import fmt_ts, parse_ts
from .reviews import _FENCE, _fixups, game_context, session_packet_lines

SCHEMA = "autoclipper.rough_cut.v1"
DEFAULT_FPS = 60
CUTS_DIR = "rough-cuts"
MAX_RESPONSE_CHARS = 1_000_000
MAX_CLIPS = 60
MIN_SOURCE_CLIPS = 2
MIN_CLIP_SECONDS = 0.5
MAX_CLIP_SECONDS = 300
MAX_TOTAL_SECONDS = 1800
MAX_PAD_SECONDS = 10
MAX_PLACEHOLDER_SECONDS = 30
MAX_QUOTES = 20
MAX_TITLE = 120
MAX_BRIEF = 4000
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_CUT_ID = re.compile(r"^\d{8}-\d{6}(?:-\d{1,3})?$")

_TOP_KEYS = {"schema", "export_id", "title", "clips", "notes"}
_SOURCE_KEYS = {"id", "kind", "title", "start_line", "end_line", "suggestion_id", "session_in_frame",
                "session_out_frame", "start_time", "end_time", "pad_before", "pad_after", "pov",
                "audio_pov", "quotes", "why"}
_PLACEHOLDER_KEYS = {"id", "kind", "title", "seconds", "why"}
_QUOTE_KEYS = {"line", "text", "speaker"}

RESPONSE_JSON_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "AutoClipper AI rough cut",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema", "export_id", "title", "clips"],
    "properties": {
        "schema": {"const": SCHEMA},
        "export_id": {"type": "string", "description": "Copy EXPORT ID from the prompt exactly"},
        "title": {"type": "string", "maxLength": MAX_TITLE},
        "notes": {"type": "string"},
        "clips": {"type": "array", "minItems": MIN_SOURCE_CLIPS, "maxItems": MAX_CLIPS, "items": {"oneOf": [
            {"type": "object", "additionalProperties": False,
             "required": ["id", "kind", "title"],
             "properties": {
                 "id": {"type": "string", "pattern": _ID.pattern}, "kind": {"const": "source"},
                 "title": {"type": "string", "maxLength": MAX_TITLE},
                 "start_line": {"type": "string", "description": "Transcript line id that starts the clip"},
                 "end_line": {"type": "string", "description": "Transcript line id that ends the clip"},
                 "suggestion_id": {"type": "string", "description": "Existing suggestion id"},
                 "session_in_frame": {"type": "integer"}, "session_out_frame": {"type": "integer"},
                 "start_time": {"type": "string"}, "end_time": {"type": "string"},
                 "pad_before": {"type": "number", "minimum": 0, "maximum": MAX_PAD_SECONDS},
                 "pad_after": {"type": "number", "minimum": 0, "maximum": MAX_PAD_SECONDS},
                 "pov": {"type": ["string", "null"]}, "audio_pov": {"type": ["string", "null"]},
                 "quotes": {"type": "array", "maxItems": MAX_QUOTES, "items": {
                     "type": "object", "additionalProperties": False, "required": ["line", "text"],
                     "properties": {"line": {"type": "string"}, "text": {"type": "string"},
                                    "speaker": {"type": "string"}}}},
                 "why": {"type": "string"}}},
            {"type": "object", "additionalProperties": False,
             "required": ["id", "kind", "title", "seconds"],
             "properties": {
                 "id": {"type": "string", "pattern": _ID.pattern}, "kind": {"const": "placeholder"},
                 "title": {"type": "string", "maxLength": MAX_TITLE},
                 "seconds": {"type": "number", "minimum": MIN_CLIP_SECONDS, "maximum": MAX_PLACEHOLDER_SECONDS},
                 "why": {"type": "string"}}},
        ]}},
    },
}

# Deliberately content-free so it cannot bias the editor toward any real moment.
_EXAMPLE = {
    "schema": SCHEMA, "export_id": "<EXPORT ID>", "title": "<working title>",
    "clips": [
        {"id": "C01", "kind": "source", "title": "<what this clip shows>",
         "start_line": "<line id>", "end_line": "<line id>", "pad_before": 1.5, "pad_after": 2.0,
         "pov": None, "audio_pov": None,
         "quotes": [{"line": "<line id>", "text": "<verbatim transcript text>"}],
         "why": "<editorial reason>"},
        {"id": "C02", "kind": "source", "title": "<title>", "suggestion_id": "<suggestion id>",
         "pov": "<pov id>", "audio_pov": None, "why": "<editorial reason>"},
        {"id": "P01", "kind": "placeholder", "title": "<missing beat or title card>", "seconds": 3},
    ],
}


class RoughCutError(ValueError):
    """The rough cut cannot be prepared, stored or built (not a fixable AI answer problem)."""


# --------------------------------------------------------------------------- context

def _digest(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                             default=str).encode("utf-8")).hexdigest()


def _seconds_to_frame(seconds, rate):
    return rm._record_frame(seconds, 0, rate)


def _clock(frame, fps):
    return fmt_ts(frame / fps)


def _intervals(spans, test):
    out = []
    for left, right, angles, _ in spans:
        if test(angles):
            if out and out[-1][1] == left:
                out[-1][1] = right
            else:
                out.append([left, right])
    return out


def _suggestion_range(session, suggestion):
    review = session.reviews.get(suggestion.suggestion_id)
    if review and review.approved_start is not None and review.approved_end is not None:
        start, end = review.approved_start, review.approved_end
    else:
        start, end = suggestion.start, suggestion.end
    status = review.status if review else None
    if not (type(start) in (int, float) and type(end) in (int, float)
            and isfinite(start) and isfinite(end) and end > start):
        return None, None, status
    return start, end, status


def prompt_export_id(context, *, brief="", target_seconds=None):
    """Bind one answer to both the verified session and the prompt options."""
    return "rc-" + _digest({"session": context["session_export_id"],
                            "brief": _clean_brief(brief), "target_seconds": _target(target_seconds)})[:16]


def prepare(session, *, transcripts_dir=None, confirmations=None, fps=DEFAULT_FPS):
    """Read the session, transcript and verified coverage that one prompt and its answer depend on."""
    lines = session_packet_lines(session)
    mapping, coverage, first, end, spans = rm._review_coverage(
        session, transcripts_dir, confirmations, fps, reference=True)
    fps = mapping["fps"]
    transcript_digest = _digest([[p.packet_id, repr(p.session_start), repr(p.session_end), p.label, p.text]
                                 for p in lines])
    session_digest = _digest(session.to_dict())
    identity = {"schema": SCHEMA, "session_id": session.session_id, "fps": fps,
                "session_sha256": session_digest, "transcript_sha256": transcript_digest,
                "source_mapping_digest": mapping["source_mapping_digest"]}
    base_id = "rc-" + _digest(identity)[:16]
    context = {
        **identity, "session_export_id": base_id,
        "session": session, "lines": lines, "line_index": {p.packet_id: p for p in lines},
        "mapping": mapping, "coverage": coverage, "first": first, "end": end, "spans": spans,
        "transcripts_dir": transcripts_dir, "confirmations": confirmations,
    }
    context["export_id"] = prompt_export_id(context)
    return context


def excluded_parts(context):
    return [blocker for blocker in context["mapping"]["blockers"] if blocker.get("part_id")]


def _coverage_text(context):
    session, fps, spans = context["session"], context["fps"], context["spans"]
    rows = [f"Session window: {_clock(context['first'], fps)} - {_clock(context['end'], fps)} "
            f"(frames [{context['first']}, {context['end']}) at {fps} fps)"]
    anywhere = _intervals(spans, bool)
    rows.append("Multicam (pov null) is possible during: "
                + (", ".join(f"{_clock(a, fps)}-{_clock(b, fps)}" for a, b in anywhere) or "NOWHERE"))
    for pov in session.povs:
        covered = _intervals(spans, lambda angles, pov_id=pov.pov_id: any(a["pov_id"] == pov_id for a in angles))
        text = ", ".join(f"{_clock(a, fps)}-{_clock(b, fps)}" for a, b in covered) or "no verified footage"
        rows.append(f"- pov id \"{pov.pov_id}\" ({pov.name}): video + own mic verified during {text}")
    excluded = excluded_parts(context)
    if excluded:
        rows.append("Recordings excluded (unverified, cannot be used): " + "; ".join(
            f"{item.get('pov_name')} {item['part_id']} ({item['code']})" for item in excluded))
    return "\n".join(rows)


def _suggestions_text(session):
    rows = []
    for suggestion in session.suggestions:
        start, end, status = _suggestion_range(session, suggestion)
        if start is None:
            continue
        reviewer = suggestion.reviewer_name or suggestion.reviewer
        rows.append(f"- {suggestion.suggestion_id} [{status or 'unreviewed'}] {fmt_ts(start)}-{fmt_ts(end)} "
                    f"{suggestion.title!r} ({reviewer}, {suggestion.priority})")
    return "\n".join(rows) or "(none)"


def build_prompt(context, *, brief="", target_seconds=None) -> str:
    from .reconcile import COMPACT_LEGEND, compact_transcript

    brief = _clean_brief(brief)
    target = _target(target_seconds)
    export_id = prompt_export_id(context, brief=brief, target_seconds=target)
    example = json.dumps(_EXAMPLE, indent=1).replace("<EXPORT ID>", export_id)
    length = f"about {target} seconds of source footage" if target else "about 60-120 seconds of source footage"
    return f"""You are the editor of a ROUGH CUT built from ONE multiplayer playtest session that was recorded
from several player POVs (cameras). Choose transcript-supported moments from anywhere in the session
and put them in the order they should PLAY. Auto-Clipper validates your answer strictly and assembles it
into a new DaVinci Resolve timeline; the human editor refines it afterwards.

GAME CONTEXT
{game_context(context["session"])}

EDITORIAL BRIEF
{brief or "Make the most entertaining, self-contained cut of this session for a game marketing team."}
Target length: {length} (guidance only).

RULES
- Order: clips play in the order you list them. They need not be chronological.
- Anchor every source clip with exactly ONE of:
  (a) start_line + end_line: transcript line ids; the clip runs from the start of start_line to the end
      of end_line, widened by pad_before / pad_after seconds (0-{MAX_PAD_SECONDS}; default 0);
  (b) suggestion_id: an existing suggestion below (its approved range when set), widened by the pads;
  (c) session_in_frame + session_out_frame (exclusive) at {context['fps']} fps, plus start_time and end_time
      (HH:MM:SS.mmm) that agree with those frames. Use only when you need exact frames.
  You may also add suggestion_id to (a) or (c) to credit the suggestion that inspired a clip.
- quotes: at least one verbatim quote ({{"line", "text"}}) from inside the clip, unless anchored by (b).
  Copy the text exactly from the transcript. Never invent line ids, quotes, POV ids or suggestion ids.
- pov: null (recommended) = multicam of every camera covering the clip, switched later by the editor;
  or one pov id from COVERAGE that has verified video for the WHOLE clip including pads.
- audio_pov: null (recommended) = each angle's own mic, switching with the angle;
  or one pov id whose own mic covers the WHOLE clip. The group conversation is added automatically.
- Clips: {MIN_CLIP_SECONDS}-{MAX_CLIP_SECONDS} s each; at most {MAX_CLIPS} entries and {MAX_TOTAL_SECONDS} s in total;
  at least {MIN_SOURCE_CLIPS} source clips. Use a placeholder ({MIN_CLIP_SECONDS}-{MAX_PLACEHOLDER_SECONDS} s) only for a beat that
  must be filmed or a title card; it becomes a labelled slate.
- A clip outside verified coverage is rejected, not filled with a slate. Check COVERAGE first.
- ids: short unique ids such as C01, C02, P01.

OUTPUT
Return ONLY one JSON object (no prose before or after) shaped like this (<...> are placeholders):
{example}
If asked for several cuts, you may instead return {{"cuts": [<cut>, <cut>, ...]}}, each cut shaped as above with the same export_id.

JSON schema: {json.dumps(RESPONSE_JSON_SCHEMA, separators=(",", ":"))}

SESSION
Session id: {context["session"].session_id}
EXPORT ID: {export_id} (copy into "export_id"; it proves the answer matches this session state and prompt options)

COVERAGE
{_coverage_text(context)}

EXISTING SUGGESTIONS (hints from AI Review; [status] is the owner's decision; optional to use)
{_suggestions_text(context["session"])}

TRANSCRIPT ({len(context["lines"])} lines)
{COMPACT_LEGEND}

{compact_transcript(context["lines"])}
"""


def _clean_brief(brief):
    if brief is None:
        return ""
    if not isinstance(brief, str):
        raise RoughCutError("The editorial brief must be text.")
    brief = brief.strip()
    if len(brief) > MAX_BRIEF:
        raise RoughCutError(f"The editorial brief is longer than {MAX_BRIEF} characters.")
    return brief


def _target(value):
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise RoughCutError("Target length must be a whole number of seconds.") from None
    if isinstance(value, bool) or not 5 <= number <= MAX_TOTAL_SECONDS:
        raise RoughCutError(f"Target length must be 5-{MAX_TOTAL_SECONDS} seconds.")
    return number


# --------------------------------------------------------------------------- extraction

def extract_json(text, *, key="clips"):
    """Return the pasted answer object that has a ``key`` list. Accepts code fences, prose and smart quotes."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Paste the AI's JSON answer.")
    if len(text) > MAX_RESPONSE_CHARS:
        raise ValueError(f"The pasted answer is longer than {MAX_RESPONSE_CHARS} characters.")
    text = text.strip().lstrip("﻿")
    decoder = json.JSONDecoder()
    candidates = []
    for blob in [text, *(match.group(1) for match in _FENCE.finditer(text))]:
        for variant in (blob, _fixups(blob)):
            try:
                value = json.loads(variant)
            except ValueError:
                value = None
            if isinstance(value, dict) and key in value:
                return value
            for match in re.finditer(r"\{", variant):
                try:
                    value, end = decoder.raw_decode(variant, match.start())
                except ValueError:
                    continue
                if isinstance(value, dict) and key in value:
                    candidates.append((end - match.start(), value))
    if not candidates:
        raise ValueError(f'Could not find a JSON object with a "{key}" list in the pasted answer.')
    return max(candidates, key=lambda item: item[0])[1]


MAX_CUTS = 12


def _top_level_objects(text):
    decoder = json.JSONDecoder()
    found, position = [], 0
    while True:
        start = text.find("{", position)
        if start < 0:
            return found
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            position = start + 1
            continue
        if isinstance(value, dict):
            found.append(value)
        position = end


def split_answers(text):
    """Several pasted answers, or a {"cuts": [...]} wrapper, as (answers, unparsed_count).

    Returns None when the paste holds at most one plain answer; callers then use the single-answer
    path unchanged. Elements of "cuts" are returned as-is (they may be malformed; validation reports that).
    """
    if not isinstance(text, str) or not text.strip():
        return None
    if len(text) > MAX_RESPONSE_CHARS * MAX_CUTS:
        raise ValueError(f"The pasted answers are longer than {MAX_RESPONSE_CHARS * MAX_CUTS} characters.")
    text = text.strip().lstrip("﻿")
    answers, wrapped = [], False
    for variant in (text, _fixups(text)):
        answers, wrapped = [], False
        for obj in _top_level_objects(variant):
            if isinstance(obj.get("cuts"), list) and "clips" not in obj:
                wrapped = True
                answers.extend(obj["cuts"])
            elif "clips" in obj:
                answers.append(obj)
        if answers:
            break
    if not answers or (len(answers) == 1 and not wrapped):
        return None
    # An answer that is not valid JSON cannot be seen above; count the export_id fields to notice.
    claimed = len(re.findall(r'"export_id"', text))
    unparsed = 0 if wrapped else max(0, claimed - len(answers))
    return answers, unparsed


def combined_repair_prompt(context, problems, export_id):
    """One repair prompt for every rejected cut. ``problems`` is [(label, [error, ...])]."""
    errors = [f"{label}: {error}" for label, items in problems for error in items]
    text = _repair_prompt(context, errors, export_id=export_id)
    return (text.replace("Nothing was saved.", "Nothing was saved for the rejected cuts; the other cuts were saved.", 1)
            .replace("return the COMPLETE corrected JSON object only,",
                     'return ONLY {"cuts": [...]} holding the COMPLETE corrected version of each rejected cut '
                     "(not the accepted ones),", 1))


# --------------------------------------------------------------------------- validation

def _normalize_quote(text):
    text = (text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
            .replace("…", "..."))
    text = re.sub(r"\s+", " ", text).strip().casefold()
    text = re.sub(r"\s*\(\?\)$", "", text)
    return text.strip(" .\"'")


def _number(value):
    return type(value) in (int, float) and value == value and abs(value) != float("inf")


def _text_field(clip, key, where, errors, *, required=True, limit=MAX_TITLE):
    value = clip.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where}: \"{key}\" must be nonempty text.")
        return None
    if len(value) > limit or _CONTROL.search(value):
        errors.append(f"{where}: \"{key}\" must be one line of at most {limit} characters.")
        return None
    return value.strip()


def _pov(context, value, key, where, errors):
    if value is None:
        return None
    povs = context["session"].povs
    if isinstance(value, str) and any(value == pov.pov_id for pov in povs):
        return value
    valid = ", ".join(f'"{pov.pov_id}"' for pov in povs)
    errors.append(f"{where}: \"{key}\" must be null or one of the pov ids {valid}; got {value!r}.")
    return None


def _range_problems(context, first, end, pov, audio_pov):
    """Return messages for any part of [first, end) the chosen video or mic cannot supply."""
    fps, names = context["fps"], {p.pov_id: p.name for p in context["session"].povs}
    spans = list(rm._coverage_spans(context["session"].povs, context["coverage"], first, end))
    checks = [("video", pov, lambda ids: pov in ids if pov else bool(ids))]
    if audio_pov:
        checks.append(("own mic", audio_pov, lambda ids: audio_pov in ids))
    problems = []
    for label, pov_id, ok in checks:
        missing = _intervals(spans, lambda angles: not ok({a["pov_id"] for a in angles}))
        if missing:
            who = f"{names[pov_id]} (\"{pov_id}\")" if pov_id else "any camera (multicam)"
            ranges = ", ".join(f"{_clock(a, fps)}-{_clock(b, fps)}" for a, b in missing)
            problems.append(f"{who} has no verified {label} during {ranges}.")
    return problems


def _validate_clip(context, clip, where, errors, anchors=None):
    """Validate one entry and return its edit-plan projection, or None when it has errors.

    ``anchors`` is an optional hook for extra anchor kinds (the Director bridge's note anchors). It has
    ``keys`` (the full allowed source keys), ``hint`` (the anchor-missing message) and
    ``resolve(clip, where, errors, pads, rate)`` returning ``(anchor, first, end, extra)``; ``anchor`` is
    None when the clip uses none of its kinds, and ``extra`` entries are merged into the projection.
    """
    count = len(errors)
    fps, rate = context["fps"], Fraction(context["fps"])
    kind = clip.get("kind")
    allowed = _PLACEHOLDER_KEYS if kind == "placeholder" else (anchors.keys if anchors else _SOURCE_KEYS)
    if kind not in ("source", "placeholder"):
        errors.append(f"{where}: \"kind\" must be \"source\" or \"placeholder\".")
        return None
    unknown = sorted(set(clip) - allowed)
    if unknown:
        errors.append(f"{where}: unsupported keys {', '.join(unknown)}; allowed keys are {', '.join(sorted(allowed))}.")
    title = _text_field(clip, "title", where, errors)
    if kind == "placeholder":
        seconds = clip.get("seconds")
        if not _number(seconds) or not MIN_CLIP_SECONDS <= seconds <= MAX_PLACEHOLDER_SECONDS:
            errors.append(f"{where}: placeholder \"seconds\" must be a number from {MIN_CLIP_SECONDS} "
                          f"to {MAX_PLACEHOLDER_SECONDS}.")
        if len(errors) > count:
            return None
        return {"kind": "placeholder", "title": title,
                "provisional_frames": max(1, _seconds_to_frame(seconds, rate))}

    lines, session = context["line_index"], context["session"]
    suggestion = None
    if clip.get("suggestion_id") is not None:
        sid = clip["suggestion_id"]
        suggestion = next((s for s in session.suggestions if s.suggestion_id == sid), None)
        if suggestion is None:
            errors.append(f"{where}: suggestion_id {sid!r} is not in EXISTING SUGGESTIONS.")
        elif _suggestion_range(session, suggestion)[0] is None:
            errors.append(f"{where}: suggestion {sid!r} has no timed range.")
            suggestion = None
    has_lines = any(key in clip for key in ("start_line", "end_line"))
    has_frames = anchors is None and any(
        key in clip for key in ("session_in_frame", "session_out_frame", "start_time", "end_time"))
    pads = {}
    for key in ("pad_before", "pad_after"):
        value = clip.get(key, 0)
        if value is None:
            value = 0
        if not _number(value) or not 0 <= value <= MAX_PAD_SECONDS:
            errors.append(f"{where}: \"{key}\" must be a number of seconds from 0 to {MAX_PAD_SECONDS}.")
            value = 0
        pads[key] = Fraction(str(value))

    first = end = None
    anchor = None
    hook_anchor, extra = None, {}
    if anchors is not None:
        hook_anchor, hook_first, hook_end, extra = anchors.resolve(clip, where, errors, pads, rate)
    if hook_anchor and (has_lines or has_frames):
        errors.append(f"{where}: use exactly one anchor kind.")
    elif hook_anchor:
        anchor, first, end = hook_anchor, hook_first, hook_end
    elif has_lines and has_frames:
        errors.append(f"{where}: use either start_line/end_line or session frames, not both.")
    elif has_lines:
        anchor = "lines"
        start, stop = clip.get("start_line"), clip.get("end_line")
        known = [isinstance(v, str) and v in lines for v in (start, stop)]
        for key, value, ok in (("start_line", start, known[0]), ("end_line", stop, known[1])):
            if not ok:
                errors.append(f"{where}: \"{key}\" {value!r} is not a transcript line id.")
        if all(known):
            if lines[start].session_start > lines[stop].session_start:
                errors.append(f"{where}: start_line {start} comes after end_line {stop}.")
            else:
                first = _seconds_to_frame(Fraction(str(lines[start].session_start)) - pads["pad_before"], rate)
                end = _seconds_to_frame(Fraction(str(max(lines[start].session_end, lines[stop].session_end)))
                                        + pads["pad_after"], rate)
    elif has_frames:
        anchor = "frames"
        first, end = clip.get("session_in_frame"), clip.get("session_out_frame")
        if any(clip.get(key, 0) not in (0, None) for key in ("pad_before", "pad_after")):
            errors.append(f"{where}: frame-anchored clips cannot use pads; widen the frames instead.")
        if type(first) is not int or type(end) is not int:
            errors.append(f"{where}: session_in_frame and session_out_frame must be integers.")
            first = end = None
        for key, frame in (("start_time", first), ("end_time", end)):
            try:
                claimed = _seconds_to_frame(Fraction(str(parse_ts(clip[key]))), rate)
            except (KeyError, TypeError, ValueError):
                errors.append(f"{where}: frame anchors need \"{key}\" as HH:MM:SS.mmm.")
                first = end = None
                continue
            if frame is not None and abs(claimed - frame) > 1:
                errors.append(f"{where}: \"{key}\" {clip[key]} is frame {claimed}, which disagrees with "
                              f"frame {frame}. Recheck the timecode or the frame number.")
                first = end = None
    elif suggestion is not None:
        anchor = "suggestion"
        start, stop, _ = _suggestion_range(session, suggestion)
        first = _seconds_to_frame(Fraction(str(start)) - pads["pad_before"], rate)
        end = _seconds_to_frame(Fraction(str(stop)) + pads["pad_after"], rate)
    elif clip.get("suggestion_id") is None:
        errors.append(f"{where}: " + (anchors.hint if anchors else
                      "anchor the clip with start_line/end_line, suggestion_id or session frames."))

    pov = _pov(context, clip.get("pov"), "pov", where, errors)
    audio_pov = _pov(context, clip.get("audio_pov"), "audio_pov", where, errors)

    if first is not None and end is not None:
        if end - first < MIN_CLIP_SECONDS * fps or end - first > MAX_CLIP_SECONDS * fps:
            errors.append(f"{where}: the clip is {(end - first) / fps:.2f} s; clips must be "
                          f"{MIN_CLIP_SECONDS}-{MAX_CLIP_SECONDS} s.")
        elif first < context["first"] or end > context["end"]:
            errors.append(f"{where}: {_clock(first, fps)}-{_clock(end, fps)} is outside the session window "
                          f"{_clock(context['first'], fps)}-{_clock(context['end'], fps)}.")
        elif len(errors) == count:
            errors.extend(f"{where}: {problem}" for problem in _range_problems(context, first, end, pov, audio_pov))

    quotes = clip.get("quotes")
    if quotes is None:
        quotes = []
    if not isinstance(quotes, list) or len(quotes) > MAX_QUOTES:
        errors.append(f"{where}: \"quotes\" must be a list of at most {MAX_QUOTES} objects.")
        quotes = []
    elif not quotes and anchor in ("lines", "frames"):
        errors.append(f"{where}: give at least one verbatim quote (line id + text) from inside the clip.")
    cited = []
    for number, quote in enumerate(quotes, 1):
        label = f"{where} quote {number}"
        if not isinstance(quote, dict) or set(quote) - _QUOTE_KEYS:
            errors.append(f"{label}: must be an object with \"line\" and \"text\".")
            continue
        line = lines.get(quote.get("line")) if isinstance(quote.get("line"), str) else None
        text = quote.get("text")
        if line is None:
            errors.append(f"{label}: line {quote.get('line')!r} is not a transcript line id.")
            continue
        normalized = _normalize_quote(text) if isinstance(text, str) else ""
        full_line = _normalize_quote(line.text)
        if (not normalized or normalized not in full_line or
                not (len(normalized.split()) >= 3 or len(normalized) >= 12 or normalized == full_line)):
            errors.append(f"{label}: text is not verbatim from {line.packet_id} ({line.text!r}).")
            continue
        if first is not None and end is not None and not (
                _seconds_to_frame(line.session_start, rate) < end and _seconds_to_frame(line.session_end, rate) > first):
            errors.append(f"{label}: {line.packet_id} ({fmt_ts(line.session_start)}) is outside the clip "
                          f"{_clock(first, fps)}-{_clock(end, fps)}.")
            continue
        cited.append(line.packet_id)
    if len(errors) > count:
        return None
    return {"kind": "source", "title": title, "session_in": first, "session_out_exclusive": end,
            "pov_id": pov, "audio_pov_id": audio_pov,
            "candidate_ids": [suggestion.suggestion_id] if suggestion else [],
            "_anchor": anchor, "_quotes": cited, **extra}


def _repair_prompt(context, errors, stale=False, export_id=None, *, schema=None, reminders=None,
                   label="rough-cut"):
    if stale:
        return ""
    schema = schema or SCHEMA
    povs = ", ".join(f'"{pov.pov_id}" ({pov.name})' for pov in context["session"].povs)
    problems = "\n".join(f"{number}. {error}" for number, error in enumerate(errors, 1))
    export_id = export_id or context["export_id"]
    if reminders is None:
        reminders = f"""- Valid pov ids: {povs}. pov and audio_pov may be null (recommended).
- Anchor each source clip with start_line + end_line, OR suggestion_id, OR session_in_frame +
  session_out_frame with matching start_time/end_time. Pads are 0-{MAX_PAD_SECONDS} s.
- Quotes must be copied verbatim with the line id of a line inside the clip.
- Only use times inside the COVERAGE listed in the original prompt; never invent ids."""
    return f"""Auto-Clipper rejected your {label} JSON for export {export_id}. Nothing was saved.
Fix EVERY problem below and return the COMPLETE corrected JSON object only, with "schema": "{schema}"
and "export_id": "{export_id}". Keep the clips that had no problems unchanged and in order.

PROBLEMS
{problems}

REMINDERS
{reminders}
"""


def gate_plan(context, plan, result, *, compile_fn, repair, require_reference=False):
    """Compile ``plan`` dry-run and check what the compiled manifest actually does.

    Returns the manifest, or None after appending to ``result["errors"]`` (and setting ``result["stale"]``
    or ``result["repair_prompt"]`` where a repair is possible). ``compile_fn(context, plan)`` is the
    caller's compiler; ``repair()`` builds the caller's repair prompt.
    """
    errors = result["errors"]
    try:
        manifest = compile_fn(context, plan)
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"Compile dry run failed: {exc}")
        return None
    if manifest["source_mapping_digest"] != context["source_mapping_digest"]:
        result["stale"] = True
        errors.append("Recordings or mic confirmations changed during validation. Copy a new prompt.")
        return None
    unavailable = [s["event_id"] for s in manifest["segments"] if s.get("placeholder_reason") == "source_unavailable"]
    if unavailable:
        errors.append(f"Compile dry run found unavailable footage in {', '.join(dict.fromkeys(unavailable))}.")
        result["repair_prompt"] = repair()
        return None
    if require_reference:
        reference_gaps = [s["event_id"] for s in manifest["segments"]
                          if s.get("kind") == "media" and s.get("reference_audio") is None]
        if reference_gaps:
            errors.append("No verified GROUP REFERENCE AUDIO mix covers the whole range of "
                          + ", ".join(dict.fromkeys(reference_gaps)) + ". Choose ranges with a verified group mix.")
            result["repair_prompt"] = repair()
            return None
    # Parts that failed verification only remove their coverage; other blockers remain fatal.
    if any(not blocker.get("part_id") for blocker in manifest["blockers"]):
        errors.extend(f"Compile dry run blocked: {blocker['message']}" for blocker in manifest["blockers"]
                      if not blocker.get("part_id"))
        return None
    for blocker in manifest["blockers"]:
        manifest["warnings"].append({**blocker, "code": "part_excluded", "excluded_code": blocker["code"],
                                     "message": f"Excluded from coverage: {blocker['message']}"})
    manifest["blockers"] = []
    used = {a["part_id"] for s in manifest["segments"]
            for a in (*s["angles"], *(s[k] for k in ("audio_angle", "reference_angle") if s.get(k)))}
    if used & {item["part_id"] for item in excluded_parts(context)}:
        errors.append("Compile dry run used an excluded recording part.")
        return None
    return manifest


def validate_response(context, text, *, brief="", target_seconds=None):
    """Validate an AI answer completely. Return a result dict; result["ok"] is False on any error."""
    from .resolve_builder import validate_manifest

    brief, target = _clean_brief(brief), _target(target_seconds)
    export_id = prompt_export_id(context, brief=brief, target_seconds=target)
    prompt = build_prompt(context, brief=brief, target_seconds=target)
    result = {"ok": False, "export_id": export_id, "errors": [], "warnings": [],
              "stale": False, "repair_prompt": ""}
    errors = result["errors"]
    try:
        data = extract_json(text)
    except ValueError as exc:
        errors.append(str(exc))
        result["repair_prompt"] = _repair_prompt(context, errors, export_id=export_id)
        return result

    claimed = data.get("export_id")
    if not isinstance(claimed, str) or not re.fullmatch(r"rc-[0-9a-f]{16}", claimed):
        errors.append(f"\"export_id\" must be copied exactly from the prompt: \"{export_id}\".")
    elif claimed != export_id:
        result["stale"] = True
        errors.append(f"The answer is for export {data.get('export_id')!r}, but the session is now at "
                      f"{export_id}. The session, prompt options, transcript, mic confirmations or recordings "
                      "changed since the prompt was copied (or the answer belongs to another session). "
                      "Copy a new prompt.")
        return result
    if data.get("schema") != SCHEMA:
        errors.append(f"\"schema\" must be \"{SCHEMA}\".")
    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        errors.append(f"Unsupported top-level keys {', '.join(unknown)}; allowed: {', '.join(sorted(_TOP_KEYS))}.")
    title = _text_field(data, "title", "Answer", errors)
    if "notes" in data and not isinstance(data["notes"], str):
        errors.append("\"notes\" must be text.")
    clips = data.get("clips")
    if not isinstance(clips, list) or not clips:
        errors.append("\"clips\" must be a nonempty list.")
        clips = []
    elif len(clips) > MAX_CLIPS:
        errors.append(f"\"clips\" has {len(clips)} entries; the limit is {MAX_CLIPS}.")
        clips = []

    entries, seen = [], set()
    for number, clip in enumerate(clips, 1):
        if not isinstance(clip, dict):
            errors.append(f"Clip #{number}: must be an object.")
            continue
        clip_id = clip.get("id")
        where = f"Clip #{number} ({clip_id})" if isinstance(clip_id, str) else f"Clip #{number}"
        if not isinstance(clip_id, str) or not _ID.match(clip_id):
            errors.append(f"{where}: \"id\" must be a short id such as C01 (letters, digits, _ . -).")
        elif clip_id in seen:
            errors.append(f"{where}: duplicate id.")
        seen.add(clip_id)
        entry = _validate_clip(context, clip, where, errors)
        if entry is not None:
            entries.append({"entry_id": clip_id, **entry})

    fps = context["fps"]
    sources = [e for e in entries if e["kind"] == "source"]
    if clips and sum(isinstance(c, dict) and c.get("kind") == "source" for c in clips) < MIN_SOURCE_CLIPS:
        errors.append(f"A rough cut needs at least {MIN_SOURCE_CLIPS} source clips.")
    if clips and not errors:
        total = sum(e["session_out_exclusive"] - e["session_in"] for e in sources) + \
            sum(e["provisional_frames"] for e in entries if e["kind"] == "placeholder")
        if total > MAX_TOTAL_SECONDS * fps:
            errors.append(f"The cut is {total / fps:.1f} s; the limit is {MAX_TOTAL_SECONDS} s.")
    if errors:
        result["repair_prompt"] = _repair_prompt(context, errors, export_id=export_id)
        return result

    warnings = result["warnings"]
    ordered = sorted(sources, key=lambda e: e["session_in"])
    for left, right in zip(ordered, ordered[1:]):
        if right["session_in"] < left["session_out_exclusive"]:
            warnings.append({"code": "overlapping_clips", "event_id": right["entry_id"],
                             "message": f"{right['entry_id']} overlaps {left['entry_id']} in session time."})
    total = sum(e["session_out_exclusive"] - e["session_in"] for e in sources) / fps
    if target and not 0.5 * target <= total <= 2 * target:
        warnings.append({"code": "target_length", "message": f"Source clips total {total:.1f} s; target was {target} s."})

    response_sha = sha256(text.encode("utf-8")).hexdigest()
    plan = {
        "plan_id": f"rough-cut-{export_id}", "revision_id": response_sha[:16],
        "revision_history": [], "session_id": context["session"].session_id, "fps": fps,
        "source_mapping_digest": context["source_mapping_digest"],
        "entries": [{key: value for key, value in entry.items() if not key.startswith("_")} for entry in entries],
    }
    # Final gate: compile with the shared edit-plan compiler and run the builder's own preflight.
    manifest = gate_plan(
        context, plan, result, require_reference=True,
        compile_fn=lambda ctx, proposed: rm.compile_edit_plan(
            ctx["session"], proposed, ctx["transcripts_dir"], ctx["confirmations"], reference_audio=True),
        repair=lambda: _repair_prompt(context, errors, export_id=export_id))
    if manifest is None:
        return result
    manifest["rough_cut"] = {"schema": SCHEMA, "export_id": export_id, "title": title,
                             "brief": brief, "target_seconds": target,
                             "prompt_sha256": sha256(prompt.encode("utf-8")).hexdigest(),
                             "response_sha256": response_sha}
    try:
        validate_manifest(manifest)
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"Resolve builder preflight failed: {exc}")
        return result
    manifest["warnings"].extend(warnings)
    result.update(ok=True, title=title, plan=plan, manifest=manifest, prompt=prompt, response_sha256=response_sha,
                  warnings=manifest["warnings"],
                  clips=[{"id": e["entry_id"], "kind": e["kind"], "title": e["title"],
                          **({"anchor": e["_anchor"], "quotes": e["_quotes"], "pov_id": e["pov_id"],
                              "audio_pov_id": e["audio_pov_id"],
                              "session_start": e["session_in"] / fps, "session_end": e["session_out_exclusive"] / fps}
                             if e["kind"] == "source" else {"seconds": e["provisional_frames"] / fps})}
                         for e in entries])
    result["report"] = render_report(result)
    return result


def render_report(result) -> str:
    manifest = result["manifest"]
    cut = manifest["rough_cut"]
    rows = [f"# AI rough cut: {result['title']}", "",
            f"Export {cut['export_id']}, {len(result['clips'])} entries, {len(manifest['segments'])} timeline segments, "
            f"{manifest['fps']} fps. Validated {datetime.now(timezone.utc).isoformat(timespec='seconds')}.", "",
            "| # | Clip | Title | Anchor | Session | POV | Mic | Quotes |", "| ---: | --- | --- | --- | --- | --- | --- | --- |"]
    for number, clip in enumerate(result["clips"], 1):
        title = clip["title"].replace("|", r"\|")
        if clip["kind"] == "placeholder":
            rows.append(f"| {number} | {clip['id']} | {title} | placeholder | {clip['seconds']:.2f} s slate | — | — | — |")
            continue
        rows.append(f"| {number} | {clip['id']} | {title} | {clip['anchor']} | "
                    f"{fmt_ts(clip['session_start'])}–{fmt_ts(clip['session_end'])} | "
                    f"{clip['pov_id'] or 'multicam'} | {clip['audio_pov_id'] or 'source (per angle)'} | "
                    f"{', '.join(clip['quotes']) or '—'} |")
    return "\n".join(rows) + "\n\n" + rm.render_report(manifest)


# --------------------------------------------------------------------------- storage

def _is_link(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def safe_directory(base, *names):
    """Create or open base/names... refusing links, junctions and paths that leave base."""
    base = Path(base)
    if not base.is_dir() or _is_link(base):
        raise RoughCutError(f"Output folder is missing or is a link: {base}")
    current = base
    for name in names:
        current = current / name
        if _is_link(current):
            raise RoughCutError(f"Refusing to write through a link or junction: {current}")
        if not current.exists():
            current.mkdir()
        if not current.is_dir():
            raise RoughCutError(f"Expected a folder: {current}")
    if not current.resolve().is_relative_to(base.resolve()):
        raise RoughCutError(f"Output leaves its base folder: {current}")
    return current


def session_cuts_root(session):
    return safe_directory(Path(session.folder), "autoclipper_exports", CUTS_DIR)


def save_cut(cuts_root, result, response_text):
    """Write one accepted rough cut atomically: a complete new folder or nothing."""
    if not result.get("ok"):
        raise RoughCutError("Only a validated rough cut can be saved.")
    cuts_root = Path(cuts_root)
    if _is_link(cuts_root) or not cuts_root.is_dir():
        raise RoughCutError(f"Rough-cut folder is missing or is a link: {cuts_root}")
    manifest_text = json.dumps(result["manifest"], ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    record = {key: result[key] for key in ("export_id", "title", "response_sha256", "warnings", "clips")}
    record.update(schema=SCHEMA, saved_at=datetime.now(timezone.utc).isoformat(),
                  prompt_sha256=result["manifest"]["rough_cut"]["prompt_sha256"],
                  manifest_sha256=sha256(manifest_text.encode("utf-8")).hexdigest())
    files = {
        "prompt.txt": result["prompt"], "response.txt": response_text,
        "plan.json": json.dumps(result["plan"], ensure_ascii=False, indent=2) + "\n",
        "manifest.json": manifest_text, "report.md": result["report"],
        "validation.json": json.dumps(record, ensure_ascii=False, indent=2) + "\n",
    }
    partial = Path(mkdtemp(prefix=".partial-", dir=cuts_root))
    try:
        for name, content in files.items():
            with open(partial / name, "x", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for suffix in ["", *(f"-{n}" for n in range(2, 100))]:
            target = cuts_root / f"{stamp}{suffix}"
            if target.exists() or _is_link(target):
                continue
            try:
                os.rename(partial, target)
            except FileExistsError:
                continue
            return target
        raise RoughCutError("Could not choose a new rough-cut folder name.")
    finally:
        if partial.exists():
            shutil.rmtree(partial, ignore_errors=True)


def load_cut(cuts_root, cut_id):
    """Read a saved cut's manifest after checking its name, folder and recorded checksum."""
    if not isinstance(cut_id, str) or not _CUT_ID.match(cut_id):
        raise RoughCutError("Unknown rough cut.")
    folder = Path(cuts_root) / cut_id
    if _is_link(folder) or not folder.is_dir():
        raise RoughCutError("Unknown rough cut.")
    for name in ("manifest.json", "validation.json"):
        if _is_link(folder / name) or not (folder / name).is_file():
            raise RoughCutError(f"Rough cut {cut_id} is incomplete.")
    manifest_bytes = (folder / "manifest.json").read_bytes()
    record = json.loads((folder / "validation.json").read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise RoughCutError(f"Rough cut {cut_id} validation record is malformed. Import the answer again.")
    if sha256(manifest_bytes).hexdigest() != record.get("manifest_sha256"):
        raise RoughCutError(f"Rough cut {cut_id} manifest was changed after validation. Import the answer again.")
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("rough_cut"), dict):
        raise RoughCutError(f"Rough cut {cut_id} manifest is malformed. Import the answer again.")
    if manifest.get("rough_cut", {}).get("export_id") != record.get("export_id"):
        raise RoughCutError(f"Rough cut {cut_id} records disagree. Import the answer again.")
    return folder, manifest, record


# --------------------------------------------------------------------------- CLI (testing adapter)

def main(argv=None):
    from .store import load_session

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prompt", "import"):
        cmd = sub.add_parser(name)
        cmd.add_argument("session", type=Path, help="Session folder or session.json (read only)")
        cmd.add_argument("--transcripts", type=Path)
        cmd.add_argument("--confirmations", type=Path)
        cmd.add_argument("--brief", default="")
        cmd.add_argument("--target", type=int)
        if name == "import":
            cmd.add_argument("response", type=Path)
            cmd.add_argument("--out", type=Path, required=True,
                             help="Existing folder that receives the new rough-cut folder")
    args = parser.parse_args(argv)
    try:
        session = load_session(args.session)
        confirmations = args.confirmations
        if confirmations is None:
            confirmations = Path(session.folder) / ".autoclipper" / "mic_confirmations.json"
        context = prepare(session, transcripts_dir=args.transcripts, confirmations=confirmations)
        if args.command == "prompt":
            sys.stdout.write(build_prompt(context, brief=args.brief, target_seconds=args.target))
            return 0
        text = args.response.read_text(encoding="utf-8-sig")
        result = validate_response(context, text, brief=args.brief, target_seconds=args.target)
        if not result["ok"]:
            sys.stdout.write("\n".join(result["errors"]) + "\n\n" + result["repair_prompt"])
            return 1
        folder = save_cut(args.out, result, text)
        print(json.dumps({"cut_dir": str(folder), "export_id": result["export_id"],
                          "segments": len(result["manifest"]["segments"])}, indent=2))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, f"{parser.prog}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
