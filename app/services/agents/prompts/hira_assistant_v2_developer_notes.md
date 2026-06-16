# HIRA Assistant v1.0.0 — Developer Notes

These notes are operator-facing only. They are **not** sent to the model. They live in this companion file so the prompt file stays clean of meta-information that would waste tokens at runtime and confuse the model.

Maps to: `hira_assistant_v2.md` (AgentPrompt.version = 2 in the database; semantic release = v1.0.0, first production release after the Phase 2 overhaul).

## Token budget

The system prompt is approximately 6,400 tokens including the four worked examples. The examples consume roughly 3,400 tokens.

**Do not remove examples.** During v0.7.0 development, removing examples to reduce cost caused:

- Generic hazard suggestions (safety boilerplate instead of activity-specific)
- Loss of multi-signal hazard detection (Example 4 capability)
- Control suggestions collapsing toward PPE-only instead of full hierarchy
- Hazard suggestion acceptance rate dropped from 64% to 41%

The examples teach the model what "good" looks like across the range from routine (Example 1) to high-hazard (Example 2) to sparse-input (Example 3) to multi-signal (Example 4). All four are necessary anchors.

## Runtime input contract

The prompt above is the static system prompt. At call time, the orchestrator appends a user message of this exact shape:

```
ACTIVITY:
{full activity JSON}

TEAM_ALREADY_ADDED:
{hazards and controls the team has entered}

LAYER_A_RULES_FINDINGS:
{
  "mandatory_hazards": ["hazard IDs that must be included"],
  "flags": [{"rule_id": "HA-XX", "note": "string"}]
}

CONTEXT:
{
  "similar_past_entries": [...],
  "area_incident_history": [...],
  "applicable_hazard_library": [...],
  "applicable_regulations": [...]
}

Assist with this assessment per your instructions. Output the JSON object only.
```

Keep activity, team additions, rules findings, and context as user-message data — not system prompt. This separation matters for prompt-injection resistance and clean audit trails.

The HIRA Phase 2 backend builds this user message in `app/services/agents/context_builders/` (TODO: HIRA-specific builder; until then the generic JSON-dump in `agent_service.run_invocation()` is used and the LLM tolerates that shape too).

## Output validation

After receiving the model response, the runtime must:

1. Strip whitespace, markdown fences, prose.
2. Parse JSON; **one retry** on parse failure with stricter suffix.
3. Validate against the schema in `agents/hira_assistant/output.py` (to add — currently freeform).
4. Verify every `hazard_master_id` exists in `applicable_hazard_library` — reject hallucinated IDs.
5. Verify every referenced past entry/incident ID exists in context.
6. Verify confidence values in [0.0, 1.0] for hazards, valid enums for risk calibration.
7. Verify `suggested_hazards` ≤ 8.
8. Verify control suggestions follow hierarchy structure.

On validation failure: one retry with stricter suffix, then return Layer-A-only suggestions (mandatory hazards from rules) with a note that AI suggestions were unavailable.

This validator lives in `app/services/agents/agent_service.py` (`_detect_hallucinations` covers ID checks). Schema validation against the HIRA-specific shape is a follow-up item.

## Acceptance tracking (feedback loop)

Every suggestion the agent makes is tracked through the UI:

- Hazard suggestions: accepted / modified / rejected
- Control suggestions: accepted / modified / rejected
- Risk calibration: heeded / ignored
- Pattern observations: acted on / dismissed

This data feeds prompt refinement at version boundaries. Weekly review of rejection patterns reveals systematic issues:

- Hazards consistently rejected → over-suggesting, refine relevance threshold
- Controls consistently modified → control specificity or feasibility issues
- Calibration consistently ignored → calibration logic needs work

**Do NOT feed this back into the agent during operation.** Refinement happens at version boundaries with human review.

## Prompt version change log

| Version  | Date     | Eval | Hazard Accept | Control Accept | Notes |
|----------|----------|------|---------------|----------------|-------|
| v0.1.0   | [draft]  | 54%  | 38%           | 29%            | Initial draft, generic suggestions |
| v0.4.0   | [draft]  | 71%  | 52%           | 41%            | Added control hierarchy enforcement |
| v0.7.0   | [draft]  | 66%  | 41%           | 38%            | Removed examples for cost — REGRESSION, restored |
| v0.9.0   | [draft]  | 83%  | 59%           | 48%            | Added multi-signal hazard examples |
| v0.9.5   | [draft]  | 86%  | 62%           | 51%            | Added calibration grounding in history |
| v1.0.0   | [release]| 88%  | 64%           | 53%            | Production release. Locked. |

Control acceptance is structurally lower than hazard acceptance because controls are highly site-specific — the team often modifies a suggested control to fit their actual equipment. A modified control is a successful suggestion (it gave the team a starting point), so track **accepted + modified** as the success metric: that combined rate is **78%** at v1.0.0.

## Change control

Any change to v1.0.0 requires:

1. New version number.
2. Full eval suite run with documented delta (see `tests/eval/hira_assistant_v2/`).
3. Acceptance rate monitoring in shadow before production cutover.
4. ADR if behavioral change is material.

## Known limitations of v1.0.0

- English-only activity descriptions. Hindi support planned for v1.1.0.
- No vision analysis of activity photos. Photo-based hazard detection planned for v2.0.0.
- Cannot reason about equipment condition (age, maintenance state) unless in description.
- Cross-plant memory limited to same tenant; no cross-tenant learning (correct for data isolation).
- Calibration weak when fewer than 3 similar past entries exist.
- Does not auto-generate Bowtie diagrams (suggests barriers when Bowtie context provided; full diagram is human-built).

## When to escalate to product team

Operational signals warranting product investigation (not silent prompt edits):

- Hazard acceptance rate drops below 55% in any 30-day window.
- Control accepted+modified rate drops below 70%.
- Any hallucination incident (hazard ID or past entry not in inputs) — immediate rollback.
- Calibration consistently ignored by teams (suggests calibration logic is off).
- Latency P95 drifts above 8s.
- Cost per invocation drifts above $0.06.

File a product ticket, develop v1.0.x in branch, run full eval, ship through normal release with shadow mode.

## Integration with Bowtie editor (Phase 2 cross-feature)

When the HIRA Assistant is invoked from within the Bowtie editor context, the input includes the Bowtie's current threats, top event, and consequences. In this mode the agent additionally suggests:

- Preventive barriers for identified threats
- Mitigative barriers for identified consequences
- Escalation factors that could degrade barriers

This is handled by a context flag in the request (`bowtie_context: true`) and an additional output section. The core hazard/control/calibration logic remains the same. Documented separately in the Bowtie integration spec.

Note: Bowtie is **not** in the current Phase 2 short scope shipped to production; this is here as forward-looking documentation for the Bowtie workstream when it lands.
