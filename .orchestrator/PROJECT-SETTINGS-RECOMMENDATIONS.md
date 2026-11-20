# Orchestrator Project Settings — Auto-Clipper (review before applying)

**Existing GUI Agents screenshot (2026-11-20)**
- Worker: Codex / GPT-6.2-Sol / Low / Approval Auto.
- Orchestrator: Claude Code / model not explicitly selected in screenshot / Approval Auto.
- Reviewer: Codex / GPT-6.2-Sol / Low / Approval Auto.

**Recommended minimal changes; DO NOT replace the entire project config**
- Keep installed Worker and Orchestrator harness choices. For cost control, start with a capable but economical Claude model if actually available; do not assume 'Select model' means anything is broken.
- **Default base branch:** set `master` explicitly (project General/settings, where exposed).
- **Worker rules file:** `.orchestrator/worker-rules.md` (do not point at AGENTS.md; Codex reads it separately).
- **Orchestrator rules file:** `.orchestrator/orchestrator-rules.md`.
- **Review policy:** preserve project review preferences until checked. If `autoReview` / `workersRequestReview` are disabled to reduce cost, manually trigger **independent review for production/risky changes** before merge. Reviewer Codex same as worker is a second run, but a different harness/model may give better independence when available.
- **Concurrency:** target **1 worker** for Stage 1; at most 2 for independent follow-up tasks. Project may not provide a global concurrency setting; this is an instruction policy.
- **postCreate:** leave current setting unchanged if present. **Do not install Claude's Node `setup.mjs` yet.** Repo screenshot doesn't show a Node/requirements dependency, and an unnecessary install step can fail every new worktree.
- **symlinks:** do not add `.env` absent a verified need. Do not symlink or copy `SampleData/`.
- **.worktreeinclude:** not needed now; don't install starter kit's wildcards (`.env.local`, `*.local.json`) because they may propagate secrets.
- **Agent approval 'Auto':** this relates to tool/agent approval behavior; **it does not override the explicit human merge/release and scope gates** in the policy. Review Auto carefully if an agent can execute commands that touch shared external files.

**Why this document is not project-config.json:** Orchestrator persists typed settings in its database; overwriting a whole config could destroy existing choices. Apply only confirmed fields via Project Settings. Existing sessions may retain old rules; new sessions pick up updated rule files.
