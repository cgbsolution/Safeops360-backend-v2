"""
One-shot data migration: copy ALL rows from the OLD Supabase project into the
NEW one. Schemas are already identical (pushed from the same Prisma schema).

Strategy:
  - Connect to both via the session-mode pooler (port 5432).
  - Disable FK/trigger enforcement on the target (session_replication_role=replica)
    so table load order doesn't matter and circular FKs are fine.
  - For every base table in public, stream rows source->target with binary COPY,
    using an explicit, ordinal-ordered column list on both ends.
  - Reset all owned sequences to MAX(col) afterwards.
  - Verify by comparing per-table row counts.

Safe to re-run: each table is TRUNCATEd on the target before reload.
"""
import io
import sys
import psycopg2

OLD = "postgresql://postgres.xwyzujrdlsdhezfikbku:Safeops321%40123@aws-1-ap-south-1.pooler.supabase.com:5432/postgres"
NEW = "postgresql://postgres.ndgodrxmguqeagpadxal:Safeops%40%21%40%23@aws-1-ap-south-1.pooler.supabase.com:5432/postgres"


def get_tables(cur):
    cur.execute("""
        select table_name
        from information_schema.tables
        where table_schema='public' and table_type='BASE TABLE'
          and table_name <> '_prisma_migrations'
        order by table_name
    """)
    return [r[0] for r in cur.fetchall()]


def get_columns(cur, table):
    cur.execute("""
        select column_name
        from information_schema.columns
        where table_schema='public' and table_name=%s
          and is_generated='NEVER'           -- skip GENERATED ALWAYS cols
          and (is_identity='NO' or identity_generation is null
               or identity_generation <> 'ALWAYS')
        order by ordinal_position
    """, (table,))
    return [r[0] for r in cur.fetchall()]


def main():
    src = psycopg2.connect(OLD, connect_timeout=30)
    src.autocommit = True
    scur = src.cursor()

    dst = psycopg2.connect(NEW, connect_timeout=30)
    dst.autocommit = True
    dcur = dst.cursor()
    dcur.execute("SET session_replication_role = replica;")  # disable FK/triggers

    tables = get_tables(dcur)
    print(f"Copying {len(tables)} tables...\n", flush=True)

    # Clear all target tables in one shot (CASCADE satisfies cross-table FKs;
    # per-table TRUNCATE is rejected even under session_replication_role=replica).
    all_tbls = ", ".join('"%s"' % t for t in tables)
    dcur.execute(f"TRUNCATE TABLE {all_tbls} CASCADE")
    print("Target tables cleared.\n", flush=True)

    total_rows = 0
    failures = []
    copied = []

    for i, t in enumerate(tables, 1):
        cols = get_columns(scur, t)
        if not cols:
            print(f"[{i:>3}/{len(tables)}] {t:<45} skip (no insertable columns)")
            continue
        collist = ", ".join('"%s"' % c for c in cols)
        # source row count
        scur.execute(f'select count(*) from "{t}"')
        n = scur.fetchone()[0]
        if n == 0:
            print(f"[{i:>3}/{len(tables)}] {t:<45} 0 rows")
            copied.append((t, 0))
            continue
        buf = io.BytesIO()
        try:
            scur.copy_expert(
                f'COPY (SELECT {collist} FROM "{t}") TO STDOUT WITH (FORMAT binary)', buf)
            buf.seek(0)
            dcur.copy_expert(
                f'COPY "{t}" ({collist}) FROM STDIN WITH (FORMAT binary)', buf)
            # verify target count
            dcur.execute(f'select count(*) from "{t}"')
            m = dcur.fetchone()[0]
            flag = "" if m == n else f"  <-- MISMATCH (target={m})"
            print(f"[{i:>3}/{len(tables)}] {t:<45} {n} rows{flag}", flush=True)
            total_rows += m
            copied.append((t, m))
            if m != n:
                failures.append((t, f"count mismatch src={n} dst={m}"))
        except Exception as e:
            print(f"[{i:>3}/{len(tables)}] {t:<45} ERROR: {repr(e)[:140]}", flush=True)
            failures.append((t, repr(e)[:200]))

    # ---- reset sequences to MAX(owned column) ----
    print("\nResetting sequences...", flush=True)
    dcur.execute("""
        select s.relname as seq, t.relname as tbl, a.attname as col
        from pg_class s
        join pg_depend d on d.objid = s.oid and d.deptype='a'
        join pg_class t on t.oid = d.refobjid
        join pg_attribute a on a.attrelid = t.oid and a.attnum = d.refobjsubid
        where s.relkind='S' and s.relnamespace = 'public'::regnamespace
    """)
    seqs = dcur.fetchall()
    for seq, tbl, col in seqs:
        dcur.execute(f'select max("{col}") from "{tbl}"')
        mx = dcur.fetchone()[0]
        if mx is None:
            dcur.execute("select setval(%s, 1, false)", (f'public.{seq}',))
        else:
            dcur.execute("select setval(%s, %s, true)", (f'public.{seq}', mx))
    print(f"  reset {len(seqs)} sequences")

    dcur.execute("SET session_replication_role = origin;")
    print(f"\nDONE. {len(copied)} tables processed, {total_rows} total rows copied.")
    if failures:
        print(f"\n{len(failures)} ISSUES:")
        for t, msg in failures:
            print(f"  - {t}: {msg}")
        sys.exit(1)
    else:
        print("No errors.")

    src.close()
    dst.close()


if __name__ == "__main__":
    main()
