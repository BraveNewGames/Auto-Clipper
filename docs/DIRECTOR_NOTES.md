# Director Notes and the Footage Knowledge Base

Phase B stores one FKB per session at `.autoclipper/director-fkb.json`. It
reads the existing Auto-Clipper session, aligned offsets and reconciled SRT
evidence. Existing session, review and export files retain their formats.
The FKB snapshot and its backup contain sensitive notes, transcript text and
source paths. Keep them in approved local storage.

The canonical session clock uses integer frames at the Phase A timeline's
integer non-drop rate. Frame zero is Auto-Clipper session time zero.
Pre-roll evidence may have negative frames. Every interval includes In and
excludes Out. Resolve timeline start is a separate absolute frame origin.
The persistent catalog checks both endpoints by adding the verified review
session origin to the timeline-relative marks. The coverage manifest and
reopened builder readback must agree before a clipped review can initialize
or open a persistent catalog. Older snapshots default to zero, and legacy
review manifests must map to zero across the timeline. Imported transcript
ranges round outward, while media coverage includes full frames
only. Unknown-duration and offline parts supply no confirmed coverage.
Separate part intervals retain gaps and overlaps. Assembler microphone
mappings are optional and retain their original evidence fields.

Use `FKBStore.ingest` to initialize or check a snapshot and `NotesCatalog`
to edit annotations. Part and transcript IDs are session-scoped and
deterministic. Notes use independent UUIDs. Imported transcript evidence
and model proposals carry distinct provenance. When ingestion finds changed
evidence with existing notes, candidates or edit plans, it flags the mapping
as stale and retains the prior snapshot so existing references still resolve.
Stale snapshots require explicit reconciliation before range edits.

Use one writer process per session. Writes use atomic replacement and save
the prior snapshot to `director-fkb.json.bak`. Schema version 1 is the first
FKB format. An unsupported version or unreadable JSON raises an error.
For recovery, close the writer, preserve the current file, validate the
backup, then restore it manually. Future schema migrations should retain a
pre-migration backup and replace the main file only after validation.
Phase A disposable `notes.json` and its unverified captures stay separate.

## Scratch companion

With an already built, authorized scratch timeline and a synthetic or
approved session JSON copy under system temp, including its relative media
and SRT references:

```powershell
python -m autoclipper.director_poc index-notes $scratch $sessionCopy
python -m autoclipper.director_poc companion $scratch --persistent-notes
```

Confirm paused playback before each new capture. The companion keeps
Resolve calls in its child process, disables continuous diagnostic
probing and discards captures returned after 750 ms. The persisted capture
records operator confirmation and unverified API playback state. Check the
displayed marks before saving. The hotkey appends to an explicitly selected
note or starts a new note. **New capture** always starts a separate note.
Use **Adjust range** to preview old and new frame boundaries before saving.
Full text edits, revision restoration and delete/recover operate on the
selected note. Search matches text, title, tags and overlapping speech.
Jump and marker operations run only on explicit request. A marker at an
occupied frame leaves both notes available in the catalog.

The companion saves text before running deterministic local title/tag
generation in the background. `MetadataProvider` supports injection for
testing. User-customized titles and tags survive regeneration. For retry
after failure, call `NotesCatalog.refresh_metadata` off the UI thread.
There are no external provider calls in this implementation.

Automated verification uses synthetic inputs and mocked Resolve. Live
verification still covers hotkey focus, paused capture and mark readback,
marker behavior on re-open, and source coverage on the installed Resolve
Studio build. Independent review is required for timing and persistence.
