"""Guided Field Capture — async voice pipeline (spec 1.2 screen 5).

Two passes, run by the ``capture_voice_pipeline`` scheduler job (never in the
submit path — transcription/translation must not block submission):

1. **Transcription**: submissions with ``transcriptionStatus='pending'`` (a
   VOICE attachment exists, no on-device transcript, feature flag on) go
   through ``ITranscriptionProvider``. The default stub returns None — the
   audio stays stored, status flips back to 'none' so the queue never wedges;
   a real provider drop-in makes this pass productive without code changes.

2. **Translation**: any transcriptOriginal without a transcriptEnglish is
   translated via the platform Anthropic client (fails soft — untouched rows
   are retried next run).
"""

from __future__ import annotations

import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capture import CaptureAttachment, CaptureSubmission
from app.services.ai.anthropic_client import complete_json, is_configured
from app.services.ai.capture_providers import get_transcription_provider

_TRANSLATE_SYSTEM = """You translate short factory-floor safety voice notes into English.
Respond ONLY with JSON: {"english": "<faithful, plain-English translation>"}.
Keep machine names / codes as spoken. If the text is already English, return it unchanged."""


async def run_capture_voice(db: AsyncSession) -> dict:
    transcribed = 0
    stubbed = 0
    translated = 0
    failed = 0

    # ── pass 1: provider transcription ──
    provider = get_transcription_provider()
    pending = (
        await db.execute(
            select(CaptureSubmission)
            .where(CaptureSubmission.transcriptionStatus == "pending")
            .where(CaptureSubmission.isDeleted.is_(False))
            .limit(20)
        )
    ).scalars().all()
    for sub in pending:
        voice_att = (
            await db.execute(
                select(CaptureAttachment)
                .where(CaptureAttachment.submissionId == sub.id)
                .where(CaptureAttachment.kind == "VOICE")
                .where(CaptureAttachment.deletedAt.is_(None))
                .order_by(CaptureAttachment.uploadedAt.desc())
            )
        ).scalars().first()
        if voice_att is None:
            sub.transcriptionStatus = "none"
            continue
        try:
            from app.services.storage import download_object, is_storage_configured

            if not is_storage_configured():
                continue  # retry when storage comes back
            audio = download_object(voice_att.storagePath)
            result = await provider.transcribe(audio, voice_att.mimeType, sub.voiceLangCode)
            if result is None:
                # stub (or provider miss): audio stays stored, transcript null —
                # release the row so the queue never wedges (see module docstring)
                sub.transcriptionStatus = "none"
                stubbed += 1
            else:
                sub.transcriptOriginal = result["text"]
                if result.get("languageCode"):
                    sub.voiceLangCode = result["languageCode"]
                sub.transcriptionStatus = "done"
                transcribed += 1
        except Exception as e:  # noqa: BLE001
            print(f"[capture-voice] transcription failed for {sub.number}: {e}", file=sys.stderr)
            sub.transcriptionStatus = "failed"
            failed += 1

    # ── pass 2: English translation of any transcript ──
    if is_configured():
        untranslated = (
            await db.execute(
                select(CaptureSubmission)
                .where(CaptureSubmission.transcriptOriginal.is_not(None))
                .where(CaptureSubmission.transcriptEnglish.is_(None))
                .where(CaptureSubmission.isDeleted.is_(False))
                .limit(20)
            )
        ).scalars().all()
        for sub in untranslated:
            data = await complete_json(
                system=_TRANSLATE_SYSTEM,
                user=f"languageCode={sub.voiceLangCode or 'unknown'}\ntext={sub.transcriptOriginal}",
                max_tokens=400,
                temperature=0.1,
            )
            english = (data or {}).get("english")
            if isinstance(english, str) and english.strip():
                sub.transcriptEnglish = english.strip()
                translated += 1

    await db.commit()
    return {
        "provider": provider.name,
        "transcribed": transcribed,
        "stubReleased": stubbed,
        "translated": translated,
        "failed": failed,
    }
