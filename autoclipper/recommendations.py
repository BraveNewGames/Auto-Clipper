"""Session-local editorial proposals grounded in the existing FKB."""
from copy import deepcopy
from dataclasses import dataclass, replace
from fractions import Fraction
from math import isfinite
from typing import Protocol, Sequence

from . import reviews
from .fkb import CandidateMoment, FrameRange, Provenance, stable_id
from .reconcile import content_tokens, normalize, tokens

_VISUAL_CHECK = "Visual payoff needs review."
_STATUSES = {"keep", "maybe", "reject"}


def _terms(text):
    return {word.removesuffix("s") for word in content_tokens(tokens(text)) if len(word) >= 3}


@dataclass(frozen=True)
class EvidenceHit:
    evidence_id: str
    frames: FrameRange
    text: str
    speaker: str | None
    provenance: Provenance
    score: int
    context_note_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Proposal:
    logline: str
    evidence_id: str


class RecommendationProvider(Protocol):
    model: str
    version: str

    def propose(self, query: str, evidence: Sequence[EvidenceHit]) -> Sequence[Proposal]:
        """Select anchors from the supplied session evidence."""


class OfflineProvider:
    model = "offline-keyword"
    version = "1"

    def propose(self, query, evidence):
        return [Proposal(hit.text, hit.evidence_id) for hit in evidence[:5]]


def _note_terms(note):
    return _terms(" ".join((note.text, note.title, *note.tags)))


def _notes(database):
    return [note for note in database.director_notes
            if not note.deleted and note.provenance == Provenance.USER]


def search_evidence(database, query="", *, speaker=None, frames=None):
    """Rank timed FKB records with optional exact speaker and overlapping time filters."""
    words = _terms(query)
    notes = _notes(database)
    matching_notes = [(note, len(words & _note_terms(note))) for note in notes]
    matching_notes = [(note, score) for note, score in matching_notes
                      if score and (frames is None or frames.overlaps(FrameRange(**note.frames)))]
    hits = []
    for span in database.transcript_spans:
        bounds = FrameRange(**span.frames)
        if frames is not None and not frames.overlaps(bounds):
            continue
        if speaker is not None and normalize(span.speaker) != normalize(speaker):
            continue
        direct = len(words & _terms(span.text))
        context = [(note.note_id, score) for note, score in matching_notes
                   if bounds.overlaps(FrameRange(**note.frames))]
        if direct or context or not query.strip():
            hits.append(EvidenceHit(span.span_id, bounds, span.text, span.speaker,
                                    Provenance.TRANSCRIPT,
                                    direct + 2 * max((score for _, score in context), default=0),
                                    tuple(key for key, _ in context)))
    if speaker is None:
        for note in notes:
            bounds = FrameRange(**note.frames)
            score = len(words & _note_terms(note))
            if (score or not query.strip()) and (frames is None or frames.overlaps(bounds)):
                hits.append(EvidenceHit(note.note_id, bounds, note.text, None,
                                        Provenance.USER, 3 * score))
    return sorted(hits, key=lambda hit: (-hit.score, hit.provenance != Provenance.USER,
                                         hit.frames.start, hit.evidence_id))


def available_povs(database, frames):
    """Known online coverage intersections, one entry per part. Empty when stale."""
    if database.mapping_stale or frames is None:
        return []
    return [(part.pov_id, part.part_id,
             FrameRange(max(frames.start, part.session_range["start"]),
                        min(frames.end, part.session_range["end"])))
            for part in database.coverage(frames) if part.duration_source != "srt_estimate"]


def _uncertainty(database, frames, spans, caveats):
    messages = [*filter(None, caveats), _VISUAL_CHECK]
    if frames is not None:
        if any(span.uncertain or span.confidence == "low" for span in spans):
            messages.append("Transcript wording or speaker attribution is uncertain.")
        covered_until = frames.start
        for _, _, covered in sorted(available_povs(database, frames), key=lambda row: row[2].start):
            if covered.start > covered_until:
                break
            covered_until = max(covered_until, covered.end)
        if covered_until < frames.end:
            messages.append("Online source coverage is incomplete or unknown.")
    if database.mapping_stale:
        messages.append("Session mapping is stale.")
    return " ".join(messages)


def _source_key(ref, part_id):
    return part_id, ref["cue_index"], ref["local_start"], ref["local_end"]


def _packet_spans(database, packet_lines, source):
    """Bind packet members to FKB spans via cue identity, text and session range."""
    part_ids = {part.part_id: stable_id(source.session_id, "part", pov.pov_id, part.part_id)
                for pov, part in source.all_parts()}
    spans = {span.span_id: span for span in database.transcript_spans}
    result = {}
    for line in packet_lines:
        members = []
        for member in line.members:
            identity = sorted({_source_key(ref, part_ids[ref["part_id"]])
                               for ref in member.sources})
            span = spans.get(stable_id(source.session_id, "span", identity))
            if (span and span.text == member.text and span.speaker == member.speaker
                    and member.session_end > member.session_start
                    and span.frames == database.session.frames(
                        member.session_start, member.session_end).to_dict()):
                members.append(span)
        result[line.packet_id] = members
    return result


def _contains_quote(text, span):
    quoted = normalize(text)
    return bool(quoted and f" {quoted} " in f" {normalize(span.text)} ")


def _quote_matches(quote, local_spans, packet_spans, database):
    matches = [span for span in local_spans if _contains_quote(quote["text"], span)]
    if quote.get("span_id"):
        return [span for span in matches if span.span_id == quote["span_id"]]
    if quote.get("time") is not None and matches:
        # Consider occurrences outside the proposed cut too. A quote that
        # points there must never be reassigned to an in-range repetition.
        target = Fraction(str(quote["time"])) * database.session.fps
        timed = [span for span in database.transcript_spans if _contains_quote(quote["text"], span)]
        distances = [abs(span.frames["start"] - target) for span in timed]
        nearest = min(distances)
        closest = [span for span, distance in zip(timed, distances) if distance == nearest]
        return closest if len(closest) > 1 else [span for span in closest if span in matches]
    packet_ids = {span.span_id for span in packet_spans.get(quote.get("line"), [])
                  if _contains_quote(quote["text"], span)}
    return [span for span in matches if span.span_id in packet_ids] if packet_ids else matches


def _review_candidates(database, source, suggestions, packet_lines, run_id, model, model_version):
    packet_spans = _packet_spans(database, packet_lines, source)
    for suggestion in suggestions:
        logline = " — ".join(filter(None, (suggestion.title, suggestion.what_happens)))
        frames = None
        if (suggestion.kind == "clip_now" and isfinite(suggestion.start)
                and isfinite(suggestion.end) and suggestion.end > suggestion.start):
            frames = database.session.frames(suggestion.start, suggestion.end)
        local_spans = [span for span in database.transcript_spans
                       if frames and frames.overlaps(FrameRange(**span.frames))]
        cited = {}
        missed = 0
        ambiguous = 0
        for quote in suggestion.quotes:
            matches = _quote_matches(quote, local_spans, packet_spans, database)
            if len(matches) == 1:
                cited[matches[0].span_id] = matches[0]
            elif matches:
                ambiguous += 1
            else:
                missed += 1

        caveats = [suggestion.uncertainty, suggestion.needs_visual_check]
        if ambiguous:
            caveats.append("Some quotes are ambiguous across repeated transcript occurrences.")
        if missed:
            caveats.append("Some quotes could not be uniquely verified in the proposed range.")
        if not cited and not suggestion.quotes:
            words = _terms(logline)
            cited = {span.span_id: span for span in local_spans if words & _terms(span.text)}
            if cited:
                caveats.append("Dialogue matched by keywords only.")
        if not cited:
            frames = None
            caveats.append("No supported session range for this proposal.")
        else:
            caveats.append("In/Out comes from the reviewer.")
        selection = source.reviews.get(suggestion.suggestion_id)
        status = selection.status if selection and selection.status in _STATUSES else "maybe"
        yield CandidateMoment(
            stable_id(source.session_id, "review-candidate", run_id, suggestion.suggestion_id),
            logline, frames.to_dict() if frames else {}, list(cited), status=status,
            uncertainty=_uncertainty(database, frames, cited.values(), caveats),
            model=model or suggestion.reviewer_name, run_id=run_id, model_version=model_version)


def import_recommendations(database, source, review_json=None, *, run_id, model=None,
                           model_version=None, reviewer=None, cues_by_part=None):
    """Adapt pasted review JSON or the session's existing imported suggestions."""
    if source.session_id != database.session.session_id:
        raise ValueError("Review source and footage index must belong to the same session")
    if not run_id:
        raise ValueError("A stable review run ID is required")
    existing = review_json is None
    if existing:
        suggestions = deepcopy(source.suggestions)
        for suggestion in suggestions:
            if suggestion.raw:
                # Legacy import fills missing quote timestamps from its packet match.
                # Recover the reviewer's actual hints before disambiguating.
                parsed, = reviews.parse_review([suggestion.raw], suggestion.reviewer_name)
                suggestion.quotes = parsed.quotes
    else:
        suggestions = reviews.parse_review(review_json, reviewer)
    if not existing:
        # Parser IDs are provisional and may coincide with unrelated legacy decisions.
        source = replace(source, reviews={})
    packet = (reviews.session_packet_lines(source, cues_by_part)
              if any(quote.get("line") for item in suggestions for quote in item.quotes) else [])
    candidates = list(_review_candidates(database, source, suggestions, packet,
                                         run_id, model, model_version))
    return _upsert(database, candidates)


def recommend(database, query, *, run_id, provider=None, speaker=None, frames=None):
    """Let an injectable provider propose ideas anchored to current search results."""
    if not run_id:
        raise ValueError("A stable recommendation run ID is required")
    provider = OfflineProvider() if provider is None else provider
    evidence = search_evidence(database, query, speaker=speaker, frames=frames)
    by_id = {hit.evidence_id: hit for hit in evidence}
    spans = {span.span_id: span for span in database.transcript_spans}
    proposals = provider.propose(query, tuple(evidence)) if evidence else ()
    candidates = []
    occurrences = {}
    for proposal in proposals:
        index = occurrences.get(proposal.evidence_id, 0)
        occurrences[proposal.evidence_id] = index + 1
        hit = by_id.get(proposal.evidence_id)
        bounds = hit.frames if hit else None
        supporting = [spans[hit.evidence_id]] if hit and hit.evidence_id in spans else []
        caveats = [] if hit else ["Provider proposal has no matching session evidence."]
        candidates.append(CandidateMoment(
            stable_id(database.session.session_id, "concept", run_id, proposal.evidence_id, index),
            proposal.logline, bounds.to_dict() if bounds else {},
            [hit.evidence_id, *hit.context_note_ids] if hit else [],
            uncertainty=_uncertainty(database, bounds, supporting, caveats),
            model=provider.model, run_id=run_id, model_version=provider.version))
    if not candidates:
        candidates.append(CandidateMoment(
            stable_id(database.session.session_id, "concept-missing", run_id, query), query, {}, [],
            uncertainty=_uncertainty(database, None, [], ["No supported session moment found."]),
            model=provider.model, run_id=run_id, model_version=provider.version))
    return _upsert(database, candidates)


def _with_notes(notes, span_ids, candidate):
    if not candidate.frames:
        return candidate
    frames = FrameRange(**candidate.frames)
    words = _terms(candidate.logline)
    transcript_ids = [key for key in candidate.evidence_ids if key in span_ids]
    if not transcript_ids and candidate.evidence_ids:
        # The first reference is the sole anchor for a note-based proposal.
        anchor = next((note for note in notes
                       if note.note_id == candidate.evidence_ids[0]), None)
        if anchor is None or anchor.frames != candidate.frames:
            return replace(candidate, frames={}, evidence_ids=[],
                           uncertainty="The original note was deleted or retimed. " + _VISUAL_CHECK)
        return candidate
    linked = [note.note_id for note in notes
              if frames.overlaps(FrameRange(**note.frames))
              and (note.note_id in candidate.evidence_ids or words & _note_terms(note))]
    return replace(candidate, evidence_ids=transcript_ids + linked)


def _record_revision(previous, candidate):
    if candidate == previous:
        return previous
    snapshot = previous.to_dict()
    snapshot.pop("revisions")
    return replace(candidate, revisions=[*previous.revisions, snapshot])


def _upsert(database, candidates):
    positions = {candidate.candidate_id: index
                 for index, candidate in enumerate(database.candidate_moments)}
    saved = []
    notes = _notes(database)
    span_ids = {span.span_id for span in database.transcript_spans}
    for candidate in candidates:
        candidate = _with_notes(notes, span_ids, candidate)
        position = positions.get(candidate.candidate_id)
        if position is None:
            positions[candidate.candidate_id] = len(database.candidate_moments)
            database.candidate_moments.append(candidate)
        else:
            previous = database.candidate_moments[position]
            candidate = replace(candidate, status=previous.status, revisions=previous.revisions)
            candidate = _record_revision(previous, candidate)
            database.candidate_moments[position] = candidate
        saved.append(candidate)
    return saved


def refresh_recommendations(database):
    """Re-link saved proposals to current notes and rank annotated proposals first."""
    saved = _upsert(database, database.candidate_moments[:])
    note_ids = {note.note_id for note in _notes(database)}
    return sorted(saved, key=lambda candidate: (-len(note_ids.intersection(candidate.evidence_ids)),
                                                not bool(candidate.frames)))


def set_candidate_status(database, candidate_id, status):
    """Record an editorial decision without changing the verification state."""
    if status not in _STATUSES:
        raise ValueError("Expected keep, maybe or reject")
    for index, candidate in enumerate(database.candidate_moments):
        if candidate.candidate_id == candidate_id:
            updated = _record_revision(candidate, replace(candidate, status=status))
            database.candidate_moments[index] = updated
            return updated
    raise KeyError(candidate_id)
