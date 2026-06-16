"""Tool: search_documents_reviewed.

Surfaces IncidentDocumentReview rows from PAST closed incidents that
share characteristics with the current one. The signal: "in similar
past investigations, here are the SOPs / standards / drawings the
investigators reviewed — and what they found wrong with them."

This is the bibliographic equivalent of find_similar_incidents — same
hindsight, narrower lens. The investigator can use it to skip ahead:
if the same SOP was implicated in three past incidents, that SOP is
likely worth re-reading.

NOTE: The repo has no first-class Document master table — documents
are referenced by free-text `documentReference` on the document-review
rows. We aggregate those references across similar incidents.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.incident import (
    Incident,
    IncidentDocumentReview,
    IncidentStatus,
    IncidentType,
)


DEFINITION: dict[str, Any] = {
    "name": "search_documents_reviewed",
    "description": (
        "Find documents (SOPs, standards, drawings, permits, training records) "
        "that were reviewed in PAST closed incidents matching the current one. "
        "Returns each document with the compliance verdict from that prior "
        "review and the incident it came from. Use this to identify "
        "documentation gaps that have been flagged before — if the same SOP "
        "shows up across multiple past incidents with NON_COMPLIANT verdicts, "
        "the SOP itself is a candidate root cause. Does NOT include the source "
        "incident's own document reviews; those are already in your input "
        "context."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "incidentType": {
                "type": "string",
                "description": (
                    "Type code of similar incidents to draw document reviews from. "
                    "Defaults to the source incident's type."
                ),
            },
            "documentType": {
                "type": "string",
                "description": (
                    "Optional filter on document category. Free-text — values seen "
                    "in seed data: 'SOP', 'STANDARD', 'DRAWING', 'PERMIT', "
                    "'TRAINING_RECORD', 'INSPECTION_REPORT', 'MOC'."
                ),
            },
            "complianceFinding": {
                "type": "string",
                "description": (
                    "Optional filter on the reviewer's verdict. Values seen: "
                    "'COMPLIANT', 'NON_COMPLIANT', 'NOT_APPLICABLE', 'INADEQUATE'. "
                    "Set to NON_COMPLIANT to focus only on documents that failed "
                    "review in past investigations."
                ),
            },
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional keywords to match against documentReference and "
                    "reviewNotes (case-insensitive substring)."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max document-review rows to return. Default 10, max 25.",
                "minimum": 1,
                "maximum": 25,
            },
        },
        "required": [],
    },
}


async def handle(
    input: dict[str, Any],  # noqa: A002
    *,
    db: AsyncSession,
    source_record_id: str,
    source_module: str,
) -> dict[str, Any]:
    incident_type_filter = input.get("incidentType")
    if not incident_type_filter and source_module == "INCIDENT":
        source = await db.get(Incident, source_record_id)
        if source is not None:
            incident_type_filter = (
                source.type.value if hasattr(source.type, "value") else str(source.type)
            )

    if incident_type_filter:
        valid_types = {t.value for t in IncidentType}
        if incident_type_filter not in valid_types:
            raise ValueError(
                f"Unknown incidentType {incident_type_filter!r}. "
                f"Valid: {sorted(valid_types)}"
            )

    limit = min(int(input.get("limit", 10)), 25)

    stmt = (
        select(IncidentDocumentReview, Incident.number, Incident.type, Incident.severity)
        .join(Incident, Incident.id == IncidentDocumentReview.incidentId)
        .where(Incident.status == IncidentStatus.CLOSED)
        .where(Incident.id != source_record_id)
    )

    if incident_type_filter:
        stmt = stmt.where(Incident.type == IncidentType(incident_type_filter))

    if doc_type := input.get("documentType"):
        stmt = stmt.where(IncidentDocumentReview.documentType == doc_type)

    if finding := input.get("complianceFinding"):
        stmt = stmt.where(IncidentDocumentReview.complianceFinding == finding)

    if keywords := input.get("keywords"):
        clauses = []
        for kw in keywords:
            pat = f"%{kw}%"
            clauses.append(
                or_(
                    IncidentDocumentReview.documentReference.ilike(pat),
                    IncidentDocumentReview.reviewNotes.ilike(pat),
                )
            )
        stmt = stmt.where(or_(*clauses))

    stmt = stmt.order_by(Incident.date.desc()).limit(limit)
    rows = (await db.execute(stmt)).all()

    return {
        "incidentTypeMatched": incident_type_filter,
        "documents": [
            {
                "documentType": dr.documentType,
                "documentReference": dr.documentReference,
                "complianceFinding": dr.complianceFinding,
                "reviewNotesPreview": (dr.reviewNotes or "")[:240],
                "fromIncident": {
                    "incidentNumber": inc_number,
                    "incidentType": _enum_value(inc_type),
                    "severity": severity,
                },
            }
            for dr, inc_number, inc_type, severity in rows
        ],
    }


def _enum_value(v: Any) -> str | None:
    if v is None:
        return None
    return v.value if hasattr(v, "value") else str(v)
