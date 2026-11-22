"""AI editorial review packets, tolerant review import, and event grouping.

Workflow: build_review_packet() -> user pastes it into ChatGPT / Claude -> pastes the
answer back -> import_review() parses it, gives every suggestion a unique ID and groups
suggestions from different reviewers that cover the same moment under one event ID.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from .models import DEFAULT_GAME_CONTEXT, Event, Suggestion, fmt_ts, parse_ts

DEFAULT_PREFIX = "EV"
EVENT_OVERLAP = 0.5          # overlap / shorter range needed to share an event
DEFAULT_LENGTH = 15.0        # assumed clip length when a reviewer gives only a start time

_TIME = {"type": ["string", "null"],
         "description": "Session time HH:MM:SS from the transcript. Required for clip_now; omit or null "
                        "for needs_filming ideas that are not tied to a moment."}

REVIEW_JSON_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "AutoClipper editorial review",
    "type": "object",
    "required": ["reviewer", "suggestions"],
    "properties": {
        "reviewer": {"type": "string", "description": "Your reviewer name, e.g. Claude or ChatGPT"},
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "title", "priority", "kind", "what_happens", "why"],
                "if": {"properties": {"kind": {"const": "clip_now"}}},
                "then": {"required": ["start", "end"],
                         "properties": {"start": {"type": "string"}, "end": {"type": "string"}}},
                "properties": {
                    "id": {"type": "string", "description": "Your own short ID, e.g. R-01"},
                    "title": {"type": "string", "description": "2-5 word memorable title"},
                    "start": _TIME,
                    "end": {**_TIME, "description": "Session time HH:MM:SS, after start (clip_now)."},
                    "priority": {"enum": ["high", "medium", "low"]},
                    "kind": {"enum": ["clip_now", "needs_filming"]},
                    "what_happens": {"type": "string"},
                    "why": {"type": "string", "description": "Why it works as a short video"},
                    "hook": {"type": "string", "description": "First 1-2 seconds / caption hook"},
                    "structure": {"type": "string", "description": "Setup -> escalation -> payoff, edit approach"},
                    "quotes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["text"],
                            "properties": {
                                "line": {"type": "string", "description": "Transcript line id, e.g. L0042"},
                                "time": {"type": "string"}, "speaker": {"type": "string"},
                                "text": {"type": "string"}}}},
                    "povs": {"type": "array", "items": {"type": "string"}},
                    "pov_notes": {"type": "string"},
                    "confirmed_by_transcript": {"type": "string"},
                    "needs_visual_check": {"type": "string"},
                    "uncertainty": {"type": "string"},
                }}}},
}

# Deliberately content-free so it cannot bias reviewers toward any real moment.
_EXAMPLE = {
    "reviewer": "<REVIEWER>",
    "suggestions": [
        {"id": "R-01", "title": "<short title>", "start": "<HH:MM:SS>", "end": "<HH:MM:SS>",
         "priority": "<high|medium|low>", "kind": "clip_now",
         "what_happens": "<what happens, from the transcript>",
         "why": "<why it works as a self-contained short video>",
         "hook": "<opening line or caption>", "structure": "<setup -> escalation -> payoff; edit notes>",
         "quotes": [{"line": "<line id>", "time": "<HH:MM:SS>", "speaker": "<speaker as shown>",
                     "text": "<verbatim transcript text>"}],
         "povs": ["<player>"], "pov_notes": "<which camera likely shows it and why>",
         "confirmed_by_transcript": "<what the transcript proves>",
         "needs_visual_check": "<what must be checked in the footage>",
         "uncertainty": "<doubts, unknown speakers, timing>"},
        {"id": "R-02", "title": "<short title>", "priority": "<high|medium|low>", "kind": "needs_filming",
         "what_happens": "<the idea to stage or film>", "why": "<why it would work>",
         "hook": "<opening line or caption>", "structure": "<shot plan>",
         "povs": [], "uncertainty": "<anything unclear>"},
    ]}


# --------------------------------------------------------------------------- helpers

def reviewer_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    return slug or "reviewer"


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _part_ranges(session, cues_by_part=None):
    """[(pov, [(part, start, end|None, estimated:bool)])]; unknown durations estimated from the SRT."""
    out = []
    for pov in session.povs:
        rows = []
        for pt in sorted(pov.parts, key=lambda p: (p.index, p.part_id)):
            start = pt.offset or 0.0
            est = False
            if pt.duration is not None and pt.duration_source != "srt_estimate":
                end = start + pt.duration
            else:
                cues = (cues_by_part or {}).get(pt.part_id) or []
                last = max((c.local_end for c in cues), default=pt.duration)
                end = None if last is None else start + last
                est = end is not None
            rows.append((pt, start, end, est))
        out.append((pov, rows))
    return out


def session_summary(session, cues_by_part=None) -> str:
    lines = [f"Session: {session.session_id or '(unnamed)'}"]
    if getattr(session, "game", ""):
        lines.append(f"Game / client: {session.game}")
    if getattr(session, "notes", ""):
        lines.append(f"Notes: {session.notes}")
    lines.append("Recordings on the session timeline (one POV = one player's camera):")
    unaligned = []
    estimated = False
    for pov, rows in _part_ranges(session, cues_by_part):
        segs = []
        for pt, s, e, est in rows:
            estimated = estimated or est
            end_txt = "?" if e is None else ("~" if est else "") + fmt_ts(e)
            segs.append(f"part {pt.index} {fmt_ts(s)}-{end_txt}")
            if pt.offset_source == "default" and not (pt is rows[0][0] and pov is session.povs[0]):
                unaligned.append(f"{pov.name} part {pt.index}")
        gaps = []
        for (a, _, ae, _), (b, bs, _, _) in zip(rows, rows[1:]):
            if ae is not None and bs - ae > 0.5:
                gaps.append(f"{fmt_ts(ae)}-{fmt_ts(bs)}")
        line = f"  - {pov.name}: " + "; ".join(segs)
        if gaps:
            line += "  (NO footage during gap " + ", ".join(gaps) + ")"
        lines.append(line)
    if estimated:
        lines.append("  (~ = end estimated from the transcript; video length unknown)")
    if unaligned:
        lines.append("  Alignment not yet verified for: " + ", ".join(unaligned)
                     + " (timestamps for these may be off).")
    return "\n".join(lines)


def game_context(session) -> str:
    return (getattr(session, "game_context", "") or "").strip() or DEFAULT_GAME_CONTEXT


def session_packet_lines(session, cues_by_part=None):
    """The AI-facing transcript lines for the session's current state (ids are re-derivable)."""
    from .reconcile import build_session_transcript, load_cues, packet_lines
    if cues_by_part is None:
        cues_by_part = load_cues(session)
    return packet_lines(build_session_transcript(session, cues_by_part))


def build_review_packet(session, reviewer_name, cues_by_part=None) -> str:
    """Prompt + format + coverage + compact transcript. Uses only the session's own recordings:
    never other reviewers' suggestions or earlier imports (independent reviews)."""
    from .reconcile import COMPACT_LEGEND, compact_transcript, load_cues
    if cues_by_part is None:
        cues_by_part = load_cues(session)
    plines = session_packet_lines(session, cues_by_part)
    reviewer = reviewer_name or "Reviewer"
    example = json.dumps(_EXAMPLE, indent=1).replace("<REVIEWER>", reviewer)
    return f"""You are {reviewer}, an independent editorial reviewer for a video game marketing team.
Below is the transcript of ONE multiplayer playtest session recorded from several player POVs.
Find the moments most worth turning into short-form social videos (TikTok / Shorts / Reels),
plus ideas worth filming or staging later.

GAME CONTEXT
{game_context(session)}

PRIORITIES (most important first)
1. Funny, self-contained short videos that make sense without the rest of the session.
2. Natural gameplay moments: emergent physics comedy, mistakes, traps, rescues, disasters.
3. Comedic setups and payoffs: setup -> escalation -> reversal/punchline.
4. Repeatable formats that could become a series; mechanics shown naturally by players.
5. Ideas that need staging or extra filming (kind "needs_filming").

RULES
- Work independently; you have not seen any other review.
- clip_now: start and end are required session times (HH:MM:SS) copied from the transcript, start
  before end, with a few seconds of context; typical clips are 10-60 s.
  needs_filming: omit start/end (or use null) unless the idea comes from a specific moment.
- You only have the transcript. Put what the transcript proves in confirmed_by_transcript and what
  you infer might be on screen in needs_visual_check. Never claim a visual payoff is verified.
- Do not invent speakers for "Team?" lines.
- POVs: players whose camera likely shows the moment (pov_notes says why). A player with no
  footage at that time (see COVERAGE) cannot be used.
- Quotes: copy text verbatim and include its line id.
- Priority: high = post-worthy as is; medium = good with editing; low = worth a look.
- Quality over quantity (roughly 5-20 suggestions).

OUTPUT
Return ONLY a JSON object (no prose before or after) shaped like this (<...> are placeholders):
{example}

JSON schema: {json.dumps(REVIEW_JSON_SCHEMA, separators=(",", ":"))}

COVERAGE
{session_summary(session, cues_by_part)}

TRANSCRIPT ({len(plines)} lines)
{COMPACT_LEGEND}

{compact_transcript(plines)}
"""


# --------------------------------------------------------------------------- parsing

_FENCE = re.compile(r"```[a-zA-Z0-9_-]*\s*\n?(.*?)```", re.S)
_LIST_KEYS = ("suggestions", "clips", "moments", "events", "ideas", "items", "results",
              "highlights", "needs_filming", "filming_ideas")


def _fixups(text):
    t = text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    t = re.sub(r",\s*([}\]])", r"\1", t)
    return t


def _decode_any(text):
    """Return the most plausible JSON value found in text, or raise ValueError."""
    candidates = []
    text = text.strip().lstrip("﻿")
    blobs = [text] + [m.group(1) for m in _FENCE.finditer(text)]
    dec = json.JSONDecoder()
    for blob in blobs:
        for variant in (blob, _fixups(blob)):
            try:
                return json.loads(variant)
            except ValueError:
                pass
            for m in re.finditer(r"[\[{]", variant):
                try:
                    val, end = dec.raw_decode(variant, m.start())
                except ValueError:
                    continue
                if _items_of(val):
                    candidates.append((end - m.start(), val))
    if candidates:
        candidates.sort(key=lambda c: -c[0])
        return candidates[0][1]
    raise ValueError("Could not find review JSON in the pasted text. Paste the reviewer's JSON answer.")


def _looks_like_suggestion(d):
    return isinstance(d, dict) and any(k in d for k in ("title", "start", "start_time", "time_range",
                                                       "range", "what_happens", "name"))


def _items_of(val):
    if isinstance(val, list):
        return [x for x in val if _looks_like_suggestion(x)]
    if isinstance(val, dict):
        out = []
        for k in _LIST_KEYS:
            v = val.get(k)
            if isinstance(v, list):
                for x in v:
                    if _looks_like_suggestion(x):
                        if k in ("needs_filming", "filming_ideas", "ideas") and "kind" not in x:
                            x = {**x, "kind": "needs_filming"}
                        out.append(x)
        if not out:
            for k in ("review", "data", "result"):
                if isinstance(val.get(k), (dict, list)):
                    out = _items_of(val[k])
                    if out:
                        break
        if not out and _looks_like_suggestion(val):
            out = [val]
        return out
    return []


def _time(v):
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lstrip("~≈ ").rstrip("sS ").strip()
    s = re.sub(r"^(at|about|around|approx\.?)\s+", "", s, flags=re.I)
    m = re.search(r"-?\d+(?::\d+){0,2}(?:[.,]\d+)?", s)
    if not m:
        return None
    try:
        return parse_ts(m.group(0))
    except ValueError:
        return None


def _range(v):
    if v is None:
        return None, None
    if isinstance(v, (list, tuple)) and len(v) >= 2:
        return _time(v[0]), _time(v[1])
    if isinstance(v, dict):
        return _time(v.get("start")), _time(v.get("end"))
    found = re.findall(r"\d+(?::\d+){0,2}(?:[.,]\d+)?", str(v))
    if len(found) >= 2:
        return _time(found[0]), _time(found[1])
    if found:
        return _time(found[0]), None
    return None, None


def _text(v):
    if v is None:
        return ""
    if isinstance(v, list):
        return "\n".join(_text(x) for x in v if x not in (None, ""))
    if isinstance(v, dict):
        return "; ".join(f"{k}: {_text(x)}" for k, x in v.items())
    return str(v).strip()


def _first(d, *keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _priority(v):
    s = str(v or "").strip().lower()
    if s in ("high", "h", "p1", "1", "top", "must", "must-have", "critical", "a") or s.startswith("high"):
        return "high"
    if s in ("low", "l", "p3", "3", "c", "nice-to-have") or s.startswith("low"):
        return "low"
    return "medium"


def _kind(v, d):
    s = str(v or "").lower()
    if any(w in s for w in ("film", "stage", "idea", "future", "shoot", "record")):
        return "needs_filming"
    if d.get("needs_filming") is True:
        return "needs_filming"
    return "clip_now"


def _quotes(v):
    out = []
    if v is None:
        return out
    if isinstance(v, (str, dict)):
        v = [v]
    for q in v:
        if isinstance(q, str):
            m = re.match(r"\s*\[?(\d+(?::\d+){1,2}(?:[.,]\d+)?)\]?\s*(.*)$", q)
            t, rest = (_time(m.group(1)), m.group(2)) if m else (None, q)
            spk = None
            m2 = re.match(r"([^:\"]{1,40}):\s*(.*)$", rest)
            if m2:
                spk, rest = m2.group(1).strip(), m2.group(2)
            out.append({"text": rest.strip().strip('"'), "speaker": spk, "time": t, "source": ""})
        elif isinstance(q, dict):
            line_id = _line_id(_first(q, "line", "line_id", "id", "ref"))
            text = _first(q, "text", "quote") or ("" if line_id else _first(q, "line"))
            item = {"text": _text(text),
                    "speaker": _first(q, "speaker", "who", "name"),
                    "time": _time(_first(q, "time", "timestamp", "at", "start")),
                    "source": _text(_first(q, "source", "src", "provenance"))}
            if line_id:
                item["line"] = line_id
            if isinstance(q.get("span_id"), str) and q["span_id"].strip():
                item["span_id"] = q["span_id"].strip()
            out.append(item)
    return out


_LINE_ID = re.compile(r"^\s*\[?\s*(L\d{1,6})\s*\]?\s*$", re.I)


def _line_id(v):
    m = _LINE_ID.match(str(v)) if isinstance(v, (str, int)) and v != "" else None
    return m.group(1).upper() if m else None


def _povs(v):
    if v is None:
        return []
    if isinstance(v, str):
        v = re.split(r"[,;/]| and ", v)
    out = []
    for x in v:
        if isinstance(x, dict):
            x = _first(x, "name", "pov", "player") or ""
        x = str(x).strip()
        if x and x not in out:
            out.append(x)
    return out


_START_KEYS = ("start", "start_time", "from", "begin", "session_start")
_END_KEYS = ("end", "end_time", "to", "until", "stop", "session_end")
_RANGE_KEYS = ("time_range", "range", "time", "timestamp", "window", "timestamps")


def _one(d, reviewer, reviewer_name, n, now, warnings):
    """One raw suggestion -> Suggestion. Untimed (start == end == 0.0, what the guide/export treat
    as an untimed idea) when no valid range is given; a clip_now without one also adds a warning."""
    raw_start, raw_end = _first(d, *_START_KEYS), _first(d, *_END_KEYS)
    start, end = _time(raw_start), _time(raw_end)
    if start is None or end is None:
        rs, re_ = _range(_first(d, *_RANGE_KEYS))
        start = start if start is not None else rs
        end = end if end is not None else re_
    title = _text(_first(d, "title", "name", "headline")) or f"Untitled {n}"
    kind = _kind(_first(d, "kind", "type", "category"), d)
    given = any(_first(d, k) is not None for k in _START_KEYS + _END_KEYS + _RANGE_KEYS)
    notes = []
    if start is None or end is None or not end > start:
        if given:
            got = f"start={_text(raw_start) or '?'}, end={_text(raw_end) or '?'}"
            notes.append(f"no valid session time range from the reviewer ({got})")
        else:
            notes.append("no session time given by the reviewer")
        if kind == "clip_now" or given:
            warnings.append(f"\"{title}\" ({reviewer}-{n:02d}): {kind} without a valid start/end "
                            f"({'got ' + got if given else 'none given'}); imported as untimed - "
                            "set the range by hand or ask the reviewer.")
        start = end = 0.0
    unc = _text(_first(d, "uncertainty", "uncertain", "caveats", "risks"))
    if notes:
        unc = "; ".join([unc] + notes if unc else notes)
    return Suggestion(
        suggestion_id=f"{reviewer}-{n:02d}",
        reviewer=reviewer,
        reviewer_name=reviewer_name,
        event_id="",
        title=title,
        start=round(float(start), 3),
        end=round(float(end), 3),
        priority=_priority(_first(d, "priority", "rating", "tier")),
        kind=kind,
        what_happens=_text(_first(d, "what_happens", "summary", "description", "context")),
        why=_text(_first(d, "why", "reason", "why_it_matters", "rationale")),
        hook=_text(_first(d, "hook", "caption")),
        structure=_text(_first(d, "structure", "edit", "edit_structure", "editing")),
        quotes=_quotes(_first(d, "quotes", "evidence", "transcript_evidence", "quote")),
        povs=_povs(_first(d, "povs", "pov", "cameras", "players")),
        pov_notes=_text(_first(d, "pov_notes", "pov_note", "best_pov")),
        confirmed_by_transcript=_text(_first(d, "confirmed_by_transcript", "confirmed", "transcript_confirmed")),
        needs_visual_check=_text(_first(d, "needs_visual_check", "visual_check", "verify", "to_verify")),
        uncertainty=unc,
        imported_at=now,
        raw=d,
    )


class ImportResult(list):
    """A list of Suggestions with .warnings (human-readable strings) from parsing/import."""

    def __init__(self, items=(), warnings=None):
        super().__init__(items)
        self.warnings = list(warnings or [])


def parse_review(text_or_obj, reviewer_name) -> list:
    """Tolerant parse of a pasted review -> ImportResult([Suggestion]) with provisional IDs
    "<slug>-NN" and .warnings for suggestions that could not be fully understood."""
    val = text_or_obj
    if isinstance(val, (bytes, bytearray)):
        val = val.decode("utf-8-sig", errors="replace")
    if isinstance(val, str):
        val = _decode_any(val)
    if not reviewer_name and isinstance(val, dict) and isinstance(val.get("reviewer"), str):
        reviewer_name = val["reviewer"]
    reviewer_name = (reviewer_name or "Reviewer").strip()
    items = _items_of(val)
    if not items:
        raise ValueError("The review JSON contains no suggestions.")
    slug = reviewer_slug(reviewer_name)
    now = _now()
    warnings = []
    out = [_one(d, slug, reviewer_name, n, now, warnings) for n, d in enumerate(items, 1)]
    return ImportResult(out, warnings)


# --------------------------------------------------------------------------- import + events

def _next_suggestion_id(slug, used):
    nums = [int(m.group(1)) for sid in used for m in [re.match(rf"^{re.escape(slug)}-(\d+)$", sid)] if m]
    n = max(nums, default=0) + 1
    while f"{slug}-{n:02d}" in used:
        n += 1
    return f"{slug}-{n:02d}"


def import_review(session, text_or_obj, reviewer_name) -> list:
    """Parse + unique IDs + quote line resolution + events. Returns ImportResult (.warnings)."""
    new = parse_review(text_or_obj, reviewer_name)
    used = {s.suggestion_id for s in session.suggestions}
    if any(s.suggestion_id in used for s in new):
        # re-import / collision: number after this reviewer's existing suggestions
        old_ids = [s.suggestion_id for s in new]
        for s in new:
            s.suggestion_id = _next_suggestion_id(s.reviewer, used)
            used.add(s.suggestion_id)
        remap = dict(zip(old_ids, (s.suggestion_id for s in new)))
        new.warnings = [_renumber(w, remap) for w in new.warnings]
    resolve_quote_lines(session, new, new.warnings)
    session.suggestions.extend(new)
    assign_events(session, new)
    return new


def _renumber(text, remap):
    return re.sub(r"\(([a-z0-9]+-\d+)\)", lambda m: f"({remap.get(m.group(1), m.group(1))})", text)


def _source_detail(src):
    from .reconcile import source_ref
    return {"part_id": src.get("part_id"), "pov_name": src.get("pov_name"),
            "srt_time": source_ref(src).split(" ")[1], "local_start": src.get("local_start"),
            "channel": src.get("channel"), "text": src.get("text"), "ref": source_ref(src)}


def _quote_fits(text, pline):
    from .reconcile import contain, similarity
    if not text:
        return True
    return contain(text, pline.text) >= 0.6 or similarity(text, pline.text) >= 0.6


def _best_text_match(text, t, sug, plines):
    """Best line for a quote: text similarity first, then named speaker, then time proximity
    (quote time, else the suggestion's window)."""
    from .reconcile import contain, similarity
    if t is not None:
        lo, hi = t - 20, t + 20
    elif sug.end > sug.start:
        lo, hi = sug.start - 30, sug.end + 30
    else:
        lo, hi = float("-inf"), float("inf")
    best, best_key = None, None
    for p in plines:
        if not (lo <= p.session_start <= hi) or not _quote_fits(text, p):
            continue
        centre = t if t is not None else (lo + hi) / 2 if hi != float("inf") else p.session_start
        key = (round(max(similarity(text, p.text), contain(text, p.text)), 2), p.speaker is not None,
               -abs(p.session_start - centre))
        if best_key is None or key > best_key:
            best, best_key = p, key
    return best


def resolve_quote_lines(session, suggestions, warnings=None, plines=None) -> int:
    """Resolve quotes that cite transcript line ids ("L0042") to detailed sources (POV, part, SRT
    time, channel). If the id no longer matches the quoted text (e.g. offsets changed after the
    packet was made) the best text match near the quote time is used instead. Quotes without a
    line id are left exactly as imported. Returns the number of resolved quotes."""
    cited = [(s, q) for s in suggestions for q in s.quotes if q.get("line")]
    if not cited:
        return 0
    if plines is None:
        try:
            plines = session_packet_lines(session)
        except Exception:
            plines = []
    by_id = {p.packet_id: p for p in plines}
    resolved = 0
    for sug, q in cited:
        pl = by_id.get(q["line"])
        if pl is not None and not _quote_fits(q.get("text"), pl):
            pl = None
        if pl is None and q.get("text"):
            pl = _best_text_match(q["text"], q.get("time"), sug, plines)
        if pl is None:
            q["line_unresolved"] = True
            if warnings is not None:
                warnings.append(f"\"{sug.title}\" ({sug.suggestion_id}): quote line {q['line']} "
                                "not found in the current transcript; kept as quoted.")
            continue
        if pl.packet_id != q["line"]:
            q["line_cited"] = q["line"]
        q["line"] = pl.packet_id
        q["sources"] = [_source_detail(src) for src in pl.sources]
        if not q.get("source") and q["sources"]:
            q["source"] = q["sources"][0]["ref"]
        if q.get("time") is None:
            q["time"] = pl.session_start
        if not q.get("speaker"):
            q["speaker"] = pl.label
        if not q.get("text"):
            q["text"] = pl.text
        resolved += 1
    return resolved


def remove_reviewer(session, reviewer) -> int:
    """Delete every suggestion (and its review state) of one reviewer slug; drop emptied events."""
    slug = reviewer_slug(reviewer)
    gone = {s.suggestion_id for s in session.suggestions if s.reviewer == slug}
    session.suggestions = [s for s in session.suggestions if s.suggestion_id not in gone]
    for sid in gone:
        session.reviews.pop(sid, None)
    for e in session.events:
        e.suggestion_ids = [x for x in e.suggestion_ids if x not in gone]
    session.events = [e for e in session.events if e.suggestion_ids]
    return len(gone)


def _overlap_ratio(a, b):
    la, lb = a.end - a.start, b.end - b.start
    shorter = min(la, lb)
    if shorter <= 0:
        return 0.0
    inter = min(a.end, b.end) - max(a.start, b.start)
    return max(0.0, inter) / shorter


def _prefix(session):
    p = (getattr(session, "event_prefix", "") or "").strip()
    return p or DEFAULT_PREFIX


def _next_event_id(session):
    prefix = _prefix(session)
    pat = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    ids = [e.event_id for e in session.events] + [s.event_id for s in session.suggestions if s.event_id]
    nums = [int(m.group(1)) for i in ids for m in [pat.match(i or "")] if m]
    return f"{prefix}-{max(nums, default=0) + 1:03d}"


def assign_events(session, new_suggestions) -> None:
    """Give each new suggestion an event: join an existing event when it overlaps (>= 50% of the
    shorter range) a suggestion from a DIFFERENT reviewer, else open a new event. An event gets at
    most one suggestion per reviewer automatically (two overlapping ideas from one reviewer stay
    separate). Existing events are never renumbered."""
    by_id = {s.suggestion_id: s for s in session.suggestions}
    events = {e.event_id: e for e in session.events}
    new_ids = {s.suggestion_id for s in new_suggestions}
    pending = [s for s in new_suggestions if not s.event_id or s.event_id not in events]

    def reviewers_of(ev):
        return {by_id[x].reviewer for x in ev.suggestion_ids if x in by_id}

    # 1. greedy best-overlap matching against existing events (from earlier imports)
    pairs = []
    for s in pending:
        for ev in events.values():
            best = 0.0
            for x in ev.suggestion_ids:
                o = by_id.get(x)
                if o is None or o.suggestion_id in new_ids or o.reviewer == s.reviewer:
                    continue
                best = max(best, _overlap_ratio(s, o))
            if best >= EVENT_OVERLAP:
                pairs.append((best, s.start, s.suggestion_id, ev.event_id))
    pairs.sort(key=lambda p: (-p[0], p[1], p[2], p[3]))
    placed = set()
    for ratio, _, sid, eid in pairs:
        s = by_id[sid]
        ev = events[eid]
        if sid in placed or s.reviewer in reviewers_of(ev):
            continue
        ev.suggestion_ids.append(sid)
        s.event_id = eid
        placed.add(sid)

    # 2. new events (numbered in session-time order), still merging different-reviewer
    #    suggestions within this batch (e.g. a combined import)
    for s in sorted(pending, key=lambda x: (not x.end > x.start, x.start, x.end, x.suggestion_id)):
        if s.suggestion_id in placed:
            continue
        target = None
        best = 0.0
        for ev in session.events:
            if ev.event_id in events and not all(x in new_ids for x in ev.suggestion_ids):
                continue          # existing events were handled in step 1
            if s.reviewer in reviewers_of(ev):
                continue
            r = max((_overlap_ratio(s, by_id[x]) for x in ev.suggestion_ids if x in by_id), default=0.0)
            if r >= EVENT_OVERLAP and r > best:
                target, best = ev, r
        if target is None:
            target = Event(event_id=_next_event_id(session), title=s.title, suggestion_ids=[], notes="")
            session.events.append(target)
        target.suggestion_ids.append(s.suggestion_id)
        s.event_id = target.event_id
        placed.add(s.suggestion_id)


def reassign_event(session, suggestion_id, event_id=None) -> str:
    """Move a suggestion to an existing event, a new event with the given ID, or (None) a new
    auto-numbered event. Emptied events are removed. Returns the event ID."""
    sug = next((s for s in session.suggestions if s.suggestion_id == suggestion_id), None)
    if sug is None:
        raise KeyError(suggestion_id)
    event_id = (event_id or "").strip() or None
    if event_id and event_id == sug.event_id:
        return event_id
    for ev in session.events:
        if suggestion_id in ev.suggestion_ids:
            ev.suggestion_ids.remove(suggestion_id)
    if event_id is None:
        event_id = _next_event_id(session)
    session.events = [e for e in session.events if e.suggestion_ids]
    ev = next((e for e in session.events if e.event_id == event_id), None)
    if ev is None:
        ev = Event(event_id=event_id, title=sug.title, suggestion_ids=[], notes="")
        session.events.append(ev)
    ev.suggestion_ids.append(suggestion_id)
    sug.event_id = event_id
    return event_id
