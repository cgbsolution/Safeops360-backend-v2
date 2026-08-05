"""Training & Competency Engine — the deterministic rule core (spec §B).

PURE functions only: no DB, no ORM, no I/O, no LLM. Every rule takes already-
fetched data + a config view and returns AssignmentDrafts + ReviewFlags. This is
what lets all four rule types be unit-tested independently of the UI and DB
(tests/test_training_rules.py) — the service layer (service.py) does the queries
and persistence, then calls these.

Four rule types (spec §B):
  1. threshold_rule  — N mapped records at a site/dept in a rolling window
  2. severity_rule   — SIF-potential / >= threshold → immediate individual assign
  3. role-scoping    — apply_role_scoping: NEVER blanket; resolve to role-required
  4. recert_rule     — competency expiry approaching → refresher

Design invariants enforced here (spec business rules):
  • The engine can NEVER produce a blanket site-wide assignment — every draft's
    worker must come from the set whose role requires the competency
    (apply_role_scoping). Scoping failure yields a ReviewFlag, never an assignment.
  • Severity-rule drafts are isMandatory + non-dismissible + escalationFlag.
  • All thresholds/windows come from the RuleConfigView (never hardcoded).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Severity ordering shared across rules. Incident.severity / NearMiss.riskLevel /
# Observation.severity are all constrained to these labels.
_SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def severity_rank(sev: str | None) -> int:
    return _SEVERITY_RANK.get((sev or "").upper(), 0)


# ── config view (rules never touch the ORM object) ───────────────────────────
@dataclass
class RuleConfigView:
    thresholdCount: int = 3
    thresholdWindowDays: int = 90
    severitySifImmediate: bool = True
    severityThreshold: str = "HIGH"
    recertWindowDays: int = 30
    assignmentDueDays: int = 30
    correlationWindowDays: int = 90
    # person-risk analytics (repeat-involvement flag)
    personFlagThreshold: int = 2
    personFlagWindowDays: int = 365
    personRiskElevated: int = 3
    personRiskHigh: int = 6
    personRiskCritical: int = 10

    @classmethod
    def defaults(cls) -> "RuleConfigView":
        return cls()


# ── small value objects passed into the rules ────────────────────────────────
@dataclass
class WorkerRef:
    userId: str
    plantId: str | None = None
    department: str | None = None


@dataclass
class RecordDueRef:
    personUserId: str
    competencyId: str
    plantId: str
    dueDate: object | None = None  # datetime; kept opaque so the rule stays pure


# ── outputs ──────────────────────────────────────────────────────────────────
@dataclass
class AssignmentDraft:
    personUserId: str
    competencyId: str
    source: str  # threshold_rule | severity_rule | recert_rule | manual
    plantId: str
    sourceModule: str | None = None
    sourceRecordId: str | None = None
    sourceRecordRef: str | None = None
    triggerMappingId: str | None = None
    provenance: dict = field(default_factory=dict)
    isMandatory: bool = False
    dismissible: bool = True
    escalationFlag: bool = False
    dueOffsetDays: int = 30


@dataclass
class ReviewFlag:
    """A case the engine refuses to auto-resolve — surfaced to the HSE Manager
    for manual review (spec: "log a validation error … flag to HSE Manager")."""

    reason: str  # scoping_failed | no_involved_workers | no_mapping
    competencyId: str | None = None
    plantId: str | None = None
    sourceModule: str | None = None
    sourceRecordId: str | None = None
    detail: str = ""


@dataclass
class RuleOutcome:
    drafts: list[AssignmentDraft] = field(default_factory=list)
    flags: list[ReviewFlag] = field(default_factory=list)


@dataclass
class ScopingResult:
    scoped_worker_ids: list[str]
    scoping_failed: bool  # True when NO worker's role requires the competency
    rejected_count: int   # candidates dropped because their role doesn't require it


# ── RULE 3: role/exposure scoping (the "never blanket" guarantee) ────────────
def apply_role_scoping(
    *,
    candidate_worker_ids: list[str] | None,
    requiring_worker_ids: list[str],
) -> ScopingResult:
    """Resolve an assignment target set to ONLY workers whose role requires the
    competency (spec §B.3). ``requiring_worker_ids`` is the authoritative set
    (from RoleCompetencyRequirement). ``candidate_worker_ids`` is who we *would*
    assign; None means "the whole site roster" — the classic blanket case, which
    this function collapses to the role-required subset.

    Returns scoping_failed=True (→ ReviewFlag, no assignments) when no role
    requires the competency, so a threshold can never fan out to a full roster.
    """
    requiring = set(requiring_worker_ids)
    if candidate_worker_ids is None:
        scoped = list(dict.fromkeys(requiring_worker_ids))  # blanket → role-required only
        rejected = 0
    else:
        scoped = [w for w in candidate_worker_ids if w in requiring]
        rejected = sum(1 for w in candidate_worker_ids if w not in requiring)
    return ScopingResult(
        scoped_worker_ids=scoped,
        scoping_failed=len(requiring) == 0,
        rejected_count=rejected,
    )


# ── RULE 1: threshold ─────────────────────────────────────────────────────────
def threshold_rule(
    *,
    competency_id: str,
    plant_id: str,
    department_id: str | None,
    matched_record_count: int,
    requiring_worker_ids: list[str],
    already_covered_ids: set[str],
    config: RuleConfigView,
    trigger_mapping_id: str | None = None,
    provenance: dict | None = None,
) -> RuleOutcome:
    """N records of a mapped hazard at the same site/department within the rolling
    window → assign the mapped training to workers whose role requires that
    competency. Scoping is mandatory — failure flags for manual review."""
    if matched_record_count < config.thresholdCount:
        return RuleOutcome()

    scoping = apply_role_scoping(
        candidate_worker_ids=requiring_worker_ids,
        requiring_worker_ids=requiring_worker_ids,
    )
    if scoping.scoping_failed:
        return RuleOutcome(
            flags=[
                ReviewFlag(
                    reason="scoping_failed",
                    competencyId=competency_id,
                    plantId=plant_id,
                    detail=(
                        f"{matched_record_count} mapped records reached the threshold "
                        f"but no role at this site requires the competency — manual review."
                    ),
                )
            ]
        )

    base_prov = {
        "ruleType": "threshold_rule",
        "thresholdCount": config.thresholdCount,
        "windowDays": config.thresholdWindowDays,
        "matchedRecordCount": matched_record_count,
        "departmentId": department_id,
        **(provenance or {}),
    }
    drafts = [
        AssignmentDraft(
            personUserId=w,
            competencyId=competency_id,
            source="threshold_rule",
            plantId=plant_id,
            triggerMappingId=trigger_mapping_id,
            provenance=dict(base_prov),
            isMandatory=False,
            dismissible=True,
            dueOffsetDays=config.assignmentDueDays,
        )
        for w in scoping.scoped_worker_ids
        if w not in already_covered_ids
    ]
    return RuleOutcome(drafts=drafts)


# ── RULE 2: severity (SIF-potential) ──────────────────────────────────────────
def severity_rule(
    *,
    classification: dict,
    mapped_competency_ids: list[str],
    plant_id: str,
    source_module: str | None,
    source_record_id: str | None,
    source_record_ref: str | None,
    config: RuleConfigView,
    mapping_by_competency: dict[str, str] | None = None,
) -> RuleOutcome:
    """Any record flagged SIF-potential (Serious Injury or Fatality potential) or
    at/above the configured severity threshold → immediate individual assignment
    to the specific worker(s) involved, bypassing the threshold count, plus an
    escalation flag to the HSE Manager. Non-dismissible (spec business rule)."""
    if not config.severitySifImmediate:
        return RuleOutcome()

    severity = (classification.get("severity") or "").upper()
    is_serious = bool(classification.get("sifPotential")) or severity_rank(severity) >= severity_rank(
        config.severityThreshold
    )
    if not is_serious:
        return RuleOutcome()

    if not mapped_competency_ids:
        return RuleOutcome(
            flags=[
                ReviewFlag(
                    reason="no_mapping",
                    plantId=plant_id,
                    sourceModule=source_module,
                    sourceRecordId=source_record_id,
                    detail="Serious event with no hazard→skill mapping — add a mapping or assign manually.",
                )
            ]
        )

    involved = [u for u in (classification.get("involvedUserIds") or []) if u]
    if not involved:
        # Serious, but no identified worker to scope to → escalate, don't blanket.
        return RuleOutcome(
            flags=[
                ReviewFlag(
                    reason="no_involved_workers",
                    competencyId=mapped_competency_ids[0],
                    plantId=plant_id,
                    sourceModule=source_module,
                    sourceRecordId=source_record_id,
                    detail="Serious event with no identified workers — HSE Manager to assign manually.",
                )
            ]
        )

    mapping_by_competency = mapping_by_competency or {}
    sif = bool(classification.get("sifPotential"))
    outcome = RuleOutcome()
    seen: set[tuple[str, str]] = set()
    for cid in mapped_competency_ids:
        for u in involved:
            if (u, cid) in seen:
                continue
            seen.add((u, cid))
            outcome.drafts.append(
                AssignmentDraft(
                    personUserId=u,
                    competencyId=cid,
                    source="severity_rule",
                    plantId=plant_id,
                    sourceModule=source_module,
                    sourceRecordId=source_record_id,
                    sourceRecordRef=source_record_ref,
                    triggerMappingId=mapping_by_competency.get(cid),
                    provenance={
                        "ruleType": "severity_rule",
                        "severity": severity,
                        "sifPotential": sif,
                    },
                    isMandatory=True,   # severity-rule assignments cannot be dismissed
                    dismissible=False,
                    escalationFlag=True,
                    dueOffsetDays=min(7, config.assignmentDueDays),
                )
            )
    return outcome


# ── RULE 4: recertification ───────────────────────────────────────────────────
def recert_rule(
    *,
    records_due: list[RecordDueRef],
    already_covered_ids: set[tuple[str, str]],
    config: RuleConfigView,
) -> RuleOutcome:
    """A CompetencyRecord's expiry (validUntil / nextRevalidationDue) is within the
    configured window → auto-assign a refresher, independent of incident triggers."""
    outcome = RuleOutcome()
    for r in records_due:
        key = (r.personUserId, r.competencyId)
        if key in already_covered_ids:
            continue
        outcome.drafts.append(
            AssignmentDraft(
                personUserId=r.personUserId,
                competencyId=r.competencyId,
                source="recert_rule",
                plantId=r.plantId,
                provenance={
                    "ruleType": "recert_rule",
                    "dueDate": r.dueDate.isoformat() if hasattr(r.dueDate, "isoformat") else None,
                    "windowDays": config.recertWindowDays,
                },
                isMandatory=False,
                dismissible=True,
                dueOffsetDays=config.recertWindowDays,
            )
        )
    return outcome


# ── RULE 5: person-risk (repeat-involvement flag) ─────────────────────────────
# Per-person, weighted by module + severity. A serious event weighs more; an
# incident weighs more than a near miss than an observation.
_EVENT_WEIGHT = {
    "INCIDENT": {"LOW": 1.0, "MEDIUM": 2.0, "HIGH": 4.0, "CRITICAL": 8.0},
    "NEAR_MISS": {"LOW": 0.5, "MEDIUM": 1.0, "HIGH": 2.0, "CRITICAL": 4.0},
    "OBSERVATION": {"LOW": 0.5, "MEDIUM": 1.0, "HIGH": 2.0, "CRITICAL": 3.0},
}


def event_weight(module: str, severity: str | None, *, sif: bool = False) -> float:
    """Deterministic risk contribution of one event 'against a person's name'.
    A SIF/serious event doubles its base weight."""
    base = _EVENT_WEIGHT.get(module, {}).get((severity or "").upper(), 1.0)
    return base * (2.0 if sif else 1.0)


@dataclass
class PersonEventStat:
    """Aggregated events attributed to one person in the window (built by the
    service; the rule stays pure)."""

    personUserId: str
    plantId: str
    incidentCount: int = 0
    nearMissCount: int = 0
    observationCount: int = 0
    sifCount: int = 0
    severityWeight: float = 0.0
    contributing: list = field(default_factory=list)  # [{module,id,ref,date,role,severity}]

    @property
    def totalEvents(self) -> int:
        return self.incidentCount + self.nearMissCount + self.observationCount


@dataclass
class PersonRiskResult:
    flagged: bool
    riskScore: float
    riskBand: str  # none | elevated | high | critical
    totalEvents: int
    reasons: list[str] = field(default_factory=list)


def person_involvement_rule(*, stats: PersonEventStat, config: RuleConfigView) -> PersonRiskResult:
    """A worker who accumulates events 'against their name' → risk flag (spec:
    "users who have multiple incidents etc logged against their name should have
    a flag automatically into the training module"). Deterministic; airgap-safe.

    Flagged when: total events ≥ personFlagThreshold, OR any SIF/serious event,
    OR the weighted riskScore reaches the elevated band. Band comes from the
    weighted score against the configurable cutoffs."""
    total = stats.totalEvents
    score = round(stats.severityWeight, 1)
    reasons: list[str] = []
    flagged = False

    if stats.sifCount > 0:
        flagged = True
        reasons.append(f"{stats.sifCount} serious/SIF-potential event(s)")
    if total >= config.personFlagThreshold:
        flagged = True
        reasons.append(
            f"{total} events in {config.personFlagWindowDays}d (flag threshold {config.personFlagThreshold})"
        )

    if score >= config.personRiskCritical:
        band = "critical"
    elif score >= config.personRiskHigh:
        band = "high"
    elif score >= config.personRiskElevated:
        band = "elevated"
    else:
        band = "none"
    if band != "none":
        flagged = True
        reasons.append(f"risk score {score} (band {band})")

    if not flagged:
        return PersonRiskResult(flagged=False, riskScore=score, riskBand="none", totalEvents=total)
    if band == "none":
        band = "elevated"  # flagged by count/SIF but below the score cutoff
    return PersonRiskResult(flagged=True, riskScore=score, riskBand=band, totalEvents=total, reasons=reasons)


__all__ = [
    "RuleConfigView",
    "WorkerRef",
    "RecordDueRef",
    "AssignmentDraft",
    "ReviewFlag",
    "RuleOutcome",
    "ScopingResult",
    "PersonEventStat",
    "PersonRiskResult",
    "severity_rank",
    "event_weight",
    "apply_role_scoping",
    "threshold_rule",
    "severity_rule",
    "recert_rule",
    "person_involvement_rule",
]
