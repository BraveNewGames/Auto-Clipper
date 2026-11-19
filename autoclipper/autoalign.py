"""Transcript-based offset suggestions (never applied automatically).

Idea: the same utterance appears in several recordings - as the speaker's named
mic line in their own file and as a `Team` line in everybody else's.  If cue a
(local time la in part A) and cue b (local time lb in part B) are the same
utterance then  offset_B - offset_A = la - lb.  We collect many such candidate
pairs per pair of parts, take a robust estimate (densest window of differences,
then median of inliers), score confidence from support/spread/ambiguity, and
propagate along the most confident path from a reference part.
"""
from __future__ import annotations

import heapq
import math
from collections import defaultdict
from statistics import median

from .reconcile import load_cues, similarity, tokens

MIN_TOKENS = 3            # ignore very short lines ("yeah", "oh no") - too ambiguous for alignment
PAIR_SIM = 0.72           # min fuzzy similarity for a candidate pair
WINDOW = 2.0              # width (s) of the densest-difference window
INLIER = 1.0              # inlier radius (s) around the estimate
HOP_DECAY = 0.95          # each extra hop away from the reference costs a little confidence
MIN_SUPPORT = 3           # min inlier matches for an edge between two parts

_STOP = set("""a an and are as at be but by can did do does for from get go got had has have he her
him his how i if im in is it its just me my no not of oh ok okay on or our out she so that the
them then there they this to up us was we what when where who why will with yeah yes you your""".split())


def _keywords(toks):
    return {t for t in toks if len(t) >= 3 and t not in _STOP}


class _C:
    __slots__ = ("cue", "toks", "kw", "pov_id")

    def __init__(self, cue, pov_id):
        self.cue = cue
        self.toks = tokens(cue.text)
        self.kw = _keywords(self.toks)
        self.pov_id = pov_id


def _allowed(a, b, same_pov):
    ca, cb = a.cue.channel, b.cue.channel
    if same_pov:
        # overlapping parts of one recording: identical channels hear identical audio
        if ca == "named" and cb == "named":
            return (a.cue.speaker or "").lower() == (b.cue.speaker or "").lower()
        return ca == cb
    # different POVs: named<->team or team<->team; named<->named are different people
    if ca == "named" and cb == "named":
        return False
    return True


def _pair_diffs(A, B, same_pov):
    """Candidate (diff, sim) where diff = offset_B - offset_A, best match per cue of A."""
    index = defaultdict(list)
    for j, b in enumerate(B):
        for k in b.kw:
            index[k].append(j)
    out = []
    for a in A:
        if len(a.toks) < MIN_TOKENS or not a.kw:
            continue
        counts = defaultdict(int)
        for k in a.kw:
            for j in index.get(k, ()):
                counts[j] += 1
        need = 1 if len(a.kw) <= 2 else 2
        best = {}
        for j, n in counts.items():
            if n < need:
                continue
            b = B[j]
            if len(b.toks) < MIN_TOKENS or not _allowed(a, b, same_pov):
                continue
            sim = similarity(a.toks, b.toks)
            if sim < PAIR_SIM:
                continue
            best[j] = sim
        if not best:
            continue
        top = max(best.values())
        # keep every near-best match (repeated phrases): the robust estimator sorts them out
        for j, sim in best.items():
            if sim >= top - 0.05:
                out.append((a.cue.local_start - B[j].cue.local_start, sim))
    return out


def _robust(diffs):
    """-> (estimate, support, mad, confidence) or None."""
    if not diffs:
        return None
    diffs = sorted(diffs)
    xs = [d for d, _ in diffs]
    ws = [w for _, w in diffs]
    # densest window of width WINDOW (weighted by similarity)
    best_w, best_i, best_j = -1.0, 0, 0
    j = 0
    acc = 0.0
    for i in range(len(xs)):
        while j < len(xs) and xs[j] - xs[i] <= WINDOW:
            acc += ws[j]
            j += 1
        if acc > best_w:
            best_w, best_i, best_j = acc, i, j
        acc -= ws[i]
    est = median(xs[best_i:best_j])
    for _ in range(3):
        inl = [x for x in xs if abs(x - est) <= INLIER]
        if not inl:
            return None
        est = median(inl)
    inl = [x for x in xs if abs(x - est) <= INLIER]
    support = len(inl)
    mad = median(abs(x - est) for x in inl)
    # ambiguity: strongest competing cluster away from the estimate
    rest = sorted(x for x in xs if abs(x - est) > 3 * INLIER)
    second = 0
    j = 0
    for i in range(len(rest)):
        while j < len(rest) and rest[j] - rest[i] <= WINDOW:
            j += 1
        second = max(second, j - i)
    conf = (1 - math.exp(-support / 3.0)) * max(0.0, 1 - mad / (2 * INLIER)) * (support / (support + second))
    return est, support, mad, conf


def _edges(session, cues_by_part):
    parts = [(pov, pt) for pov, pt in session.all_parts()]
    data = {pt.part_id: [_C(c, pov.pov_id) for c in cues_by_part.get(pt.part_id, []) or []
                         if (c.text or "").strip()]
            for pov, pt in parts}
    edges = defaultdict(list)   # part_id -> [(other_id, delta(other - this), conf, support)]
    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            (pa, A), (pb, B) = parts[i], parts[j]
            diffs = _pair_diffs(data[A.part_id], data[B.part_id], pa.pov_id == pb.pov_id)
            r = _robust(diffs)
            if not r:
                continue
            est, support, mad, conf = r
            if support < MIN_SUPPORT or conf <= 0:
                continue
            edges[A.part_id].append((B.part_id, est, conf, support, mad))
            edges[B.part_id].append((A.part_id, -est, conf, support, mad))
    return edges


def _part(session, part_id):
    for _, pt in session.all_parts():
        if pt.part_id == part_id:
            return pt
    return None


def default_reference(session):
    for pov in session.povs:
        if pov.parts:
            return sorted(pov.parts, key=lambda p: (p.index, p.part_id))[0].part_id
    return None


def suggest_offsets(session, cues_by_part=None, reference_part_id=None) -> list:
    """Suggest offsets for every part relative to a reference part (keeps its current offset).

    Returns one dict per part:
      {"part_id", "offset", "confidence", "support", "matched_with", "method", "current_offset", "delta"}
    method: "reference" | "transcript" | "none" (no reliable match; offset None).
    """
    if cues_by_part is None:
        cues_by_part = load_cues(session)
    ref = reference_part_id or default_reference(session)
    if ref is None or _part(session, ref) is None:
        return []
    edges = _edges(session, cues_by_part)

    # Most-confident path from the reference: path confidence = weakest edge, decayed per hop.
    ref_off = _part(session, ref).offset or 0.0
    result = {ref: (ref_off, 1.0, 0, None)}
    heap = [(-1.0, ref)]
    best = {ref: 1.0}
    done = set()
    while heap:
        negc, pid = heapq.heappop(heap)
        if pid in done:
            continue
        done.add(pid)
        conf_here = -negc
        off_here = result[pid][0]
        for other, delta, conf, support, mad in edges.get(pid, ()):
            c = min(conf_here, conf) * (1.0 if pid == ref else HOP_DECAY)
            if other in done or c <= best.get(other, 0.0):
                continue
            best[other] = c
            result[other] = (off_here + delta, c, support, pid)
            heapq.heappush(heap, (-c, other))

    out = []
    for pov, pt in session.all_parts():
        cur = pt.offset or 0.0
        if pt.part_id == ref:
            out.append({"part_id": pt.part_id, "offset": round(cur, 3), "confidence": 1.0, "support": 0,
                        "matched_with": None, "method": "reference", "current_offset": cur, "delta": 0.0})
        elif pt.part_id in result:
            off, c, support, via = result[pt.part_id]
            out.append({"part_id": pt.part_id, "offset": round(off, 3), "confidence": round(c, 3),
                        "support": support, "matched_with": via, "method": "transcript",
                        "current_offset": cur, "delta": round(off - cur, 3)})
        else:
            out.append({"part_id": pt.part_id, "offset": None, "confidence": 0.0, "support": 0,
                        "matched_with": None, "method": "none", "current_offset": cur, "delta": None})
    return out


def apply_suggested_offsets(session, suggestions, min_confidence=0.5) -> None:
    """Apply suggestions with confidence >= min_confidence; never touches manual offsets."""
    for s in suggestions or []:
        if not isinstance(s, dict) or s.get("method") == "reference" or s.get("offset") is None:
            continue
        conf = s.get("confidence") or 0.0
        if conf < min_confidence:
            continue
        part = _part(session, s.get("part_id"))
        if part is None or part.offset_source == "manual":
            continue
        part.offset = float(s["offset"])
        part.offset_source = "transcript"
        part.offset_confidence = round(float(conf), 3)
