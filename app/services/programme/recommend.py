"""Risk-based audit frequency recommendation — the moat, deterministically.

docs/cams/08 §5.

ISO 45001/9001/14001 clause 9.2.2 requires the audit programme to take into
consideration *"the importance of the processes concerned and the results of
previous audits."* That phrase is the requirement most tools ignore. Gensuite
and Enablon do risk-based scheduling off a static risk rating; here the
frequency recommendation derives from the client's own cross-module operational
history — findings, repeat chains, overdue CAPAs, incidents — because all of it
already lives in one database.

**No LLM. No hosted call.** Weighted arithmetic over rows, and the arithmetic is
rendered to the user.

**Recommends, never applies.** Every recommendation persists with its INPUTS, and
nothing mutates `ProgrammeScopeUnit.requiredPerCycle` without a logged human
acceptance. A programme that rewrites itself is not auditable, and "the
algorithm changed it" is not an answer an auditor accepts.

**Unavailable inputs are declared, never defaulted to zero.** That is the F-48
lesson: a silent all-zero fallback turned a broken dependency into a confident
0% assurance figure. Here a missing signal redistributes its weight and says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_compliance import AuditCheckpointResponse, ComplianceAudit
from app.models.cams import CamsFinding
from app.models.capa import Capa
from app.models.programme import (
    DisciplineHazardMap,
    ProgrammeCycle,
    ProgrammeRecommendation,
    ProgrammeScopeUnit,
)

# Weights sum to 100. Stated here rather than buried so a client can argue with
# them — which is the point: the weighting IS the policy, and it should be
# visible and challengeable.
WEIGHTS: dict[str, int] = {
    "openCriticalMajorNCs": 30,
    "repeatFindingChains": 25,
    "overdueCapas": 15,
    "incidentSignal": 15,
    "statutoryCriticality": 10,
    "timeSinceLastAudit": 5,
}

# Above this the band says "increase"; below the lower bound, "reduce".
BAND_INCREASE = 70.0
BAND_HOLD = 40.0

# Signals that veto a REDUCE regardless of the total score.
#
# Without this a scope unit with the maximum number of repeat findings and
# nothing else scores 25 — below BAND_HOLD — and the engine would recommend
# auditing it LESS often. That is the opposite of what clause 9.2.2 asks for:
# "the results of previous audits" are precisely the reason to keep auditing
# something. Real audit practice is the same — you do not cut frequency on a
# process with unresolved repeat findings just because its total is low.
#
# A veto downgrades REDUCE to HOLD. It never forces an INCREASE, because the
# magnitude of the problem is still what the score is for.
REDUCTION_VETO_INPUTS = ("repeatFindingChains", "openCriticalMajorNCs")

# Saturation points — the value at which an input contributes its full weight.
# Without these a site with 40 open NCs would swamp every other signal.
SATURATION: dict[str, float] = {
    "openCriticalMajorNCs": 5.0,
    "repeatFindingChains": 3.0,
    "overdueCapas": 4.0,
    "incidentSignal": 5.0,
    "statutoryCriticality": 1.0,
    "timeSinceLastAudit": 540.0,  # days; ~18 months = full weight
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class InputSignal:
    key: str
    rawValue: float | None
    available: bool
    label: str
    detail: str = ""

    @property
    def weight(self) -> int:
        return WEIGHTS[self.key]

    def normalised(self) -> float:
        if not self.available or self.rawValue is None:
            return 0.0
        cap = SATURATION[self.key]
        return max(0.0, min(1.0, self.rawValue / cap)) if cap else 0.0

    def contribution(self, scale: float = 1.0) -> float:
        return round(self.normalised() * self.weight * scale, 2)

    def as_dict(self, scale: float = 1.0) -> dict[str, Any]:
        return {
            "input": self.key,
            "label": self.label,
            "rawValue": self.rawValue,
            "available": self.available,
            "weight": round(self.weight * scale, 1),
            "contribution": self.contribution(scale),
            "detail": self.detail,
        }


@dataclass
class Recommendation:
    scopeUnitId: str
    currentFrequency: int | None
    recommendedFrequency: int
    score: float
    band: str
    signals: list[InputSignal] = field(default_factory=list)
    scale: float = 1.0
    vetoes: list[str] = field(default_factory=list)

    @property
    def unavailable(self) -> list[str]:
        return [s.key for s in self.signals if not s.available]

    def narrative(self) -> str:
        """The sentence rendered under the recommendation.

        Built from the signals that actually contributed, plus an explicit
        mention of any that could not be measured — so the reader can tell a
        genuine zero from a missing feed.
        """
        parts = [
            f"{s.label} ({s.contribution(self.scale):g})"
            for s in self.signals
            if s.available and (s.rawValue or 0) > 0
        ]
        missing = [s.label.lower() for s in self.signals if not s.available]
        text = " · ".join(parts) if parts else "no adverse signals"
        if missing:
            text += f" · {', '.join(missing)} unavailable"
        out = f"{text} → {self.score:g}/100"
        if self.vetoes and self.band == "HOLD" and self.score < BAND_HOLD:
            # Say WHY the band is not REDUCE, or the number and the verdict look
            # inconsistent to a reader.
            labels = [
                s.label.lower() for s in self.signals if s.key in self.vetoes
            ]
            out += f" — held rather than reduced because {', '.join(labels)} remain open"
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "scopeUnitId": self.scopeUnitId,
            "currentFrequency": self.currentFrequency,
            "recommendedFrequency": self.recommendedFrequency,
            "score": self.score,
            "band": self.band,
            "inputs": [s.as_dict(self.scale) for s in self.signals],
            "unavailableInputs": self.unavailable,
            "reductionVetoedBy": self.vetoes,
            "narrative": self.narrative(),
        }


# ─────────────────────────────────────────────────────────────────────
# Pure scoring core
# ─────────────────────────────────────────────────────────────────────


def score_signals(signals: list[InputSignal]) -> tuple[float, float]:
    """Return (score, scale).

    Weight from unavailable inputs is redistributed proportionally across the
    available ones, so a missing feed does not silently drag every score down —
    it widens the others instead, and the caller reports which were missing.
    """
    available_weight = sum(s.weight for s in signals if s.available)
    if available_weight <= 0:
        return 0.0, 1.0
    scale = sum(WEIGHTS.values()) / available_weight
    score = sum(s.contribution(scale) for s in signals)
    return round(min(100.0, score), 1), scale


def reduction_vetoed(signals: Iterable[InputSignal]) -> list[str]:
    """Which veto signals are present. Empty means a reduction is permissible."""
    return [
        s.key
        for s in signals
        if s.key in REDUCTION_VETO_INPUTS and s.available and (s.rawValue or 0) > 0
    ]


def band_for(score: float, *, vetoes: Iterable[str] | None = None) -> str:
    """Score → band, with the reduction veto applied.

    `vetoes` is the output of `reduction_vetoed`. Passing none keeps the plain
    threshold behaviour, which is what the pure-score tests exercise.
    """
    if score >= BAND_INCREASE:
        return "INCREASE"
    if score >= BAND_HOLD:
        return "HOLD"
    return "HOLD" if list(vetoes or []) else "REDUCE"


def frequency_for(current: int | None, band: str, periods_per_cycle: int) -> int:
    """Translate a band into a concrete frequency.

    Bounded at 1 (never recommend auditing something zero times — if it is in
    scope it gets audited) and at `periodsPerCycle` (no point recommending more
    audits than there are periods to hold them).
    """
    base = current or 1
    if band == "INCREASE":
        out = base + 1
    elif band == "REDUCE":
        out = base - 1
    else:
        out = base
    return max(1, min(periods_per_cycle, out))


# ─────────────────────────────────────────────────────────────────────
# Signal gathering
# ─────────────────────────────────────────────────────────────────────


async def _open_nc_count(db: AsyncSession, site_id: str | None, disc: str) -> float:
    """Open critical/major non-conformities on this scope unit.

    Both engines contribute: the audit side derives findings from checkpoint
    verdicts (no first-class Finding row until WP-19), the inspection side has
    real CamsFinding rows.
    """
    q = select(func.count(AuditCheckpointResponse.id)).where(
        AuditCheckpointResponse.categoryId == disc,
        AuditCheckpointResponse.assessmentStatus.in_(("FAIL", "PARTIAL")),
        AuditCheckpointResponse.criticality.in_(("critical", "major")),
        AuditCheckpointResponse.workflowState.notin_(("FINALIZED", "RESOLVED", "PASSED")),
    )
    if site_id:
        q = q.where(AuditCheckpointResponse.plantId == site_id)
    audit_side = (await db.execute(q)).scalar_one() or 0

    q2 = select(func.count(CamsFinding.id)).where(
        CamsFinding.severity.in_(("CRITICAL_NC", "MAJOR_NC")),
        CamsFinding.status.in_(("OPEN", "CAPA_RAISED")),
        CamsFinding.isDeleted.is_(False),
    )
    if site_id:
        q2 = q2.where(CamsFinding.siteId == site_id)
    insp_side = (await db.execute(q2)).scalar_one() or 0
    # Inspection findings carry a standard, not an audit discipline, so they are
    # counted only when the scope unit is a STANDARD-dimension one. Attributing
    # them to a discipline would be a guess, and a wrong guess here inflates the
    # recommendation for the wrong scope unit. Understated on purpose until
    # WP-18 gives both engines one scope model.
    return float(audit_side + (insp_side if disc in (None, "", "_UNSPECIFIED") else 0))


async def _repeat_chain_count(db: AsyncSession, site_id: str | None) -> float:
    """Repeat findings — the single strongest evidence that prior audits did not
    stick, and the input clause 9.2.2 most directly asks for."""
    q = select(func.count(CamsFinding.id)).where(
        CamsFinding.isRepeatFinding.is_(True), CamsFinding.isDeleted.is_(False)
    )
    if site_id:
        q = q.where(CamsFinding.siteId == site_id)
    return float((await db.execute(q)).scalar_one() or 0)


async def _overdue_capa_count(db: AsyncSession, site_id: str | None) -> float:
    """Open CAPAs past their closure target.

    `closureTargetDate` is the column that exists (there is no
    `targetCompletionDate`), and `Capa` carries no `isDeleted` — the open-state
    filter is what bounds this.
    """
    q = select(func.count(Capa.id)).where(
        Capa.state.notin_(("CLOSED", "CANCELLED", "VERIFIED")),
        Capa.closureTargetDate.isnot(None),
        Capa.closureTargetDate < _utcnow(),
    )
    if site_id:
        q = q.where(Capa.plantId == site_id)
    return float((await db.execute(q)).scalar_one() or 0)


async def _incident_signal(
    db: AsyncSession, site_id: str | None, disc: str
) -> tuple[float | None, bool, str]:
    """The cross-module signal — and the one that can be genuinely unavailable.

    `Incident` carries a plant, an area and its own category taxonomy;
    `AuditCheckpointResponse.categoryId` is the audit discipline taxonomy.
    Nothing joined them, so `DisciplineHazardMap` was added to hold that mapping
    (docs/cams/08 §5.1, option 2). Without map rows the signal reports
    UNAVAILABLE — it does not report zero. A zero would read as "no incidents
    touch this discipline", which is a claim we cannot make.
    """
    q = select(DisciplineHazardMap).where(
        DisciplineHazardMap.disciplineCode == disc,
        DisciplineHazardMap.isActive.is_(True),
    )
    if site_id:
        q = q.where(
            (DisciplineHazardMap.plantId == site_id) | (DisciplineHazardMap.plantId.is_(None))
        )
    maps = list((await db.execute(q)).scalars().all())
    if not maps:
        return None, False, "No discipline↔hazard mapping configured for this discipline."

    categories = {m.hazardCategory: m.weight for m in maps}
    try:
        from app.models.incident import Incident  # local import: optional module

        cutoff = _utcnow() - timedelta(days=365)
        rows = (
            await db.execute(
                select(Incident).where(
                    Incident.isDeleted.is_(False),
                    Incident.occurredAt >= cutoff,
                    *( [Incident.plantId == site_id] if site_id else [] ),
                )
            )
        ).scalars().all()
    except Exception:  # noqa: BLE001 — module shape varies; treat as unavailable
        return None, False, "Incident data could not be read."

    total = 0.0
    matched = 0
    for inc in rows:
        # `Incident.type` is an enum; other taxonomies on other modules are
        # plain strings. Normalise both to the enum's `.value` before matching,
        # or an enum member would never equal a mapped category string.
        for attr in ("category", "incidentCategory", "categoryCode", "type"):
            val = getattr(inc, attr, None)
            if val is None:
                continue
            key = str(getattr(val, "value", val))
            if key in categories:
                total += categories[key]
                matched += 1
                break
    return (
        total,
        True,
        f"{matched} of {len(rows)} incident(s) in 12 months matched "
        f"{len(categories)} mapped categor{'y' if len(categories) == 1 else 'ies'}.",
    )


async def _days_since_last_audit(
    db: AsyncSession, site_id: str | None, disc: str
) -> tuple[float | None, bool]:
    q = (
        select(func.max(ComplianceAudit.closedAt))
        .join(
            AuditCheckpointResponse,
            AuditCheckpointResponse.auditId == ComplianceAudit.id,
        )
        .where(
            ComplianceAudit.status == "closed",
            ComplianceAudit.isDeleted.is_(False),
            AuditCheckpointResponse.categoryId == disc,
        )
    )
    if site_id:
        q = q.where(ComplianceAudit.plantId == site_id)
    last = (await db.execute(q)).scalar_one_or_none()
    if last is None:
        # Never audited is the strongest possible case for auditing it — full
        # saturation rather than "unavailable".
        return SATURATION["timeSinceLastAudit"], True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return float((_utcnow() - last).days), True


# ─────────────────────────────────────────────────────────────────────
# Public entry points
# ─────────────────────────────────────────────────────────────────────


async def recommend_for_scope_unit(
    db: AsyncSession, unit: ProgrammeScopeUnit, *, periods_per_cycle: int = 4
) -> Recommendation:
    site = unit.siteId
    disc = unit.dimensionKey

    nc = await _open_nc_count(db, site, disc)
    repeats = await _repeat_chain_count(db, site)
    capas = await _overdue_capa_count(db, site)
    inc_val, inc_ok, inc_detail = await _incident_signal(db, site, disc)
    days, days_ok = await _days_since_last_audit(db, site, disc)

    signals = [
        InputSignal("openCriticalMajorNCs", nc, True, "Open critical/major NCs"),
        InputSignal("repeatFindingChains", repeats, True, "Repeat-finding chains"),
        InputSignal("overdueCapas", capas, True, "Overdue CAPAs"),
        InputSignal("incidentSignal", inc_val, inc_ok, "Incident signal", inc_detail),
        # Statutory criticality needs the obligation↔engagement link, which holds
        # 5 rows across 72 obligations — too sparse to weight honestly (F-50).
        # Declared unavailable until WP-52 makes assurance derived.
        InputSignal(
            "statutoryCriticality",
            None,
            False,
            "Statutory criticality",
            "Obligation links are too sparse to weight (see WP-52).",
        ),
        InputSignal("timeSinceLastAudit", days, days_ok, "Time since last audit"),
    ]

    score, scale = score_signals(signals)
    vetoes = reduction_vetoed(signals)
    band = band_for(score, vetoes=vetoes)
    return Recommendation(
        scopeUnitId=unit.id,
        currentFrequency=unit.requiredPerCycle,
        recommendedFrequency=frequency_for(unit.requiredPerCycle, band, periods_per_cycle),
        score=score,
        band=band,
        signals=signals,
        scale=scale,
        vetoes=vetoes,
    )


async def recommend_for_cycle(
    db: AsyncSession, cycle_id: str, *, persist: bool = True
) -> list[dict[str, Any]]:
    """Compute (and optionally persist) a recommendation per scope unit.

    Persisting stores the inputs alongside the output so the UI can render the
    arithmetic and a reviewer can disagree with a number rather than a verdict.
    Nothing here mutates a frequency — see `accept_recommendation`.
    """
    cycle = await db.get(ProgrammeCycle, cycle_id)
    if cycle is None:
        raise ValueError("Programme cycle not found")

    units = list(
        (
            await db.execute(
                select(ProgrammeScopeUnit).where(ProgrammeScopeUnit.cycleId == cycle_id)
            )
        ).scalars().all()
    )

    out: list[dict[str, Any]] = []
    for u in units:
        rec = await recommend_for_scope_unit(db, u, periods_per_cycle=cycle.periodsPerCycle)
        payload = rec.as_dict()
        out.append(payload)

        if persist:
            prior = (
                await db.execute(
                    select(ProgrammeRecommendation).where(
                        ProgrammeRecommendation.cycleId == cycle_id,
                        ProgrammeRecommendation.scopeUnitId == u.id,
                        ProgrammeRecommendation.acceptedAt.is_(None),
                        ProgrammeRecommendation.rejectedAt.is_(None),
                    )
                )
            ).scalars().first()
            if prior is not None:
                # Refresh the open recommendation rather than stacking duplicates
                # every time someone opens the screen.
                prior.recommendedFrequency = rec.recommendedFrequency
                prior.currentFrequency = rec.currentFrequency
                prior.score = rec.score
                prior.band = rec.band
                prior.inputs = payload["inputs"]
                prior.unavailableInputs = payload["unavailableInputs"]
                prior.narrative = payload["narrative"]
                prior.computedAt = _utcnow()
                payload["id"] = prior.id
            else:
                row = ProgrammeRecommendation(
                    cycleId=cycle_id,
                    scopeUnitId=u.id,
                    currentFrequency=rec.currentFrequency,
                    recommendedFrequency=rec.recommendedFrequency,
                    score=rec.score,
                    band=rec.band,
                    inputs=payload["inputs"],
                    unavailableInputs=payload["unavailableInputs"],
                    narrative=payload["narrative"],
                )
                db.add(row)
                await db.flush()
                payload["id"] = row.id

    if persist:
        await db.flush()
    return out


async def accept_recommendation(
    db: AsyncSession,
    *,
    recommendation_id: str,
    user_id: str,
    frequency: int | None = None,
) -> dict[str, Any]:
    """The human gate. This is the ONLY path that writes a frequency.

    `frequency` lets the reviewer accept the recommendation at a different
    number — accepting the *direction* while disagreeing with the *magnitude* is
    a normal outcome and forcing a binary choice would push people to reject
    good recommendations.
    """
    rec = await db.get(ProgrammeRecommendation, recommendation_id)
    if rec is None:
        raise ValueError("Recommendation not found")
    if rec.acceptedAt or rec.rejectedAt:
        raise ValueError("This recommendation has already been actioned")

    unit = await db.get(ProgrammeScopeUnit, rec.scopeUnitId)
    if unit is None:
        raise ValueError("Scope unit no longer exists")

    applied = frequency if frequency is not None else rec.recommendedFrequency
    if applied < 1:
        raise ValueError("Frequency must be at least 1 — a scope unit in scope gets audited")

    unit.requiredPerCycle = applied
    rec.acceptedByUserId = user_id
    rec.acceptedAt = _utcnow()
    rec.acceptedFrequency = applied
    await db.flush()
    return {
        "ok": True,
        "scopeUnitId": unit.id,
        "appliedFrequency": applied,
        "matchedRecommendation": applied == rec.recommendedFrequency,
    }


async def reject_recommendation(
    db: AsyncSession, *, recommendation_id: str, user_id: str, reason: str
) -> dict[str, Any]:
    rec = await db.get(ProgrammeRecommendation, recommendation_id)
    if rec is None:
        raise ValueError("Recommendation not found")
    if rec.acceptedAt or rec.rejectedAt:
        raise ValueError("This recommendation has already been actioned")
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise ValueError("A rejection reason is required")
    rec.rejectedByUserId = user_id
    rec.rejectedAt = _utcnow()
    rec.rejectionReason = reason
    await db.flush()
    return {"ok": True}


__all__ = [
    "WEIGHTS",
    "SATURATION",
    "BAND_INCREASE",
    "BAND_HOLD",
    "REDUCTION_VETO_INPUTS",
    "InputSignal",
    "Recommendation",
    "score_signals",
    "band_for",
    "reduction_vetoed",
    "frequency_for",
    "recommend_for_scope_unit",
    "recommend_for_cycle",
    "accept_recommendation",
    "reject_recommendation",
]
