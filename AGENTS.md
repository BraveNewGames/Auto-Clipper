# Auto-Clipper Agent Repo Map

Verified against tracked source at baseline `4b4f512`.

## Purpose

Local Auto-Clipper for multiplayer playtest recordings. It discovers per-player recordings and SRTs, estimates or accepts offsets, builds a consolidated transcript and review packets, imports AI review JSON, and maps selected moments to LosslessCut CSVs. The next planned task is the Resolve Timeline Assembler Stage 1 POC, subject to its own approval.

## Entry points and dependencies

- Windows launcher: `run.bat` changes to the repo root and passes arguments through to `python -m autoclipper`.
- UI: `python -m autoclipper [session_folder] [--port N] [--no-browser]`. `autoclipper/__main__.py` parses arguments, optionally opens or creates a session, prints the bound URL, and opens the default browser unless suppressed.
- `autoclipper/server.py` serves the API and vanilla HTML, JS and CSS in `autoclipper/web/` using a threaded standard-library HTTP server on `127.0.0.1`. The default is 8765, with up to 20 consecutive ports tried if binding fails. Port 0 requests an ephemeral port.
- Headless: `python -m autoclipper.cli {scan,chain,map,export} FOLDER`. `cli.py` prints JSON and saves session state. Use each subcommand's `--help` for options.
- Python 3.10+ with standard-library imports. `media.py` optionally uses `ffprobe` for video durations and returns an unknown duration when probing fails. `samples/make_sample.py` uses `ffmpeg` for video generation. Both look on PATH, then use the configured Windows fallback at `C:/Program Files/FFMPEG/5.1.2/bin`.

## Source map

Module paths below are relative to `autoclipper/`.

- `scan.py` discovers recordings and SRTs, pairs matching stems, and groups ordered parts into player POVs. Supported video types include MP4 and MKV.
- `models.py` defines session, POV, part, review, event and suggestion records and timestamp helpers. `srt.py` parses subtitle cues and speaker labels.
- `autoalign.py` suggests and applies transcript-derived offsets. `alignment.py` sets offsets, chains parts and reports coverage. `mapping.py` converts between session and part-local time and maps ranges to covered source segments.
- `reconcile.py` combines aligned cues into a consolidated transcript and compacts it for review packets. `reviews.py` builds packets, imports reviewer JSON, resolves quote references and groups suggestions into events.
- `store.py` reads and writes `<session folder>/.autoclipper/session.json` and keeps a recent-session list under `%APPDATA%/AutoClipper/recent.json` (with a home-directory fallback).
- `losslesscut.py` writes reviewer-specific CSVs and accompanying instructions under `<session folder>/autoclipper_exports/` by default. Preserve LosslessCut compatibility during Stage 1.
- `samples/` contains the synthetic fixture generator and tracked SRT/JSON fixtures. To generate text fixtures without ffmpeg, run `python -m samples.make_sample <scratch_dir> --no-video`.
- `README.md` covers setup and workflow. `docs/DESIGN.md` covers architecture, formats and the headless sample walkthrough. `docs/policies/` contains the execution contract.

## Verification commands

- Development server from an isolated worktree: `python -m autoclipper --port 8865`.
- From the repository root: `python -m unittest discover -s tests`. At the bootstrap baseline, all 86 tests passed on Windows with Python 3.10.6. The suite uses synthetic fixtures and the HTTP tests bind an ephemeral port. Report the actual command and result for each new change.
- Git ignores `SampleData/`, `.autoclipper/`, `autoclipper_exports/`, generated MKVs under `samples/`, and Python bytecode and `__pycache__/`. Sample MP4s, `.pytest_cache/`, `.mypy_cache/`, and `.venv/` have no matching ignore rule in this worktree.

## Repo boundaries
- Git base branch: `master`; existing agent sessions use separate worktrees.
- `SampleData/` is **untracked real playtest material**: never stage, commit, copy into worktrees/CI, delete, rewrite, or inspect beyond owner-approved read-only test access.
- Original MKV, manually OBS-remuxed MP4, SRT, transcriber JSON and real Resolve projects are read-only.
- Use synthetic fixtures for committed tests. Stage 1 uses OBS-remuxed `.mp4` sources (not MKV), already confirmed playable in Resolve Studio with accessible mic tracks.
- Stage 1 should reuse existing offsets and `speaker_tracks` metadata where available. The current `Part` model does not expose `speaker_tracks`. Do not implement new synchronization or microphone detection.
- Stage 1 POC: use new scratch Resolve projects only. No broad Stage 2 implementation or Director Mode implementation without approval.

## Resource isolation
- Owner runs Auto-Clipper on **8765**. Developer instances should use `--port 8865` and distinct data/output locations. Check actual assigned port; do not send tests to the owner's server.
- Worktree isolation alone does **not** isolate external media, scratch projects, processes or network ports.

## Do not read by default
- `SampleData/`, real client video/audio/transcripts and any exports containing client content (unless owner has approved specific read-only tests).
- `.git/` internals, `.venv/`, `__pycache__/`, generated exports, binary media, installed packages and caches.

## Risky changes — independent review required
- Timeline/source time-coordinate conversions, synchronization and coverage mappings.
- Import/export formats, transcription metadata/schema, privacy, file deletion/overwriting, project settings or release code.

## Operating policy
- Testing policy: every worker follows `docs/DEVELOPMENT_AND_TESTING_POLICY.md`.
- Canonical approval and privacy contract: `docs/policies/AutoClipper_Orchestrator_Execution_Contract_v0.1.md` (read relevant sections when necessary).
- No unapproved substantive implementation; no merge/push/release or paid service/dependency without owner authorization.
- One focused task per worker; keep findings concise.
