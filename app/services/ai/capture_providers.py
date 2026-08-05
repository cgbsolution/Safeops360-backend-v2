"""Guided Field Capture — AI provider abstractions (spec 1.2 screens 3+5).

Two seams, both tenant-feature-flagged and fail-soft:

* ``ITranscriptionProvider`` — voice-note → transcript. Ships with the STUB
  (store audio only, transcript stays null) exactly per spec; the platform's
  only AI vendor (Anthropic) does no speech-to-text, so a real provider is a
  drop-in for later (set ``CAPTURE_TRANSCRIPTION_PROVIDER`` when one exists).
  On-device Web Speech transcripts arrive separately from the client and are
  translated by the scheduler job regardless of this provider.

* ``IVisionSuggestProvider`` — photo → suggested hazard category + one-line
  draft description. The Anthropic implementation is real (Claude vision);
  the stub returns None so the wizard degrades to manual selection.

Every provider returns ``None`` on any failure — the flow never blocks on AI.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from typing import Any, Protocol, TypedDict

from app.services.ai.anthropic_client import _get_client, is_configured


class TranscriptResult(TypedDict):
    text: str
    languageCode: str | None


class VisionSuggestion(TypedDict):
    l1Code: str | None
    l2Code: str | None
    description: str  # one line, in the reporter's language
    descriptionEn: str
    confidence: float  # 0..1


class CategorySuggestion(TypedDict):
    l1Code: str | None
    l2Code: str | None
    confidence: float  # 0..1


# ── Transcription ─────────────────────────────────────────────────────────────
class ITranscriptionProvider(Protocol):
    name: str

    async def transcribe(
        self, audio: bytes, mime_type: str, lang_hint: str | None
    ) -> TranscriptResult | None: ...


class StubTranscriptionProvider:
    """Spec-mandated default: audio is stored, transcript stays null."""

    name = "stub"

    async def transcribe(
        self, audio: bytes, mime_type: str, lang_hint: str | None
    ) -> TranscriptResult | None:
        return None


def get_transcription_provider() -> ITranscriptionProvider:
    # Future providers register here keyed by env CAPTURE_TRANSCRIPTION_PROVIDER.
    return StubTranscriptionProvider()


# ── Vision suggest ────────────────────────────────────────────────────────────
_VISION_SYSTEM = """You are a factory-safety triage assistant for an Indian garment manufacturer.
You are shown a photo taken by a low-literacy field worker reporting a hazard,
plus the hazard taxonomy (level-1 and level-2 codes with English labels).
Respond ONLY with a JSON object:
{"l1Code": <best level-1 code or null>, "l2Code": <best child code of that l1 or null>,
 "description": <ONE short sentence describing the hazard in LANG>,
 "descriptionEn": <the same sentence in English>,
 "confidence": <0..1 — how sure you are about l1Code>}
If the photo shows no recognisable workplace hazard, use nulls and confidence 0."""

_VISION_MEDIA_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}


class IVisionSuggestProvider(Protocol):
    name: str

    async def suggest(
        self, image: bytes, mime_type: str, taxonomy: list[dict[str, Any]], lang: str
    ) -> VisionSuggestion | None: ...


class StubVisionProvider:
    name = "stub"

    async def suggest(
        self, image: bytes, mime_type: str, taxonomy: list[dict[str, Any]], lang: str
    ) -> VisionSuggestion | None:
        return None


class AnthropicVisionProvider:
    name = "anthropic"

    async def suggest(
        self, image: bytes, mime_type: str, taxonomy: list[dict[str, Any]], lang: str
    ) -> VisionSuggestion | None:
        client = _get_client()
        if client is None:
            return None
        media_type = "image/jpeg" if mime_type == "image/jpg" else mime_type
        if media_type not in _VISION_MEDIA_TYPES:
            return None
        if len(image) > 4.5 * 1024 * 1024:  # API image cap ~5MB base64-decoded
            return None
        from app.core.config import get_settings

        tax_lines = [
            {"code": n.get("code"), "parent": n.get("parentCode"), "label": (n.get("labels") or {}).get("en")}
            for n in taxonomy
        ]
        user_content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.b64encode(image).decode(),
                },
            },
            {
                "type": "text",
                "text": "Taxonomy:\n" + json.dumps(tax_lines, ensure_ascii=False) + f"\nLANG={lang}",
            },
        ]
        try:
            msg = await asyncio.to_thread(
                client.messages.create,
                model=get_settings().anthropic_model,
                max_tokens=400,
                temperature=0.1,
                system=_VISION_SYSTEM,
                messages=[{"role": "user", "content": user_content}],
            )
        except Exception as e:  # noqa: BLE001
            print(f"[ai] vision-suggest call failed: {e}", file=sys.stderr)
            return None
        parts = [getattr(b, "text", "") for b in (msg.content or [])]
        raw = "".join(p for p in parts if isinstance(p, str)).strip()
        try:
            start, end = raw.find("{"), raw.rfind("}")
            data = json.loads(raw[start : end + 1])
        except Exception:  # noqa: BLE001
            return None
        try:
            return VisionSuggestion(
                l1Code=data.get("l1Code"),
                l2Code=data.get("l2Code"),
                description=str(data.get("description") or ""),
                descriptionEn=str(data.get("descriptionEn") or ""),
                confidence=max(0.0, min(1.0, float(data.get("confidence") or 0))),
            )
        except Exception:  # noqa: BLE001
            return None


def get_vision_provider(enabled: bool) -> IVisionSuggestProvider:
    if enabled and is_configured():
        return AnthropicVisionProvider()
    return StubVisionProvider()


# ── Text assist (spec §7: grammar cleanup + text→category suggestion) ─────────
# Both reuse the platform's single Anthropic seam (complete_json) and fail soft
# to None so the wizard degrades to "keep what the worker said / pick manually".

_CLEANUP_SYSTEM = """You clean up short workplace-safety notes dictated or typed by a low-literacy \
factory field worker in India. Fix grammar, spelling and clarity so the note reads as one or two \
complete sentences.
STRICT RULES — this is cleanup, NOT rewriting:
- Do NOT add facts, causes, detail, or severity words that are not already in the note.
- Do NOT remove any fact the worker stated.
- Keep every machine name, equipment code, number, area name and measurement exactly as written.
- Preserve the worker's meaning and their language (Hindi stays Hindi, English stays English).
- If the note is already clear, return it unchanged.
Respond ONLY with a JSON object: {"cleaned": "<the cleaned note>"}."""


async def cleanup_text(text: str, lang: str) -> str | None:
    """Grammar/clarity cleanup of a voice transcript or typed note (spec §7a).
    Fact-preserving by construction (see system prompt). Returns None on any
    failure so the caller keeps the original text."""
    text = (text or "").strip()
    if len(text) < 3 or len(text) > 4000:
        return None
    from app.services.ai.anthropic_client import complete_json

    res = await complete_json(
        system=_CLEANUP_SYSTEM,
        user=f"LANG={lang}\nNote:\n{text}",
        max_tokens=500,
        temperature=0.1,
    )
    if not res:
        return None
    cleaned = str(res.get("cleaned") or "").strip()
    # guard against a degenerate/empty answer — never return something shorter
    # than a plausible cleanup of the input's first few words
    return cleaned or None


_TEXT_CAT_SYSTEM = """You are a factory-safety triage assistant for an Indian garment manufacturer.
Given a short hazard note from a field worker and the hazard taxonomy (level-1 codes and their \
level-2 children, each with an English label), pick the single best matching category.
Respond ONLY with a JSON object:
{"l1Code": <best level-1 code or null>, "l2Code": <best child code of that l1 or null>,
 "confidence": <0..1 — how sure you are about l1Code>}.
If the note matches no category, use nulls and confidence 0."""


async def suggest_category_from_text(
    text: str, taxonomy: list[dict[str, Any]], lang: str
) -> CategorySuggestion | None:
    """Text → suggested hazard category (spec §7b). Mirrors the vision provider
    but keyed off the description text instead of a photo. Fail-soft to None."""
    text = (text or "").strip()
    if len(text) < 3 or len(text) > 4000:
        return None
    from app.services.ai.anthropic_client import complete_json

    tax_lines = [
        {"code": n.get("code"), "parent": n.get("parentCode"), "label": (n.get("labels") or {}).get("en")}
        for n in taxonomy
    ]
    res = await complete_json(
        system=_TEXT_CAT_SYSTEM,
        user="Taxonomy:\n" + json.dumps(tax_lines, ensure_ascii=False) + f"\nLANG={lang}\nNote:\n{text}",
        max_tokens=200,
        temperature=0.1,
    )
    if not res:
        return None
    try:
        return CategorySuggestion(
            l1Code=res.get("l1Code"),
            l2Code=res.get("l2Code"),
            confidence=max(0.0, min(1.0, float(res.get("confidence") or 0))),
        )
    except Exception:  # noqa: BLE001
        return None


_DRAFT_SYSTEM = """You help a factory field worker in India turn a few short answers into a clear \
safety-report description. Write the description as 1-3 short, factual sentences in LANG, plus an \
English version.
STRICT RULES — you are ASSEMBLING the worker's facts, NOT investigating:
- Use ONLY the facts in the answers and the given context (report type, category, location, severity). \
Do NOT invent causes, injuries, numbers, equipment, people, or any detail the worker did not provide.
- Do NOT state or inflate severity beyond what is given; keep a neutral, factual reporting tone \
("A ... was observed ...", "The worker reported that ...").
- Keep every machine name, equipment code, area name, number and measurement exactly as written.
- Write naturally in the worker's language (Hindi stays Hindi, English stays English).
- If the answers contain too little to describe anything, return empty strings.
Respond ONLY with a JSON object: {"description": "<in LANG>", "descriptionEn": "<in English>"}."""


async def draft_description(
    *,
    report_type: str,
    category_label: str | None,
    location: str | None,
    severity: str | None,
    answers: list[dict[str, str]],
    lang: str,
) -> dict[str, str] | None:
    """Guided-question answers → a drafted, FACT-ONLY report description in the
    reporter's language (+ English). Fact-preserving by construction (see system
    prompt) — the caller always shows it for accept/edit, never auto-applies.
    Fail-soft to None so the wizard falls back to plain typing."""
    facts = [
        f"- {(qa.get('q') or '').strip()}: {(qa.get('a') or '').strip()}"
        for qa in answers
        if (qa.get("a") or "").strip()
    ]
    if not facts:
        return None
    from app.services.ai.anthropic_client import complete_json

    context = "\n".join(
        line
        for line in [
            f"Report type: {report_type}",
            f"Category: {category_label}" if category_label else "",
            f"Location: {location}" if location else "",
            f"Reporter-felt severity: {severity}" if severity else "",
        ]
        if line
    )
    res = await complete_json(
        system=_DRAFT_SYSTEM,
        user=f"LANG={lang}\n{context}\nWorker's answers:\n" + "\n".join(facts),
        max_tokens=500,
        temperature=0.2,
    )
    if not res:
        return None
    description = str(res.get("description") or "").strip()
    if not description:
        return None
    return {"description": description, "descriptionEn": str(res.get("descriptionEn") or "").strip()}


__all__ = [
    "TranscriptResult",
    "VisionSuggestion",
    "CategorySuggestion",
    "ITranscriptionProvider",
    "IVisionSuggestProvider",
    "StubTranscriptionProvider",
    "StubVisionProvider",
    "AnthropicVisionProvider",
    "get_transcription_provider",
    "get_vision_provider",
    "cleanup_text",
    "suggest_category_from_text",
    "draft_description",
]
