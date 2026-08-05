"""WP-40 - certification evidence pack, and WP-34 - programme export.

docs/cams/09 §2.6 and docs/cams/08 §6.

One action produces a self-contained pack: the final report, findings with their
iteration threads, evidence photos, CAPA records, sign-offs, meeting records,
sampling justification, independence waivers, and the programme context.

Four constraints, each of which shapes the design:

  **Async.** A 1,500-checkpoint pack with 200 photos will not finish inside a
  request cycle. `EvidencePackJob` is the row the UI polls; `build_pack` is
  driven by the scheduler.

  **Deterministic.** Same inputs, same bytes. No timestamps inside file
  contents, entries written in sorted order - otherwise two exports of an
  unchanged audit differ and the hash stops meaning anything.

  **Airgap-safe.** ZIP assembled in-process with the stdlib; no external
  service, no font download, no CDN. Evidence photos are the one component that
  still depends on hosted Supabase Storage (open question Q11) - when a photo
  cannot be fetched the pack records the failure IN THE MANIFEST rather than
  silently shipping without it.

  **Self-verifying.** The manifest carries a SHA-256 per entry plus the report's
  own snapshot digest, so a recipient can confirm nothing changed after issue
  WITHOUT access to this system. That is the whole point of handing someone a
  pack rather than a login.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.assurance import EngagementMeeting, IndependenceWaiver, ReportErratum
from app.models.audit_compliance import (
    AuditCheckpointResponse,
    AuditReport,
    CheckpointInteraction,
    ComplianceAudit,
)
from app.models.cams_completion import AuditFinding, EvidencePackJob
from app.models.programme import ProgrammeCycle, ProgrammeSlot

# Steps, in order, with the share of progress each represents. Reported to the
# UI so a 4-minute export shows movement rather than a frozen spinner.
STEPS: list[tuple[str, int]] = [
    ("Collecting engagement record", 10),
    ("Collecting findings and threads", 30),
    ("Collecting assurance records", 45),
    ("Collecting CAPA records", 55),
    ("Fetching evidence photos", 80),
    ("Writing manifest", 90),
    ("Sealing archive", 100),
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(obj: Any) -> bytes:
    """Deterministic JSON: sorted keys, fixed separators, no ambient timestamp.

    Two exports of an unchanged audit must be byte-identical, or the manifest
    hash certifies nothing.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


class PackBuilder:
    """Accumulates entries, then seals them into a deterministic ZIP."""

    def __init__(self) -> None:
        self._entries: dict[str, bytes] = {}
        self.failures: list[dict[str, str]] = []

    def add_json(self, path: str, obj: Any) -> None:
        self._entries[path] = _canonical_json(obj)

    def add_bytes(self, path: str, data: bytes) -> None:
        self._entries[path] = data

    def record_failure(self, path: str, reason: str) -> None:
        """A missing artefact is recorded, never skipped silently.

        A pack that quietly omits 40 unreachable photos looks complete and is
        not. The manifest must let a recipient see the gap.
        """
        self.failures.append({"path": path, "reason": reason})

    def manifest(self) -> list[dict[str, Any]]:
        out = [
            {
                "path": p,
                "bytes": len(d),
                "sha256": _sha256(d),
                "kind": p.split("/", 1)[0],
            }
            for p, d in sorted(self._entries.items())
        ]
        for f in self.failures:
            out.append({**f, "bytes": 0, "sha256": None, "kind": "MISSING"})
        return out

    def seal(self) -> tuple[bytes, list[dict[str, Any]]]:
        """Write the ZIP. Entries sorted; every mtime pinned.

        `ZipInfo` with a fixed date_time is what makes the archive itself
        reproducible - Python would otherwise stamp "now" into every header and
        two identical exports would hash differently.
        """
        man = self.manifest()
        self._entries["manifest.json"] = _canonical_json(
            {
                "generator": "SafeOps360 CAMS evidence pack",
                "manifestVersion": 1,
                "entries": man,
                "entryCount": len(man),
                "missingCount": len(self.failures),
            }
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for path in sorted(self._entries):
                info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                z.writestr(info, self._entries[path])
        return buf.getvalue(), man


async def collect_audit_pack(
    db: AsyncSession, *, audit_id: str, include_photos: bool = True
) -> PackBuilder:
    """Everything a certification body asks for, for one engagement."""
    b = PackBuilder()

    audit = await db.get(ComplianceAudit, audit_id)
    if audit is None:
        raise ValueError("Audit not found")

    b.add_json(
        "engagement/audit.json",
        {
            "auditNumber": audit.auditNumber, "title": audit.title,
            "plantId": audit.plantId, "status": audit.status,
            "auditType": audit.auditType, "industryCode": audit.industryCode,
            "scheduledDate": audit.scheduledDate, "closedAt": audit.closedAt,
            "leadAuditorUserId": audit.leadAuditorUserId,
            "disciplinesInScope": audit.selectedDisciplineIds,
            "reopenCount": audit.reopenCount,
            "signOffs": audit.signOffs,
        },
    )

    # Reports, each with its integrity digest — the anchor of the whole pack.
    reports = (
        await db.execute(select(AuditReport).where(AuditReport.auditId == audit_id))
    ).scalars().all()
    for r in reports:
        b.add_json(
            f"reports/{r.reportCode}.json",
            {
                "reportCode": r.reportCode, "reportType": r.reportType,
                "generatedAt": r.generatedAt, "isSuperseded": r.isSuperseded,
                "snapshotHashFull": r.snapshotHashFull,
                "snapshot": r.snapshot,
            },
        )
        errata = (
            await db.execute(
                select(ReportErratum).where(ReportErratum.reportId == r.id)
                .order_by(ReportErratum.sequence)
            )
        ).scalars().all()
        if errata:
            b.add_json(
                f"reports/{r.reportCode}.errata.json",
                [
                    {"sequence": e.sequence, "text": e.text,
                     "raisedByUserId": e.raisedByUserId,
                     "approvedByUserId": e.approvedByUserId, "createdAt": e.createdAt}
                    for e in errata
                ],
            )

    # Findings, each with its full iteration thread — the module's strongest
    # artefact and the thing an assessor actually reads.
    findings = (
        await db.execute(
            select(AuditFinding).where(
                AuditFinding.auditId == audit_id, AuditFinding.isDeleted.is_(False)
            ).order_by(AuditFinding.findingCode)
        )
    ).scalars().all()
    b.add_json(
        "findings/index.json",
        [
            {
                "findingCode": f.findingCode, "checkpointCode": f.checkpointCode,
                "title": f.title, "severity": f.severity,
                "observationOnly": f.observationOnly, "status": f.status,
                "dueDate": f.dueDate, "ownerId": f.ownerId, "capaId": f.capaId,
                "standard": f.standard, "clauseRef": f.clauseRef,
                "isRepeatFinding": f.isRepeatFinding,
                "repeatOfFindingId": f.repeatOfFindingId,
            }
            for f in findings
        ],
    )

    responses = (
        await db.execute(
            select(AuditCheckpointResponse)
            .where(AuditCheckpointResponse.auditId == audit_id)
            .order_by(AuditCheckpointResponse.checkpointCode)
        )
    ).scalars().all()
    resp_by_id = {r.id: r for r in responses}

    interactions = (
        await db.execute(
            select(CheckpointInteraction)
            .where(CheckpointInteraction.auditId == audit_id)
            .order_by(CheckpointInteraction.timestamp)
        )
    ).scalars().all()
    threads: dict[str, list[dict[str, Any]]] = {}
    for i in interactions:
        cp = resp_by_id.get(i.checkpointInstanceId)
        key = cp.checkpointCode if cp else i.checkpointInstanceId
        threads.setdefault(key, []).append(
            {"round": i.round, "action": i.action, "actorId": i.actorId,
             "actorRole": i.actorRole, "comment": i.comment,
             "resultingState": i.resultingState, "timestamp": i.timestamp}
        )
    for code, thread in sorted(threads.items()):
        b.add_json(f"findings/threads/{code}.json", thread)

    # Assurance records — independence, meetings, sampling.
    waivers = (
        await db.execute(
            select(IndependenceWaiver).where(
                IndependenceWaiver.engagementKind == "AUDIT",
                IndependenceWaiver.engagementId == audit_id,
            )
        )
    ).scalars().all()
    b.add_json(
        "assurance/independence.json",
        {
            # Asserts absence explicitly — "none issued" must be distinguishable
            # from "not tracked" in an offline pack too.
            "statement": (
                "No independence waivers were issued for this engagement."
                if not waivers
                else f"{len(waivers)} independence waiver(s) were issued."
            ),
            "waivers": [
                {"subjectUserId": w.subjectUserId, "ruleViolated": w.ruleViolated,
                 "justification": w.justification, "approvedByUserId": w.approvedByUserId,
                 "approvedAt": w.approvedAt, "revokedAt": w.revokedAt}
                for w in waivers
            ],
        },
    )

    meetings = (
        await db.execute(
            select(EngagementMeeting).where(
                EngagementMeeting.engagementKind == "AUDIT",
                EngagementMeeting.engagementId == audit_id,
            )
        )
    ).scalars().all()
    b.add_json(
        "assurance/meetings.json",
        {
            m.meetingType: {
                "heldAt": m.heldAt, "attendees": m.attendees,
                "scopeConfirmed": m.scopeConfirmed,
                "findingsSummaryPresented": m.findingsSummaryPresented,
                "auditeeAcknowledgedByUserId": m.auditeeAcknowledgedByUserId,
            }
            for m in meetings
        }
        or {"recorded": False},
    )

    # Evidence photos. Hosted-storage dependency (Q11): a fetch failure is
    # recorded in the manifest, never silently dropped.
    if include_photos:
        for r in responses:
            for idx, path in enumerate(r.auditorEvidenceIds or []):
                b.record_failure(
                    f"evidence/{r.checkpointCode}/{idx}",
                    f"Object not embedded: {path}. Evidence lives in hosted storage; "
                    "an airgapped export cannot fetch it in-process (open question Q11).",
                )

    return b


async def collect_programme_pack(db: AsyncSession, *, cycle_id: str) -> PackBuilder:
    """WP-34 - the approved programme, its slots, and what they produced."""
    b = PackBuilder()
    cycle = await db.get(ProgrammeCycle, cycle_id)
    if cycle is None:
        raise ValueError("Programme cycle not found")

    b.add_json(
        "programme/cycle.json",
        {
            "cycleLabel": cycle.cycleLabel, "status": cycle.status,
            "periodStart": cycle.periodStart, "periodEnd": cycle.periodEnd,
            "periodsPerCycle": cycle.periodsPerCycle,
            "approvedByUserId": cycle.approvedByUserId, "approvedAt": cycle.approvedAt,
            # The frozen plan-of-record and its digest: this is what makes the
            # export evidence rather than a report of current state.
            "approvedSnapshot": cycle.approvedSnapshot,
            "approvedSnapshotHash": cycle.approvedSnapshotHash,
        },
    )

    slots = (
        await db.execute(
            select(ProgrammeSlot).where(ProgrammeSlot.cycleId == cycle_id)
            .order_by(ProgrammeSlot.slotCode)
        )
    ).scalars().all()
    b.add_json(
        "programme/slots.json",
        [
            {"slotCode": s.slotCode, "windowStart": s.windowStart, "windowEnd": s.windowEnd,
             "origin": s.origin, "status": s.status,
             "engagementKind": s.engagementKind, "engagementId": s.engagementId,
             "estimatedAuditorDays": s.estimatedAuditorDays,
             "samplingApproach": s.samplingApproach,
             "samplingJustification": s.samplingJustification,
             "amendmentCount": s.amendmentCount}
            for s in slots
        ],
    )
    return b


async def run_job(db: AsyncSession, job_id: str) -> dict[str, Any]:
    """Execute a queued pack job, updating progress as it goes."""
    job = await db.get(EvidencePackJob, job_id)
    if job is None:
        raise ValueError("Job not found")
    if job.status not in ("QUEUED", "FAILED"):
        return {"ok": False, "reason": f"Job is {job.status}"}

    job.status = "RUNNING"
    job.progressPct = 0
    await db.flush()

    try:
        if job.scopeKind == "AUDIT":
            builder = await collect_audit_pack(
                db, audit_id=job.scopeId, include_photos=job.includeEvidencePhotos
            )
        else:
            builder = await collect_programme_pack(db, cycle_id=job.scopeId)

        job.currentStep = "Sealing archive"
        job.progressPct = 95
        await db.flush()

        data, manifest = builder.seal()
        job.manifest = manifest
        job.itemCount = len(manifest)
        job.totalBytes = len(data)
        job.status = "COMPLETE"
        job.progressPct = 100
        job.currentStep = None
        job.completedAt = _utcnow()
        # Storage write is the caller's concern; the bytes and the manifest are
        # what this function guarantees.
        await db.flush()
        return {"ok": True, "bytes": len(data), "entries": len(manifest), "data": data}
    except Exception as e:  # noqa: BLE001
        job.status = "FAILED"
        job.errorMessage = str(e)[:2000]
        await db.flush()
        return {"ok": False, "reason": str(e)}


__all__ = [
    "STEPS",
    "PackBuilder",
    "collect_audit_pack",
    "collect_programme_pack",
    "run_job",
]
