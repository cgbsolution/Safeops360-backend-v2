"""Feature 4 — Statutory form auto-generation.

Two jobs: (1) determine WHETHER a form is required at classification, and
(2) render an actual filled form (not a blank one) at the submission stage.

Determination runs `StatutoryTemplate.triggerConditions` against the incident;
with no templates seeded it falls back to the incident's reportability so a
property-damage-only, non-reportable incident correctly yields ZERO forms.

Generation maps incident fields into the form, renders a PDF via the platform's
fpdf2 pipeline, and persists an immutable `StatutoryFormInstance` — regeneration
mints a NEW version, never overwrites. The rendered bytes are deterministic from
the stored `fieldData` snapshot, so a form can always be re-previewed exactly as
generated.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fpdf import FPDF
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.incident import Incident, IncidentPerson
from app.models.incident_intel import StatutoryFormInstance, StatutoryTemplate
from app.models.plant import Plant

_SEV_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}

# Fallback synthetic forms when no StatutoryTemplate rows are seeded — derived
# from the regulator flags the classifier already sets on `reportableUnder`.
_REGULATOR_TO_FORM = {
    "FACTORIES_ACT": ("FORM_18", "Factories Act 1948 — Form 18 (Accident Report)"),
    "DGFASLI": ("DGFASLI_REPORT", "DGFASLI Notification of Accident"),
    "CPCB": ("CPCB_NOTIFICATION", "CPCB Environmental Incident Notification"),
}

_REPL = {"—": "-", "–": "-", "₹": "Rs. ", "→": "->", "≥": ">=", "•": "*", "'": "'", """: '"', """: '"'}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _s(text: Any) -> str:
    """fpdf2 core fonts are latin-1 only — sanitise like report_pdf._s."""
    out = str(text if text is not None else "")
    for k, v in _REPL.items():
        out = out.replace(k, v)
    return out.encode("latin-1", "replace").decode("latin-1")


def _matches(tc: dict, incident: Incident) -> bool:
    types = tc.get("incidentType") or []
    if types and (incident.type.value if incident.type else None) not in types:
        return False
    min_sev = tc.get("minSeverity")
    if min_sev and _SEV_RANK.get((incident.severity or "").upper(), -1) < _SEV_RANK.get(min_sev.upper(), 0):
        return False
    if tc.get("reportableFlag") and not incident.isReportable:
        return False
    return True


async def determine_obligation(db: AsyncSession, incident: Incident) -> dict[str, Any]:
    """Return {required, forms:[formType], jurisdiction} for the incident.
    Persisted onto `incident.statutoryObligation` by the caller."""
    templates = (
        await db.execute(select(StatutoryTemplate).where(StatutoryTemplate.active.is_(True)))
    ).scalars().all()

    forms: list[str] = []
    jurisdiction: str | None = None
    if templates:
        for t in templates:
            if _matches(t.triggerConditions or {}, incident):
                if t.formType not in forms:
                    forms.append(t.formType)
                    jurisdiction = jurisdiction or t.jurisdiction
    else:
        # Fallback: derive from reportability. Non-reportable → zero forms.
        if incident.isReportable:
            for reg in incident.reportableUnder or []:
                mapped = _REGULATOR_TO_FORM.get(reg)
                if mapped and mapped[0] not in forms:
                    forms.append(mapped[0])
            jurisdiction = "India-Factories-Act"
        # ESIC Form 16 for reportable injuries with an injured person.
        if incident.isReportable and (incident.injuredPersonName or incident.lostDays):
            if "ESIC_FORM_16" not in forms:
                forms.append("ESIC_FORM_16")

    return {"required": bool(forms), "forms": forms, "jurisdiction": jurisdiction}


def _form_title(form_type: str) -> str:
    for _, (ft, title) in _REGULATOR_TO_FORM.items():
        if ft == form_type:
            return title
    if form_type == "ESIC_FORM_16":
        return "ESIC Form 16 — Accident Report (Reportable Injury)"
    return form_type.replace("_", " ").title()


async def _field_data(db: AsyncSession, incident: Incident, form_type: str) -> dict[str, Any]:
    """Map incident fields into an ordered, human-labelled field set. Uses a
    StatutoryTemplate.fieldMapping (dot-path) when one exists for the form type,
    else a sensible built-in mapping for FORM_18 / ESIC_FORM_16."""
    plant = await db.get(Plant, incident.plantId) if incident.plantId else None
    injured = (
        await db.execute(
            select(IncidentPerson).where(IncidentPerson.incidentId == incident.id).where(IncidentPerson.isInjured.is_(True)).limit(1)
        )
    ).scalar_one_or_none()

    def occurred() -> str:
        d = incident.occurredAt or incident.date
        return d.strftime("%Y-%m-%d %H:%M") if d else "—"

    fields: dict[str, Any] = {
        "Incident Number": incident.number,
        # This form goes to a factory inspector. A cuid in the "Factory" field
        # is worse than an explicit blank — it reads as a filled-in answer.
        "Factory / Plant": (f"{plant.name} ({plant.code})" if plant else "—"),
        "State": (plant.state if plant else "—"),
        "Date & Time of Accident": occurred(),
        "Location": " ".join(filter(None, [incident.location, incident.specificLocation])) or "—",
        "Incident Type": incident.type.value if incident.type else "—",
        "Severity": incident.severity or "—",
        "Description": incident.description or "—",
        "Immediate Cause": incident.immediateCause or "—",
        "Activity Being Performed": incident.activityBeingPerformed or "—",
    }
    # Injured-person block (Form 18 / ESIC 16).
    name = (injured.externalName if injured else None) or incident.injuredPersonName
    fields.update({
        "Injured Person": name or "—",
        "Age": incident.injuredPersonAge or "—",
        "Designation": incident.injuredPersonDesignation or "—",
        "Nature of Injury": (injured.natureOfInjury if injured else None) or incident.natureOfInjury or "—",
        "Body Part Affected": (injured.bodyPartAffected if injured else None) or incident.bodyPart or "—",
        "Days Lost": (injured.daysOff if injured else None) or incident.lostDays or 0,
        "Hospital": (injured.hospitalName if injured else None) or "—",
    })

    # A seeded template can override/extend via dot-path field mapping.
    template = (
        await db.execute(
            select(StatutoryTemplate).where(StatutoryTemplate.formType == form_type).where(StatutoryTemplate.active.is_(True)).limit(1)
        )
    ).scalar_one_or_none()
    if template and template.fieldMapping:
        for form_key, dot_path in (template.fieldMapping or {}).items():
            fields[form_key] = _resolve_path(incident, dot_path)
    return fields


def _resolve_path(obj: Any, dot_path: str) -> Any:
    cur = obj
    for part in str(dot_path).split("."):
        cur = getattr(cur, part, None)
        if cur is None:
            return "—"
    return cur


def render_form_pdf(form_type: str, title: str, fields: dict[str, Any], meta: dict[str, Any]) -> bytes:
    """Render a filled statutory form as a PDF (bytes). Generic label/value
    layout in the platform's report style."""
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    # Header band.
    pdf.set_fill_color(11, 31, 77)
    pdf.rect(0, 0, 210, 30, style="F")
    pdf.set_xy(10, 8)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(190, 8, _s(title), align="L")
    pdf.set_xy(10, 18)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(190, 6, _s(f"Jurisdiction: {meta.get('jurisdiction') or '-'}   |   Form: {form_type}   |   Version {meta.get('version', 1)}"), align="L")

    pdf.set_xy(10, 38)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 10)

    for label, value in fields.items():
        pdf.set_x(10)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_fill_color(240, 243, 250)
        pdf.cell(65, 7, _s(label), border=1, fill=True)
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(125, 7, _s(value), border=1)

    # Footer note.
    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(
        190, 4,
        _s(
            f"Auto-generated by SafeOps360 on {meta.get('generatedAt', '')} from incident "
            f"{meta.get('incidentNumber', '')}. Immutable version {meta.get('version', 1)} — "
            "regeneration produces a new version. This is a system-filled draft for review "
            "before statutory submission."
        ),
    )
    out = pdf.output()
    return bytes(out)


async def generate_form(
    db: AsyncSession, incident: Incident, form_type: str, *, actor_id: str | None = None
) -> StatutoryFormInstance:
    """Generate an immutable filled form instance (new version each call)."""
    obligation = incident.statutoryObligation or {}
    jurisdiction = obligation.get("jurisdiction")
    fields = await _field_data(db, incident, form_type)

    # Version = existing count + 1; supersede the prior current version.
    prior = (
        await db.execute(
            select(StatutoryFormInstance)
            .where(StatutoryFormInstance.incidentId == incident.id)
            .where(StatutoryFormInstance.formType == form_type)
        )
    ).scalars().all()
    version = len(prior) + 1
    for p in prior:
        p.isCurrent = False

    title = _form_title(form_type)
    file_name = f"{form_type}_{incident.number}_v{version}.pdf"
    meta = {
        "jurisdiction": jurisdiction, "version": version,
        "incidentNumber": incident.number, "generatedAt": _now().strftime("%Y-%m-%d %H:%M"),
    }
    pdf_bytes = render_form_pdf(form_type, title, fields, meta)

    storage_path = None
    try:
        from app.services.storage import build_storage_path, is_storage_configured, upload_object

        if is_storage_configured():
            storage_path = build_storage_path(incident_id=incident.id, category="EXTERNAL_REPORT", file_name=file_name)
            upload_object(storage_path, pdf_bytes, "application/pdf")
    except Exception:  # noqa: BLE001 — storage optional; download re-renders from fieldData
        storage_path = None

    inst = StatutoryFormInstance(
        incidentId=incident.id, formType=form_type, jurisdiction=jurisdiction, version=version,
        fileName=file_name, storagePath=storage_path,
        fieldData={"title": title, "fields": fields, "meta": meta}, generatedById=actor_id, isCurrent=True,
    )
    db.add(inst)
    await db.flush()
    await db.refresh(inst)
    return inst


def rerender_instance(inst: StatutoryFormInstance) -> bytes:
    """Deterministically re-render a stored instance from its immutable snapshot."""
    data = inst.fieldData or {}
    return render_form_pdf(
        inst.formType, data.get("title") or _form_title(inst.formType),
        data.get("fields") or {}, data.get("meta") or {"version": inst.version},
    )
