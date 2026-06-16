"""RCA helpers — port of `src/lib/rca/types.ts`. Just the bits the API
needs: method normalisation + summary generation.
"""

from __future__ import annotations

from typing import Any

# Bridge any legacy code value (5-Why / Fishbone / etc.) to canonical RcaMethod.
_NORMALISE = {
    "5-Why": "FIVE_WHY",
    "FIVE_WHY": "FIVE_WHY",
    "Fishbone": "FISHBONE",
    "FISHBONE": "FISHBONE",
    "FTA": "FTA",
    "Bowtie": "BOWTIE",
    "BOWTIE": "BOWTIE",
    "TapRoot": "TAPROOT",
    "TAPROOT": "TAPROOT",
    "Cause Map": "CAUSE_MAP",
    "CAUSE_MAP": "CAUSE_MAP",
}


def normalise_rca_method(input_str: str | None) -> str | None:
    if not input_str:
        return None
    return _NORMALISE.get(input_str.strip())


def is_empty_rca_data(method: str, data: Any) -> bool:
    if data is None or not isinstance(data, dict):
        return True
    if method == "FIVE_WHY":
        whys = data.get("whys") or []
        return (
            not (data.get("problemStatement") or "").strip()
            and not (data.get("rootCause") or "").strip()
            and not any((w.get("question", "").strip() or w.get("answer", "").strip()) for w in whys)
        )
    if method == "FISHBONE":
        cats = data.get("categories") or {}
        any_cause = any(len(cats.get(k) or []) > 0 for k in ("manpower", "machine", "method", "material", "measurement", "environment"))
        return not (data.get("problemStatement") or "").strip() and not any_cause and not (data.get("rootCauses") or [])
    if method == "FTA":
        root = data.get("rootNode") or {}
        return not (data.get("topEvent") or "").strip() and not (root.get("children") or [])
    if method == "BOWTIE":
        return not (data.get("topEvent") or "").strip() and not (data.get("threats") or []) and not (data.get("consequences") or [])
    if method == "TAPROOT":
        return not (data.get("eventDescription") or "").strip() and not (data.get("snapChart") or []) and not (data.get("causalFactors") or [])
    if method == "CAUSE_MAP":
        return not (data.get("rootEvent") or "").strip() and not (data.get("impacts") or []) and not (data.get("causeNodes") or [])
    return True


def generate_rca_summary(method: str | None, data: Any) -> str | None:
    """Plain-English summary used on dashboards / list views / statutory exports."""
    if not method or data is None or is_empty_rca_data(method, data):
        return None
    if method == "FIVE_WHY":
        root = (data.get("rootCause") or "").strip()
        whys = data.get("whys") or []
        last_answer = next((w.get("answer", "").strip() for w in reversed(whys) if (w.get("answer") or "").strip()), "")
        cause = root or last_answer or ""
        problem = (data.get("problemStatement") or "Incident").strip()
        return f"{problem}. Root cause: {cause or '—'}."
    if method == "FISHBONE":
        roots = (data.get("rootCauses") or [])[:2]
        cats = data.get("categories") or {}
        all_count = sum(len(cats.get(k) or []) for k in ("manpower", "machine", "method", "material", "measurement", "environment"))
        problem = (data.get("problemStatement") or "Incident").strip()
        suffix = f" Root cause(s): {'; '.join(roots)}." if roots else ""
        return f"{problem}. {all_count} contributing factor(s) identified across 6M categories.{suffix}"
    if method == "FTA":
        return f"{data.get('topEvent') or 'Top event'}."
    if method == "BOWTIE":
        return f"{data.get('topEvent') or 'Top event'}. {len(data.get('threats') or [])} threat(s), {len(data.get('consequences') or [])} consequence(s)."
    if method == "TAPROOT":
        cfs = [(cf.get("description") or "").strip() for cf in (data.get("causalFactors") or [])][:3]
        cf_text = f" Top: {'; '.join([c for c in cfs if c])}." if any(cfs) else ""
        return f"{data.get('eventDescription') or 'Event'}. {len(data.get('causalFactors') or [])} causal factor(s) identified.{cf_text}"
    if method == "CAUSE_MAP":
        impacts = ", ".join(data.get("impacts") or []) or "—"
        return f"{data.get('rootEvent') or 'Event'}. Impacts: {impacts}. {len(data.get('causeNodes') or [])} cause node(s) mapped."
    return None
