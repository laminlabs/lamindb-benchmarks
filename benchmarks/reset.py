"""Wipe a benchmark target instance. Refuses to run without --yes.

One-off maintenance: no tracking run or transform is created on the benchmark
instance. Connects directly to the target being wiped.

Usage:
  python benchmarks/reset.py --yes
  python benchmarks/reset.py --yes --instance owner/name
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import lamindb as ln

from benchmarks.config import BENCHMARK_INSTANCE, LOAD_INSTANCE

KEEP = {
    "django_migrations",
    "lamindb_space",
    "lamindb_branch",
    "lamindb_user",
    "lamindb_storage",
    "lamindb_run",
    "lamindb_transform",
    "hubmodule_account",
    "secrets",
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
    parser = argparse.ArgumentParser(
        description="Wipe a benchmark target instance without tracking a run."
    )
    parser.add_argument(
        "--yes", action="store_true", help="Confirm removal of the target's data."
    )
    parser.add_argument("--instance", default=LOAD_INSTANCE)
    args = parser.parse_args()
    if not args.yes:
        parser.error("refusing without --yes; this wipes the target instance")
    _wipe(args.instance)


def _wipe(target: str) -> None:
    ln.connect(target, use_root_db_user=True)
    conn = _pg(ln.setup.settings.instance.db)
    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public';")
        tables = [r[0] for r in cur.fetchall()]
        to_clear = [
            t
            for t in tables
            if t not in KEEP and not t.startswith("bionty_") and t != DBWRITE
        ]

        if to_clear:
            try:
                cur.execute(
                    "TRUNCATE " + ", ".join(f'"{t}"' for t in to_clear) + " CASCADE;"
                )
                print(f"truncated {len(to_clear)} user tables")
            except Exception as e:  # noqa: BLE001 — best-effort diagnostics across database drivers
                sys.exit(
                    f"TRUNCATE failed: {e}\n(a kept table likely references one of these — "
                    "add it to KEEP or tell me the name)"
                )

        try:
            cur.execute(f'TRUNCATE "{DBWRITE}" CASCADE;')
            print("truncated hubmodule_dbwrite")
        except Exception as e:  # noqa: BLE001 — best-effort diagnostics across database drivers
            print(f"[warn] TRUNCATE {DBWRITE} failed ({e}); trying DELETE...")
            try:
                cur.execute(f'DELETE FROM "{DBWRITE}";')
                print("cleared hubmodule_dbwrite via DELETE")
            except Exception as e2:  # noqa: BLE001 — best-effort diagnostics across database drivers
                print(f"[warn] could not clear {DBWRITE} ({e2})")

        for t in ("lamindb_record", "lamindb_feature", "lamindb_schema", DBWRITE):
            try:
                cur.execute(f'SELECT count(*) FROM "{t}";')
                print(f"{t}: {cur.fetchone()[0]} rows")
            except Exception:  # noqa: BLE001 — best-effort diagnostics across database drivers
                print(f"[warn] could not count {t}")

    if target == BENCHMARK_INSTANCE:
        try:
            expected = ln.setup.settings.instance.storage.root_as_str
            if not ln.Storage.filter(root=expected).exists():
                ln.Storage(root=expected).save()
                print(f"re-registered storage: {expected}")
        except Exception as e:  # noqa: BLE001 — best-effort diagnostics across database drivers
            print(f"[warn] could not re-register storage ({e})")
    print(f"reset complete for {target}")


if __name__ == "__main__":
    main()
