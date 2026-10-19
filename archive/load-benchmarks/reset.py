from __future__ import annotations
import sys

import config

KEEP = {
    "django_migrations",
    "lamindb_space", "lamindb_branch", "lamindb_user", "lamindb_storage",
    "lamindb_run", "lamindb_transform",
    "hubmodule_account", "secrets",
}
DBWRITE = "hubmodule_dbwrite"


def _pg(uri):
    try:
        import psycopg
        return psycopg.connect(uri)
    except ImportError:
        import psycopg2
        return psycopg2.connect(uri)


def _table(name):
    return 'ONLY "public"."' + name.replace('"', '""') + '"'


def _clear_tables(cur, tables):
    """Never expand the reset to tables excluded by KEEP, even transitively."""
    if not tables:
        return "nothing to clear"

    cur.execute("SAVEPOINT reset_truncate;")
    try:
        cur.execute("TRUNCATE " + ", ".join(map(_table, tables)) + " RESTRICT;")
    except Exception as error:
        cur.execute("ROLLBACK TO SAVEPOINT reset_truncate;")
        cur.execute("RELEASE SAVEPOINT reset_truncate;")
        # PostgreSQL refuses TRUNCATE even if the referencing kept tables are
        # empty or their foreign keys are NULL. DELETE checks actual rows.
        sqlstate = getattr(error, "sqlstate", None) or getattr(error, "pgcode", None)
        if sqlstate != "0A000":
            raise
        # DELETE can also have cascading actions in custom schemas. Refuse any
        # action that could delete or modify rows outside the reset's table list.
        cur.execute("""
            SELECT child_ns.nspname, child.relname, c.conname
            FROM pg_constraint c
            JOIN pg_class parent ON parent.oid = c.confrelid
            JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
            JOIN pg_class child ON child.oid = c.conrelid
            JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace
            WHERE c.contype = 'f' AND c.confdeltype IN ('c', 'n', 'd')
              AND parent_ns.nspname = 'public' AND parent.relname = ANY(%s)
              AND NOT (child_ns.nspname = 'public' AND child.relname = ANY(%s));
        """, (tables, tables))
        unsafe = cur.fetchall()
        if unsafe:
            raise RuntimeError(f"DELETE could modify kept tables through foreign keys: {unsafe}")
        cur.execute("SET CONSTRAINTS ALL DEFERRED;")
        for table in tables:
            cur.execute(f"DELETE FROM {_table(table)};")
        # Validate before reporting success. Dangling references in kept rows
        # abort the transaction rather than deleting those rows too.
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE;")
        return f"cleared {len(tables)} tables via DELETE (kept tables block TRUNCATE)"
    else:
        cur.execute("RELEASE SAVEPOINT reset_truncate;")
        return f"truncated {len(tables)} tables"


def _reset(conn):
    """Commit the whole reset, or roll it all back on any failure."""
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public';")
            tables = {r[0] for r in cur.fetchall()}
            to_clear = sorted(
                (t for t in tables if t not in KEEP and not t.startswith("bionty_")),
                key=lambda t: (t == DBWRITE, t),
            )
            # Include dbwrite in the same operation: truncating it separately
            # with CASCADE used to provide a second path into kept tables. On
            # the DELETE path, clear it last to remove newly generated log rows.
            result = _clear_tables(cur, to_clear)
            counts = {}
            for table in ("lamindb_record", "lamindb_feature", "lamindb_schema",
                          "lamindb_storage", DBWRITE):
                if table in tables:
                    cur.execute(f"SELECT count(*) FROM {_table(table)};")
                    counts[table] = cur.fetchone()[0]
    return result, counts


def main():
    if "--yes" not in sys.argv:
        sys.exit("refusing without --yes. Wipes the target instance.\n"
                 "  python reset.py --yes [--instance owner/name]")
    target = config.LOAD_INSTANCE
    if "--instance" in sys.argv:
        target = sys.argv[sys.argv.index("--instance") + 1]

    import lamindb as ln

    ln.connect(target, use_root_db_user=True)
    conn = _pg(ln.setup.settings.instance.db)
    try:
        result, counts = _reset(conn)
    except Exception as error:
        sys.exit(f"Reset failed; all reset changes were rolled back: {error}\n"
                 "Kept records may still reference benchmark data. Resolve those "
                 "references explicitly; do not use TRUNCATE CASCADE.")
    finally:
        conn.close()
    print(result)
    for table, count in counts.items():
        print(f"{table}: {count} rows")
    print(f"reset complete for {target}")


if __name__ == "__main__":
    main()
