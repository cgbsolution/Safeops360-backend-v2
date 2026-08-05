"""One-time data fix for the PTW wizard.

1. Normalise TrainingCertificate.status to UPPERCASE ("active" -> "ACTIVE").
   The app + ~10 read sites expect "ACTIVE"; some seed data persisted it
   lowercase, which made the receiver competency gate reject competent
   receivers ("certificate is in an unknown state (active)") and hid valid
   certs from dashboards / SCI scoring.

2. Seed a standard Department set for every plant that has Areas (the
   industrial plants), so the PTW / Near-Miss Department dropdown is populated.
   Idempotent: re-running inserts nothing new.

Read-mostly + additive. Safe to re-run.
"""
from uuid import uuid4
from sqlalchemy import create_engine, text

ENV = r"C:\Users\Deepak\Desktop\code-files\safeops\safeops_360_bakend\.env"
url = None
for line in open(ENV):
    if line.startswith("DATABASE_URL_SYNC="):
        url = line.split("=", 1)[1].strip()
assert url, "no DATABASE_URL_SYNC"

# Standard EHS-relevant department set. Includes IT / HR because existing user
# rows carry those free-text department strings (so the RBAC OWN_DEPARTMENT
# path, which matches Department.name == User.department, resolves).
DEPARTMENTS = [
    ("Operations", "OPS"),
    ("Maintenance", "MAINT"),
    ("Production", "PROD"),
    ("Engineering", "ENGG"),
    ("HSE", "HSE"),
    ("Quality", "QA"),
    ("Stores & Logistics", "STORES"),
    ("Utilities", "UTIL"),
    ("Projects", "PROJ"),
    ("IT", "IT"),
    ("HR", "HR"),
    ("Administration", "ADMIN"),
]

eng = create_engine(url)
with eng.begin() as c:
    # ── 1. Normalise certificate status casing ────────────────────────
    before = c.execute(text(
        'SELECT status, count(*) n FROM "TrainingCertificate" GROUP BY status'
    )).fetchall()
    print("TrainingCertificate.status BEFORE:", {r.status: r.n for r in before})
    res = c.execute(text(
        'UPDATE "TrainingCertificate" SET status = upper(status) '
        'WHERE status <> upper(status)'
    ))
    print(f"  -> normalised {res.rowcount} certificate row(s) to uppercase")
    after = c.execute(text(
        'SELECT status, count(*) n FROM "TrainingCertificate" GROUP BY status'
    )).fetchall()
    print("TrainingCertificate.status AFTER: ", {r.status: r.n for r in after})

    # ── 2. Seed departments for plants that have areas ────────────────
    plants = c.execute(text('''
        SELECT DISTINCT p.id, p.name, p.code
        FROM "Plant" p JOIN "Area" a ON a."plantId" = p.id
        ORDER BY p.name
    ''')).fetchall()
    print(f"\nSeeding departments for {len(plants)} plant(s) with areas:")
    total_inserted = 0
    for p in plants:
        inserted = 0
        for name, code in DEPARTMENTS:
            exists = c.execute(text(
                'SELECT 1 FROM "Department" WHERE "plantId" = :pid AND name = :name'
            ), {"pid": p.id, "name": name}).first()
            if exists:
                continue
            c.execute(text(
                'INSERT INTO "Department" (id, "plantId", name, code, active) '
                'VALUES (:id, :pid, :name, :code, true)'
            ), {"id": uuid4().hex, "pid": p.id, "name": name, "code": code})
            inserted += 1
        total_inserted += inserted
        print(f"  {p.code or '?':8} {p.name[:45]:45} +{inserted}")
    print(f"\nTotal departments inserted: {total_inserted}")

    # ── verify ────────────────────────────────────────────────────────
    nw = c.execute(text('''
        SELECT count(*) FROM "Department" d JOIN "Plant" p ON p.id = d."plantId"
        WHERE p.code = 'NW' AND d.active = true
    ''')).scalar()
    print(f"Verify: Meridian North Works now has {nw} active departments")
