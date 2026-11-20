# Auto-Clipper — Agent Repo Map (bootstrap v0.1)

> Source: known repository layout and inspected command/output from owner, 2026-11-20. **Verify this map and the test command in the authorized one-time bootstrap task before treating it as complete.** Keep under 150 lines.

## Purpose
Local Python Auto-Clipper for multiplayer playtest recordings. Ingest per-player recordings and SRTs, align by existing transcript offsets, create consolidated review packets, import AI review JSON, and prepare selected moments. Next task: **Resolve Timeline Assembler Stage 1 POC only**; full Director Mode is later.

## Stack and entry points
- Windows; Python application with localhost UI; owner runs `run.bat` from the repository root.
- `autoclipper/__main__.py`: CLI, `--port`, startup URL.
- `autoclipper/server.py`: local HTTP server and application endpoints; default port 8765, automatic next-free fallback.
- `autoclipper/scan.py`: recording and SRT discovery; supports `.mp4` and `.mkv` among other extensions.
- `autoclipper/autoalign.py`: transcript-based existing synchronization; do not rewrite for Stage 1.
- `autoclipper/losslesscut.py`: current CSV-export compatibility; Stage 1 must not break it.
- `tests/`: unit and integration tests. `samples/`: generated/synthetic fixtures.
- `docs/`: project documentation (contents to verify).

## Commands (verify before institutionalizing)
- Run: `run.bat` (on Windows, owner currently uses port 8765).
- Development server: `python -m autoclipper --port 8865` (only from an isolated worktree).
- Candidate standard test command: `python -m unittest discover -s tests` (**verify in bootstrap task** and update if actual test runner differs).
- Do not claim tests passed unless you ran and reported them.

## Repo boundaries
- Git base branch: `master`; existing agent sessions use separate worktrees.
- `SampleData/` is **untracked real playtest material**: never stage, commit, copy into worktrees/CI, delete, rewrite, or inspect beyond owner-approved read-only test access.
- Original MKV, manually OBS-remuxed MP4, SRT, transcriber JSON and real Resolve projects are read-only.
- Use synthetic fixtures for committed tests. Stage 1 uses OBS-remuxed `.mp4` sources (not MKV), already confirmed playable in Resolve Studio with accessible mic tracks.
- Use existing offsets and `speaker_tracks` metadata; do not implement new synchronization or microphone detection.
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
- Canonical approval and privacy contract: `docs/policies/AutoClipper_Orchestrator_Execution_Contract_v0.1.md` (read relevant sections when necessary).
- No unapproved substantive implementation; no merge/push/release or paid service/dependency without owner authorization.
- One focused task per worker; keep findings concise. More project-specific file locations should be filled in by one authorized repo-map task.
