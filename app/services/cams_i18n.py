"""WP-46 - field-facing internationalisation.

docs/cams/09 §3.6. Open question Q18 answered **no** to Tamil and Kannada, so
this ships **en + hi** and nothing else.

**The mechanism is language-agnostic; only the shipped set is opinionated.**
Adding a language is rows in `CheckpointTranslation` plus one entry in
`LANGUAGES` - never a schema change. That is what makes honouring Q18 free
rather than a decision to unwind later.

**Scope, per the brief.** Checkpoint question text, guidance, and the response
UI labels an auditee reads. Auditor chrome stays English-first: a lead auditor
writing a report in English does not benefit from a translated nav bar, and
half-translating an interface is worse than not translating it.

**Fallback is explicit, never silent.** A missing translation returns the
English source *and says so*, so the conduct screen can mark it. A field auditor
reading a safety question needs to know whether they are reading a reviewed
translation, a machine one, or the English original.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cams_completion import CheckpointTranslation

# Q18: en + hi. The registry is the ONLY place a language is declared.
LANGUAGES: dict[str, dict[str, str]] = {
    "en": {"code": "en", "label": "English", "nativeLabel": "English", "dir": "ltr"},
    "hi": {"code": "hi", "label": "Hindi", "nativeLabel": "हिन्दी", "dir": "ltr"},
}

DEFAULT_LANGUAGE = "en"

# Where a translation came from. A machine translation of a safety question is
# not the same artefact as a reviewed one, and the auditor deserves to know.
SOURCES = ("HUMAN", "MACHINE", "SOURCE")


@dataclass(frozen=True)
class ResolvedText:
    text: str
    language: str
    # False when we fell back to English because no translation exists.
    isTranslated: bool
    source: str
    reviewed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "language": self.language,
            "isTranslated": self.isTranslated,
            "source": self.source,
            "reviewed": self.reviewed,
            # The banner the conduct screen shows when reading a fallback.
            "fallbackNotice": (
                ""
                if self.isTranslated
                else "Shown in English - no translation has been published for this question."
            ),
        }


def is_supported(language: str | None) -> bool:
    return (language or "").lower() in LANGUAGES


def normalise(language: str | None) -> str:
    """Coerce to a supported language. Unknown -> English, never an error.

    A field auditor whose device reports `ta-IN` should get a working screen in
    English, not a 400.
    """
    lang = (language or "").lower().split("-")[0]
    return lang if lang in LANGUAGES else DEFAULT_LANGUAGE


def list_languages() -> list[dict[str, Any]]:
    return list(LANGUAGES.values())


async def resolve_checkpoints(
    db: AsyncSession,
    *,
    library_code: str,
    checkpoint_codes: Iterable[str],
    language: str,
    english_source: dict[str, dict[str, str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Batch-resolve question + guidance for a conduct screen.

    Batched on purpose: a 1,500-checkpoint engagement resolving one row at a
    time would issue 1,500 queries, and the offline pack builds from this too.

    `english_source` is `{code: {questionText, guidance}}` from the materialised
    rows - the fallback text, so this never needs to re-read the library.
    """
    lang = normalise(language)
    codes = [c for c in checkpoint_codes if c]
    english_source = english_source or {}

    rows: list[CheckpointTranslation] = []
    if codes and lang != DEFAULT_LANGUAGE:
        rows = list(
            (
                await db.execute(
                    select(CheckpointTranslation).where(
                        CheckpointTranslation.libraryCode == library_code,
                        CheckpointTranslation.checkpointCode.in_(codes),
                        CheckpointTranslation.language == lang,
                    )
                )
            ).scalars().all()
        )
    by_code = {r.checkpointCode: r for r in rows}

    out: dict[str, dict[str, Any]] = {}
    for code in codes:
        src = english_source.get(code, {})
        tr = by_code.get(code)
        if tr is not None:
            out[code] = {
                "question": ResolvedText(
                    tr.questionText, lang, True, tr.source,
                    reviewed=tr.reviewedById is not None,
                ).as_dict(),
                "guidance": ResolvedText(
                    tr.guidance or src.get("guidance", ""), lang, bool(tr.guidance),
                    tr.source, reviewed=tr.reviewedById is not None,
                ).as_dict(),
            }
        else:
            # Explicit fallback. The English text IS the source of record, so
            # `source="SOURCE"` is accurate rather than a stand-in.
            out[code] = {
                "question": ResolvedText(
                    src.get("questionText", ""), DEFAULT_LANGUAGE, False, "SOURCE"
                ).as_dict(),
                "guidance": ResolvedText(
                    src.get("guidance", ""), DEFAULT_LANGUAGE, False, "SOURCE"
                ).as_dict(),
            }
    return out


async def coverage_for_library(
    db: AsyncSession, *, library_code: str, total_checkpoints: int
) -> list[dict[str, Any]]:
    """How much of a library is translated, per language.

    Surfaced in the Templates screen so nobody discovers mid-audit that Hindi
    covers 12 of 82 questions.
    """
    rows = (
        await db.execute(
            select(CheckpointTranslation.language, CheckpointTranslation.reviewedById)
            .where(CheckpointTranslation.libraryCode == library_code)
        )
    ).all()

    counts: dict[str, dict[str, int]] = {}
    for lang, reviewed in rows:
        e = counts.setdefault(lang, {"translated": 0, "reviewed": 0})
        e["translated"] += 1
        if reviewed:
            e["reviewed"] += 1

    out = []
    for code, meta in LANGUAGES.items():
        if code == DEFAULT_LANGUAGE:
            out.append({
                **meta, "translated": total_checkpoints, "reviewed": total_checkpoints,
                "coveragePct": 100.0, "isSource": True,
            })
            continue
        c = counts.get(code, {"translated": 0, "reviewed": 0})
        pct = round(c["translated"] / total_checkpoints * 100, 1) if total_checkpoints else 0.0
        out.append({
            **meta, "translated": c["translated"], "reviewed": c["reviewed"],
            "coveragePct": pct, "isSource": False,
            # A partially-translated language is worse than none if nobody says
            # so: the auditor hits English halfway through and loses trust.
            "usable": pct >= 100.0,
            "note": (
                "" if pct >= 100.0
                else f"{total_checkpoints - c['translated']} question(s) fall back to English."
            ),
        })
    return out


async def upsert_translation(
    db: AsyncSession,
    *,
    library_code: str,
    checkpoint_code: str,
    language: str,
    question_text: str,
    guidance: str | None = None,
    source: str = "HUMAN",
    reviewed_by: str | None = None,
) -> CheckpointTranslation:
    lang = (language or "").lower()
    if lang not in LANGUAGES:
        raise ValueError(
            f"{language!r} is not a supported language. Supported: {', '.join(LANGUAGES)}"
        )
    if lang == DEFAULT_LANGUAGE:
        raise ValueError(
            "English is the source language - edit the checkpoint itself rather than "
            "translating it."
        )
    if not (question_text or "").strip():
        raise ValueError("Translated question text cannot be empty.")
    if source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")

    row = (
        await db.execute(
            select(CheckpointTranslation).where(
                CheckpointTranslation.libraryCode == library_code,
                CheckpointTranslation.checkpointCode == checkpoint_code,
                CheckpointTranslation.language == lang,
            )
        )
    ).scalars().first()
    if row is None:
        row = CheckpointTranslation(
            libraryCode=library_code, checkpointCode=checkpoint_code, language=lang,
            questionText=question_text.strip(),
        )
        db.add(row)
    row.questionText = question_text.strip()
    row.guidance = (guidance or "").strip() or None
    row.source = source
    if reviewed_by:
        row.reviewedById = reviewed_by
        from datetime import datetime, timezone

        row.reviewedAt = datetime.now(timezone.utc)
    await db.flush()
    return row


__all__ = [
    "LANGUAGES",
    "DEFAULT_LANGUAGE",
    "SOURCES",
    "ResolvedText",
    "is_supported",
    "normalise",
    "list_languages",
    "resolve_checkpoints",
    "coverage_for_library",
    "upsert_translation",
]
