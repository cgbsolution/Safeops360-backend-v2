"""One-off diagnostic for the PTW dropdown emptiness issue.

Checks, per plant, whether the data the New-Permit wizard needs actually
exists: users, PERMIT_ISSUER role holders, departments, equipment.
Safe read-only. Delete after use.
"""
import os
from sqlalchemy import create_engine, text

url = None
env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
for line in open(env_path):
    if line.startswith("DATABASE_URL_SYNC="):
        url = line.split("=", 1)[1].strip()
if not url:
    raise SystemExit("no DATABASE_URL_SYNC in .env")

eng = create_engine(url)
with eng.connect() as c:
    print("=== PLANTS ===")
    for p in c.execute(text('SELECT id, name, code FROM "Plant" ORDER BY name')):
        print(f"  {p.id} | {p.name} | {p.code}")

    print("\n=== USERS per plant ===")
    for r in c.execute(text('''
        SELECT p.name AS plant, count(u.id) AS n
        FROM "Plant" p LEFT JOIN "User" u ON u."plantId" = p.id
        GROUP BY p.name ORDER BY p.name''')):
        print(f"  {r.plant}: {r.n} users")

    print("\n=== Users with NULL plantId ===")
    n = c.execute(text('SELECT count(*) FROM "User" WHERE "plantId" IS NULL')).scalar()
    print(f"  {n} users have no plantId")

    print("\n=== Departments per plant (active) ===")
    for r in c.execute(text('''
        SELECT p.name AS plant, count(d.id) AS n
        FROM "Plant" p LEFT JOIN "Department" d ON d."plantId" = p.id AND d.active = true
        GROUP BY p.name ORDER BY p.name''')):
        print(f"  {r.plant}: {r.n} active departments")

    print("\n=== PERMIT_ISSUER role holders ===")
    # via UserRole assignment
    rows = c.execute(text('''
        SELECT p.name AS plant, count(DISTINCT ur."userId") AS n
        FROM "UserRole" ur
        JOIN "Role" r ON r.id = ur."roleId"
        JOIN "User" u ON u.id = ur."userId"
        LEFT JOIN "Plant" p ON p.id = u."plantId"
        WHERE r.code = 'PERMIT_ISSUER'
        GROUP BY p.name ORDER BY p.name''')).fetchall()
    if rows:
        for r in rows:
            print(f"  (UserRole) {r.plant}: {r.n}")
    else:
        print("  (UserRole) NONE — no PERMIT_ISSUER assignments at all")
    # via legacy User.role column
    rows = c.execute(text('''
        SELECT COALESCE(p.name,'(no plant)') AS plant, count(*) AS n
        FROM "User" u LEFT JOIN "Plant" p ON p.id = u."plantId"
        WHERE u.role = 'PERMIT_ISSUER'
        GROUP BY p.name ORDER BY p.name''')).fetchall()
    if rows:
        for r in rows:
            print(f"  (User.role) {r.plant}: {r.n}")
    else:
        print("  (User.role) NONE")

    print("\n=== Does the PERMIT_ISSUER role row even exist? ===")
    for r in c.execute(text('SELECT id, code, name FROM "Role" WHERE code = \'PERMIT_ISSUER\'')):
        print(f"  {r.id} | {r.code} | {r.name}")

    print("\n=== Equipment per plant (active) ===")
    for r in c.execute(text('''
        SELECT COALESCE(p.name,'(no plant)') AS plant, count(e.id) AS n
        FROM "Equipment" e LEFT JOIN "Plant" p ON p.id = e."plantId"
        WHERE e.active = true
        GROUP BY p.name ORDER BY p.name''')):
        print(f"  {r.plant}: {r.n} active equipment")
