# Auto-Clipper — Orchestrator Execution and Release Contract

**Policy | v0.1 — November 20, 2026**  
**Document role:** Cross-cutting development policy (historically named `06-ORCHESTRATOR-EXECUTION-CONTRACT.md` / “Milestone 0”). **This is not Milestone 6 of the product.**  
**Status:** **OWNER-APPROVED POLICY.** This document defines permitted actions; it does not assert that Orchestrator's current configuration, repository protections, or review automation have already been verified.  
**Immediate process pilot:** **Resolve Timeline Assembler — Stage 1 technical proof of concept**, under its separately scoped assignment v0.1. The formerly proposed 60-clip preview-bug pilot is no longer the first execution-contract pilot; that issue remains independently trackable under Stabilization.

## 1. Goal and authority

Turn narrowly authorized tasks into reproducible, reviewed changes without risking the existing working Auto-Clipper MVP, private client footage, or developer worktrees. **A specification being owner-approved does not itself authorize implementation of the entire specification.**

### 1.1 Permission levels

| Activity | Permission |
|---|---|
| Inspect code, metadata schemas, existing tests, relevant documentation, installed Resolve Studio version, and available APIs | Permitted without separate approval, using authorized local/project access. |
| Run small, isolated, reversible feasibility experiments (including disposable scripts/test timelines in a scratch project) | Permitted without separate approval **within approved task boundaries**, with read-only treatment of all source content. |
| Prepare an engineering proposal, test fixtures, a report, and a proof-of-concept branch/worktree | Permitted within the Stage 1 technical investigation. A disposable POC is not authorization to integrate a production feature. |
| Implement production features, change application workflows, or expand beyond the approved task | **Requires explicit owner authorization of that implementation scope.** |
| Add a paid API/dependency, authorize new expenses, undertake architectural rewrites, upload raw client media, or perform destructive operations | **Requires explicit owner authorization.** |
| Merge, deploy, distribute, overwrite existing production/source projects, or delete active worktrees | **Requires explicit owner authorization.** |

If the next step crosses an approval boundary, stop, document the requested action and reason, and ask the owner. An instruction to *investigate* is not an instruction to *implement everything discovered*.

## 2. Recommended repository controls

Maintain or propose the following files in the actual repository **after inspecting its existing conventions**; do not blindly create redundant copies:

- `AGENTS.md` — scope rules, allowed directories, build and test commands, privacy constraints, forbidden operations, required review steps.
- `docs/roadmap/` — canonical active versions of numbered milestone specifications; historical versions retained rather than overwritten.
- `docs/TEST_RESULTS.md` — reproducible command, expected/actual output, artifact locations, environmental limitations, and test evidence.
- `docs/DECISIONS.md` — accepted, rejected, and deferred technical choices with supporting evidence.

Source documents for the current investigation:

- `AutoClipper_Resolve_Timeline_Assembler_Spec_v0.2.md` — **owner-approved functional design**, with Stage 1 and Stage 2 separated.
- `02-METADATA-AND-SYNC.md` — source metadata and synchronization workstream; synchronization improvements are **not** in the Stage 1 assignment.
- `AutoClipper_Director_Mode_Spec_v0.6.md` — later milestone that will reuse the assembler; **not** authorized for implementation by this contract.

## 3. Required task intake

Every task must identify:

1. Owner-approved objective and **specific authorization level** (read-only research, isolated POC, or production implementation).
2. Inputs, dependencies, available files, and assumptions that need verification.
3. In-scope tasks, expected code/artifact locations **after repo inspection**, and explicit out-of-scope list.
4. Observable acceptance criteria, failure/stop conditions, tests, and evidence required.
5. Supported environment and compatibility constraints.
6. Privacy, financial, media, filesystem, and repository impacts.
7. Who may approve implementation, merging, deployment, or new expenses.

Do not infer permission to start a broader milestone from an ambitious roadmap or a successful POC.

## 4. Preferred agent execution loop

1. **Inspect first.** Read the current repository state, project architecture, active worktrees, relevant instructions, and tests. Check task dependencies and permissions.
2. **Isolate work.** Use a scratch Resolve project and an isolated feature branch/worktree for POC code, if code is necessary. Never work directly in a real production/client project.
3. **Choose the narrowest demonstrated approach.** Do not replace working transcription, alignment, or media workflows; do not conduct a large comparative survey unless the first practical approach fails.
4. **Test reproducibly.** Unit tests for time-coordinate conversions and boundary conditions; actual local Resolve test(s) where available. Synthetic fixtures go into the repo; real recordings stay outside it.
5. **Inspect the diff and outputs.** No private footage, unredacted transcripts, SRT/JSON payloads, API tokens, personal absolute paths, machine-specific artifacts, or exported media in a commit or PR.
6. **Independent review.** For production-intended code, seek a separate coding reviewer; fix findings and rerun relevant tests. Independent *editorial AI reviewers* are a separate concept and do not substitute for code review.
7. **Report to owner.** Summarize what demonstrably works, what failed or remains unverified, exact tests and manual verification instructions, proposed next step, and any branch/PR link. Do not claim a task is done merely because a script ran.
8. **Stop at the gate.** Leave POC work isolated until the owner authorizes subsequent implementation and merging.

A PR may be prepared for review when supported; **no auto-merge**, even when automated checks pass. Cap repeated repair attempts and record blockers rather than creating an uncontrolled agent loop.

## 5. Non-negotiable safety and privacy rules

- Original MKVs, manually OBS-remuxed MP4s, client gameplay, SRTs, transcriber JSON, and user projects are **read-only source inputs**. Produce only new scratch files/projects/timelines for experiments.
- Synthetic/anonymized test fixtures may be committed. Real client recordings, private transcript contents, credentials, or machine-specific personal paths must **not** be added to the repository or CI artifacts.
- CI must not upload raw source/client material or test artifacts containing it. **Cloud AI processing of transcripts, Director Notes, and relevant metadata has been approved for the product workflow**; this does not grant coding agents carte blanche to send unrelated local content, nor allow raw video/audio upload without specific authorization.
- Do not modify the external transcriber, its JSON schema, transcription pipeline, or microphone detection algorithm without explicit approval. Consume observed JSON; no hard-coded universal OBS microphone track number.
- The owner has confirmed that OBS remuxes MKV into Resolve-usable MP4 while preserving accessible audio. **Reuse manually prepared MP4s**; don't build alternative remux tooling in Stage 1.
- Reuse existing Auto-Clipper session offsets, coverage, and recommendations. **Improving cross-POV synchronization is a separate milestone**; no fabricated two-frame sync guarantee.
- Do not delete or clean active agent worktrees or branches until outstanding commits and review states are secured and the owner approves cleanup.
- No unauthorized paid dependencies, large downloads, external distribution, destructive filesystem actions, or edits to the user's sole Resolve project.
- Stop on missing/ambiguous media, contradictory metadata, unverified microphone identities, or unsafe API behavior; report rather than silently guessing.

## 6. Initial execution-contract pilot: Timeline Assembler Stage 1

**Approved pilot assignment:** `AutoClipper_Timeline_Assembler_Stage1_Assignment_v0.1.md`.

The pilot is an **isolated technical feasibility exercise**, not Stage 2 feature delivery. Use a known approximately 30-second Nightbrats event, two already OBS-remuxed MP4 POVs, and Auto-Clipper's existing alignment metadata to:

- prove correct reference of source-local ranges and editable Resolve timeline placement;
- demonstrate and characterize native multicam creation/angle switching *if supported by the verified local workflow*, or document a precise blocker and smallest next experiment;
- perform a limited check of imported, accessible microphone audio;
- give deterministic evidence for frame/timebase placement using the provided offsets;
- deliver findings, scratch artifacts, tests, and a proposed Stage 2 approach **without implementing the complete Stage 2 feature**.

The former 60-clip preview issue remains part of the Stabilization workstream if still reproducible; it is not a gate for this pilot.

**Pilot process acceptance:** A bounded POC has reproducible evidence, no source modifications/private commits, an independently reviewable diff if code was produced, a clearly documented blocker if needed, and a human decision gate before further work or merge.

## 7. Later unattended/night automation — not authorized here

Running agents unattended requires a separate explicit task queue and owner approval for schedule, host availability, paid-model budget, concurrency, stop/timeout limits, safe branch/worktree handling, monitoring and morning report. Routing and cost restrictions must be enforced by Orchestrator configuration, not merely prompt wording.

## 8. Revision history and sign-off

| Version | Date | Authority | Changes |
|---|---|---|---|
| Original unversioned | 2026-11-19 | Draft, not approved | Initial contract, proposed 60-clip preview-bug pilot and tentative merge policy. |
| **v0.1** | **2026-11-20** | **Owner-approved policy** | Explicit autonomy for isolated technical research/POCs; explicit human authorization before substantive production implementation and merge/release; initial pilot changed to Resolve Timeline Assembler Stage 1; aligned privacy and OBS-remuxed MP4 rules with approved specifications. |

**Approval interpretation:** The owner approved the above policy changes and requested preparation of the Stage 1 assignment. This policy does **not** assert that Studio integration or Stage 1 tests have been performed, nor authorize a broad autonomous development run.
