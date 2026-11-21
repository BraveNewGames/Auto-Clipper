# Director Mode Phase A feasibility harness

A0 is an offline experiment. Run the live commands only within the owner-approved A1 Resolve window.

## Installed SDK findings

Paths are relative to the installed Resolve `Support/Developer` directory under ProgramData. These are declarations and examples. Playback behavior and timing remain to be measured.

| Surface | Installed evidence |
|---|---|
| Active identity | `Scripting/DaVinciResolveScript.pyi:2166,2241,2268,2394,2691,2862` declares project-manager `GetCurrentProject`, project `GetCurrentTimeline`, and `GetName` / `GetUniqueId` on both project and timeline. |
| Playhead and origin | Stub lines 2703–2707, 2718–2725 and 2832–2837 declare `GetStartFrame`, `GetEndFrame`, `GetCurrentTimecode`, `SetCurrentTimecode`, `GetCurrentVideoItem`, and `GetStartTimecode` / `SetStartTimecode`. |
| Frame rate | Stub lines 661, 2340–2346 and 2826–2831 cover playback-rate and project/timeline settings surfaces. `Scripting/README.md:266–269` documents `timelineFrameRate` and the DF suffix. |
| In/Out | Stub lines 439–448, 2880–2888 declare timeline-relative integer `in` / `out` fields under optional `video` / `audio` dictionaries, plus timeline `GetMarkInOut`, `SetMarkInOut`, and `ClearMarkInOut`. Out inclusivity and how partial marks appear need live verification. Media-pool items also expose marks at 2661–2669. |
| Markers | Stub lines 2727–2749 declare `AddMarker` with optional custom data, `GetMarkers`, `GetMarkerByCustomData`, `UpdateMarkerCustomData`, `GetMarkerCustomData`, and `DeleteMarkerByCustomData`. Custom-data lookup and delete target the first match. `Workflow Integrations/Scripts/HandleTimelineMarkers.js:58–116` demonstrates these operations. |
| Selection and transport | Stub lines 2715–2725 declare selected timeline clips and the item at the playhead. Lines 2532–2537 declare media-pool selection. The inspected stub and scripting examples expose no documented timeline playback-state getter or selected-marker getter. Use timecode samples and owner observation together. |
| Internal scripts | `Scripting/README.md:20–44,53–80` describes Workspace > Scripts, Console, and local external scripting. A Utility script can appear on every page. The inspected SDK does not document binding an individual script in Keyboard Customization. Verify in the owner's UI before counting on a native shortcut. |
| Workflow Integrations | `Workflow Integrations/README.txt:10–12,27–61,94–125,137–144` describes Studio's startup discovery and Workspace > Workflow Integrations entry. An Electron plugin loads HTML in a separate window using `WorkflowIntegration.node` and its JavaScript bridge. A Python/Lua script can use the supplied Resolve objects and UIManager. Neither route documents a shortcut that fires with Resolve editing focus. |

## Host choice and timing contract

The primary host is the Tk companion with a single Windows-wide `RegisterHotKey` binding, **Ctrl+Alt+Shift+F11** by default. `--key` changes its function key. A registration conflict is visible at startup. The binding exists while the companion runs and uses the Windows message queue, with no keyboard hook. The hotkey opens the input immediately while a dedicated child process reads the active scratch timeline. The range remains pending until the SDK returns it. A range returned after 750 ms is flagged for manual verification because the SDK may have sampled the marks after invocation. The diagnostics dropdown defaults to Off because playback-time polling blocked in A1.

A tiny native fallback would be a Workspace > Scripts command or Workflow Integration button that reads the same identity, video marks and playhead, then writes a single capture JSON file into the established scratch directory for the companion. That adds install/discovery and a handoff, and a native keyboard binding remains unverified. Keep it a comparison in A1. The companion needs no localhost server.

The review timeline uses integer non-drop fps and the builder's `01:00:00:00` start. At 60 fps, that timecode is absolute frame 216000. Subtract the timeline start frame to get the timeline-relative index. Each manifest segment maps `record_start_frame` to its rounded `session_start × fps`. An equal offset within that segment gives the session frame and, per covered POV, `source_in_frame + offset` for the matching part. Missing POVs have no source entry. Captures can span adjacent segments on the same session clock, and fail on an uncovered interval or discontinuity. The existing builder blocks a review build if any interval has zero angles.

The default interprets Resolve video Out as inclusive and stores `[In, Out + 1)`. Compare against known displayed marks and duration before retaining any measurement notes. If A1 verifies exclusive Out, restart with `--out-exclusive`. Both modes require two distinct mark values. A single-frame mark pair is treated as a point for this POC. JSON is authoritative, supports duplicate starts, and stores exclusive boundaries, the captured playhead, stable IDs, original text and revisions. Run one companion against a scratch directory at a time. Marker mirroring is explicit and refuses an occupied frame. A failed marker operation leaves the note available for catalog navigation.

## Owner-approved A1 runbook (~20–30 minutes)

1. **Prerequisites (3–5 min).** Owner saves and closes their project, leaves Resolve Studio 21.2.0.6 at Project Manager, and verifies **Preferences > System > General > External scripting using > Local**. Review **Preferences > User > Playback Settings** and search within Preferences for any playback-on-focus-loss or inactive-window option. Record the visible setting or its absence. The worker leaves preferences untouched. Owner supplies temp copies of the approved session JSON and mic confirmations, and approved temp copies of transcriber metadata if any mappings rely on recorder attribution. Keep metadata filenames/stem layout used by the assembler. Owner supplies the approved read-only media root with the OBS-remuxed MP4s. Python needs Tk and ffprobe for preparation. Use the same external scripting environment as the working assembler (`Scripting/README.md:126–133`).
2. **Prepare and build (3–8 min).** Worker runs the following from the worktree after the window opens for any real input access. `prepare` is offline. Stop if there are manifest blockers. `build` creates a timestamped scratch project, verifies the multicam segment placement, saves and reopens it, and records exact IDs. Owner opens the Edit page on `DirectorA REVIEW`. Use the printed temp directory for `$scratch`.

   ```powershell
   $env:RESOLVE_SCRIPT_API = "$env:ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting"
   $env:RESOLVE_SCRIPT_LIB = "$env:ProgramFiles\Blackmagic Design\DaVinci Resolve\fusionscript.dll"
   $env:PYTHONPATH = "$env:RESOLVE_SCRIPT_API\Modules;$env:PYTHONPATH"
   python -m autoclipper.director_poc prepare $sessionCopy $confirmationsCopy $mediaRoot --session-root $mediaRoot --fps 60
   # Append --metadata $metadataCopies to prepare when supplied.
   python -m autoclipper.director_poc build $scratch
   python -m autoclipper.director_poc index-notes $scratch $sessionCopy --session-root $mediaRoot
   python -m autoclipper.director_poc companion $scratch --hz 10
   ```

Pass the approved unmodified session JSON copy and the same approved media/session
folder as `--session-root` to both offline commands. No hand edit of the JSON
`folder` is needed. The override affects memory only. An MP4-only session with
stale same-stem `.mkv` references is supported natively; the JSON is not rewritten.
Preparation pins the resolved root and SRT content digests. A missing relative
SRT refuses before scratch creation with `SRT missing for <part-id>: <srt-path>.
Supply --session-root with the recording folder.` (For a missing absolute SRT,
the final sentence is `Check this absolute SRT path.`) Indexing refuses a changed
root or SRT with `Cannot index the source session. Session root or SRT contents
differ from prepare. Use the same --session-root and unchanged SRTs, or prepare
a new review.` Older manifests without this source binding remain indexable but
print `Warning: this older manifest has no pinned session root or SRT digests;
index-notes cannot verify those inputs against prepare.` A different recording
reference changes catalog identity and is rejected during indexing.

3. **Capture (5 min).** Keep automatic polling Off. With playback paused, owner sets known distinct video marks in Resolve and presses the displayed hotkey with Resolve focused. Compare the displayed and saved range with the UI endpoints and duration. Clear both marks, then test equal In and Out. A1 first measured polling stalls in the original `playhead.csv` and isolated the timecode read in `rpc-probe.csv`. The child-process companion uses `rpc-probe.csv` for subsequent capture and diagnostic timings.
4. **Catalog, markers and jumps (4–5 min).** Owner sets ranges for three test notes, including two identical starts and a partially overlapping range. Worker saves distinct labels and checks UUIDs and boundaries in scratch `notes.json`. With playback stopped, select each note and click **Jump**. Compare target, immediate API readback, and Resolve's settled UI timecode. Record error in frames for each. Add a marker from one note at a frame free of assembler markers, read its custom data, and try adding a marker for a second note at the same start. Verify refusal and continued access to both notes. Leave one marker for the reopen check.
5. **Reopen and negative controls (5 min).** Owner saves and closes only the scratch project, closes the companion, reopens the scratch project, and restarts the companion with the same arguments. Verify note IDs and range persistence, repeat the jumps, read the saved marker, then delete it by custom data and verify its absence. In the scratch project, owner creates and activates a disposable timeline and confirms capture, jump and marker actions fail visibly. Close the scratch project to return to Project Manager and repeat the guard check. For disconnect, owner exits Resolve after saving the scratch project. Observe the companion status and measure any timeout or stall. If time permits, owner checks Keyboard Customization for a direct script binding without installing a plugin or changing a preference.
6. **One playback capture.** With distinct marks set, owner presses the hotkey during playback. Observe whether the input opens, whether the range arrives before playback stops, and whether the controls stay responsive. If Resolve permits changing marks before playback stops, compare the returned range with both sets of marks. Discard the experimental capture after recording the result.
7. **Stop.** Close the companion when the owner ends the window. Keep evidence local to scratch and report the actions actually observed, frame errors, polling statistics, focus behavior and any steps that could not run.

## A1 observations on Resolve Studio 21.2.0.6

- Full-session preflight found four source-frame range blockers and at least one interval with zero usable angles. An owner-approved selection of session seconds 1500–2100 built, saved and reopened with three segments at 60 fps. There were four POVs on either side of a six-frame, three-POV interval at review timecodes 01:05:00:24 through 01:05:00:29. The gap was verified from the manifest and builder readback.
- External scripting was set to Local. The owner found no pause-on-focus-loss or inactive-window option under User > Playback Settings.
- The owner observed smooth video and audio while the guarded playhead sample blocked for 11.731 seconds. Isolated `GetCurrentTimecode` calls returned after 6.762–21.762 seconds during the thread experiment. They returned values without a timeout error, and Tk froze despite the worker thread. This is consistent with a native call holding Python's interpreter lock.
- In the process-based companion, three paused captures returned in 30.6–31.9 ms. The owner confirmed the first capture matched Resolve. The store held three distinct IDs with two identical ranges `[3144, 6375)` and one overlapping range `[3508, 6375)`. Empty marks (None/None) and equal marks (4684/4684) were refused. The wrong-timeline test was refused visibly.
- Paused jumps to 01:00:52:24 and 01:00:58:28 read back with zero frame error. The first note's marker read back with its UUID and 3231-frame duration, survived project reopen, and was then deleted. A same-start marker was refused while both notes remained. The marker uses a generic display label and the note store retains the text.
- During the first playback capture, the owner could type while playback continued and the companion stayed responsive. The request was pending at 750 ms and returned after 22.140 seconds. The owner reported a range mismatch without frame-level endpoints. A controlled repeat returned after 4.388 seconds and retained the original displayed range after the owner changed Out; the owner could not place the response relative to pause. A third request took 9.399 seconds and the owner saw the range appear only after playback stopped. All three test captures stayed unsaved. The companion is responsive during playback, and exact keypress-time range capture remains unverified.
- The native script and Workflow Integration alternatives were documented only. No live disconnect test was run. For a later authorized phase, treat full-session edges and short media tails as unavailable coverage, and use paused capture or explicit confirmation of a delayed range until exact playback-time sampling is independently demonstrated.

## Coverage-aware review planner

`python -m autoclipper.resolve_manifest <session-folder> --full-session --dry-run`
plans a review without contacting Resolve or writing outputs. Omit `--dry-run` and
provide `--out <scratch-dir>` to save the manifest when authorized. Use an
owner-approved directory beneath system temp, separate from the source session.
Director reads and saves `mic_confirmations.json` in that output directory.
The existing
`resolve_builder` accepts this manifest. The scratch companion's `prepare` command
uses the same coverage-aware planner with its approved temp inputs. Ordinary
reviewed Keep exports still use the existing strict Stage 2 planning path.

The review anchors each part at its nearest session frame and uses the verified
MP4 frame count or a conservative count from video-stream duration. The requested
session extent includes estimated bounds, while the playable review clips edges
with no usable footage. The manifest reports those edges, preserves blank interior
spans at their original session positions, and omits a POV wherever its parts
overlap ambiguously. Missing or unverified media and microphone mappings still
block the build. Review gap and placement handling have synthetic and mocked
coverage. A later authorized Resolve session must verify native blank spans,
one-angle and changing-angle multicams, and reopen behavior.

## EditPlan assembler adapter

`compile_edit_plan(session, plan, ...)` in `autoclipper.resolve_manifest`
accepts the assembler projection of a Director plan. The projection has a plan
ID, session ID, integer fps, revision ID, revision history, and ordered entries.
A source entry provides half-open session frames and can specify a video POV and
an independent microphone POV. An undecided choice keeps the verified multicam
angles and existing Source-mode audio. Entries can carry candidate and note IDs.
Placeholder entries supply a title and provisional output frames.

`compile_fkb_edit_plan(session, knowledge_base, plan_id, revision_id=...)`
reads the FKB session and the existing `EditPlan` entity. Source entries use
`frames: {"start": In, "end": exclusiveOut}`, the FKB frame-range convention.
Draft revisions take a caller-supplied revision ID and retain the FKB plan's
revision history. The existing timeline ID is never used as a write target.

The compiler splits entries at verified coverage changes and preserves edit
order and evidence IDs. Where requested footage or the chosen mic has no
coverage, it emits a labeled slate with no source coordinates. The existing
builder renders slates with the installed FFmpeg, imports them as silent video,
and assembles to a new project. Keep the generated slate files in the temporary
directory while the project references them. A later authorized Resolve test
must verify the native audio-only append, slate visibility and exact duration,
and source and slate readback after reopen. No Resolve connection was made for
Phase B verification.

## Known limitations / deferred

- **Historical review receipt.** The receipt binds the saved and reopened
  segment map. An in-place edit to the same timeline can change its contents
  while leaving its project ID, timeline ID and start clock intact. In a
  future authorized disposable project, build and reopen a review, edit one
  segment in place, then check what the companion can still detect. Rebuild
  into a new disposable project after changing a review used for capture.
- **Independent mic coverage (Medium 4).** Video coverage currently stands in
  for mic availability. A delayed or truncated mic can yield silence, shifted
  audio, or a rejected append. Test a delayed and short mic stream, comparing
  its probed bounds with native audio-only append and readback after reopening.
- **Variable cadence (Medium 6).** A nominal 60 fps report can hide irregular
  timestamps and undermine frame-to-time mapping. Probe a synthetic
  irregular-timestamp input and verify fixed-cadence detection before changing
  rounding.
- **Stale packet citations (Low 2).** A failed line citation can fall back to
  another repetition of the same quote. Test a stale explicit line with no
  timestamp or span ID and a single repeated hit in range. Require unresolved
  or explicitly weaker attribution.

## Legacy scratch recovery

If `index-notes` or `companion` refuses an old receipt or catalog binding,
keep the old scratch directory, `.autoclipper/director-fkb.json`, and
`director-fkb.json.bak` intact. Do not copy the old catalog over a new one or
edit its clock, coverage digest, or receipt by hand.

1. Run the current `prepare` using the approved session and microphone copies
   and the approved read-only media root.
2. In a separately approved Resolve window, run `build` in the new scratch
   directory. Keep the generated `manifest.json` and `build.json` together.
3. If the old zero-origin scratch review is under system temp and still has
   its original manifest, verified build receipt, and matching legacy catalog,
   run the following before creating any catalog in the new scratch directory.
   Zero-origin alone is insufficient: the old manifest must have no mode, the
   old catalog must have no mapping digest, and the original receipt, session,
   and clock must match. Otherwise the CLI reports that the old catalog does
   not match the receipted legacy review and source session. Keep that catalog
   as a reference and reselect ranges against a fresh verified catalog.

   ```powershell
   python -m autoclipper.director_poc index-notes <new-scratch> <approved-session-copy> --migrate-from <old-scratch>
   ```

   Migration verifies the session ID, frame rate, and recorded source placement
   of every note and revision. It preserves note and marker UUIDs, text, revision
   numbers, and history. Captures and playheads are rebound to the new review,
   with original capture metadata kept under `migrated_from`. The old snapshot
   stays untouched. Existing plans are copied without rebinding. A migrated
   source-backed plan therefore fails compilation with "EditPlan source mapping
   is unverified" until the operator saves a new selection against the verified
   catalog. A legacy receipt verifies recorded placement and provides no
   historical file-identity check.
4. Open the new companion with `companion <new-scratch> --persistent-notes`.
   For separate-root edit compilation, use the original approved session copy.
   The verified catalog supplies pinned media paths and, by default, verified
   scratch copies of the confirmations and optional metadata from `prepare`.
   The entire metadata directory is pinned, including every file and relative
   name; an explicitly passed directory must be identical. Explicit
   confirmations or metadata must match the pinned hashes. Catalogs created
   before input pinning require the same confirmations file used at `prepare`
   to be passed explicitly, plus the metadata directory if one was used.
   Source-backed compilation never searches the original session folder or
   its parent for omitted metadata.

If the old receipt is missing or any range cannot be verified, omit
`--migrate-from` to create a fresh catalog. Keep the old catalog as a reference
and reselect any ranges needed in the new verified review. Persistent capture
requires the new verified source mapping. A receipted, zero-origin legacy
review can still open in disposable mode.

## Offline synthetic harness

From the repository root, run:

```powershell
python -m unittest discover -s tests -p director_synthetic_harness.py -v
```

Use Python 3.10+ with `ffmpeg` (libx264 and AAC encoders) and `ffprobe` on
`PATH`. The harness skips if either executable is missing. It generates and
cleans up its own temporary MP4s, checks separate media roots and exact note
trims, and verifies that changed source files block both Director build paths.
Resolve is mocked. Normal unit-test discovery does not run this harness.
