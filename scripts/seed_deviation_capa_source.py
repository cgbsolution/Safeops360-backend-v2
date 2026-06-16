"""Additive: register the DEVIATION CAPA source type + SLA (Pharma IMS).

So a deviation can spawn a CAPA via the canonical POST /api/capa path
(sourceTypeCode="DEVIATION"). Idempotent — upserts the QUALITY category, the
DEVIATION source type, and a DEVIATION SLA profile without touching anything
else. Run from the backend root:
    .venv/Scripts/python.exe scripts/seed_deviation_capa_source.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.capa import CapaSlaProfile, CapaSourceCategory, CapaSourceType


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    with Session(engine) as s:
        # 1) QUALITY category (prefix Q) — create if absent.
        cat = s.execute(select(CapaSourceCategory).where(CapaSourceCategory.code == "QUALITY")).scalar_one_or_none()
        if cat is None:
            # Avoid a prefix collision if 'Q' is already taken.
            taken = {c.prefix for c in s.execute(select(CapaSourceCategory)).scalars().all()}
            prefix = "Q" if "Q" not in taken else "D"
            cat = CapaSourceCategory(code="QUALITY", name="Quality", prefix=prefix, sortOrder=20, isActive=True)
            s.add(cat)
            s.flush()
            print(f"   + created CapaSourceCategory QUALITY (prefix {prefix})")

        # 2) DEVIATION source type.
        st = s.execute(select(CapaSourceType).where(CapaSourceType.code == "DEVIATION")).scalar_one_or_none()
        if st is None:
            st = CapaSourceType(
                code="DEVIATION", name="Deviation", categoryId=cat.id,
                description="Process or specification deviation spawning corrective/preventive action.",
                parentModuleLive=True, parentModuleName="DEVIATION", sortOrder=20, isActive=True,
            )
            s.add(st)
            print("   + created CapaSourceType DEVIATION")
        else:
            st.isActive = True
            st.parentModuleLive = True
            st.parentModuleName = "DEVIATION"
            print("   = DEVIATION source type already present (refreshed)")

        # 3) DEVIATION SLA profile (severity-agnostic default).
        sla = s.execute(select(CapaSlaProfile).where(CapaSlaProfile.code == "DEVIATION_DEF")).scalar_one_or_none()
        if sla is None:
            s.add(CapaSlaProfile(
                code="DEVIATION_DEF", sourceTypeCode="DEVIATION", severity=None,
                initialResponseHours=24, rcaDueDays=14, actionsPlannedDueDays=21,
                closureTargetDays=60, recurrenceCheckDays=90, isActive=True,
            ))
            print("   + created CapaSlaProfile DEVIATION_DEF")

        s.commit()
        print("Done. DEVIATION CAPA source type ready.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
