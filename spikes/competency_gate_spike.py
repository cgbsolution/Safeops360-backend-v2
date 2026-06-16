"""PHASE 0 SPIKE — D6 feasibility: additive competency gate with training fallback.

THROWAWAY. Not imported by app/. No call sites wired. Deleted/replaced in
Phase B by app/services/competency_state.py. Its only job is to prove the
one decision that carries the risk in the Skill Matrix plan (R1):

    "Can the PTW/HIRA gate consult competency STATE without breaking the
     existing training-only check for every seeded permit?"

The risk is entirely in the *decision logic*, not the DB plumbing — so this
spike isolates that logic as a pure function and asserts the four properties
the plan promises:

  P1  Flag OFF  -> result is byte-for-byte the existing training result.
                  (zero-impact default; this is how every plant starts.)
  P2  Flag ON, but no Competency maps to this permit type
                  -> still the existing training result. (purely additive)
  P3  Flag ON, a Competency maps, person is validated_active
                  -> gate passes, sourced from competency state.
  P4  Flag ON, a Competency maps, person SUSPENDED while their training
      certificate is still ACTIVE
                  -> gate BLOCKS. This is the headline: competency state is
                  not the same thing as training validity (spec §1, §4.5).

Run it (no pytest needed — stdlib only, no app/DB imports):

    .venv/Scripts/python.exe spikes/competency_gate_spike.py
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ─────────────────────────────────────────────────────────────────────────
# Stand-in types — deliberately mirror the REAL shapes so the production
# port is a rename, not a redesign:
#   - CompetencyCheckResult  == app/services/competency.py:69   (verbatim)
#   - CompetencyState string == spec §3.2 CompetencyRecord.state enum
# We re-declare them here only so the spike runs with zero imports.
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class Blocker:
    code: str
    message: str


@dataclass
class Warning:
    code: str
    message: str


@dataclass
class CompetencyCheckResult:
    """Mirror of app/services/competency.py CompetencyCheckResult."""

    ok: bool
    blockers: list[Blocker] = field(default_factory=list)
    warnings: list[Warning] = field(default_factory=list)
    satisfied: list[str] = field(default_factory=list)
    # Spike-only marker so the test can assert WHERE the verdict came from.
    source: str = "training"  # "training" | "competency"


# The closed set from spec §3.2 (will live in competency_states.py, D2).
ACTIVE_STATES = {"validated_active"}
WARN_STATES = {"expiring_soon"}
BLOCK_STATES = {
    "not_yet_attempted",
    "in_progress",
    "training_complete_pending_assessment",
    "assessment_complete_pending_supervised_practice",
    "expired_in_grace",
    "expired_revoked",
    "suspended",
    "lapsed_requires_full_redo",
    "superseded",
}

_BLOCK_REASON = {
    "not_yet_attempted": "competency not yet started",
    "in_progress": "competency still in development",
    "training_complete_pending_assessment": "training done but not yet assessed",
    "assessment_complete_pending_supervised_practice": "assessed but supervised practice incomplete",
    "expired_in_grace": "competency expired (in grace period — not valid for safety-critical work)",
    "expired_revoked": "competency expired and revoked",
    "suspended": "competency is SUSPENDED",
    "lapsed_requires_full_redo": "competency lapsed — full re-certification required",
    "superseded": "competency superseded by a newer one which the person does not hold",
}


@dataclass
class MappedCompetency:
    """A Competency whose enables_permit_types contains this permit type
    (spec §3.1). In production this is a row read from the Competency table."""

    competency_id: str
    name: str
    is_mandatory: bool  # mandatory competencies block; preferred only warn


# ─────────────────────────────────────────────────────────────────────────
# THE DECISION UNDER TEST — this is the whole spike.
# In production this becomes competency_state.check_competency_state_for_permit_type;
# the activation gate and ptw.py crew-add call it instead of the training check.
# ─────────────────────────────────────────────────────────────────────────


def decide_permit_competency(
    *,
    flag_enabled: bool,
    mapped_competencies: list[MappedCompetency],
    person_states: dict[str, str],  # competency_id -> CompetencyRecord.state
    training_result: CompetencyCheckResult,
) -> CompetencyCheckResult:
    """Additive competency gate with training fallback (plan D6).

    Backward-compat contract: when the flag is off OR no competency maps to
    this permit type, return the EXISTING training result unchanged. Only when
    a real competency mapping exists for an enabled plant do we evaluate live
    competency state — and a suspended/expired competency blocks even if the
    underlying training certificate is still active.
    """
    # P1 + P2: the additive guarantee. Nothing changes for existing permits.
    if not flag_enabled or not mapped_competencies:
        # Return the training result verbatim; tag it so callers/telemetry
        # can see the fallback path was taken.
        training_result.source = "training"
        return training_result

    # Flag on AND at least one competency governs this permit type:
    # competency state is now authoritative.
    result = CompetencyCheckResult(ok=True, source="competency")
    for mc in mapped_competencies:
        state = person_states.get(mc.competency_id)  # None == no record on file
        if state in ACTIVE_STATES:
            result.satisfied.append(mc.competency_id)
        elif state in WARN_STATES:
            result.satisfied.append(mc.competency_id)
            result.warnings.append(
                Warning(code="COMPETENCY_EXPIRING", message=f'"{mc.name}" competency expires soon.')
            )
        else:
            # Missing record, or any blocking state.
            if not mc.is_mandatory:
                result.warnings.append(
                    Warning(code="PREFERRED_GAP", message=f'Preferred competency "{mc.name}" not held.')
                )
                continue
            reason = _BLOCK_REASON.get(state, "no competency record on file")
            result.ok = False
            result.blockers.append(
                Blocker(code="COMPETENCY_NOT_MET", message=f'"{mc.name}": {reason}.')
            )
    return result


# ─────────────────────────────────────────────────────────────────────────
# Assertions — the four properties, run as a plain script (no pytest).
# ─────────────────────────────────────────────────────────────────────────


def _passing_training() -> CompetencyCheckResult:
    """What competency.py returns today when training is valid."""
    return CompetencyCheckResult(ok=True, satisfied=["HOT-WORK-TRAINING"])


def main() -> int:
    checks: list[tuple[str, bool]] = []

    # P1 — flag OFF: identical to training result, even with a suspended competency present.
    r = decide_permit_competency(
        flag_enabled=False,
        mapped_competencies=[MappedCompetency("CW-001", "GMAW Welder", True)],
        person_states={"CW-001": "suspended"},
        training_result=_passing_training(),
    )
    checks.append(("P1 flag OFF -> training verdict preserved (ok)", r.ok is True))
    checks.append(("P1 flag OFF -> source is training fallback", r.source == "training"))

    # P2 — flag ON but no competency maps to this permit type: still training.
    r = decide_permit_competency(
        flag_enabled=True,
        mapped_competencies=[],
        person_states={},
        training_result=_passing_training(),
    )
    checks.append(("P2 flag ON, no mapping -> training verdict preserved (ok)", r.ok is True))
    checks.append(("P2 flag ON, no mapping -> source is training fallback", r.source == "training"))

    # P3 — flag ON, mapped competency validated_active: pass via competency.
    r = decide_permit_competency(
        flag_enabled=True,
        mapped_competencies=[MappedCompetency("CW-001", "GMAW Welder", True)],
        person_states={"CW-001": "validated_active"},
        training_result=_passing_training(),
    )
    checks.append(("P3 validated_active -> ok", r.ok is True))
    checks.append(("P3 validated_active -> source is competency", r.source == "competency"))

    # P4 — THE HEADLINE. Training certificate ACTIVE, but competency SUSPENDED.
    # Today's training-only gate would PASS this welder. Competency state blocks.
    r = decide_permit_competency(
        flag_enabled=True,
        mapped_competencies=[MappedCompetency("CW-001", "GMAW Welder", True)],
        person_states={"CW-001": "suspended"},
        training_result=_passing_training(),  # training says OK
    )
    checks.append(("P4 suspended-but-training-active -> BLOCKED", r.ok is False))
    checks.append(("P4 block is attributed to competency", r.source == "competency"))
    checks.append(
        ("P4 blocker explains it is the competency, not training",
         any("SUSPENDED" in b.message for b in r.blockers)),
    )

    # Bonus — expiring_soon warns but does not block; expired_revoked blocks.
    r = decide_permit_competency(
        flag_enabled=True,
        mapped_competencies=[MappedCompetency("CW-001", "GMAW Welder", True)],
        person_states={"CW-001": "expiring_soon"},
        training_result=_passing_training(),
    )
    checks.append(("B1 expiring_soon -> ok with warning", r.ok is True and len(r.warnings) == 1))
    r = decide_permit_competency(
        flag_enabled=True,
        mapped_competencies=[MappedCompetency("CW-001", "GMAW Welder", True)],
        person_states={"CW-001": "expired_revoked"},
        training_result=_passing_training(),
    )
    checks.append(("B2 expired_revoked -> blocked", r.ok is False))

    # ── Report ──
    width = max(len(name) for name, _ in checks)
    all_ok = True
    print("\nD6 SPIKE — additive competency gate with training fallback\n" + "=" * (width + 8))
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name.ljust(width)}")
        all_ok = all_ok and ok
    print("=" * (width + 8))
    print(f"  {'ALL PROPERTIES HOLD' if all_ok else 'SPIKE FAILED'}  ({sum(1 for _, ok in checks if ok)}/{len(checks)})\n")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
