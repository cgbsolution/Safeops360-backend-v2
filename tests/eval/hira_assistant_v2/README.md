# HIRA Assistant v2 — Eval Suite

Reference cases for HIRA Phase 2 prompt v2 (Layer A + `analyze_full_entry` + `compare_versions` task types).

## Pass criteria (per Phase 2 spec §2.8)

- Aggregate ≥ 85% of expectations met across all cases.
- Zero hallucinated hazard references.
- Zero hallucinated past entries.

## Case coverage

| Case ID  | Category | Tests |
|----------|----------|-------|
| HA2-001 | Clear hazard hot work | Layer A HA-04/HA-05/HA-09/HA-10 fire + electrical + height combination |
| HA2-002 | Confined space | HA-01 + contractor competency floor |
| HA2-003 | Multi-signal chemical+height | HA-02/HA-03/HA-05/HA-08/HA-09 plus LLM should surface acid+fall interaction |
| HA2-004 | Routine high-frequency baseline | HA-07 raises likelihood floor to ≥4 |
| HA2-005 | Sparse information | LLM must NOT over-suggest; surface "too sparse" reviewer note |
| HA2-006 | Lifting operation | HA-02 + lifting plan + contractor competency |
| HA2-007 | HV electrical | HA-04 + arc flash + LOTO reviewer note |
| HA2-008 | Duplicate hazards already present | LLM must not re-suggest hazards in `existingEntryHazards` |
| HA2-009 | No clear physical hazards (office) | Low confidence; surfaces ergonomic/psychosocial only |
| HA2-010 | `compare_versions` drift detection | Surface removed engineering control + unjustified residual increase |

## Status

Scaffold + 10 reference cases. Full 30-case spec target deferred to follow-up — these 10 cover all task types and the 10 Layer A rule classes.

## Running

(Harness implementation pending — currently a manual review pack for prompt engineering.)
