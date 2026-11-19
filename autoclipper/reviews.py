"""AI editorial review packets, tolerant review import, and event grouping.

Workflow: build_review_packet() -> user pastes it into ChatGPT / Claude -> pastes the
answer back -> import_review() parses it, gives every suggestion a unique ID and groups
suggestions from different reviewers that cover the same moment under one event ID.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from .models import Event, Suggestion, fmt_ts, parse_ts

DEFAULT_PREFIX = "EV"
EVENT_OVERLAP = 0.5          # overlap / shorter range needed to share an event
DEFAULT_LENGTH = 15.0        # assumed clip length when a reviewer gives only a start time

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
                "required": ["id", "title", "start", "end", "priority", "kind", "what_happens", "why"],
                "properties": {
                    "id": {"type": "string", "description": "Your own short ID, e.g. C-01"},
                    "title": {"type": "string", "description": "2-5 word memorable title"},
                    "start": {"type": "string", "description": "Session time HH:MM:SS(.mmm) where the clip should start"},
                    "end": {"type": "string", "description": "Session time HH:MM:SS(.mmm) where the clip should end"},
                    "priority": {"enum": ["high", "medium", "low"]},
                    "kind": {"enum": ["clip_now", "needs_filming"]},
                    "what_happens": {"type": "string"},
                    "why": {"type": "string", "description": "Why it is marketable / funny / interesting"},
                    "hook": {"type": "string", "description": "First 1-2 seconds / caption hook"},
                    "structure": {"type": "string", "description": "Setup -> escalation -> punchline, edit approach"},
                    "quotes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["time", "text"],
                            "properties": {
                                "time": {"type": "string"}, "speaker": {"type": "string"},
                                "text": {"type": "string"}, "source": {"type": "string"}}}},
                    "povs": {"type": "array", "items": {"type": "string"}},
                    "pov_notes": {"type": "string"},
                    "confirmed_by_transcript": {"type": "string"},
                    "needs_visual_check": {"type": "string"},
                    "uncertainty": {"type": "string"},
                }}}},
}

_EXAMPLE = {
    "reviewer": "<REVIEWER>",
    "suggestions": [{
        "id": "X-01", "title": "Zombie Slide", "start": "00:06:35", "end": "00:06:50",
        "priority": "high", "kind": "clip_now",
        "what_happens": "Cindy proposes throwing a zombie down the slide; the team tries it.",
        "why": "Absurd physics setup with an immediate payoff; easy to understand without context.",
        "hook": "\"Throw the zombie down the slide!\"",
        "structure": "Setup (the idea) -> attempt -> reaction. Cut on the laugh.",
        "quotes": [{"time": "00:06:37.200", "speaker": "Cindy", "text": "Throw the zombie down the slide!",
                    "source": "cindy-p2 00:03:17,200 Named"}],
        "povs": ["Cindy", "Andres"],
        "pov_notes": "Cindy says it; Andres's POV may show the slide (unverified).",
        "confirmed_by_transcript": "The line and the laughter that follows.",
        "needs_visual_check": "Whether the zombie actually goes down the slide on camera.",
        "uncertainty": "Reaction speaker is Team (unattributed)."}]}


# --------------------------------------------------------------------------- helpers

def reviewer_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    return slug or "reviewer"


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _part_ranges(session):
    out = []
    for pov in session.povs:
        rows = []
        for pt in sorted(pov.parts, key=lambda p: (p.index, p.part_id)):
            start = pt.offset or 0.0
            end = None if pt.duration is None else start + pt.duration
            rows.append((pt, start, end))
        out.append((pov, rows))
    return out


def session_summary(session) -> str:
    lines = [f"Session: {session.session_id or '(unnamed)'}"]
    if getattr(session, "game", ""):
        lines.append(f"Game: {session.game}")
    if getattr(session, "notes", ""):
        lines.append(f"Notes: {session.notes}")
    lines.append("Recordings on the session timeline (one POV = one player's camera):")
    unaligned = []
    for pov, rows in _part_ranges(session):
        segs = []
        for pt, s, e in rows:
            segs.append(f"part {pt.index} {fmt_ts(s)}-{fmt_ts(e) if e is not None else '?'}")
            if pt.offset_source == "default" and not (pt is rows[0][0] and pov is session.povs[0]):
                unaligned.append(f"{pov.name} part {pt.index}")
        gaps = []
        for (a, _, ae), (b, bs, _) in zip(rows, rows[1:]):
            if ae is not None and bs - ae > 0.5:
                gaps.append(f"{fmt_ts(ae)}-{fmt_ts(bs)}")
        line = f"  - {pov.name}: " + "; ".join(segs)
        if gaps:
            line += "  (NO footage during gap " + ", ".join(gaps) + ")"
        lines.append(line)
    if unaligned:
        lines.append("  Alignment not yet verified for: " + ", ".join(unaligned)
                     + " (timestamps for these may be off).")
    return "\n".join(lines)


def build_review_packet(session, reviewer_name, cues_by_part=None) -> str:
    from .reconcile import build_session_transcript, transcript_text
    lines = build_session_transcript(session, cues_by_part)
    example = json.dumps(_EXAMPLE, indent=2).replace("<REVIEWER>", reviewer_name or "Reviewer")
    return f"""You are {reviewer_name or 'an'} independent editorial reviewer for a video game marketing team.
Below is the reconciled transcript of ONE multiplayer playtest session, recorded from several
player POVs. Find the moments most worth turning into short-form social videos (TikTok / Shorts /
Reels), and ideas worth filming or staging later.

LOOK FOR
- Funny dialogue and reactions that probably have a visual payoff on screen.
- Emergent physics comedy, mistakes, traps, rescues, disasters, unexpected interactions.
- Clear setup -> escalation -> reversal/punchline arcs.
- Game mechanics demonstrated naturally through what players do.
- Strong short-form hooks and repeatable formats (series potential).
- Ideas that need additional filming or staging (kind = "needs_filming").

RULES
- Work independently; do not assume anyone else's review exists.
- Use SESSION timestamps exactly as shown in the transcript (HH:MM:SS.mmm). Give each clip a
  start/end window with a few seconds of context; typical clips are 10-60 seconds.
- You only have the transcript. Separate what is CONFIRMED by the transcript
  (confirmed_by_transcript) from what you INFER might be visible (needs_visual_check).
  Never claim a visual payoff is verified.
- Speaker names come from each player's own clean microphone. "Team (unattributed)" lines were
  heard over Discord and the speaker is unknown - do not invent a speaker for them. Lines marked
  "uncertain" have disagreeing renditions (alternatives shown as alt:).
- POVs: list players whose camera likely shows the moment and explain in pov_notes. A POV with
  no footage at that time (see coverage below) cannot be used.
- Quote the transcript verbatim with its time, speaker and source tag.
- Priority: high = post-worthy as is; medium = good with editing; low = worth a look.
- Prefer quality over quantity (roughly 5-20 suggestions for a session this long).

OUTPUT
Return ONLY a JSON object (no prose before or after) in exactly this format:
{example}

JSON schema:
{json.dumps(REVIEW_JSON_SCHEMA, indent=1)}

COVERAGE
{session_summary(session)}

TRANSCRIPT FORMAT
[session time] Speaker: text  {{confidence / uncertainty, src: <part> <local SRT time> <Named|Team>; ...}}

TRANSCRIPT ({len(lines)} lines)
{transcript_text(lines, include_sources=True)}
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
            out.append({"text": _text(_first(q, "text", "quote", "line")),
                        "speaker": _first(q, "speaker", "who", "name"),
                        "time": _time(_first(q, "time", "timestamp", "at", "start")),
                        "source": _text(_first(q, "source", "src", "provenance"))})
    return out


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


def _one(d, reviewer, reviewer_name, n, now):
    start = _time(_first(d, "start", "start_time", "from", "begin", "session_start"))
    end = _time(_first(d, "end", "end_time", "to", "until", "stop", "session_end"))
    if start is None or end is None:
        rs, re_ = _range(_first(d, "time_range", "range", "time", "timestamp", "window", "timestamps"))
        start = start if start is not None else rs
        end = end if end is not None else re_
    notes = []
    if start is None and end is None:
        start = end = 0.0
        notes.append("no session time given by the reviewer")
    elif start is None:
        start = max(0.0, end - DEFAULT_LENGTH)
        notes.append(f"start time missing; assumed {DEFAULT_LENGTH:g} s before end")
    elif end is None:
        end = start + DEFAULT_LENGTH
        notes.append(f"end time missing; assumed {DEFAULT_LENGTH:g} s after start")
    if end < start:
        start, end = end, start
    unc = _text(_first(d, "uncertainty", "uncertain", "caveats", "risks"))
    if notes:
        unc = "; ".join([unc] + notes if unc else notes)
    return Suggestion(
        suggestion_id=f"{reviewer}-{n:02d}",
        reviewer=reviewer,
        reviewer_name=reviewer_name,
        event_id="",
        title=_text(_first(d, "title", "name", "headline")) or f"Untitled {n}",
        start=round(float(start), 3),
        end=round(float(end), 3),
        priority=_priority(_first(d, "priority", "rating", "tier")),
        kind=_kind(_first(d, "kind", "type", "category"), d),
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


def parse_review(text_or_obj, reviewer_name) -> list:
    """Tolerant parse of a pasted review -> [Suggestion] with provisional IDs "<slug>-NN"."""
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
    return [_one(d, slug, reviewer_name, n, now) for n, d in enumerate(items, 1)]


# --------------------------------------------------------------------------- import + events

def _next_suggestion_id(slug, used):
    nums = [int(m.group(1)) for sid in used for m in [re.match(rf"^{re.escape(slug)}-(\d+)$", sid)] if m]
    n = max(nums, default=0) + 1
    while f"{slug}-{n:02d}" in used:
        n += 1
    return f"{slug}-{n:02d}"


def import_review(session, text_or_obj, reviewer_name) -> list:
    new = parse_review(text_or_obj, reviewer_name)
    used = {s.suggestion_id for s in session.suggestions}
    if any(s.suggestion_id in used for s in new):
        # re-import / collision: number after this reviewer's existing suggestions
        for s in new:
            s.suggestion_id = _next_suggestion_id(s.reviewer, used)
            used.add(s.suggestion_id)
    session.suggestions.extend(new)
    assign_events(session, new)
    return new


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
    for s in sorted(pending, key=lambda x: (x.start, x.end, x.suggestion_id)):
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
