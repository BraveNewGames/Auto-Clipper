# One-time repo-map verification (give to ONE fresh worker)

**Authorization:** Documentation/bootstrap ONLY. Do not change application behavior, tests, transcriber, agent rules, or metadata formats. Do not touch `SampleData/`.

**Goal:** Verify and improve the supplied `AGENTS.md` using the actual Auto-Clipper repository. Keep it concise and accurate for future Codex and Claude workers.

**Instructions:**
1. Inspect tracked Python files, `README.md`, `run.bat`, tests, and relevant docs with targeted searches. Avoid large generated/client files and binary assets.
2. Correct module/folder descriptions and entry points. Determine runtime dependencies accurately; don't introduce an install process unless needed.
3. Run the actual applicable test command. Record it in AGENTS.md; remove 'candidate' wording once verified. If tests cannot run, document exactly why instead of fabricating results.
4. Check whether `SampleData/` and generated output folders are safely ignored by Git; **do not modify `.gitignore` without a separate approval**. Report any issue.
5. Keep AGENTS.md under 150 lines. Do not duplicate the execution contract. Modify **AGENTS.md only**, and report findings, tests, and unresolved items.

**Review:** A human checks the resulting repo map before merging. Stop after this task.
