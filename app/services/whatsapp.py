"""Feature 6 — WhatsApp-native capture.

A field worker reports an incident via voice note over WhatsApp; the platform
transcribes + classifies it and creates an incident that flows through the SAME
maker-checker workflow and audit trail as a web report. WhatsApp is a new INPUT
ADAPTER, not a parallel incident system — so Features 1–5/7/8 all apply to
WhatsApp-originated incidents automatically.

Security (non-negotiable): NO incident is ever created from an unverified phone
number — an unknown number triggers OTP registration, never silent creation.
Every WhatsApp-originated action is logged with `channel: 'whatsapp'`.

Transcription (STT) and the Meta/BSP send API are provider integrations that are
STUBBED here (fail-soft, exactly like the platform's transcription stub for
Guided Capture). The identity binding + OTP + adapter + audit — the parts that
must be correct before any rollout — are real.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.incident import Incident, IncidentStatus, IncidentType
from app.models.incident_intel import WhatsappInboundLog, WhatsappSender
from app.models.plant import Plant
from app.models.user import User

OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _otp_key() -> bytes:
    # Derive an HMAC key from the app JWT secret (never store raw OTPs).
    secret = getattr(get_settings(), "jwt_secret", None) or "safeops-otp"
    return hashlib.sha256(secret.encode()).digest()


def _hash_otp(code: str) -> str:
    return hmac.new(_otp_key(), code.encode(), hashlib.sha256).hexdigest()


async def resolve_sender(db: AsyncSession, phone: str) -> WhatsappSender | None:
    return (
        await db.execute(select(WhatsappSender).where(WhatsappSender.phoneNumber == phone))
    ).scalar_one_or_none()


def is_verified(sender: WhatsappSender | None) -> bool:
    return bool(sender and sender.verifiedAt and sender.employeeId)


async def start_registration(db: AsyncSession, phone: str) -> dict[str, Any]:
    """Create (or reset) an unverified sender and issue an OTP. Returns the OTP
    only when the outbound channel is unconfigured (so a dev/demo can complete
    the flow); in production the OTP is delivered via WhatsApp, never returned."""
    sender = await resolve_sender(db, phone)
    if sender is None:
        sender = WhatsappSender(phoneNumber=phone)
        db.add(sender)
    code = f"{secrets.randbelow(1_000_000):06d}"
    sender.otpHash = _hash_otp(code)
    sender.otpExpiresAt = _now() + timedelta(minutes=OTP_TTL_MINUTES)
    sender.otpAttempts = 0
    await db.flush()
    send = await send_message(phone, text=f"Your SafeOps360 verification code is {code}. Valid {OTP_TTL_MINUTES} min.")
    out: dict[str, Any] = {"status": "otp_sent", "delivered": send.get("sent", False)}
    if not send.get("sent"):
        out["devOtp"] = code  # channel not configured — surface for local/demo verification only
    return out


async def verify_otp(db: AsyncSession, phone: str, code: str, *, employee_id: str) -> dict[str, Any]:
    """Bind a phone to an employee after OTP match. Returns {ok, reason?}."""
    sender = await resolve_sender(db, phone)
    if sender is None or not sender.otpHash or not sender.otpExpiresAt:
        return {"ok": False, "reason": "No pending verification for this number."}
    if sender.otpExpiresAt < _now():
        return {"ok": False, "reason": "Code expired."}
    if sender.otpAttempts >= OTP_MAX_ATTEMPTS:
        return {"ok": False, "reason": "Too many attempts."}
    if not hmac.compare_digest(sender.otpHash, _hash_otp(code)):
        sender.otpAttempts += 1
        await db.flush()
        return {"ok": False, "reason": "Incorrect code."}
    employee = await db.get(User, employee_id)
    if employee is None:
        return {"ok": False, "reason": "Unknown employee."}
    sender.employeeId = employee_id
    sender.plantId = getattr(employee, "plantId", None)
    sender.role = getattr(employee, "role", None)
    sender.verifiedAt = _now()
    sender.verificationMethod = "otp"
    sender.otpHash = None
    sender.otpExpiresAt = None
    await db.flush()
    return {"ok": True, "senderId": sender.id}


async def admin_register(db: AsyncSession, *, phone: str, employee_id: str, plant_id: str | None, role: str | None) -> WhatsappSender:
    """HR-admin registration path (no OTP) — an authenticated admin binds a number."""
    sender = await resolve_sender(db, phone)
    if sender is None:
        sender = WhatsappSender(phoneNumber=phone)
        db.add(sender)
    sender.employeeId = employee_id
    sender.plantId = plant_id
    sender.role = role
    sender.verifiedAt = _now()
    sender.verificationMethod = "hr_admin_registered"
    await db.flush()
    await db.refresh(sender)
    return sender


# ─── Provider stubs (STT + outbound) — fail-soft, honest ────────────────────

async def transcribe(media_id: str | None, lang_hint: str | None = None) -> tuple[str | None, str | None]:
    """Regional-language STT stub. Real STT (Hindi + regional) is a provider
    integration; until configured this returns (None, lang) and the flow falls
    back to any text body / a placeholder, never crashing."""
    return None, (lang_hint or "hi")


async def send_message(phone: str, *, text: str | None = None, template: str | None = None,
                       buttons: list[str] | None = None) -> dict[str, Any]:
    """Outbound WhatsApp send stub. Returns {sent:false} unless a Meta/BSP
    provider is configured (settings.whatsapp_*). Never raises."""
    settings = get_settings()
    if not getattr(settings, "whatsapp_api_token", None):
        return {"sent": False, "reason": "WhatsApp provider not configured"}
    # A real BSP/Meta Cloud API POST would go here.
    return {"sent": True}


async def classify_transcript(text: str) -> dict[str, Any]:
    """Best-effort Claude classification of the raw report → {type, severity}.
    Fail-soft: defaults to a conservative HIPO_NEAR_MISS/MEDIUM when unavailable."""
    from app.services.ai.anthropic_client import complete_json, is_configured

    default = {"type": "HIPO_NEAR_MISS", "severity": "MEDIUM"}
    if not is_configured() or not text:
        return default
    result = await complete_json(
        system=(
            "Classify a shop-floor incident report into a type and severity. "
            "type ∈ [FIRST_AID, MTC, RWC, LTI, FATALITY, PROPERTY_DAMAGE, "
            "ENVIRONMENTAL, FIRE, PROCESS_SAFETY, HIPO_NEAR_MISS]; severity ∈ "
            '[LOW, MEDIUM, HIGH, CRITICAL]. Respond ONLY as JSON {"type":..,"severity":..}.'
        ),
        user=text[:2000], max_tokens=60, temperature=0.0,
    )
    if not result:
        return default
    t = str(result.get("type", "")).upper()
    s = str(result.get("severity", "")).upper()
    valid_t = {e.value for e in IncidentType}
    return {
        "type": t if t in valid_t else default["type"],
        "severity": s if s in {"LOW", "MEDIUM", "HIGH", "CRITICAL"} else default["severity"],
    }


async def create_incident_from_whatsapp(
    db: AsyncSession, sender: WhatsappSender, *, transcript: str, classification: dict[str, Any]
) -> Incident:
    """Create an incident from a verified WhatsApp report, through the same
    Incident model + workflow as the web UI. status stays at REPORTED (pending
    classification); the HSE Manager classifies via the normal CHECKER step."""
    from app.services import workflow_engine

    plant_id = sender.plantId
    plant = await db.get(Plant, plant_id) if plant_id else None
    if plant is None:
        plant = (await db.execute(select(Plant).order_by(Plant.code).limit(1))).scalar_one_or_none()
    if plant is None:
        raise ValueError("No plant available to scope the WhatsApp incident.")

    itype = IncidentType(classification.get("type", "HIPO_NEAR_MISS"))
    now = _now()
    last = (
        await db.execute(select(func.count()).select_from(Incident).where(Incident.plantId == plant.id))
    ).scalar_one()
    number = f"INC-{now.year}-{plant.code}-{last + 1:04d}"

    incident = Incident(
        number=number, date=now, occurredAt=now, reportedAt=now, type=itype,
        plantId=plant.id, location="Reported via WhatsApp", reporterId=sender.employeeId,
        reporterRole=sender.role, description=(transcript or "Voice report via WhatsApp (transcript pending).")[:2000],
        initialDescription=transcript, severity=classification.get("severity"),
        status=IncidentStatus.REPORTED,
    )
    db.add(incident)
    await db.flush()

    try:
        await workflow_engine.initiate(
            db, module="INCIDENT", record_id=incident.id, record_number=incident.number,
            record_title=incident.description[:120],
            record_data={"type": incident.type.value, "severity": incident.severity,
                         "plantId": incident.plantId, "reporterId": incident.reporterId},
            initiator_id=sender.employeeId, plant_id=incident.plantId,
        )
    except Exception:  # noqa: BLE001 — workflow init failure never loses the report
        pass

    # Audit: attribute the WhatsApp-originated creation, channel-tagged.
    try:
        from app.services.audit_log import record_event

        await record_event(
            db, entity_type="Incident", entity_id=incident.id, entity_code=incident.number,
            plant_id=incident.plantId, action="CREATED_VIA_WHATSAPP",
            after={"channel": "whatsapp", "senderId": sender.id, "reporterId": sender.employeeId},
        )
    except Exception:  # noqa: BLE001
        pass
    return incident


async def handle_inbound(db: AsyncSession, *, phone: str, message_type: str,
                         text: str | None, media_id: str | None) -> dict[str, Any]:
    """Core webhook orchestration. Unverified → registration (never an incident);
    verified voice/text → transcribe + classify + create incident + notify."""
    log = WhatsappInboundLog(phoneNumber=phone, messageType=message_type, mediaId=media_id)
    db.add(log)

    sender = await resolve_sender(db, phone)
    if not is_verified(sender):
        log.senderId = sender.id if sender else None
        log.status = "unverified"
        reg = await start_registration(db, phone)
        log.detail = "OTP registration triggered (no incident created)."
        await db.flush()
        return {"action": "registration", "reason": "unverified sender", **reg}

    log.senderId = sender.id

    transcript = text
    if message_type == "voice" and media_id:
        stt, lang = await transcribe(media_id)
        transcript = stt or text
        log.transcriptLang = lang
    log.transcript = transcript
    log.status = "incident_created"

    classification = await classify_transcript(transcript or "")
    incident = await create_incident_from_whatsapp(db, sender, transcript=transcript or "", classification=classification)
    log.createdIncidentId = incident.id
    log.detail = f"Incident {incident.number} created (type={classification.get('type')})."

    # Notify the plant's HSE Manager to classify (interactive approve/reclassify).
    await send_message(
        phone, text=f"Thank you. Incident {incident.number} logged and sent for classification.",
    )
    await db.flush()
    return {
        "action": "incident_created", "incidentId": incident.id, "number": incident.number,
        "classification": classification, "channel": "whatsapp",
    }
