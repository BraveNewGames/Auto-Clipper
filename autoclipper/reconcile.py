"""Speaker-aware session transcript reconciliation (spec section 3).

Each part's SRT contains two kinds of evidence:
  * named cues  ("Cindy: ...")  - the recording owner's own, clean microphone;
  * Team cues   ("Team: ...")   - everybody else, heard through Discord.

We map every cue onto the session clock (part.offset + local time) and build ONE
conversation:

  1. Named cues are anchors.  The same speaker's named cue captured twice (two
     overlapping parts of the same POV) is merged into one anchor.
  2. Team cues from *other* POVs that fuzzily match an anchor at a plausible
     aligned time are attached to it as extra sources (one rendition per part,
     best-score-first greedy assignment).  The named text and speaker win -
     quality over majority vote.  A Team cue is never matched to the owner of the
     file it came from (the owner's own voice is not in their Team channel).
  3. Remaining Team/unlabeled cues are clustered among themselves (one rendition
     per part) and kept as unattributed lines ("Team (unattributed)") with the most
     central rendition as preferred text and explicit uncertainty.
  4. Everything unmatched is kept.  Nothing is attributed by file ownership.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path
from statistics import median

from .models import fmt_ts

# Matching parameters (seconds / similarity 0..1).
TIME_WINDOW = 2.5           # max |dt| between renditions of one utterance (after offsets)
MATCH_THRESHOLD = 0.6       # min similarity for a normal-length line
SHORT_THRESHOLD = 0.85      # min similarity when either side has <= SHORT_TOKENS tokens
SHORT_TOKENS = 2
SHORT_WINDOW = 1.2          # short lines ("yeah", "oh no") need tighter timing
SAME_SPEAKER_DUP = 0.8      # merge a speaker's duplicate named cue from overlapping parts
AGREE_THRESHOLD = 0.75      # Team renditions with mean similarity below this "disagree"

UNATTRIBUTED = "Team (unattributed)"


@dataclass
class SessionLine:
    line_id: str
    session_start: float
    session_end: float
    speaker: str | None
    channel: str            # "named" | "team" | "unlabeled"
    text: str
    confidence: str         # "high" | "medium" | "low"
    uncertain: bool
    note: str
    sources: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    @property
    def display_speaker(self):
        if self.speaker:
            return self.speaker
        return UNATTRIBUTED if self.channel == "team" else "Unlabeled"


# --------------------------------------------------------------------------- text

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = t.replace("'", "").replace("’", "")
    t = _PUNCT.sub(" ", t)
    return " ".join(t.split())


def tokens(text: str) -> list:
    return normalize(text).split()


def similarity(a, b) -> float:
    """Fuzzy similarity of two texts (or pre-tokenized lists), 0..1.

    Max of token-sequence ratio (robust to dropped/inserted words) and
    character ratio (robust to small spelling/inflection differences)."""
    ta = a if isinstance(a, list) else tokens(a)
    tb = b if isinstance(b, list) else tokens(b)
    if not ta or not tb:
        return 0.0
    if ta == tb:
        return 1.0
    tok = difflib.SequenceMatcher(None, ta, tb, autojunk=False).ratio()
    ch = difflib.SequenceMatcher(None, " ".join(ta), " ".join(tb), autojunk=False).ratio()
    return max(tok, ch * 0.97)


# --------------------------------------------------------------------------- loading

def _resolve(session, p):
    if not p:
        return None
    path = Path(p)
    if not path.is_absolute() and getattr(session, "folder", None):
        path = Path(session.folder) / path
    return path


def load_cues(session) -> dict:
    """Parse every part's SRT -> {part_id: [Cue]}.  Missing/unreadable SRTs -> []."""
    from .srt import parse_srt
    out = {}
    for pov, part in session.all_parts():
        path = _resolve(session, part.srt_path)
        cues = []
        if path is not None and path.is_file():
            try:
                cues = parse_srt(path, part.part_id, pov.pov_id)
            except Exception:  # unreadable SRT: keep going, the UI shows coverage anyway
                cues = []
        out[part.part_id] = cues
    return out


class _Item:
    """One cue placed on the session clock."""
    __slots__ = ("cue", "part", "pov", "start", "end", "toks", "key", "used")

    def __init__(self, cue, part, pov):
        self.cue = cue
        self.part = part
        self.pov = pov
        off = part.offset or 0.0
        self.start = off + cue.local_start
        self.end = off + cue.local_end
        self.toks = tokens(cue.text)
        self.key = f"{part.part_id}:{cue.index}"
        self.used = False

    @property
    def channel(self):
        return self.cue.channel


def _items(session, cues_by_part):
    if cues_by_part is None:
        cues_by_part = load_cues(session)
    items = []
    for pov, part in session.all_parts():
        for cue in cues_by_part.get(part.part_id, []) or []:
            if not (cue.text or "").strip():
                continue
            items.append(_Item(cue, part, pov))
    items.sort(key=lambda it: (it.start, it.key))
    return items


def _dt(a: _Item, b: _Item) -> float:
    return min(abs(a.start - b.start), abs(a.end - b.end))


def _match_score(a: _Item, b: _Item):
    """Return score (higher better) if a and b plausibly are the same utterance, else None."""
    dt = _dt(a, b)
    if dt > TIME_WINDOW:
        return None
    short = min(len(a.toks), len(b.toks)) <= SHORT_TOKENS
    if short and dt > SHORT_WINDOW:
        return None
    sim = similarity(a.toks, b.toks)
    if sim < (SHORT_THRESHOLD if short else MATCH_THRESHOLD):
        return None
    return sim - 0.04 * dt, sim


def _source(it: _Item, sim) -> dict:
    c = it.cue
    return {
        "part_id": it.part.part_id,
        "pov_name": it.pov.name,
        "cue_index": c.index,
        "local_start": round(c.local_start, 3),
        "local_end": round(c.local_end, 3),
        "channel": c.channel,
        "label": c.label,
        "text": c.text,
        "similarity": None if sim is None else round(sim, 3),
    }


def _windowed_pairs(left, right, accept):
    """All scored (score, sim, l, r) pairs between two start-sorted lists within the window."""
    pairs = []
    j0 = 0
    for l in left:
        while j0 < len(right) and right[j0].start < l.start - TIME_WINDOW - 30:
            j0 += 1
        j = j0
        while j < len(right) and right[j].start <= l.end + TIME_WINDOW:
            r = right[j]
            j += 1
            if r is l or not accept(l, r):
                continue
            m = _match_score(l, r)
            if m:
                pairs.append((m[0], m[1], l, r))
    pairs.sort(key=lambda p: (-p[0], p[2].key, p[3].key))
    return pairs


# --------------------------------------------------------------------------- main

def build_session_transcript(session, cues_by_part=None) -> list:
    items = _items(session, cues_by_part)
    named = [it for it in items if it.channel == "named"]
    other = [it for it in items if it.channel != "named"]

    # 1. anchors: named cues; merge the same speaker's duplicate from an overlapping part.
    clusters = []          # list of dict(anchor=_Item, extra_named=[], team={part_id: (item, sim)})
    by_key = {}
    for score, sim, a, b in _windowed_pairs(
            named, named,
            lambda a, b: a.key < b.key and a.pov.pov_id == b.pov.pov_id
            and a.part.part_id != b.part.part_id
            and (a.cue.speaker or "").lower() == (b.cue.speaker or "").lower()):
        if sim < SAME_SPEAKER_DUP or a.key in by_key and b.key in by_key:
            continue
        ca, cb = by_key.get(a.key), by_key.get(b.key)
        if ca is None and cb is None:
            c = {"anchor": a, "extra_named": [(b, sim)], "team": {}}
            clusters.append(c)
            by_key[a.key] = by_key[b.key] = c
        else:
            c = ca or cb
            new = b if ca else a
            if any(x.part.part_id == new.part.part_id for x, _ in c["extra_named"]) \
                    or c["anchor"].part.part_id == new.part.part_id:
                continue
            c["extra_named"].append((new, sim))
            by_key[new.key] = c
    for it in named:
        if it.key not in by_key:
            c = {"anchor": it, "extra_named": [], "team": {}}
            clusters.append(c)
            by_key[it.key] = c
    for c in clusters:
        for it in [c["anchor"]] + [x for x, _ in c["extra_named"]]:
            it.used = True

    # 2. attach Team/unlabeled renditions from OTHER POVs to named anchors (best score first).
    anchors = sorted({id(c): c for c in clusters}.values(), key=lambda c: c["anchor"].start)
    anchor_items = [c["anchor"] for c in anchors]
    anchor_of = {c["anchor"].key: c for c in anchors}
    for score, sim, a, t in _windowed_pairs(
            anchor_items, other,
            lambda a, t: t.pov.pov_id != a.pov.pov_id):
        if t.used:
            continue
        c = anchor_of[a.key]
        if t.part.part_id in c["team"]:
            continue
        c["team"][t.part.part_id] = (t, sim)
        t.used = True

    lines = []
    for c in anchors:
        lines.append(_named_line(c))

    # 3. cluster remaining Team/unlabeled cues among themselves.
    rest = [it for it in other if not it.used]
    groups = {it.key: [it] for it in rest}
    gid = {it.key: it.key for it in rest}
    for score, sim, a, b in _windowed_pairs(
            rest, rest,
            lambda a, b: a.key < b.key and a.part.part_id != b.part.part_id
            and a.channel == b.channel):
        ga, gb = gid[a.key], gid[b.key]
        if ga == gb:
            continue
        A, B = groups[ga], groups[gb]
        parts_a = {x.part.part_id for x in A}
        if any(x.part.part_id in parts_a for x in B):
            continue
        # complete-ish linkage: every member must be within the window of the merged centre
        merged = A + B
        centre = median(x.start for x in merged)
        if any(abs(x.start - centre) > TIME_WINDOW for x in merged):
            continue
        A.extend(B)
        for x in B:
            gid[x.key] = ga
        del groups[gb]
    for g in groups.values():
        lines.append(_team_line(g))

    lines.sort(key=lambda l: (l.session_start, l.line_id))
    _apply_overrides(session, lines)
    return lines


def _named_line(c) -> SessionLine:
    a = c["anchor"]
    srcs = [_source(a, None)]
    for it, sim in c["extra_named"]:
        srcs.append(_source(it, sim))
    team = sorted(c["team"].values(), key=lambda p: p[0].start)
    for it, sim in team:
        srcs.append(_source(it, sim))
    notes = []
    if c["extra_named"]:
        notes.append("also captured by an overlapping part of the same recording")
    if team:
        notes.append(f"heard as Team in {len(team)} other recording(s)")
        low = [s for _, s in team if s < AGREE_THRESHOLD]
        if low:
            notes.append(f"{len(low)} Team rendition(s) differ noticeably from the named mic")
    else:
        notes.append("own mic only (no Team corroboration)")
    return SessionLine(
        line_id=a.key,
        session_start=round(a.start, 3),
        session_end=round(a.end, 3),
        speaker=a.cue.speaker or a.cue.label,
        channel="named",
        text=a.cue.text,
        confidence="high",
        uncertain=False,
        note="; ".join(notes),
        sources=srcs,
    )


def _team_line(group) -> SessionLine:
    group = sorted(group, key=lambda x: (x.start, x.key))
    n = len(group)
    if n == 1:
        best, mean_sim = group[0], 1.0
        sims = {best.key: None}
    else:
        def mean_to_others(x):
            return sum(similarity(x.toks, y.toks) for y in group if y is not x) / (n - 1)
        scored = [(mean_to_others(x), len(x.toks), x) for x in group]
        scored.sort(key=lambda s: (-s[0], -s[1], s[2].key))
        best = scored[0][2]
        mean_sim = sum(s[0] for s in scored) / n
        sims = {x.key: similarity(best.toks, x.toks) for x in group}
    channel = best.channel
    disagree = n > 1 and mean_sim < AGREE_THRESHOLD
    if n == 1:
        confidence = "low"
    else:
        confidence = "medium" if not disagree else "low"
    povs = sorted({x.pov.name for x in group})
    notes = []
    if channel == "team":
        notes.append("speaker unknown: Team channel, no matching named-mic line")
        # Owners of these files can't be the speaker (their own voice isn't in their Team channel).
        notes.append("not spoken by: " + ", ".join(povs))
    else:
        notes.append("unlabeled line; speaker unknown")
    if n == 1:
        notes.append("single source")
    else:
        notes.append(f"{n} renditions" + ("; renditions disagree" if disagree else ""))
    srcs = [_source(best, None if n == 1 else 1.0)]
    srcs += [_source(x, sims[x.key]) for x in group if x is not best]
    starts = [x.start for x in group]
    ends = [x.end for x in group]
    return SessionLine(
        line_id=best.key,
        session_start=round(median(starts), 3),
        session_end=round(max(median(ends), median(starts)), 3),
        speaker=None,
        channel=channel,
        text=best.cue.text,
        confidence=confidence,
        uncertain=disagree,
        note="; ".join(notes),
        sources=srcs,
    )


def _apply_overrides(session, lines):
    """session.transcript_overrides = {line_id: {"speaker"?, "text"?, "note"?}} (human corrections)."""
    ov = getattr(session, "transcript_overrides", None) or {}
    if not isinstance(ov, dict):
        return
    for line in lines:
        o = ov.get(line.line_id)
        if not isinstance(o, dict):
            continue
        if "speaker" in o:
            line.speaker = o["speaker"] or None
        if o.get("text"):
            line.text = o["text"]
        if o.get("note"):
            line.note = (line.note + "; " if line.note else "") + "editor: " + o["note"]
        line.confidence = "high"
        line.uncertain = False


# --------------------------------------------------------------------------- display

def _srt_ts(sec):
    return fmt_ts(sec).replace(".", ",")


def source_ref(src: dict) -> str:
    """'cindy-p2 00:03:17,200 Named' - traceable provenance of one source."""
    ch = {"named": "Named", "team": "Team"}.get(src.get("channel"), "Unlabeled")
    return f"{src.get('part_id')} {_srt_ts(src.get('local_start') or 0.0)} {ch}"


def transcript_text(lines, include_sources=False) -> str:
    out = []
    for l in lines:
        s = f"[{fmt_ts(l.session_start)}] {l.display_speaker}: {l.text}"
        if include_sources:
            tags = []
            if l.speaker is None:
                tags.append(f"{l.confidence} confidence")
            if l.uncertain:
                tags.append("uncertain")
            tags.append("src: " + "; ".join(source_ref(s_) for s_ in l.sources))
            alts = [s_["text"] for s_ in l.sources[1:]
                    if s_.get("text") and normalize(s_["text"]) != normalize(l.text)]
            if l.uncertain and alts:
                tags.append("alt: " + " | ".join(f'"{a}"' for a in alts[:3]))
            s += "  {" + ", ".join(tags) + "}"
        out.append(s)
    return "\n".join(out)
