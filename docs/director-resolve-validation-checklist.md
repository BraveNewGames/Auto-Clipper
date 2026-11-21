# Director Mode Resolve validation checklist

Use this for Phase C follow-up on a new machine (currently NAS-DV), after a Resolve version upgrade, and after changes to the builder, receipt, companion, catalog, or edit-plan compiler. Repeat it for future Resolve regressions. This is an operator record, not authorization to run Resolve; complete live steps only in an owner-approved exclusive window. The command surfaces below match the Phase B base code and [Director POC runbook](../autoclipper/director_poc/README.md). Mark integration-dependent behavior **TO CONFIRM AFTER INTEGRATION**.

## Safety and run record

- [ ] Owner saves and closes any working project. Start each build from Project Manager or its verified empty default `Untitled Project` placeholder. Use **new disposable scratch projects only**; never open a production project for these tests.
- [ ] Keep original recordings unchanged. Use owner-approved copies of session JSON, mic confirmations, and optional transcriber metadata under system temp, with their needed filename/stem layout, plus an approved **read-only OBS-remuxed MP4 media root**. Keep logs, manifests, receipts, notes, and screenshots local to scratch; do not commit private paths or content.
- [ ] Use synthetic or disposable MP4s for changed-file and other negative controls. Never alter an approved source file. Run only one companion/writer per scratch catalog. Catalog writes must refuse links and junctions (**TO CONFIRM AFTER INTEGRATION**; exact message text is not established at the base commit).

| Run field | Value |
|---|---|
| Date; operator; machine name | |
| OS; GPU and driver | |
| Resolve Studio version and build; External scripting setting | |
| Python; FFmpeg; ffprobe versions | |
| Branch and commit | |
| Full unit suite; synthetic harness results | |
| Approved inputs (generic description only: temp copies, read-only MP4 root, synthetic controls) | |

## Offline preflight — no Resolve connection

- [ ] Record `git branch --show-current`, `git rev-parse HEAD`, `python --version`, `ffmpeg -version`, and `ffprobe -version`. Confirm Tk with `python -c "import tkinter"`.
- [ ] From the repository root run `python -m unittest discover -s tests` and `python -m unittest discover -s tests -p director_synthetic_harness.py -v`. Record counts, skips, and failures. The harness needs FFmpeg with libx264/AAC and ffprobe; it uses temporary MP4s and mocks Resolve.
- [ ] Set the installed SDK environment as in the runbook, adapting only to the approved installation:

  ```powershell
  $env:RESOLVE_SCRIPT_API = "$env:ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting"
  $env:RESOLVE_SCRIPT_LIB = "$env:ProgramFiles\Blackmagic Design\DaVinci Resolve\fusionscript.dll"
  $env:PYTHONPATH = "$env:RESOLVE_SCRIPT_API\Modules;$env:PYTHONPATH"
  python -c "import DaVinciResolveScript"
  ```

  Import verifies the scripting module under this Python; `scriptapp("Resolve")` attaches to a running Resolve and belongs only in the live window. Record the visible **Preferences > System > General > External scripting using > Local** setting during that window; do not change preferences without the owner.
- [ ] Prepare from approved temp copies. Add `--metadata $metadataCopies` only when supplied. `prepare` prints `Scratch directory: ...` and, on success, `Verified N planned segments at F fps`; retain that scratch path. Inspect `manifest.json` blockers and **stop before build** if any exist (`Manifest has blockers: ...`).

  ```powershell
  python -m autoclipper.director_poc prepare $sessionCopy $confirmationsCopy $mediaRoot --fps 60
  # Optional when approved: append --metadata $metadataCopies
  ```

## Required live acceptance tests

For each test, record the scratch evidence path, operator observation, and frame error where applicable. Use `python -m autoclipper.director_poc build $scratch` for a review build; it creates, saves, and reopens a timestamped `DirectorA Test` project and prints `Reopened ... / DirectorA REVIEW`. Use `python -m autoclipper.director_poc companion $scratch --hz 10` for disposable notes, or add `--persistent-notes` after indexing. The default hotkey is **Ctrl+Alt+Shift+F11**. Leave diagnostics **Off** and capture paused except in the optional playback test.

| ID | Steps | Pass criterion and evidence to record |
|---|---|---|
| A — review build/receipt | From Project Manager or the verified empty placeholder, build a new scratch review. Save and reopen it; inspect `manifest.json`, `build.json`, and the reopened project/timeline. | `build.json` has `reopened: true`; project and timeline names/unique IDs match the reopened objects and receipt identity; receipt segment digest/map matches the manifest; reopened `GetStartTimecode` and `GetStartFrame` read back `01:00:00:00` and its fps-derived frame. Record IDs, start readback, segment count, scratch receipt, and any UI discrepancy. |
| B — disposable companion | With the exact review active, set distinct **video** In/Out marks while paused, capture, and compare displayed In, exclusive Out, and duration with Resolve UI endpoints (default treats Resolve Out as inclusive). Clear marks; then set equal marks. Activate a different disposable timeline, then Project Manager, and try capture/jump/marker actions. | Distinct marks agree at frame precision; empty/equal marks refuse with `Set two distinct video In/Out marks` or `Not saved. Mark a range with different In and Out points.` Wrong timeline and Project Manager refuse visibly (`Activate the exact DirectorA review timeline` / `Open the exact DirectorA scratch project`). Record UI endpoints, saved capture, status, `rpc-probe.csv`, and screenshots. |
| C — persistent companion | Run `python -m autoclipper.director_poc index-notes $scratch $sessionCopy`, then `python -m autoclipper.director_poc companion $scratch --persistent-notes`. Save paused notes, close/reopen scratch project and companion. If an old **verified scratch** exists, separately test `index-notes <new-scratch> <approved-session-copy> --migrate-from <old-scratch>` before creating the new catalog. | IDs, half-open ranges, text/revisions, and timeline binding persist; migration preserves note/marker UUIDs and history while rebinding captures to the new review. Record before/after catalog IDs and ranges, receipts, and migration result. Preserve old catalog, backup, manifest, and receipt. |
| D — catalog UI | With persistent notes, inspect catalog rows and **Adjust range** old/new preview; scroll a long row horizontally. Add a marker to a titled note and one with only text; read an unmarked note. | Catalog and preview show readable timeline timecodes; horizontal scrollbar works; marker label uses title or text excerpt, while custom data retains the note UUID. Unmarked note reads `No marker found for this note.` Record UI screenshots, marker name/custom data, and the displayed message. |
| E — marker cycle | Add marker for a note; adjust its range, then attempt add before deleting the stale marker; delete by UUID and add at the new range. Read marker, jump, then delete by UUID. Try a different note at the same start. | No more than one marker for the UUID at any time; stale marker requires delete/re-add (`This note's marker is stale...`); occupied frame refuses (`This frame already has a marker. The note is still saved.`). Jump reports `Target ..., read back ...` with **zero frame error**, and settled Resolve UI timecode agrees. Record UUID, marker frame/duration at each step, API readback, settled UI timecode, and errors. |
| F — changed MP4 refusal | On synthetic/disposable media only, prepare a verified review and a verified edit-plan manifest; change a copied MP4's size or mtime after planning, before each `build_export` call. | Both build modes refuse **before `DaVinciResolveScript` import / `ImportMedia`** with `Verified Director source media changed.` Record before/after size and mtime, mode, exception, and proof no SDK import/media import occurred. Restore by making fresh disposable inputs, never by changing approved originals. |
| G — edit-plan build | Save an edit plan in the persistent FKB. Call `compile_fkb_edit_plan(session, knowledge_base, plan_id, revision_id=...)` through the approved offline driver, using the catalog's pinned prepare inputs by default, **without re-supplying confirmations or metadata**; then `build_export` its manifest in a new disposable project from Project Manager. No compile CLI flag exists at the base commit. | Compiler uses pinned inputs (**TO CONFIRM AFTER INTEGRATION**; exact refusal/success messages likewise). Compare ordered source segments and chosen POV/mic with manifest; check labeled slate visibility and exact frame duration; verify native audio-only append, and source/slate readback after save/reopen. Record plan/revision IDs, manifest, build readback, UI frames, and audio mapping. Keep generated slate media available to the project. |
| H — review gaps | Use approved synthetic/disposable coverage with an interior gap, a one-angle span, and a changing-angle span. Build and reopen. | Native blank spans remain at original session frame positions (no compression); available multicams have the planned angles before and after changes; behavior persists after reopen. Record manifest frame bounds, `build.json` segment rows, native timeline/UI screenshots, and deviations. |
| I — mic audition | For every available angle, audition its **owner-confirmed mic** in the disposable review and inspect expected Resolve channels/mapping. | Correct mic is audible on the expected stereo pair for each angle; record angle, confirmed stream/track, mapping readback, audible result, and any **INCONCLUSIVE** outcome explicitly. A prior run left one audition inconclusive. |

## Optional observations

- [ ] **Disconnect:** save the scratch project, exit Resolve, then record companion status, any timeout/stall, and `rpc-probe.csv` timing.
- [ ] **Playback capture:** capture once while playing; record keypress-to-response latency and whether the returned range reflects marks at keypress time. Discard experimental capture. A response after 750 ms is flagged for manual verification.
- [ ] **Keyboard Customization:** check whether a direct script binding exists; record what the installed UI exposes. Do not install a plugin or change preferences for this check.
- [ ] **Medium 4:** with a synthetic delayed or truncated mic stream, compare probed mic bounds with the native audio-only append and readback after reopen.
- [ ] **Medium 6:** probe synthetic irregular timestamps and record whether fixed-cadence assumptions hold.
- [ ] **Historical receipt observation:** in a disposable review only, edit one segment in place, then record what the companion detects and misses. Rebuild into a new disposable project before using that review for capture.

## Known limitations

- The build receipt binds the **historical saved/reopened segment map**. An in-place timeline edit may retain project ID, timeline ID, and start clock, so the companion can accept a receipt that no longer describes current content. Rebuild into a new disposable project after any manual change to a review used for capture.
- Exact playback-time range capture is unverified. Use paused capture.
- Re-review #2 Medium 1: a source can change between preflight and `ImportMedia`; size plus mtime also misses a same-size, same-mtime replacement.
- Low 1: the FKB is single-writer; concurrent initialization can clobber. Catalog link/junction write refusal is integration-dependent (**TO CONFIRM AFTER INTEGRATION**).
- Medium 4: video coverage is a proxy for mic availability. Medium 6: nominal frame rate is trusted.
- Low 2: a stale packet citation can redirect to another occurrence of the same quote.
- The tiny split-gap presentation decision remains open. Game-audio routing is deferred.

## Results and stop criteria

| Test ID | Result (PASS/FAIL/INCONCLUSIVE/SKIPPED) | Frame error | Evidence location (scratch only) | Notes |
|---|---|---:|---|---|
| A–I / optional ID | | | | |

**Stop/abort** on any manifest blocker, unexpected write outside scratch, wrong active project/timeline, changed or ambiguous media, unverified mic mapping, receipt/catalog mismatch, or unsafe Resolve behavior. Save only the disposable project if safe, close the companion, leave approved source material untouched, and record the exact blocker and last successful step. Do not turn a refusal into a pass by editing receipts, manifests, or catalog bindings.
