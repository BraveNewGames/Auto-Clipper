# Development and Testing Policy

## Scope and precedence

Every worker follows this policy for Auto-Clipper development, testing, review, and release reporting. It takes precedence over the testing and review rules in `docs/policies/AutoClipper_Orchestrator_Execution_Contract_v0.1.md` where they differ. All other contract rules, including approvals, privacy, and publishing, remain in force. Apply the tier that matches the risk of the change, within the approved task scope.

## Choose verification by risk

| Tier | Examples | Required verification |
| --- | --- | --- |
| **ROUTINE** | Documentation or tests-only changes; a small, isolated UI correction | Run focused checks for the changed behavior or documents. |
| **SIGNIFICANT** | A new CLI feature or a new workflow that joins existing components | Run relevant integration tests and obtain **one independent review**. |
| **HIGH-RISK** | Source alignment, time-coordinate or source-mapping code; file writes and destructive operations; Resolve project creation or other production-project safety changes | Thoroughly validate boundaries, failure paths, data integrity, and the affected workflow with safe synthetic or scratch inputs. Obtain independent review and record the evidence needed to judge residual risk. |

Keep verification proportional to the actual change. An **INCONCLUSIVE** result does not automatically block progress or trigger another investigation. Assess the remaining risk, available evidence, and consequences before deciding whether to proceed, limit scope, or investigate further.

Reuse previous evidence for unchanged behavior. Do not repeat live or manual checks without a concrete reason, such as a changed implementation, Resolve version, inputs, or environment. Record what was previously checked, what is unchanged, and why that evidence still applies.

The default verification budget is **one implementation/test pass and one independent review**, followed by **at most one remediation cycle for confirmed defects**. Any further cycle requires a concrete, written risk justification. Do not expand verification solely because a result was inconclusive.

## Classify findings and decide release impact

Classify every finding as exactly one of **PRODUCT DEFECT**, **TEST-HARNESS OR CRITERIA DEFECT**, **ENVIRONMENTAL ISSUE**, **INSUFFICIENT EVIDENCE**, or **KNOWN LIMITATION**. Preserve historical results. Never weaken acceptance criteria retroactively; declare any new criteria before a new run, and apply them only to that run and later runs.

Classify every outstanding issue as **BLOCKER**, **NON-BLOCKING**, or **DEFERRED**. A proposed BLOCKER needs specific evidence and a stated consequence. Report verified behavior, confirmed defects, material risks, and release impact concisely.

Prioritize feature delivery within approved scope. The current milestone is **v0.1.0: the manual Director Notes AI export/import bridge**. Avoid repeated validation of settled behavior when it does not address a concrete delivery risk.

## Non-negotiable safety constraints

- Never modify source recordings or production Resolve projects or preferences.
- Prevent data corruption or loss and invalid source mapping.
- Prevent unsafe file writes, including writes through links or junctions and writes outside approved locations.
- Validate AI output against note UUIDs, revisions, and verified source mapping before it becomes an edit plan. Never use an unverified AI edit plan.
