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

import bisect
import difflib
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path
from statistics import median

from .models import fmt_ts

# Matching parameters (seconds / similarity 0..1).
TIME_WINDOW = 4.0           # max |dt| between renditions of one utterance (tolerates alignment error)
MATCH_THRESHOLD = 0.6       # min similarity for a normal-length line
SHORT_THRESHOLD = 0.85      # min similarity when either side has <= SHORT_TOKENS tokens
SHORT_TOKENS = 2
SHORT_WINDOW = 1.2          # short lines ("yeah", "oh no") need tighter timing
SAME_SPEAKER_DUP = 0.8      # merge a speaker's duplicate named cue from overlapping parts
AGREE_THRESHOLD = 0.75      # Team renditions with mean similarity below this "disagree"
TEAM_WORD_AGREE = 0.5       # Team-vs-Team clustering: word-sequence agreement required
FRAGMENT_CONTAIN = 0.8      # share of a Team fragment's words found (in order) inside a named line
ECHO_WINDOW = 1.5           # owner's voice leaking into their own Team channel: same clock, tight timing
ECHO_SIM = 0.8
CHAR_CONTAIN = 0.9          # character-level fallback for the two containment checks above
COVER_CONTAIN = 0.8         # leftover Team line whose words are covered by nearby named lines

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


def _team_team_score(a: _Item, b: _Item):
    """Like _match_score, but two Team renditions must also agree word by word: the character
    ratio alone lets different sentences with a shared tail ("...in the room") fuse."""
    m = _match_score(a, b)
    if m is None:
        return None
    if min(len(a.toks), len(b.toks)) > SHORT_TOKENS:
        words = difflib.SequenceMatcher(None, a.toks, b.toks, autojunk=False).ratio()
        if words < TEAM_WORD_AGREE and max(contain(a.toks, b.toks), contain(b.toks, a.toks)) < FRAGMENT_CONTAIN:
            return None
    return m


def _gap(a: _Item, b: _Item) -> float:
    """Distance between the two time intervals (0 when they overlap)."""
    return max(0.0, max(a.start, b.start) - min(a.end, b.end))


def contain(a, b) -> float:
    """Share of a's tokens that appear, in order, inside b (0..1)."""
    ta = a if isinstance(a, list) else tokens(a)
    tb = b if isinstance(b, list) else tokens(b)
    if not ta or not tb:
        return 0.0
    m = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    return sum(bl.size for bl in m.get_matching_blocks()) / len(ta)


def contain_chars(a, b) -> float:
    """Like contain() but on characters with spaces removed ("alright" ~ "all right")."""
    ta = "".join(a if isinstance(a, list) else tokens(a))
    tb = "".join(b if isinstance(b, list) else tokens(b))
    if not ta or not tb:
        return 0.0
    m = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    return sum(bl.size for bl in m.get_matching_blocks() if bl.size >= 3) / len(ta)


def _contained(a_toks, b_toks, word_thr):
    """a's words inside b: word-level >= word_thr, or character-level >= CHAR_CONTAIN."""
    c = contain(a_toks, b_toks)
    if c >= word_thr:
        return c
    cc = contain_chars(a_toks, b_toks)
    return cc if cc >= CHAR_CONTAIN and c >= 0.5 else 0.0


def _fragment_ok(n_tokens, c):
    return n_tokens >= 3 and c >= (1.0 if n_tokens == 3 else FRAGMENT_CONTAIN)


def _team_vs_named(a: _Item, t: _Item):
    """Score Team/unlabeled cue t as a rendition of named anchor a -> (score, sim, kind) or None.

    kind: "rendition" (same utterance heard elsewhere), "fragment" (a phrase-split piece of it),
    "echo" (owner's own voice leaking into the Team channel of the SAME recording part)."""
    if t.pov.pov_id == a.pov.pov_id:
        if t.part.part_id != a.part.part_id:
            return None
        # Same file, same clock: only near-verbatim, near-simultaneous repeats count.
        dt = _dt(a, t)
        if min(len(a.toks), len(t.toks)) <= SHORT_TOKENS:
            if a.toks == t.toks and dt <= 0.8:
                return 1.0 - 0.04 * dt, 1.0, "echo"
            return None
        if dt > ECHO_WINDOW and _gap(a, t) > 0.3:
            return None
        sim = similarity(a.toks, t.toks)
        if sim >= ECHO_SIM or (len(t.toks) >= 4 and contain(t.toks, a.toks) >= 0.9):
            return max(sim, 0.85) - 0.04 * dt, sim, "echo"
        return None
    dt = _dt(a, t)
    short = min(len(a.toks), len(t.toks)) <= SHORT_TOKENS
    if short:
        if dt > SHORT_WINDOW:
            return None
        sim = similarity(a.toks, t.toks)
        return (sim - 0.04 * dt, sim, "rendition") if sim >= SHORT_THRESHOLD else None
    if dt <= TIME_WINDOW:
        sim = similarity(a.toks, t.toks)
        if sim >= MATCH_THRESHOLD:
            return sim - 0.04 * dt, sim, "rendition"
    gap = _gap(a, t)
    if gap <= TIME_WINDOW and len(t.toks) < len(a.toks):
        if len(t.toks) == 3:
            # 3-word pieces ("oh my god") are generic: verbatim and close in time only
            c = contain(t.toks, a.toks) if gap <= SHORT_WINDOW else 0.0
        else:
            c = _contained(t.toks, a.toks, FRAGMENT_CONTAIN)
        if _fragment_ok(len(t.toks), c):
            return 0.9 * c - 0.04 * gap - 0.05, similarity(a.toks, t.toks), "fragment"
    return None


def _source(it: _Item, sim, kind=None) -> dict:
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
        "match": kind,
    }


def _windowed_pairs(left, right, accept, scorer=None):
    """All scored (score, sim, l, r[, kind]) pairs between two start-sorted lists within the window."""
    scorer = scorer or _match_score
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
            m = scorer(l, r)
            if m:
                pairs.append((m[0], m[1], l, r) + tuple(m[2:]))
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
            c = {"anchor": a, "extra_named": [(b, sim)], "team": []}
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
            c = {"anchor": it, "extra_named": [], "team": []}
            clusters.append(c)
            by_key[it.key] = c
    for c in clusters:
        for it in [c["anchor"]] + [x for x, _ in c["extra_named"]]:
            it.used = True

    # 2. attach Team/unlabeled renditions to named anchors (best score first): renditions from other
    #    POVs, phrase-split fragments, and the owner's own voice echoed in their Team channel. This
    #    is content+time matching, never ownership: an unmatched Team line stays unattributed.
    anchors = sorted({id(c): c for c in clusters}.values(), key=lambda c: c["anchor"].start)
    anchor_items = [c["anchor"] for c in anchors]
    anchor_of = {c["anchor"].key: c for c in anchors}
    for score, sim, a, t, kind in _windowed_pairs(anchor_items, other, lambda a, t: True,
                                                  scorer=_team_vs_named):
        if t.used:
            continue
        c = anchor_of[a.key]
        # several pieces from one part are fine only if they are different stretches of audio
        if any(x.part.part_id == t.part.part_id
               and min(x.end, t.end) - max(x.start, t.start) > 0.5 for x, _, _ in c["team"]):
            continue
        c["team"].append((t, sim, kind))
        t.used = True

    # 3. cluster remaining Team/unlabeled cues among themselves.
    rest = [it for it in other if not it.used]
    groups = {it.key: [it] for it in rest}
    gid = {it.key: it.key for it in rest}
    for score, sim, a, b in _windowed_pairs(
            rest, rest,
            lambda a, b: a.key < b.key and a.part.part_id != b.part.part_id
            and a.channel == b.channel, scorer=_team_team_score):
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
    # 4. a leftover Team line whose words are (almost) all said in nearby named lines - e.g. one
    #    Team cue spanning two or three speakers' lines - is a duplicate: keep it as a source of the
    #    named line it overlaps most instead of a standalone line.
    #    Order-tolerant content-word coverage against ALL speakers' named speech nearby catches
    #    phrase splits/merges and fragments; interjection-only cues are matched within +-3 s.
    #    Where some player has no recording at that moment the rules get stricter.
    starts = [c["anchor"].start for c in anchors]
    spans = _coverage_spans(session, items)
    n_povs = len(spans)
    leftovers = []
    for g in sorted(groups.values(), key=lambda g: (min(x.start for x in g), g[0].key)):
        target = _covering_anchor(g, anchors, starts)
        kind = "covered"
        if target is None:
            full = _all_povs_recording(spans, n_povs, g)
            target = _window_cover_anchor(g, anchors, starts, strict=not full)
            kind = "window"
        if target is None:
            leftovers.append(g)
            continue
        for x in g:
            target["team"].append((x, similarity(target["anchor"].toks, x.toks), kind))

    # 5. collapse remaining Team lines that repeat another remaining Team line from a different
    #    recording a few seconds apart (one utterance, phrase boundaries differed).
    leftovers = _collapse_team_repeats(leftovers)

    extras = _source_extras(anchors, starts)
    lines = [_named_line(c, extras) for c in anchors]
    for g in leftovers:
        lines.append(_team_line(g))

    lines.sort(key=lambda l: (l.session_start, l.line_id))
    _apply_overrides(session, lines)
    return lines


FILLER = set("""a ah aha all alright an and are as at be but by can cause could did do does doing
dont for from get go going gonna got gotta guys guy dude had has have he hey her hi him his hm hmm huh
i id ill im in is it its ive just know let lets like literally lol me mhm my no nope not now of oh ok
okay on or our out right so some something such that thats the them then there theyre this to too
uh uhh um umm up us very wait want was we well were what whats when where who why will with wow
yay yeah yep yes yo you youre your if about because into over off than these those its theres
any been being am i'm how also kind sort thing things way""".split())

INTERJECTION_WINDOW = 3.0


def content_tokens(toks):
    return [t for t in toks if t not in FILLER and not t.isdigit()]


def _stem(t):
    for suf in ("ing", "ed", "es", "ly", "s"):
        if len(t) > len(suf) + 2 and t.endswith(suf):
            return t[:-len(suf)]
    return t


def _token_found(t, pool):
    if t in pool:
        return True
    st = _stem(t)
    if any(_stem(u) == st for u in pool):
        return True
    if len(t) >= 5:
        for u in pool:
            if len(u) >= 5 and u[:4] == t[:4] and abs(len(u) - len(t)) <= 3 \
                    and difflib.SequenceMatcher(None, t, u).ratio() >= 0.8:
                return True
    return False


def _coverage_spans(session, items):
    """{pov_id: [(start, end)]} session intervals where each POV has a recording."""
    spans = {}
    ends = {}
    for it in items:
        ends[it.part.part_id] = max(ends.get(it.part.part_id, 0.0), it.cue.local_end)
    for pov, part in session.all_parts():
        dur = part.duration if part.duration else ends.get(part.part_id)
        if dur is None:
            continue
        off = part.offset or 0.0
        spans.setdefault(pov.pov_id, []).append((off, off + dur))
    return spans


def _all_povs_recording(spans, n_povs, group):
    lo = min(x.start for x in group)
    hi = max(x.end for x in group)
    if n_povs == 0:
        return False
    return all(any(a <= lo and hi <= b for a, b in iv) for iv in spans.values())


def _near_anchors(anchors, starts, lo, hi, window):
    i = bisect.bisect_left(starts, lo - window - 30)
    out = []
    while i < len(anchors) and anchors[i]["anchor"].start <= hi + window:
        a = anchors[i]["anchor"]
        if max(0.0, max(lo, a.start) - min(hi, a.end)) <= window:
            out.append(anchors[i])
        i += 1
    return out


def _window_cover_anchor(group, anchors, starts, strict=False):
    """Named anchor that best covers this Team group's content words, or None to keep it."""
    lo = min(x.start for x in group)
    hi = max(x.end for x in group)
    best_member = max(group, key=lambda x: (len(content_tokens(x.toks)), len(x.toks), x.key))
    words = content_tokens(best_member.toks)
    if not words:
        # interjection-only ("Oh,", "Okay, okay."): a duplicate if those words were said nearby
        if strict or not best_member.toks:
            return None
        near = _near_anchors(anchors, starts, lo, hi, INTERJECTION_WINDOW)
        hits = [c for c in near if set(best_member.toks) & set(c["anchor"].toks)]
        if not hits:
            return None
        mine = set(best_member.toks)
        return min(hits, key=lambda c: (-len(mine & set(c["anchor"].toks)),
                                        abs(c["anchor"].start - lo), c["anchor"].key))
    near = _near_anchors(anchors, starts, lo, hi, TIME_WINDOW)
    if not near:
        return None
    pool = set(t for c in near for t in c["anchor"].toks)
    found = sum(1 for t in words if _token_found(t, pool))
    n = len(words)
    # any-order matching is the loosest rule: every content word must be accounted for
    ok = found == n and (n >= 2 or not strict)
    if not ok:
        return None

    def overlap(c):
        own = set(c["anchor"].toks)
        return (sum(1 for t in words if _token_found(t, own)), -abs(c["anchor"].start - lo))
    return max(near, key=overlap)


def _collapse_team_repeats(groups):
    """Fold a Team group into another one from different recordings when its content words are all
    found in it (+-TIME_WINDOW). Longer groups absorb shorter ones."""
    order = sorted(groups, key=lambda g: (-max(len(set(content_tokens(x.toks))) for x in g),
                                          min(x.start for x in g), g[0].key))
    kept = []
    for g in order:
        parts = {x.part.part_id for x in g}
        lo = min(x.start for x in g)
        hi = max(x.end for x in g)
        words = sorted(set(content_tokens(max(g, key=lambda x: len(set(content_tokens(x.toks)))).toks)))
        host = None
        if len(words) >= 2:
            for k in kept:
                if parts & {x.part.part_id for x in k}:
                    continue
                klo = min(x.start for x in k)
                khi = max(x.end for x in k)
                if max(0.0, max(lo, klo) - min(hi, khi)) > TIME_WINDOW:
                    continue
                pool = set(t for x in k for t in x.toks)
                if all(_token_found(t, pool) for t in words):
                    host = k
                    break
        if host is None:
            kept.append(list(g))
        else:
            host.extend(g)
    return kept


def _covering_anchor(group, anchors, starts):
    longest = max(group, key=lambda x: (len(x.toks), x.key))
    if len(longest.toks) < 4:
        return None
    lo = min(x.start for x in group)
    hi = max(x.end for x in group)
    i = bisect.bisect_left(starts, lo - TIME_WINDOW - 30)
    near = []
    while i < len(anchors) and anchors[i]["anchor"].start <= hi + TIME_WINDOW:
        a = anchors[i]["anchor"]
        if max(0.0, max(lo, a.start) - min(hi, a.end)) <= TIME_WINDOW:
            near.append(anchors[i])
        i += 1
    if not near:
        return None
    joined = [t for c in near for t in c["anchor"].toks]
    if not _contained(longest.toks, joined, COVER_CONTAIN):
        return None
    return max(near, key=lambda c: (contain(longest.toks, c["anchor"].toks), -abs(c["anchor"].start - lo)))


NEGATIONS = set("""not no never nothing nobody none nowhere cant cannot dont doesnt didnt wont
wouldnt shouldnt couldnt isnt arent wasnt werent aint havent hasnt hadnt""".split())


def _negations(toks):
    return sorted(t for t in toks if t in NEGATIONS)


def _source_extras(anchors, starts):
    """Per attached Team cue (by key): other named lines it also quotes (multi-speaker composite
    Team segments) and meaning conflicts with the preferred named text (e.g. weren't vs were)."""
    extras = {}
    for c in anchors:
        a = c["anchor"]
        own = set(a.toks)
        for x, _sim, kind in c["team"]:
            info = {}
            if kind != "echo":
                rest = [t for t in content_tokens(x.toks) if not _token_found(t, own)]
                if rest:
                    others = []
                    for o in _near_anchors(anchors, starts, x.start, x.end, TIME_WINDOW):
                        if o is c or o["anchor"].key in others:
                            continue
                        if any(_token_found(t, set(o["anchor"].toks)) for t in rest):
                            others.append(o["anchor"].key)
                    if others:
                        info["also_lines"] = others
            if kind in ("rendition", "fragment", "echo") and _negations(a.toks) != _negations(x.toks):
                info["variant"] = "negation differs: named %s vs Team %s" % (
                    "/".join(_negations(a.toks)) or "none", "/".join(_negations(x.toks)) or "none")
            if info:
                extras[x.key] = info
    return extras


def _named_line(c, extras=None) -> SessionLine:
    a = c["anchor"]
    srcs = [_source(a, None)]
    for it, sim in c["extra_named"]:
        srcs.append(_source(it, sim))
    team = sorted(c["team"], key=lambda p: (p[0].start, p[0].key))
    extras = extras or {}
    for it, sim, kind in team:
        src = _source(it, sim, kind)
        src.update(extras.get(it.key, {}))
        srcs.append(src)
    notes = []
    if c["extra_named"]:
        notes.append("also captured by an overlapping part of the same recording")
    heard = [x for x in team if x[2] != "echo"]
    echoes = [x for x in team if x[2] == "echo"]
    if heard:
        notes.append(f"heard as Team in {len({x[0].pov.pov_id for x in heard})} other recording(s)")
        low = [sim for _, sim, k in heard if k == "rendition" and sim < AGREE_THRESHOLD]
        if low:
            notes.append(f"{len(low)} Team rendition(s) differ noticeably from the named mic")
        if any(k != "rendition" for _, _, k in heard):
            notes.append("some Team pieces split or merged differently")
    else:
        notes.append("own mic only (no Team corroboration)")
    if echoes:
        notes.append("own voice also echoed in this recording's Team channel")
    variants = [x for x in srcs if x.get("variant")]
    if variants:
        notes.append("conflicting Team wording kept as evidence (%s): %s" % (
            variants[0]["variant"], "; ".join(f'{v["pov_name"]}: "{v["text"]}"' for v in variants[:3])))
    if any(x.get("also_lines") for x in srcs):
        notes.append("some Team segments also contain other speakers' lines")
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


# --------------------------------------------------------------------------- compact AI view

MERGE_GAP = 1.5             # merge consecutive same-speaker fragments closer than this (s)
MERGE_MAX_CHARS = 320       # ... while the merged text stays reasonably short

COMPACT_LEGEND = """\
Format: <line id> <session time HH:MM:SS> <speaker>: <text>
Speaker names come from that player's own microphone (reliable). "Team?" = heard over voice chat,
speaker unknown (never guess who). "(?)" = wording uncertain. Cite line ids in quotes."""


@dataclass
class PacketLine:
    """One AI-facing transcript line; may merge consecutive same-speaker SessionLines."""
    packet_id: str
    session_start: float
    session_end: float
    speaker: str | None
    channel: str
    text: str
    uncertain: bool
    members: list = field(default_factory=list)     # SessionLine objects (full provenance)

    @property
    def sources(self):
        return [src for m in self.members for src in m.sources]

    @property
    def label(self):
        if self.speaker:
            return self.speaker
        return "Team?" if self.channel == "team" else "?"


def _hms(sec: float) -> str:
    sign = "-" if sec < 0 else ""
    t = int(abs(sec))
    return f"{sign}{t // 3600:02d}:{t // 60 % 60:02d}:{t % 60:02d}"


def packet_lines(lines, merge_gap=MERGE_GAP) -> list:
    """Compact, deterministic AI view of a session transcript (ids L0001.. in session order)."""
    groups = []
    for l in sorted(lines, key=lambda x: (x.session_start, x.line_id)):
        g = groups[-1] if groups else None
        if (g is not None and l.speaker and g[-1].speaker == l.speaker
                and l.session_start - g[-1].session_end <= merge_gap
                and sum(len(m.text) + 1 for m in g) + len(l.text) <= MERGE_MAX_CHARS):
            g.append(l)
        else:
            groups.append([l])
    width = max(4, len(str(len(groups))))
    out = []
    for n, g in enumerate(groups, 1):
        out.append(PacketLine(
            packet_id=f"L{n:0{width}d}",
            session_start=g[0].session_start,
            session_end=max(m.session_end for m in g),
            speaker=g[0].speaker,
            channel=g[0].channel,
            text=" ".join(m.text.strip() for m in g),
            uncertain=any(m.uncertain for m in g),
            members=g,
        ))
    return out


def compact_transcript(plines) -> str:
    return "\n".join(f"{p.packet_id} {_hms(p.session_start)} {p.label}: {p.text}"
                     + (" (?)" if p.uncertain else "") for p in plines)
