"""DB-backed verification of the Chemical/Hazmat module's acceptance criteria.

The pure logic is covered by tests/test_chemical_threshold.py and
tests/test_trigger_engine.py. The criteria below CANNOT be verified without a
real Postgres, because their whole point is that they are database constraints
and triggers rather than application checks — an in-memory fake would pass a
test that the production database would fail, which is the opposite of useful.

Runs inside ONE session and ROLLS BACK at the end; it never mutates real data.

  CHEM-T01  a chemical cannot reach ACTIVE without an SDS       (AC #1, DB CHECK)
  CHEM-T02  quantityLedger cannot be written directly           (AC #2, DB trigger)
  CHEM-T03  currentStatus cannot be written directly            (AC #2, DB trigger)
  CHEM-T04  the ledger is append-only                           (AC #2, DB trigger)
  CHEM-T05  quantity IS the sum of transactions                 (AC #2)
  CHEM-T06  BLOCK co-storage is rejected at COMMIT              (AC #4, constraint trigger)
  CHEM-T07  WARN co-storage saves but demands a logged reason   (§4.4)
  CHEM-T08  MocTriggerLog.failureReason cannot be empty on FAILED (§3, DB CHECK)
  CHEM-T09  a FIRED trigger row must reference an MOC           (§3, DB CHECK)
  CHEM-T10  threshold breach → exactly one MOC + one FIRED row  (§4.3)
  CHEM-T11  VOLUME: 200 receipts, edge-triggered, no MOC storm  (AC #3 — "verify
            against real transaction volume, not just a single happy-path test")
  CHEM-T12  a failed MOC creation produces a FAILED row, not silence (§4.3)
  CHEM-T13  stock verification reuses the CAMS audit engine     (AC #5)
  CHEM-T14  SDS overdue flags without deactivating              (AC #9, §5 rule 6)

    python verify_chemical_constraints.py
"""

from __future__ import annotations

import asyncio
import traceback
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.db import AsyncSessionLocal
from app.models.cams import CamsAuditType
from app.models.chemical import (
    ChemicalIncompatibilityRule,
    ChemicalInventoryItem,
    ChemicalInventoryTransaction,
    ChemicalMaster,
    ChemicalStorageLocation,
    ChemicalThresholdRule,
    ChemicalThresholdState,
    MocTriggerLog,
)
from app.models.moc import ChangeRequest
from app.models.plant import Plant
from app.models.user import User
from app.services import chemical_ledger as ledger
from app.services import chemical_sds as sds

results: list[tuple[str, bool, str]] = []
TENANT = "verify-chem"


def check(tid: str, ok: bool, detail: str = "") -> None:
    results.append((tid, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {tid} {detail}")


async def _expect_db_error(
    db, coro, tid: str, expect_fragment: str, *, deferred: bool = False
) -> None:
    """Assert that a statement is rejected BY THE DATABASE.

    Each attempt runs in its own SAVEPOINT so a rejection does not poison the
    rest of the verification run — and, importantly, so a constraint that fails
    to fire is visible as a PASS-that-should-have-FAILED rather than as a
    cascade of unrelated errors.

    `deferred=True` is required for CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY
    DEFERRED. Such a trigger queues its check and fires it at COMMIT — NOT at
    savepoint release. This script deliberately never commits (it must not
    mutate real data), so without forcing the issue the check would never run
    and the assertion would silently pass as "accepted". `SET CONSTRAINTS ALL
    IMMEDIATE` drains the queue right here, which is the only way to verify a
    deferred constraint inside a transaction that will be rolled back.

    Worth being explicit, because the first version of this helper got it wrong
    and reported the co-storage constraint as not firing: the deferral is
    correct for production (a real request commits, and the legal ordering
    INSERT item → assign location needs the intermediate state tolerated), and
    it was the TEST that could not see it.
    """
    try:
        async with db.begin_nested():
            await coro
            if deferred:
                await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        check(tid, False, "— statement was ACCEPTED; the constraint did not fire")
    except (IntegrityError, DBAPIError) as e:
        msg = str(getattr(e, "orig", e))
        ok = expect_fragment.lower() in msg.lower()
        check(tid, ok, "" if ok else f"— rejected, but with an unexpected message: {msg[:160]}")


async def main() -> None:  # noqa: C901 — a linear verification script reads better flat
    async with AsyncSessionLocal() as db:
        plant = (await db.execute(select(Plant).limit(1))).scalar_one()
        user = (await db.execute(select(User).limit(1))).scalar_one()
        now = datetime.now(timezone.utc)

        # ── fixtures ──────────────────────────────────────────────────────────
        flammable = ChemicalMaster(
            tenantId=TENANT, name="VERIFY Toluene", casNumber="108-88-3",
            hazardClasses=["FLAMMABLE"], physicalState="LIQUID", status="PENDING_SDS",
        )
        oxidiser = ChemicalMaster(
            tenantId=TENANT, name="VERIFY Nitric Acid", casNumber="7697-37-2",
            hazardClasses=["OXIDIZER", "CORROSIVE"], physicalState="LIQUID",
            status="ACTIVE", sdsAttachmentId="verify-sds-2",
        )
        toxic = ChemicalMaster(
            tenantId=TENANT, name="VERIFY Methanol", casNumber="67-56-1",
            hazardClasses=["TOXIC", "FLAMMABLE"], physicalState="LIQUID",
            status="ACTIVE", sdsAttachmentId="verify-sds-3",
        )
        db.add_all([flammable, oxidiser, toxic])
        await db.flush()

        store = ChemicalStorageLocation(
            tenantId=TENANT, plantId=plant.id, code="VERIFY-ST-1",
            name="Verification flammable store", storageType="FLAMMABLE_CABINET",
        )
        db.add(store)
        await db.flush()

        # ═══ CHEM-T01 — ACTIVE requires an SDS (AC #1) ════════════════════════
        await _expect_db_error(
            db,
            db.execute(
                text('UPDATE "ChemicalMaster" SET "status" = \'ACTIVE\' WHERE "id" = :i'),
                {"i": flammable.id},
            ),
            "CHEM-T01",
            "ck_ChemicalMaster_active_requires_sds",
        )

        # Service layer must give a readable error for the same thing.
        try:
            await sds.activate(db, chemical_id=flammable.id, user_id=user.id)
            check("CHEM-T01b", False, "— service allowed activation with no SDS")
        except sds.SdsError as e:
            check("CHEM-T01b", "Safety Data Sheet" in str(e), f"— {str(e)[:80]}")

        # Now attach one and activate properly.
        await sds.attach_sds(
            db, chemical_id=flammable.id, attachment_id="verify-sds-1",
            revision_date=now - timedelta(days=30), user_id=user.id,
        )
        await sds.activate(db, chemical_id=flammable.id, user_id=user.id)
        check("CHEM-T01c", flammable.status == "ACTIVE", "— activates once an SDS is linked")

        # ── inventory fixture ────────────────────────────────────────────────
        item = ChemicalInventoryItem(
            tenantId=TENANT, chemicalId=flammable.id, plantId=plant.id,
            storageLocationId=store.id, batchLotNumber="VERIFY-B1", unit="KG",
            receiptDate=now,
        )
        db.add(item)
        await db.flush()
        await ledger.post_transaction(
            db, tenant_id=TENANT, item_id=item.id, txn_type="RECEIPT",
            quantity=500, unit="KG", user_id=user.id, evaluate_thresholds_now=False,
        )
        await db.refresh(item)

        # ═══ CHEM-T02 / T03 — quantity + status are not writable (AC #2) ═════
        await _expect_db_error(
            db,
            db.execute(
                text('UPDATE "ChemicalInventoryItem" SET "quantityLedger" = 0 WHERE "id" = :i'),
                {"i": item.id},
            ),
            "CHEM-T02",
            "ledger-derived",
        )
        await _expect_db_error(
            db,
            db.execute(
                text('UPDATE "ChemicalInventoryItem" SET "currentStatus" = \'DISPOSED\' WHERE "id" = :i'),
                {"i": item.id},
            ),
            "CHEM-T03",
            "derived from the ledger",
        )

        # ═══ CHEM-T04 — the ledger is append-only ════════════════════════════
        txn_id = (
            await db.execute(
                select(ChemicalInventoryTransaction.id)
                .where(ChemicalInventoryTransaction.itemId == item.id)
                .limit(1)
            )
        ).scalar_one()
        await _expect_db_error(
            db,
            db.execute(
                text('UPDATE "ChemicalInventoryTransaction" SET "quantity" = 1 WHERE "id" = :i'),
                {"i": txn_id},
            ),
            "CHEM-T04",
            "append-only",
        )

        # ═══ CHEM-T05 — quantity IS the ledger sum ═══════════════════════════
        await ledger.post_transaction(
            db, tenant_id=TENANT, item_id=item.id, txn_type="ISSUE",
            quantity=120, unit="KG", user_id=user.id, evaluate_thresholds_now=False,
        )
        await db.refresh(item)
        ledger_sum = (
            await db.execute(
                select(func.coalesce(func.sum(ChemicalInventoryTransaction.signedQuantity), 0))
                .where(ChemicalInventoryTransaction.itemId == item.id)
            )
        ).scalar_one()
        check(
            "CHEM-T05",
            abs(float(item.quantityLedger) - float(ledger_sum)) < 1e-9 and item.quantityLedger == 380,
            f"— column {item.quantityLedger}, ledger {ledger_sum}",
        )

        # ═══ CHEM-T06 — BLOCK co-storage rejected at COMMIT (AC #4) ══════════
        # FLAMMABLE + OXIDIZER is a seeded BLOCK rule.
        block_rule = (
            await db.execute(
                select(ChemicalIncompatibilityRule)
                .where(ChemicalIncompatibilityRule.severity == "BLOCK")
                .where(ChemicalIncompatibilityRule.hazardClassA.in_(["FLAMMABLE", "OXIDIZER"]))
                .where(ChemicalIncompatibilityRule.hazardClassB.in_(["FLAMMABLE", "OXIDIZER"]))
                .limit(1)
            )
        ).scalar_one_or_none()
        if block_rule is None:
            check("CHEM-T06", False, "— no FLAMMABLE/OXIDIZER BLOCK rule seeded; run apply-chemical-ddl.ts")
        else:
            oxi_item = ChemicalInventoryItem(
                tenantId=TENANT, chemicalId=oxidiser.id, plantId=plant.id,
                batchLotNumber="VERIFY-B2", unit="KG", receiptDate=now,
            )
            db.add(oxi_item)
            await db.flush()
            await ledger.post_transaction(
                db, tenant_id=TENANT, item_id=oxi_item.id, txn_type="RECEIPT",
                quantity=100, unit="KG", user_id=user.id, evaluate_thresholds_now=False,
            )

            # (a) the service refuses with a readable message
            try:
                await ledger.assign_storage_location(
                    db, tenant_id=TENANT, item_id=oxi_item.id,
                    storage_location_id=store.id, user_id=user.id,
                )
                check("CHEM-T06a", False, "— service allowed a BLOCK co-storage")
            except ledger.LedgerError as e:
                check("CHEM-T06a", "Incompatible co-storage" in str(e), f"— {str(e)[:90]}")

            # (b) and the DATABASE refuses even when the service is bypassed —
            #     which is what makes AC #4 "not just a toast warning".
            #     deferred=True because the trigger fires at COMMIT; see
            #     _expect_db_error for why the savepoint alone is not enough.
            await _expect_db_error(
                db,
                db.execute(
                    text('UPDATE "ChemicalInventoryItem" SET "storageLocationId" = :s WHERE "id" = :i'),
                    {"s": store.id, "i": oxi_item.id},
                ),
                "CHEM-T06b",
                "Co-storage blocked",
                deferred=True,
            )

        # ═══ CHEM-T07 — WARN saves, but demands a reason ═════════════════════
        # FLAMMABLE + TOXIC is a seeded WARN rule; methanol is both.
        tox_item = ChemicalInventoryItem(
            tenantId=TENANT, chemicalId=toxic.id, plantId=plant.id,
            batchLotNumber="VERIFY-B3", unit="KG", receiptDate=now,
        )
        db.add(tox_item)
        await db.flush()
        await ledger.post_transaction(
            db, tenant_id=TENANT, item_id=tox_item.id, txn_type="RECEIPT",
            quantity=50, unit="KG", user_id=user.id, evaluate_thresholds_now=False,
        )
        try:
            await ledger.assign_storage_location(
                db, tenant_id=TENANT, item_id=tox_item.id,
                storage_location_id=store.id, user_id=user.id,
            )
            check("CHEM-T07a", False, "— WARN conflict saved with no override reason")
        except ledger.LedgerError as e:
            check("CHEM-T07a", "requires a documented reason" in str(e), f"— {str(e)[:80]}")

        warned = await ledger.assign_storage_location(
            db, tenant_id=TENANT, item_id=tox_item.id, storage_location_id=store.id,
            user_id=user.id, override_reason="Segregated bund, verified by CSO",
        )
        await db.refresh(tox_item)
        check(
            "CHEM-T07b",
            tox_item.storageLocationId == store.id and len(warned) >= 1,
            f"— saved with {len(warned)} logged override(s)",
        )

        # ═══ CHEM-T08 / T09 — MocTriggerLog invariants ═══════════════════════
        await _expect_db_error(
            db,
            db.execute(
                text(
                    'INSERT INTO "MocTriggerLog" ("id","tenantId","triggerType","sourceEntityId","status")'
                    " VALUES ('verify-mtl-1', :t, 'THRESHOLD_BREACH', 'x', 'FAILED')"
                ),
                {"t": TENANT},
            ),
            "CHEM-T08",
            "ck_MocTriggerLog_failure_reason_present",
        )
        await _expect_db_error(
            db,
            db.execute(
                text(
                    'INSERT INTO "MocTriggerLog" ("id","tenantId","triggerType","sourceEntityId","status")'
                    " VALUES ('verify-mtl-2', :t, 'THRESHOLD_BREACH', 'x', 'FIRED')"
                ),
                {"t": TENANT},
            ),
            "CHEM-T09",
            "ck_MocTriggerLog_fired_has_moc",
        )

        # ═══ CHEM-T10 — breach raises exactly one MOC ════════════════════════
        rule = ChemicalThresholdRule(
            tenantId=TENANT, region="IN", hazardClass="FLAMMABLE",
            scheduleReference="VERIFY Schedule X", thresholdQuantity=1000, unit="KG",
            approachRatio=0.8, triggerObligation="ON_SITE_EMERGENCY_PLAN", autoMocOnBreach=True,
        )
        db.add(rule)
        await db.flush()

        before_mocs = (
            await db.execute(select(func.count(ChangeRequest.id)).where(ChangeRequest.plantId == plant.id))
        ).scalar_one()

        res = await ledger.post_transaction(
            db, tenant_id=TENANT, item_id=item.id, txn_type="RECEIPT",
            quantity=900, unit="KG", user_id=user.id,   # 380 + 900 = 1280 > 1000
        )
        fired = [r for r in (res.trigger_run.results if res.trigger_run else []) if r.fired]
        after_mocs = (
            await db.execute(select(func.count(ChangeRequest.id)).where(ChangeRequest.plantId == plant.id))
        ).scalar_one()
        log_rows = (
            await db.execute(
                select(MocTriggerLog)
                .where(MocTriggerLog.tenantId == TENANT)
                .where(MocTriggerLog.ruleId == rule.id)
            )
        ).scalars().all()
        check(
            "CHEM-T10",
            len(fired) == 1 and after_mocs == before_mocs + 1 and len(log_rows) == 1
            and log_rows[0].status == "FIRED" and log_rows[0].mocId is not None,
            f"— {len(fired)} fired, {after_mocs - before_mocs} MOC(s), {len(log_rows)} log row(s)",
        )

        # ═══ CHEM-T11 — VOLUME ═══════════════════════════════════════════════
        # The acceptance criterion asks for real transaction volume rather than a
        # single happy path, because the "0 of 22" defect was invisible precisely
        # at volume. 200 further receipts must produce ZERO extra MOCs (the site
        # is already breached — the trigger is edge-triggered) and every one must
        # still leave the ledger exact.
        mocs_before_volume = (
            await db.execute(select(func.count(ChangeRequest.id)).where(ChangeRequest.plantId == plant.id))
        ).scalar_one()
        for _ in range(200):
            await ledger.post_transaction(
                db, tenant_id=TENANT, item_id=item.id, txn_type="RECEIPT",
                quantity=1, unit="KG", user_id=user.id,
            )
        await db.refresh(item)
        mocs_after_volume = (
            await db.execute(select(func.count(ChangeRequest.id)).where(ChangeRequest.plantId == plant.id))
        ).scalar_one()
        ledger_sum = (
            await db.execute(
                select(func.coalesce(func.sum(ChemicalInventoryTransaction.signedQuantity), 0))
                .where(ChemicalInventoryTransaction.itemId == item.id)
            )
        ).scalar_one()
        check(
            "CHEM-T11",
            mocs_after_volume == mocs_before_volume
            and abs(float(item.quantityLedger) - float(ledger_sum)) < 1e-9
            and item.quantityLedger == 1480,
            f"— {mocs_after_volume - mocs_before_volume} extra MOC(s) over 200 receipts, "
            f"balance {item.quantityLedger}",
        )

        # And a re-breach after clearing must raise a NEW MOC — the edge-trigger
        # must not latch permanently.
        await ledger.post_transaction(
            db, tenant_id=TENANT, item_id=item.id, txn_type="ISSUE",
            quantity=1400, unit="KG", user_id=user.id,   # down to 80 → BELOW
        )
        from app.services.chemical_threshold import evaluate_thresholds

        await evaluate_thresholds(db, tenant_id=TENANT, plant_id=plant.id, chemical_id=flammable.id)
        state = (
            await db.execute(
                select(ChemicalThresholdState)
                .where(ChemicalThresholdState.tenantId == TENANT)
                .where(ChemicalThresholdState.ruleId == rule.id)
            )
        ).scalar_one()
        cleared = state.status == "BELOW" and state.activeMocId is None
        res2 = await ledger.post_transaction(
            db, tenant_id=TENANT, item_id=item.id, txn_type="RECEIPT",
            quantity=1500, unit="KG", user_id=user.id,
        )
        refired = [r for r in (res2.trigger_run.results if res2.trigger_run else []) if r.fired]
        check(
            "CHEM-T11b",
            cleared and len(refired) == 1,
            f"— cleared={cleared}, re-breach raised {len(refired)} MOC(s)",
        )

        # ═══ CHEM-T12 — a broken MOC creation FAILS loudly ═══════════════════
        # Force the failure by pointing a rule at a plant that does not exist.
        # The requirement is not that this cannot happen; it is that when it
        # does, there is a FAILED row with a non-empty reason rather than silence.
        bad_rule = ChemicalThresholdRule(
            tenantId=TENANT, region="IN", hazardClass="TOXIC",
            scheduleReference="VERIFY Schedule FAIL", thresholdQuantity=10, unit="KG",
            approachRatio=0.8, triggerObligation="SAFETY_REPORT", autoMocOnBreach=True,
        )
        db.add(bad_rule)
        await db.flush()
        ghost_item = ChemicalInventoryItem(
            tenantId=TENANT, chemicalId=toxic.id, plantId="plant-does-not-exist",
            batchLotNumber="VERIFY-B9", unit="KG", receiptDate=now,
        )
        db.add(ghost_item)
        await db.flush()
        res3 = await ledger.post_transaction(
            db, tenant_id=TENANT, item_id=ghost_item.id, txn_type="RECEIPT",
            quantity=99, unit="KG", user_id=user.id,
        )
        failed_rows = (
            await db.execute(
                select(MocTriggerLog)
                .where(MocTriggerLog.tenantId == TENANT)
                .where(MocTriggerLog.ruleId == bad_rule.id)
                .where(MocTriggerLog.status == "FAILED")
            )
        ).scalars().all()
        check(
            "CHEM-T12",
            len(failed_rows) == 1
            and bool((failed_rows[0].failureReason or "").strip())
            and failed_rows[0].mocId is None
            and bool(res3.warnings),
            f"— {len(failed_rows)} FAILED row(s), reason "
            f"{(failed_rows[0].failureReason if failed_rows else None)!r}",
        )

        # ═══ CHEM-T13 — stock verification reuses the CAMS engine ════════════
        audit_type = (
            await db.execute(
                select(CamsAuditType).where(CamsAuditType.typeCode == "CHEMICAL_STOCK_VERIFICATION")
            )
        ).scalar_one_or_none()
        parallel_engines = (
            await db.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name ILIKE 'Chemical%%Audit%%'"
                )
            )
        ).scalar_one()
        check(
            "CHEM-T13",
            audit_type is not None and audit_type.engagementType == "INSPECTION" and parallel_engines == 0,
            f"— CamsAuditType present={audit_type is not None}, "
            f"parallel chemical audit tables={parallel_engines}",
        )

        # ═══ CHEM-T14 — SDS overdue flags, never deactivates ═════════════════
        flammable.sdsReviewDueDate = now - timedelta(days=1)
        flammable.sdsReviewOverdue = False
        await db.flush()
        counts = await sds.flag_overdue_sds_reviews(db, tenant_id=TENANT)
        await db.refresh(flammable)
        check(
            "CHEM-T14",
            flammable.sdsReviewOverdue is True and flammable.status == "ACTIVE" and counts["flagged"] >= 1,
            f"— overdue={flammable.sdsReviewOverdue}, status still {flammable.status}",
        )

        # ── never touch real data ────────────────────────────────────────────
        await db.rollback()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    failures = [t for t, ok, _ in results if not ok]
    if failures:
        print("FAILED: " + ", ".join(failures))
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(2)
