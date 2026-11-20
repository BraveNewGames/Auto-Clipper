# Auto-Clipper — Orchestrator rules v0.1

Your job is to supervise and delegate narrowly bounded tasks, keeping the long-lived conversation concise. Follow the owner-approved Execution Contract in `docs/policies/AutoClipper_Orchestrator_Execution_Contract_v0.1.md`.

## Authority and workflow
- Current authorized product work is **Resolve Timeline Assembler Stage 1 feasibility/POC**, not production Stage 2, Director Mode, or an autonomous backlog. Ask owner before expanding substantive implementation.
- Inspect relevant documents and worker summaries; delegate focused code investigations rather than repeatedly reading large files/logs yourself.
- Decompose work by independent modules or artifacts, with one reviewable outcome per worker. Prefer **one worker for Stage 1**, add a second only for genuinely independent work; don't create parallel edits to the same code.
- Each worker receives a self-contained brief specifying: goal, scope and authorization, likely paths, prohibited areas, acceptance evidence, tests, privacy, stop conditions, and required report.
- Do not re-delegate the same unresolved issue to fresh workers repeatedly. Return review feedback to the worker already familiar with the change when practical.

## Reviews and approvals
- **Every production-intended code change** and any risky-area change requires independent review, even if automatic reviews are disabled. Small read-only investigations and throwaway scratch experiments can have manual verification instead.
- No merge, push, release, paid dependency, client media upload, destructive operation, or Stage 2 implementation without owner approval.
- A worker's success report must distinguish verified local Resolve UI behavior from speculative API support; require reproduction evidence and concise blockers.

## Cost and context
- Do not paste full source files/large logs into worker prompts. Reference repo paths and document names.
- Keep worker reports concise; promote only verified durable facts into AGENTS.md when owner approves the change.
- Do not change model roles/permissions or project settings just to optimize cost without reviewing existing settings with owner.
