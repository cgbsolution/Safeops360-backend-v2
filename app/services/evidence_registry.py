"""Evidence Attachment entity registry (Stream B §5).

Maps an `entityType` string to everything the generic attachment router needs to
serve it safely: the SQLAlchemy model (to prove the parent exists + resolve its
plant), the permission codes to gate read vs write, and the allowed upload
categories. Adding a new attachable module (EAI SDS, Contractor certs, Training
certs, …) is one `EntitySpec` here — the router never changes.

This is where the shared capability earns its keep: the spec names four priority
modules (CAMS/Statutory, EAI, Contractor, Training); each is a registry line.
This pass wires CAMS first (highest compliance cost, spec §5.3).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.audit_compliance import (
    AuditCheckpointResponse,
    ComplianceAudit,
)
from app.models.cams import CamsFinding
from app.models.chemical import ChemicalDisposalRecord, ChemicalMaster
from app.models.factory import FactoryCertification, FactoryProfile
from app.models.factory_ext import RegulatoryRegistration
from app.models.programme import ProgrammeCycle


@dataclass(frozen=True)
class EntitySpec:
    label: str
    model: type
    # Column on the model row holding the plant/site id for the permission
    # PermissionContext, or None for platform-scoped entities.
    plant_attr: str | None
    read_perm: str
    write_perm: str
    # Allowed per-entity `category` values for an upload.
    categories: frozenset[str]


REGISTRY: dict[str, EntitySpec] = {
    # ── CAMS audit-finding evidence (spec §5.3 #1 — CAMS/Statutory) ──────────
    # Attach the source document that substantiates a finding / its closure.
    "cams_finding": EntitySpec(
        label="Audit finding",
        model=CamsFinding,
        plant_attr="siteId",
        read_perm="CAMS.READ",
        write_perm="CAMS.FINDING_MANAGE",
        categories=frozenset(
            {"FINDING_EVIDENCE", "CLOSURE_EVIDENCE", "CERTIFICATE", "LICENSE", "REPORT", "OTHER"}
        ),
    ),
    # ── WP-26: audit-side evidence on the SAME platform layer ────────────────
    #
    # The audit engine stored evidence as raw storage paths in a JSON array
    # (`AuditCheckpointResponse.auditorEvidenceIds`) with no FK, no versioning,
    # no uploader and no soft-delete — which is why 15 photos across 2,503
    # checkpoints all pointed at 2 placeholder paths and nothing noticed (F-25,
    # F-26, F-54). Registering the audit entities here gives them the same
    # `Attachment` guarantees the inspection side already had, without a second
    # upload path to maintain.
    #
    # The legacy JSON array is NOT removed: existing rows still reference it and
    # the conduct screen still writes it. New evidence lands in `Attachment`;
    # the two are read together until a later migration folds the old paths in.
    "audit_checkpoint": EntitySpec(
        label="Audit checkpoint",
        model=AuditCheckpointResponse,
        plant_attr="plantId",
        read_perm="AUDIT_COMPLIANCE.READ",
        write_perm="AUDIT_COMPLIANCE.EXECUTE",
        categories=frozenset(
            {"FINDING_EVIDENCE", "CLOSURE_EVIDENCE", "OBSERVATION_PHOTO",
             "CERTIFICATE", "LICENSE", "OTHER"}
        ),
    ),
    # Engagement-level documents that belong to the audit rather than to any one
    # checkpoint: the signed report, meeting minutes, the auditee's response pack.
    "compliance_audit": EntitySpec(
        label="Audit",
        model=ComplianceAudit,
        plant_attr="plantId",
        read_perm="AUDIT_COMPLIANCE.READ",
        write_perm="AUDIT_COMPLIANCE.UPDATE",
        categories=frozenset(
            {"REPORT", "MEETING_MINUTES", "SIGNED_REPORT", "AUDITEE_RESPONSE",
             "CERTIFICATE", "CORRESPONDENCE", "OTHER"}
        ),
    ),
    # Programme-cycle documents: the approved programme document, the management
    # review minutes behind a ProgrammeReview, external-body schedules.
    "programme_cycle": EntitySpec(
        label="Programme cycle",
        model=ProgrammeCycle,
        plant_attr=None,  # a cycle spans sites; permission is platform-scoped
        read_perm="CAMS.READ",
        write_perm="CAMS.SCHEDULE",
        categories=frozenset(
            {"PROGRAMME_DOCUMENT", "REVIEW_MINUTES", "EXTERNAL_SCHEDULE",
             "CORRESPONDENCE", "OTHER"}
        ),
    ),
    # ── Chemical / Hazmat: SDS sheets and disposal manifests ────────────────
    #
    # BASIC TIER ONLY. The SDS is attached as supporting evidence and is never
    # opened by the platform: hazard classification, flash point and NFPA
    # ratings are entered by a human who has read the sheet. `extraction` stays
    # null on these rows — AI/OCR extraction of SDS content is a separate
    # airgapped commercial add-on and is deliberately out of scope for the
    # Chemical module (its build spec §0/§8). If a future change starts
    # populating `extraction` for documentCategory=SDS, that is a licensing
    # decision, not a refactor.
    "chemical_master": EntitySpec(
        label="Chemical",
        model=ChemicalMaster,
        # A ChemicalMaster is tenant-scoped, not plant-scoped: the same
        # substance is one master row used across every site.
        plant_attr=None,
        read_perm="INCIDENT.READ",
        write_perm="INCIDENT.UPDATE",
        categories=frozenset({"SDS_SHEET", "CERTIFICATE", "LICENSE", "REPORT", "OTHER"}),
    ),
    "chemical_disposal": EntitySpec(
        label="Disposal record",
        model=ChemicalDisposalRecord,
        plant_attr="plantId",
        read_perm="INCIDENT.READ",
        write_perm="INCIDENT.UPDATE",
        categories=frozenset({"DISPOSAL_MANIFEST", "CERTIFICATE", "CORRESPONDENCE", "OTHER"}),
    ),
    # ── Facilities: statutory licences, certifications and profile documents ─
    #
    # The Factory Licences register (RegulatoryRegistration) held dates and a
    # dormant `documentationIds` JSON column but no way to attach the licence
    # itself, so the statutory approvals a factory is asked for at audit — the
    # factory licence, fire NOC, structural stability certificate, the SPCB
    # (e.g. KSPCB) consents — lived in somebody's shared drive. Registering here
    # gives each licence row the same versioned, permissioned, soft-deletable
    # document store every other module already uses: a centralised repository
    # of statutory approvals that survives a renewal (re-upload against the same
    # `slotKey` supersedes rather than overwrites).
    "factory_registration": EntitySpec(
        label="Factory licence / registration",
        model=RegulatoryRegistration,
        plant_attr="siteId",
        read_perm="FACILITY.READ",
        write_perm="FACILITY.UPDATE",
        categories=frozenset(
            {"LICENSE", "CERTIFICATE", "CONSENT", "RENEWAL_APPLICATION",
             "INSPECTION_REPORT", "CORRESPONDENCE", "OTHER"}
        ),
    ),
    # Buyer / social-compliance certificates (SA8000, WRAP, ISO, SMETA …) —
    # `FactoryCertification.attachmentIds` was the same dormant hook.
    "factory_certification": EntitySpec(
        label="Factory certification",
        model=FactoryCertification,
        plant_attr="siteId",
        read_perm="FACILITY.READ",
        write_perm="FACILITY.CERT_MANAGE",
        categories=frozenset({"CERTIFICATE", "AUDIT_REPORT", "SCOPE_DOCUMENT", "CORRESPONDENCE", "OTHER"}),
    ),
    # Profile-level documents that belong to the factory rather than to any one
    # licence: the site layout / plot plan, the occupancy certificate, land
    # records, the signed profile itself.
    "factory_profile": EntitySpec(
        label="Factory profile",
        model=FactoryProfile,
        plant_attr="siteId",
        read_perm="FACILITY.READ",
        write_perm="FACILITY.UPDATE",
        categories=frozenset(
            {"SITE_LAYOUT", "OCCUPANCY_CERTIFICATE", "LAND_RECORD", "LICENSE",
             "CERTIFICATE", "REPORT", "OTHER"}
        ),
    ),
    # ── Follow-ups (spec §5.3 #2-4) — each is a single line once wired: ──────
    #   "eai_entry":        EAI SDS sheets      → read EAI.READ  / write EAI.UPDATE
    #   "contractor":       insurance/comp certs→ read EPC.READ  / write EPC.UPDATE
    #   "training_record":  training certs      → read TRAINING.READ / write TRAINING.UPDATE
}


def get_spec(entity_type: str) -> EntitySpec | None:
    return REGISTRY.get(entity_type)


def supported_entities() -> list[str]:
    return sorted(REGISTRY)
