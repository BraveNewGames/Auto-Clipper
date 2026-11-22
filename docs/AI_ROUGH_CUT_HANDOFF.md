# AI Rough Cut — Orchestrator handoff

Status: **planning handoff only.** Implementation starts only when the owner gives an explicit go-ahead to the new session.

## 1. Repository state

| Item | Value |
|---|---|
| Release line (local, **unpushed**) | `director/stabilize-rc1` @ `92b40c557f0c67d9062ca582a1479469c960bc89`. This is a no-ff merge of `fix/group-reference-audio-readback` (`9fc25ed`) into `bb1665e`. |
| `bb1665e` contains | Director Mode phases A–C, the stabilization fixes (link-safe catalog writes, pinned prepare inputs), MP4-only + `--session-root` support, the Resolve validation checklist, and `docs/DEVELOPMENT_AND_TESTING_POLICY.md`. |
| Pushed branches (non-master) | `director/stabilize-re-review-2` (`968349f`), `feature/group-reference-audio` (`7469813`), `fix/group-reference-audio-readback` (`9fc25ed`). The fix branch is what the owner currently runs in production on the workstation. |
| `master` | `d2dd1d2`. Untouched. Director Mode has not been merged to master. |
| Tests at `92b40c5` | `python -m unittest discover -s tests`: 234 pass. `python -m unittest discover -s tests -p director_synthetic_harness.py -v`: 1 pass. |

Base all rough-cut work on a new feature branch from `92b40c5` (suggested name: `feature/ai-rough-cut`). Verify ancestry first. Never reset or overwrite newer local work.

## 2. Goal

The owner pastes a prompt (session transcript + existing AI Review suggestions + verified footage coverage) into ChatGPT or Claude. The owner pastes the AI's answer back: an **ordered list of source clips** with in/out points, POV choice and audio choice, plus optional placeholders. Auto-Clipper validates it strictly, then builds it as an editable timeline in a **new** DaVinci Resolve project.

- Manual copy/paste only. No MCP, API calls or paid services.
- No Director Notes are required.

## 3. Existing functionality to reuse (read before designing)

- **Edit-plan compiler.** `autoclipper/resolve_manifest.py` `compile_edit_plan(session, plan, transcripts_dir, confirmations)`.
  - It works on the **main Auto-Clipper session** (`<session>/.autoclipper/session.json`, `mic_confirmations.json`, `_Transcripts`). It needs **no** Director catalog, notes or scratch folder.
  - Input: a projection `{plan_id, session_id, fps, revision_id, revision_history, entries[]}`.
  - `source` entries take half-open session frames `session_in` / `session_out_exclusive`, plus:
    - `pov_id`: `null` means a multicam of all covering angles with Source-mode mic switching; a value means a single POV;
    - `audio_pov_id`: optional; one player's mic, appended audio-only.
  - `placeholder` entries take `provisional_frames`.
  - It splits at coverage changes, emits labelled slates for uncovered ranges, and records `source_mapping` + `source_mapping_digest`. Mode: `director_edit_plan`.
- **Builder.** `autoclipper/resolve_builder.py` `build_export(manifest, project_name=...)` in edit-plan mode:
  - creates only a **new** project;
  - requires Project Manager or an empty Untitled Project;
  - verifies source media identity before the SDK import;
  - renders slates with FFmpeg;
  - saves, reopens and reads back.
  - Validated live on Resolve Studio 21.2.0.6 (checklist rows G and J).
- **Group Reference Audio** (separate stereo A2 `GROUP REFERENCE AUDIO` track, from a verified Track1 mix) is currently applied **only** to Stage 2 Keep exports; the builder skips it for Director modes. A Resolve 21.2.0.6 getter quirk on source start is handled by an FCP 7 XML cross-check in `read_export`.
- **AI Review workflow.** `autoclipper/reviews.py`: `build_review_packet` (transcript lines with IDs, timestamps, speakers), `REVIEW_JSON_SCHEMA`, and tolerant JSON extraction (`_FENCE`, `_fixups`, `_decode_any`). Suggestions and Keep decisions live in `session.json`.
- **Web UI.** `autoclipper/server.py` + `autoclipper/web/app.js`.
  - The AI Reviews tab supplies the copy/paste pattern.
  - The Export tab's Export to Resolve supplies the job pattern: `/api/resolve/preview`, `/api/resolve/export`, `_run_resolve_job` → `run_builder` subprocess, a single job slot, `/api/resolve/status` polling.
  - Tests: `tests/test_resolve_export_ui.py` and `tests/test_ui_server.py`.

## 4. Proposed scope (the owner's earlier assessment; confirm the open decisions at kickoff)

Target the smallest working AI rough cut, independent of the Director Notes AI bridge.

1. **Core module** (e.g. `autoclipper/rough_cut.py`).
   - **Export prompt.** Reuse the AI Review packet transcript format. Add:
     - Keep and AI Review suggestions (IDs, ranges, titles);
     - per-POV verified video and mic coverage;
     - a strict, versioned response schema;
     - an export ID / prompt hash and the session fingerprint and source-mapping digest.
   - **Import.** Tolerant extraction, then strict validation:
     - ranges anchored to transcript line IDs or suggestion IDs with bounded padding; exact frames only with a matching timecode cross-check;
     - ranges inside the session;
     - the chosen POV has verified video, and the chosen mic has verified mic coverage, across the whole range. These are errors, never silent slates;
     - POVs exist; quotes are verbatim;
     - the session is unchanged since export, and the digest matches;
     - limits;
     - a final **compile dry run**.
   - **All-or-nothing.** Any error saves nothing and produces a repair prompt.
   - **Storage.** Prompt, response, validated plan, manifest and report go under `<session>/autoclipper_exports/rough-cuts/<timestamp>/`. **No `session.json` model changes**, so the owner's fallback install must still read sessions.
2. **Web UI.** A small "Rough cut (AI)" panel in the existing **Export** tab: copy prompt, paste answer, validation report with repair prompt, Build into new Resolve project.
   - Reuse the Export to Resolve job slot and run the builder unchanged.
   - Keep existing routes and behavior unchanged.
   - Optional CLI adapters for testing.
3. **Estimate.** About 4–4.5 worker-days, including one review, one remediation cycle and one live build. About 3 days if CLI-only first.

**Open decisions — ask the owner at kickoff:**
- (a) Core plus the Export-tab panel, or CLI-only first. Recommended: the panel.
- (b) Add Group Reference Audio to rough-cut builds (about +0.5 d). This means extending the builder's reference track to edit-plan mode.
- (c) Allow the AI to pick a fixed `audio_pov_id`. Recommended: allow it, with the default `null` (multicam Source-mode mics).

**Reuse for the later Director Notes AI bridge.** The extractor, validator rules, compile gate, repair prompt and build wiring should be shared, so that the bridge (approved plan rev 3, on hold) only adds the Director-specific parts: scratch binding, note anchors and revisions, catalog persistence, and the single-writer guard.

## 5. Read first

- `AGENTS.md`, `CLAUDE.md`, `docs/DEVELOPMENT_AND_TESTING_POLICY.md`
- `docs/policies/AutoClipper_Orchestrator_Execution_Contract_v0.1.md` (approvals, privacy; its testing rules are superseded by the policy)
- `README.md`, sections "Export to Resolve" and "Where files go"
- `autoclipper/director_poc/README.md`, sections "EditPlan assembler adapter" and "Known limitations"
- `docs/director-resolve-validation-checklist.md`, rows G, J and the waveform method
- `docs/DIRECTOR_NOTES.md`, `docs/DIRECTOR_RECOMMENDATIONS.md`
- Code: `resolve_manifest.py` (`compile_edit_plan`, `_review_coverage`, `_mic`, `build_manifest`), `resolve_builder.py` (`build_export`, `read_export`, `run_builder` usage), `reviews.py`, `server.py` (resolve job flow), `web/app.js` (AI Reviews and Export tabs), `media.py` (`resolve_recording`)

## 6. Testing policy (summary of `docs/DEVELOPMENT_AND_TESTING_POLICY.md`)

- This is a **SIGNIFICANT** feature with **HIGH-RISK** parts: timestamp→frame resolution, source mapping, file writes, Resolve project creation. Required:
  - focused unit and server/UI tests (ephemeral port, stubbed builder);
  - **one** independent review;
  - **one** live Resolve build.
- Budget: one implementation pass, one review, and at most one remediation cycle. Further cycles need a written risk justification.
- INCONCLUSIVE never automatically blocks or triggers more investigation.
- Reuse prior evidence: the multicam and mic routing, the edit-plan builder and Group Reference Audio are validated. Do not re-test them unless their code changes.
- Classify findings with the five policy classes, and classify issues as BLOCKER, NON-BLOCKING or DEFERRED.
- Proposed BLOCKERS for this feature:
  - an unverified AI suggestion becomes a buildable plan;
  - wrong source mapping, timestamps or frame ranges;
  - data corruption or unsafe writes;
  - any modification of source recordings;
  - changes to existing Resolve projects;
  - actual regressions in AI Review, Export to Resolve or Group Reference Audio;
  - confirmed privacy or security defects.

## 7. Safety constraints and known limitations

- **Never** modify source recordings, SRTs or `_Transcripts`. Never modify existing or production Resolve projects or preferences. Build only into new `AutoClipper Export …` or test-prefixed projects, from Project Manager.
- The owner's approved test dataset is **read-only**. The owner supplies its location. Do not open it in the web UI, because that writes `.autoclipper` state. Use in-memory loads and scratch outputs under system temp, with a before/after integrity check.
- Committed docs and tests must contain no client names, private paths or transcript text. Use synthetic fixtures.
- Live Resolve work runs only on the development machine's **local** Resolve Studio 21.2.0.6, with the owner's approval for the session. Never contact the owner's workstation.
- Known limitations:
  - mic availability is inferred from video coverage;
  - the nominal frame rate is trusted;
  - game audio is unidentified;
  - the Resolve SDK cannot read multicam internals (use FCPXML export for structural checks);
  - Resolve 21.2.0.6's `GetSourceStartFrame` can floor a sub-frame start (already handled for reference audio);
  - the formal cross-POV audio-only waveform test is recorded as INCONCLUSIVE (a test-criteria issue), with structural evidence passing.
- Git: workers' upstream may be `origin/master`. **Never** run a bare `git push`. Push only with an explicit refspec, and only with owner approval. Workers may need sandbox escalation to write their linked worktree's Git metadata.

## 8. Authorized vs on hold

| Authorized | On hold or needs approval |
|---|---|
| Creating `feature/ai-rough-cut` from `92b40c5` and implementing the scope above **after** the owner's explicit kickoff go-ahead and answers to (a)–(c) | Pushing any branch, merging to master, release tags, release candidates |
| Focused tests, one independent review, one remediation cycle | Director Notes AI bridge implementation (plan rev 3 approved in principle; D1/D2 accepted; on hold) |
| One live Resolve build on the development machine with owner approval | Re-testing validated multicam, mic routing or Group Reference Audio behavior |
| | Companion UI and hotkey checks: pending, owner-run when convenient |
| | Deferred items: media-swap race, concurrent catalog writers, mic coverage inference, variable frame cadence, stale packet citations, split-gap presentation, game-audio routing |
