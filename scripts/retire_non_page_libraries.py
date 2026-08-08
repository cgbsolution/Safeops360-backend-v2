"""Retire every checkpoint library except the Page Industries checklist.

This instance audits one thing. The other libraries (Apparel/Garment, Cement,
Chemical, General Manufacturing, Pharmaceutical, Steel) and the buyer regimes
shipped with the generic product and have no place here — leaving them active
is what puts an "Industry" chooser back on screen the moment any surface
enumerates libraries.

Four are deliberately KEPT — the three own-facility AUDIT CATEGORIES plus the
supplier checklist:
  • PAGE_INDUSTRIES — the Internal category (HR / EHS / Production).
  • PAGE_IMS        — the QMS, EMS, OHS category (ISO 9001 / 14001 / 45001 /
                      50001), whose four disciplines come from the customer's
                      "QMS, EMS OHS" workbook.
  • PAGE_SOCIAL     — the Social Compliance category (Annexure-2, PIL Social
                      Compliance Audit checklist).
  • SUPPLIER_COC    — the supplier Code of Conduct, which is the checklist a
                      SUPPLIER audit runs against. It is never offered for an
                      own-facility audit, so it cannot be mistaken for an
                      industry option, and retiring it would silently break
                      supplier audits rather than simplify anything.

These three are not "extra industries": each one IS an audit category the
scheduling wizard offers, so retiring one removes a category from the product.
Keep this list in step with `AUDIT_CATEGORIES` in
app/services/audit_compliance.py.

Deactivation, NOT deletion. `isActive = false` removes a library from every
list the product builds, and audits already materialised from one keep working
because they hold their own snapshot of every checkpoint. Deleting the rows
would gain nothing and cannot be undone. Re-runnable, and reversible by setting
isActive back to true.

Run from the backend root:
    python scripts/retire_non_page_libraries.py
"""

from __future__ import annotations

from sqlalchemy import create_engine, text

from app.core.config import get_settings

KEEP = ("PAGE_INDUSTRIES", "PAGE_IMS", "PAGE_SOCIAL", "SUPPLIER_COC")


def main() -> None:
    settings = get_settings()
    engine = create_engine(settings.sync_database_url, pool_pre_ping=True)
    with engine.begin() as conn:
        before = conn.execute(text(
            'SELECT "industryCode", "industryName", "checkpointCount", "isActive" '
            'FROM "AuditCheckpointLibrary" ORDER BY "industryCode"'
        )).all()
        print("Libraries before:")
        for r in before:
            print(f"  {'ACTIVE  ' if r.isActive else 'retired '} {r.industryCode:<24} "
                  f"{r.industryName:<52} {r.checkpointCount:>4}")

        retired = conn.execute(
            text(
                'UPDATE "AuditCheckpointLibrary" SET "isActive" = false, "updatedAt" = now() '
                'WHERE "industryCode" NOT IN :keep AND "isActive" = true'
            ).bindparams(keep=KEEP)
        ).rowcount

        # Audit templates hang off a library via `baseIndustry` and are offered
        # as a "Template (optional)" dropdown while scheduling. A template
        # pointing at a retired library would materialise its checkpoints —
        # the library would be gone from the picker and still reachable through
        # the back door.
        retired_tpl = conn.execute(
            text(
                'UPDATE "AuditTemplate" SET "isActive" = false, "updatedAt" = now() '
                'WHERE "baseIndustry" NOT IN :keep AND "isActive" = true'
            ).bindparams(keep=KEEP)
        ).rowcount

        print(f"\nRetired {retired} library(ies) and {retired_tpl} template(s).")

        after = conn.execute(text(
            'SELECT "industryCode", "industryName", "checkpointCount" '
            'FROM "AuditCheckpointLibrary" WHERE "isActive" = true ORDER BY "industryCode"'
        )).all()
        print("\nStill active:")
        for r in after:
            print(f"  {r.industryCode:<24} {r.industryName:<52} {r.checkpointCount:>4}")


if __name__ == "__main__":
    main()
