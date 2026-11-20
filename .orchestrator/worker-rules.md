# Auto-Clipper — Worker rules v0.1

- Follow **only** the approved task brief and the owner-approved Execution Contract (`docs/policies/AutoClipper_Orchestrator_Execution_Contract_v0.1.md`). A design-approved spec does not authorize building the entire feature.
- Work in your **own** worktree and branch. Never merge, push, release, delete worktrees, or modify the owner's `master` checkout without explicit approval.
- Start from `AGENTS.md`, task paths and focused searches. Avoid reading whole directories, large logs, or private data merely to orient yourself. Report unrelated issues without fixing them.
- For experiments, keep original MKV/MP4/SRT/JSON and production Resolve projects read-only; use scratch projects and synthetic committed fixtures. No raw client media uploads or CI artifacts.
- The owner may be running port **8765**. Use **8865** (or an isolated free port) for dev instances, and independent output/test directories; do not touch the owner's running process or active data.
- Do not change the transcriber, existing sync algorithm, file formats, cloud providers or dependencies unless your brief specifically authorizes it. OBS-remuxed MP4 is the Resolve input; no new remux pipeline.
- Verify relevant tests and report actual commands/results. Use an independent reviewer for production-intended changes and any risky-area change before proposing merge; do not assume all green tests imply authorization to merge.
- When scope expands, permission is unclear, or the only path requires modifying real source assets, **stop and explain the blocker**.
- Final report: concise outcome, changed files, verified tests, open limitations, next owner decision. Avoid dumping large logs into the long-lived orchestrator context.
