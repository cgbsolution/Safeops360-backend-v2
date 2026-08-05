"""CAMS analytics engines (P2-4) — repeat-finding auto-detection, findings Pareto,
cross-site benchmarking, ISO clause analysis. Computed from real CamsFinding /
CamsEngagement data, not seed commentary. Idempotent; callable on-demand + nightly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cams import CamsEngagement, CamsFinding

_CLOSED = ("CLOSED", "RESOLVED", "VERIFIED")
_OPEN = ("OPEN", "IN_PROGRESS", "AWAITING_VERIFICATION")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


# ── 4.1 Repeat-finding auto-detection ────────────────────────────────────────
async def detect_repeat_findings(db: AsyncSession, window_days: int = 365) -> dict[str, Any]:
    """A finding is a repeat if a finding with the SAME (clause + site) — or, when
    no clause, (areaOrAssetRef + severity) — was CLOSED within window_days before
    this one opened. Sets isRepeatFinding + repeatOfFindingId. Idempotent."""
    findings = (
        await db.execute(select(CamsFinding).where(CamsFinding.isDeleted.is_(False)).order_by(CamsFinding.createdAt))
    ).scalars().all()
    flagged = 0
    for f in findings:
        created = _aware(f.createdAt)
        key_clause = f.standardClauseRef
        prior = None
        for p in findings:
            if p.id == f.id or p.status not in _CLOSED or p.siteId != f.siteId:
                continue
            same = (key_clause and p.standardClauseRef == key_clause) or (
                not key_clause and p.areaOrAssetRef and p.areaOrAssetRef == f.areaOrAssetRef and p.severity == f.severity
            )
            if not same:
                continue
            closed = _aware(p.closedAt)
            if closed and created and (created - timedelta(days=window_days)) <= closed < created:
                if prior is None or closed > _aware(prior.closedAt):
                    prior = p
        new_repeat = prior is not None
        if f.isRepeatFinding != new_repeat or f.repeatOfFindingId != (prior.id if prior else None):
            f.isRepeatFinding = new_repeat
            f.repeatOfFindingId = prior.id if prior else None
            flagged += 1
    await db.flush()
    repeats = sum(1 for f in findings if f.isRepeatFinding)
    return {"evaluated": len(findings), "updated": flagged, "currentRepeats": repeats}


# ── 4.2 Findings Pareto ──────────────────────────────────────────────────────
async def findings_pareto(db: AsyncSession, plant_ids: list[str] | None, dimension: str = "clause", days: int = 365) -> dict[str, Any]:
    """Ranked Pareto of findings by a dimension (clause | rootCause | site | severity),
    with repeat counts and cumulative %."""
    since = _now() - timedelta(days=days)
    q = select(CamsFinding).where(CamsFinding.isDeleted.is_(False)).where(CamsFinding.createdAt >= since)
    if plant_ids is not None:
        q = q.where(CamsFinding.siteId.in_(plant_ids or ["__none__"]))
    findings = (await db.execute(q)).scalars().all()

    def keyof(f: CamsFinding) -> str:
        if dimension == "rootCause":
            return f.rootCauseMethod or "UNCLASSIFIED"
        if dimension == "site":
            return f.siteId or "UNSITED"
        if dimension == "severity":
            return f.severity
        return f.standardClauseRef or "UNMAPPED"

    buckets: dict[str, dict[str, int]] = {}
    for f in findings:
        b = buckets.setdefault(keyof(f), {"count": 0, "repeats": 0, "open": 0})
        b["count"] += 1
        if f.isRepeatFinding:
            b["repeats"] += 1
        if f.status in _OPEN:
            b["open"] += 1
    total = sum(b["count"] for b in buckets.values()) or 1
    rows = sorted(buckets.items(), key=lambda kv: kv[1]["count"], reverse=True)
    out, cum = [], 0
    for key, b in rows:
        cum += b["count"]
        out.append({
            "key": key, "count": b["count"], "repeats": b["repeats"], "open": b["open"],
            "pctOfTotal": round(b["count"] * 100.0 / total, 1), "cumulativePct": round(cum * 100.0 / total, 1),
        })
    return {"dimension": dimension, "total": total, "rows": out}


# ── 4.3 Cross-site benchmarking ──────────────────────────────────────────────
async def site_benchmarks(db: AsyncSession, plant_ids: list[str] | None, days: int = 365) -> dict[str, Any]:
    """Per site: avg engagement score, findings/audit, repeat-finding rate, major/
    critical NC counts — normalised per audit so sites compare fairly. This replaces
    the previously-hardcoded 88%/79% commentary with computed numbers."""
    since = _now() - timedelta(days=days)
    eq = (
        await db.execute(
            select(CamsEngagement).where(CamsEngagement.isDeleted.is_(False))
            .where(CamsEngagement.status.in_(("completed", "closed", "COMPLETED", "CLOSED")))
        )
    ).scalars().all()
    if plant_ids is not None:
        eq = [e for e in eq if e.siteId in plant_ids]
    findings = (await db.execute(select(CamsFinding).where(CamsFinding.isDeleted.is_(False)).where(CamsFinding.createdAt >= since))).scalars().all()
    by_site_eng: dict[str, list] = {}
    for e in eq:
        by_site_eng.setdefault(e.siteId or "UNSITED", []).append(e)
    by_site_find: dict[str, list] = {}
    for f in findings:
        by_site_find.setdefault(f.siteId or "UNSITED", []).append(f)

    out = []
    for site, engs in by_site_eng.items():
        scored = [e.scorePercent for e in engs if e.scorePercent is not None]
        finds = by_site_find.get(site, [])
        n_aud = len(engs)
        out.append({
            "siteId": site, "auditsConducted": n_aud,
            "complianceScorePct": round(sum(scored) / len(scored), 1) if scored else None,
            "findingsPerAudit": round(len(finds) / max(n_aud, 1), 2),
            "repeatFindingRatePct": round(100 * sum(1 for f in finds if f.isRepeatFinding) / max(len(finds), 1), 1),
            "majorNcCount": sum(1 for f in finds if f.severity in ("MAJOR_NC", "MAJOR")),
            "criticalNcCount": sum(1 for f in finds if f.severity in ("CRITICAL_NC", "CRITICAL")),
        })
    out.sort(key=lambda b: (b["complianceScorePct"] is None, b["complianceScorePct"] or 0))
    return {"sites": out, "windowDays": days}


# ── 4.4 ISO clause analysis (non-conformance by clause) ──────────────────────
async def clause_analysis(db: AsyncSession, plant_ids: list[str] | None, standard_ref: str | None = None, days: int = 365) -> dict[str, Any]:
    """Non-conformances grouped by ISO clause (e.g. 'ISO 45001:8.1'), with severity
    mix and repeat rate — the conformance-by-clause view a compliance head asks for."""
    since = _now() - timedelta(days=days)
    q = select(CamsFinding).where(CamsFinding.isDeleted.is_(False)).where(CamsFinding.createdAt >= since).where(CamsFinding.standardClauseRef.is_not(None))
    if plant_ids is not None:
        q = q.where(CamsFinding.siteId.in_(plant_ids or ["__none__"]))
    findings = (await db.execute(q)).scalars().all()
    if standard_ref:
        findings = [f for f in findings if (f.standardClauseRef or "").startswith(standard_ref)]
    buckets: dict[str, dict[str, Any]] = {}
    for f in findings:
        b = buckets.setdefault(f.standardClauseRef, {"clauseRef": f.standardClauseRef, "ncCount": 0, "repeats": 0, "open": 0, "major": 0, "critical": 0})
        b["ncCount"] += 1
        if f.isRepeatFinding:
            b["repeats"] += 1
        if f.status in _OPEN:
            b["open"] += 1
        if f.severity in ("MAJOR_NC", "MAJOR"):
            b["major"] += 1
        if f.severity in ("CRITICAL_NC", "CRITICAL"):
            b["critical"] += 1
    rows = sorted(buckets.values(), key=lambda b: (b["critical"], b["major"], b["ncCount"]), reverse=True)
    return {"standardRef": standard_ref, "clauses": rows, "totalNc": len(findings)}
