# Director Mode recommendations

Initialize a session's FKB with the existing `FKBStore.ingest` at the session
frame rate. With that same Auto-Clipper session, adapt a pasted ChatGPT or Claude
review using the existing review format:

```python
from autoclipper.recommendations import import_recommendations, recommend

candidates = store.update(
    lambda db: import_recommendations(db, session, review_json,
                                      run_id="claude-review-1",
                                      model_version="version from review run")
)
ideas = store.update(
    lambda db: recommend(db, "funniest teamwork disasters", run_id="offline-search-1")
)
```

Omit `review_json` to adapt suggestions already imported into the Auto-Clipper
session, including their saved decisions. Use a distinct run ID for each review
or search. Reusing a run ID updates matching candidates and retains their FKB
decisions. Use `set_candidate_status` to record keep, maybe, or reject.
`refresh_recommendations` relinks and ranks saved proposals after note edits
without reading SRTs again. A note-only proposal whose anchor is deleted or
retimed loses its current range. Rerun the query to propose the new range.

`search_evidence` returns ranked transcript and Director Note hits. It accepts a
case-insensitive speaker filter for transcript hits and an overlapping session
`FrameRange` filter. Notes contribute their current human text, title, and tags.
`available_povs` returns known online coverage intersections by POV and part,
including partial coverage. Positions are session-relative frames with an
exclusive Out. An empty candidate `frames` dict represents a missing range.

Candidate loglines are editorial proposals. Candidate evidence IDs resolve to
FKB dialogue or human notes. Timed review selections use the reviewer's
suggested bounds after checking in-range evidence. Citation text must match
locally, and uncited keyword matches carry a weaker evidence flag. Visual
verification remains false and incomplete or stale media coverage is flagged.
Earlier candidate states are preserved in `revisions`.

The default provider uses deterministic keyword ranking. A provider implements
`model`, `version`, and `propose(query, evidence)` and returns `Proposal` objects
referencing supplied evidence IDs. Provider inputs are text, ranges and evidence
metadata, without media or file paths. This slice makes no provider network calls
and uses no Resolve integration. Editorial quality and downstream assembly
require owner-present validation.
