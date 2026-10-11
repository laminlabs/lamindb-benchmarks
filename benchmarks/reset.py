"""Wipe a benchmark target instance. Refuses to run without --yes.

Tracked on the benchmark instance. The target is wiped in a child process,
because a live ln.track() cannot switch instances.

Usage:
  python benchmarks/reset.py --yes
  python benchmarks/reset.py --yes --instance owner/name
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import lamindb as ln

from benchmarks.config import (
    BENCHMARK_INSTANCE,
    BENCHMARK_INSTANCE_ENV,
    LOAD_INSTANCE,
    instance_from_env,
)

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
        return psycopg.connect(uri, autocommit=True)
    except ImportError:
        import psycopg2
        c = psycopg2.connect(uri)
        c.autocommit = True
        return c


def main():
    if "--yes" not in sys.argv:
        sys.exit(
            "refusing without --yes. Wipes the target instance.\n"
            "  python benchmarks/reset.py --yes [--instance owner/name]"
        )
    target = LOAD_INSTANCE
    if "--instance" in sys.argv:
        target = sys.argv[sys.argv.index("--instance") + 1]
    if "--wipe-only" in sys.argv:
        _wipe(target)
        return
    # Track the benchmark instance, not the database being wiped. TRUNCATE CASCADE
    # on the target deletes the transform this process would otherwise finish.
    # The wipe runs in a child process: ln.track() cannot stay open across connect().
    tracking_instance = instance_from_env(BENCHMARK_INSTANCE_ENV, BENCHMARK_INSTANCE)
    if tracking_instance == target:
        _wipe(target)
        return
    ln.connect(tracking_instance)
    ln.track()
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--yes",
                "--wipe-only",
                "--instance",
                target,
            ],
            check=False,
        )
        if completed.returncode != 0:
            sys.exit(completed.returncode)
    finally:
        ln.finish()


def _wipe(target: str) -> None:
    ln.connect(target, use_root_db_user=True)
    conn = _pg(ln.setup.settings.instance.db)
    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public';")
        tables = [r[0] for r in cur.fetchall()]
        to_clear = [
            t for t in tables
            if t not in KEEP and not t.startswith("bionty_") and t != DBWRITE
        ]

        if to_clear:
            try:
                cur.execute(
                    "TRUNCATE " + ", ".join(f'"{t}"' for t in to_clear) + " CASCADE;"
                )
                print(f"truncated {len(to_clear)} user tables")
            except Exception as e:
                sys.exit(
                    f"TRUNCATE failed: {e}\n(a kept table likely references one of these — "
                    "add it to KEEP or tell me the name)"
                )

        try:
            cur.execute(f'TRUNCATE "{DBWRITE}" CASCADE;')
            print("truncated hubmodule_dbwrite")
        except Exception as e:
            print(f"[warn] TRUNCATE {DBWRITE} failed ({e}); trying DELETE...")
            try:
                cur.execute(f'DELETE FROM "{DBWRITE}";')
                print("cleared hubmodule_dbwrite via DELETE")
            except Exception as e2:
                print(f"[warn] could not clear {DBWRITE} ({e2})")

        for t in ("lamindb_record", "lamindb_feature", "lamindb_schema", DBWRITE):
            try:
                cur.execute(f'SELECT count(*) FROM "{t}";')
                print(f"{t}: {cur.fetchone()[0]} rows")
            except Exception:
                pass

    if target == BENCHMARK_INSTANCE:
        try:
            expected = ln.setup.settings.instance.storage.root_as_str
            if not ln.Storage.filter(root=expected).exists():
                ln.Storage(root=expected).save()
                print(f"re-registered storage: {expected}")
        except Exception as e:
            print(f"[warn] could not re-register storage ({e})")
    print(f"reset complete for {target}")


if __name__ == "__main__":
    main()
