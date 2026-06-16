"""Competency lifecycle states (spec §3.2) + training-driven transitions.

The 11-state machine is stored as a plain String on `CompetencyRecord.state`
(decision D2). This module is the single source of truth for the state set and
for deciding which state a record should hold given its *training* evidence —
the "training receiver" half of Phase B / decision D1 ("training feeds
competency"). Assessment, supervised-practice and expiry transitions live
elsewhere; this file only owns the training-driven decision so the receiver in
`competency_state.py` stays declarative.
"""

from __future__ import annotations

from typing import Any

# ── The 11 states (spec §3.2) ─────────────────────────────────────────
NOT_YET_ATTEMPTED = "not_yet_attempted"
IN_TRAINING = "in_training"
TRAINING_COMPLETE_PENDING_ASSESSMENT = "training_complete_pending_assessment"
UNDER_ASSESSMENT = "under_assessment"
VALIDATED_ACTIVE = "validated_active"
EXPIRING_SOON = "expiring_soon"
EXPIRED_IN_GRACE = "expired_in_grace"
EXPIRED_REVOKED = "expired_revoked"
LAPSED_REQUIRES_FULL_REDO = "lapsed_requires_full_redo"
SUSPENDED = "suspended"
SUPERSEDED = "superseded"

ALL_STATES = {
    NOT_YET_ATTEMPTED,
    IN_TRAINING,
    TRAINING_COMPLETE_PENDING_ASSESSMENT,
    UNDER_ASSESSMENT,
    VALIDATED_ACTIVE,
    EXPIRING_SOON,
    EXPIRED_IN_GRACE,
    EXPIRED_REVOKED,
    LAPSED_REQUIRES_FULL_REDO,
    SUSPENDED,
    SUPERSEDED,
}

# States set by an explicit human action / higher authority. The training
# receiver must never overwrite these — training evidence does not outrank a
# suspension, a supersession, or an assessment already in progress.
STICKY_STATES = {SUSPENDED, SUPERSEDED, UNDER_ASSESSMENT}

# States that already represent a competency validated by *some* method. The
# receiver won't push these back to "pending assessment" on a re-sync, but it
# may move them along the expiry axis when the underlying training lapses.
VALIDATED_STATES = {VALIDATED_ACTIVE, EXPIRING_SOON}


def requires_assessment_beyond_training(competency: Any) -> bool:
    """True when the competency has a *mandatory* validation method other than
    training completion — i.e. finishing the training leaves it
    `training_complete_pending_assessment` rather than `validated_active`.

    `Competency.validationMethods` is a JSON array of
    `{ method, isMandatory, ... }` (spec §3.1).
    """
    methods = competency.validationMethods or []
    training_methods = {"training_completion", "training_only", "training"}
    for m in methods:
        if not isinstance(m, dict):
            continue
        if not m.get("isMandatory"):
            continue
        if (m.get("method") or "").lower() not in training_methods:
            return True
    return False


def determine_training_state(
    *,
    required_count: int,
    satisfied_count: int,
    has_expiring: bool,
    assessment_required: bool,
    current_state: str,
    current_validation_method: str | None,
) -> str | None:
    """Decide the state a record should hold based on training evidence.

    The receiver is deliberately **additive and method-aware**: training is one
    of several validation inputs (D1), so this never revokes a competency that
    was validated by assessment / external proof / endorsement — it only
    advances records as training completes, and only expires records that were
    themselves validated *by* training. Returns the target state, or `None` to
    leave the record unchanged.
    """
    if current_state in STICKY_STATES:
        return None
    if required_count <= 0:
        # Not a training-fed competency — leave its state to the other
        # validation methods entirely.
        return None

    training_validated = current_validation_method == "training_completion"

    # All required training currently valid (ACTIVE / EXPIRING_SOON both count).
    if satisfied_count >= required_count:
        if current_state in VALIDATED_STATES:
            # Already valid — just keep the expiring/active flag in sync.
            if has_expiring and current_state != EXPIRING_SOON:
                return EXPIRING_SOON
            if not has_expiring and current_state == EXPIRING_SOON and training_validated:
                return VALIDATED_ACTIVE
            return None
        # Not yet validated → training completion advances it.
        if assessment_required:
            return TRAINING_COMPLETE_PENDING_ASSESSMENT
        return EXPIRING_SOON if has_expiring else VALIDATED_ACTIVE

    # Some — but not all — required training valid: only advance a fresh record.
    if satisfied_count > 0:
        return IN_TRAINING if current_state == NOT_YET_ATTEMPTED else None

    # No valid training. Only expire records that were validated *by* training;
    # leave assessment/external validations and untouched records alone.
    if current_state in VALIDATED_STATES and training_validated:
        return EXPIRED_REVOKED
    return None
