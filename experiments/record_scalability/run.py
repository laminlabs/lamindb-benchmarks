"""Grow the intervention registry and publish measurements at cumulative milestones.

Usage: python experiments/record_scalability/run.py CONFIG OUTPUT_DIRECTORY
Only the parent process writes tracking records and measurement artifacts.
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import lamindb as ln
import pandas as pd
from django.db import OperationalError, transaction

from benchmarks.config import (
    LOAD_INSTANCE,
    LOAD_INSTANCE_ENV,
    instance_from_env,
    read_config,
    require_int,
)
from benchmarks.measurements import load_measurements, results_dir, write_measurement
from benchmarks.observations import child_arguments, observation, write_run_information
from benchmarks.timing import rows_from_timed, time_calls
from experiments.record_scalability.explain_report import render_explain_report
from experiments.record_scalability.observe import (
    BUFFER_FIELDS,
    EVENTS_TABLE,
    count_rows,
    explain_lookup,
    profile_lookup,
)
from experiments.record_scalability.parameters import (
    DEFAULT_BATCH,
    DEFAULT_REPETITIONS,
    resolve_parameters,
)
from experiments.record_scalability.query_parameters import (
    PROTOCOLS,
    parse_timestamp,
    query_parameters,
)
from experiments.record_scalability.workload import workload_parameters

# Retain the stored record prefix so existing populations can still be resumed.
NAME_PREFIX = "experiment_002"
NAME_FIELD = "__lamindb_record_name__"
BATCH_SIZE = 1000
MAX_BATCH_ATTEMPTS = 3
RETRYABLE_SQLSTATES = {"40001", "40P01", "55P03"}
SUPPLIERS = [f"supplier_{i:02d}" for i in range(10)]
LOOKUPS = (
    ("uid_lookup", False),
    ("uid_with_features", False),
    ("uid_batch_with_features", True),
)
RECENT_QUERIES = ("recent_by_treatment_started_at", "recent_by_created_at")
GENE_QUERY = "interventions_by_gene"
COHORT_QUERY = "interventions_by_gene_protocol_time"
RECENT_LIMIT = 100
TIMESTAMP_DTYPE = pd.DatetimeTZDtype(unit="ns", tz="UTC")


def query_at(params):
    workload = workload_parameters(params)
    rng = random.Random(workload["random_seed"])
    n_rows = require_int(params, "n_rows", minimum=1)
    repetitions = require_int(
        params, "n_repetitions", minimum=1, default=DEFAULT_REPETITIONS
    )
    n_batch = require_int(params, "n_batch", minimum=1, default=DEFAULT_BATCH)
    prefix = f"{NAME_PREFIX}_"
    intervention = ensure_type("Intervention")
    timestamp_feature = ensure_feature("treatment_started_at", TIMESTAMP_DTYPE)
    gene_feature = ln.Feature.get(name="genes", is_type=False)
    query_settings = query_parameters(params)
    query_bounds = (
        parse_timestamp(query_settings["query_treatment_start"]),
        parse_timestamp(query_settings["query_treatment_end"]),
    )
    gene_name = query_settings["query_gene"]
    gene = ln.Record.filter(
        type=ensure_type("Gene"), is_type=False, name=gene_name
    ).one_or_none()
    if gene is None:
        raise ValueError(
            f"Configured query gene {gene_name!r} is missing from the load instance"
        )

    uids = list(
        ln.Record.filter(name__startswith=prefix)
        .order_by("name", "uid")
        .values_list("uid", flat=True)
    )
    if not uids:
        sys.exit(f"no intervention records found for {prefix!r}")
    single = rng.sample(uids, 1)
    batch = rng.sample(uids, min(n_batch, len(uids)))
    lookup_context = {
        "intervention": intervention,
        "timestamp_feature": timestamp_feature,
        "gene": gene,
        "gene_feature": gene_feature,
        "query_settings": query_settings,
        "query_bounds": query_bounds,
    }
    sampled_by_query = {
        **{name: batch if is_batch else single for name, is_batch in LOOKUPS},
        **{name: [] for name in RECENT_QUERIES},
        GENE_QUERY: [],
        COHORT_QUERY: [],
    }
    for query_name, sampled in sampled_by_query.items():
        sql_samples = []

        def measured_lookup(
            query_name=query_name, sampled=sampled, sql_samples=sql_samples
        ):
            value, stats = profile_lookup(
                lambda: _lookup(
                    query_name,
                    sampled,
                    **lookup_context,
                )
            )
            sql_samples.append(stats)
            return value

        timed = time_calls(measured_lookup, repetitions)
        rows = [
            {**row, **stats}
            for row, stats in zip(rows_from_timed(timed), sql_samples, strict=True)
        ]
        print(
            f"query {query_name} n_uids={len(sampled)} "
            f"rows={timed['last_value']} median={timed['latency_median_ms']} ms "
            f"({repetitions} reps)"
        )
        written = write_measurement(
            query_name,
            rows,
            columns=(
                "repetition",
                "latency_ms",
                "value",
                "sql_execute_count",
                "sql_execute_ms",
            ),
            key=f"benchmarks/measurements/{NAME_PREFIX}/{query_name}.parquet",
            description=f"Timed {query_name} of intervention records named {prefix}*",
            features={
                "measurement_kind": "lookup",
                "query_name": query_name,
                "name_prefix": prefix,
                "n_rows": n_rows,
                "n_uids": len(sampled),
                "sampled_uids": json.dumps(sampled),
                **_query_metadata(query_name, gene, query_settings),
                **workload,
            },
        )
        print(
            "measurement not saved; results directory is unset"
            if written is None
            else f"measurement written: {written.name}"
        )
    _record_explain(
        sampled_by_query,
        n_rows,
        prefix,
        workload,
        **lookup_context,
    )
    _record_database(n_rows, prefix)


def analyze_populated_tables() -> None:
    """Refresh planner statistics after insertion, outside all benchmark timers."""
    # The JWT role used by the benchmark cannot analyze tables it does not own.
    # This helper runs in a separate maintenance process. The main experiment
    # keeps its normal benchmark role for the subsequent timed queries.
    target = instance_from_env(LOAD_INSTANCE_ENV, LOAD_INSTANCE)
    ln.connect(target, use_root_db_user=True)
    import psycopg
    from psycopg import sql

    # Use a dedicated maintenance connection: switching database roles resets
    # Django, so existing model/transaction objects must not be reused here.
    with (
        psycopg.connect(ln.setup.settings.instance.db, autocommit=True) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND (tablename LIKE %s OR tablename = %s) ORDER BY tablename",
            ("lamindb_%", "hubmodule_dbwrite"),
        )
        tables = [row[0] for row in cursor.fetchall()]
        if not tables:
            raise RuntimeError("No benchmark tables found for ANALYZE")
        names = sql.SQL(", ").join(sql.Identifier("public", table) for table in tables)
        cursor.execute(sql.SQL("ANALYZE {}").format(names))
    print(f"ANALYZE completed for {len(tables)} load-instance tables")


def _insert_rows(
    intervention: ln.Record,
    pools: dict,
    n_rows: int,
    workload: dict[str, int],
    batches: list[dict],
    *,
    existing_rows: int = 0,
    run_stamp: str | None = None,
) -> int:
    stamp = run_stamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    rng = random.Random(workload["random_seed"])
    # Continue the original random sequence, outside the measured insert time.
    for _ in range(existing_rows):
        _row(pools, "", rng, workload)
    created = 0
    try:
        while created < n_rows - existing_rows:
            count = min(BATCH_SIZE, n_rows - existing_rows - created)
            batch_started = time.perf_counter()
            rows = [
                _row(
                    pools,
                    f"{NAME_PREFIX}_{stamp}_{existing_rows + created + offset:05d}",
                    rng,
                    workload,
                )
                for offset in range(count)
            ]
            attempts = []
            succeeded = False
            try:
                _save_batch(intervention, rows, attempts)
                succeeded = True
            finally:
                batch_seconds = time.perf_counter() - batch_started
                batches.append(
                    {
                        "repetition": len(batches) + 1,
                        "latency_ms": round(batch_seconds * 1000, 3),
                        "value": count if succeeded else 0,
                        "batch_index": len(batches) + 1,
                        "batch_size": count,
                        "records_before": existing_rows + created,
                        "records_after": existing_rows
                        + created
                        + (count if succeeded else 0),
                        "retry_count": max(len(attempts) - 1, 0),
                        "attempt_count": len(attempts),
                        "save_ms": round(sum(a["save_ms"] for a in attempts), 3),
                        "retry_wait_ms": sum(a["retry_wait_ms"] for a in attempts),
                        "succeeded": succeeded,
                        "sqlstate": attempts[-1]["sqlstate"] if attempts else "",
                    }
                )
            created += count
    finally:
        # The runner uploads these diagnostics even if a batch exhausts retries.
        if batches:
            write_measurement(
                "insert_batches",
                batches,
                columns=tuple(batches[0]),
                key=f"benchmarks/measurements/{NAME_PREFIX}/insert_batches.parquet",
                description="Per-batch population timings, retries, and outcomes",
                features={
                    "measurement_kind": "insert_batches",
                    "query_name": "insert_batches",
                    "n_rows": n_rows,
                    "existing_rows": existing_rows,
                    "batch_size": BATCH_SIZE,
                    "insert_retry_count": sum(b["retry_count"] for b in batches),
                    **workload,
                },
            )
    return created


def _write_insert(
    n_rows: int,
    created: int,
    elapsed: float,
    before: int | None,
    after: int | None,
    workload: dict[str, int],
    batches: list[dict],
    *,
    existing_rows: int = 0,
) -> None:
    features: dict = {
        "measurement_kind": "insert",
        "query_name": "insert",
        "name_prefix": f"{NAME_PREFIX}_",
        "n_rows": n_rows,
        "existing_rows": existing_rows,
        "n_records": created,
        "insert_seconds": round(elapsed, 6),
        "insert_retry_count": sum(b["retry_count"] for b in batches),
        "insert_batch_count": len(batches),
        **workload,
    }
    if elapsed > 0:
        features["insert_records_per_sec"] = round(created / elapsed, 3)
    if before is not None and after is not None and created:
        n_events = max(after - before, 0)
        features["n_events"] = n_events
        features["write_events_per_record"] = round(n_events / created, 6)
    written = write_measurement(
        "insert",
        [{"repetition": 1, "latency_ms": round(elapsed * 1000.0, 3), "value": created}],
        key=f"benchmarks/measurements/{NAME_PREFIX}/insert.parquet",
        description=(
            f"Timed insert of {created} intervention records named {NAME_PREFIX}_*"
        ),
        features=features,
    )
    rate = features.get("insert_records_per_sec")
    print(
        f"insert {created} records in {elapsed:.3f}s"
        + (f" ({rate:.1f}/s)" if rate is not None else "")
    )
    print(
        "insert measurement not saved; results directory is unset"
        if written is None
        else f"measurement written: {written.name}"
    )


def ensure_intervention_sheet(
    workload: dict[str, int],
) -> tuple[ln.Record, dict[str, ln.Feature], dict]:
    supplier_type = ensure_type("Supplier")
    gene_type = ensure_type("Gene")
    molecule_type = ensure_type("SmallMolecule")
    intervention = ensure_type("Intervention")
    features = {
        "dose": ensure_feature("dose", str),
        "protocol": ensure_feature("protocol", str),
        "treatment_date": ensure_feature("treatment_date", datetime),
        "treatment_started_at": ensure_feature("treatment_started_at", TIMESTAMP_DTYPE),
        "treatment_time_min": ensure_feature("treatment_time_min", int),
        "supplier": ensure_feature("supplier", supplier_type),
        "genes": ensure_feature("genes", gene_type),
        "small_molecules": ensure_feature("small_molecules", molecule_type),
    }
    if intervention.schema is None:
        intervention.schema = ln.Schema(
            list(features.values()),
            name="intervention_schema",
            minimal_set=True,
        ).save()
        intervention.save()
    else:
        present = {feature.uid for feature in intervention.schema.members.all()}
        missing = [
            feature for feature in features.values() if feature.uid not in present
        ]
        if missing:
            intervention.schema.add(missing, optional=False)
    pools = {
        "suppliers": ensure_pool(SUPPLIERS, supplier_type),
        "genes": ensure_pool(
            [f"gene_{i:03d}" for i in range(workload["n_genes"])],
            gene_type,
        ),
        "molecules": ensure_pool(
            [f"mol_{i:02d}" for i in range(workload["n_molecules"])],
            molecule_type,
        ),
    }
    return intervention, features, pools


def ensure_type(name: str) -> ln.Record:
    matches = ln.Record.filter(name=name, is_type=True).to_list()
    if len(matches) > 1:
        sys.exit(f"multiple record types named {name!r}")
    if matches:
        return matches[0]
    return ln.Record(name=name, is_type=True).save()


def ensure_feature(name: str, dtype: object) -> ln.Feature:
    from lamindb.models.feature import serialize_dtype

    matches = ln.Feature.filter(name=name, is_type=False).to_list()
    if len(matches) > 1:
        sys.exit(f"multiple features named {name!r}")
    expected = serialize_dtype(dtype)
    if matches:
        feature = matches[0]
        if feature._dtype_str != expected:
            sys.exit(
                f"feature {name!r} has dtype {feature._dtype_str}, expected {expected}. "
                "Reset the load instance so the feature can be recreated."
            )
        return feature
    return ln.Feature(name=name, dtype=dtype).save()


def ensure_pool(names: list[str], type_record: ln.Record) -> list[ln.Record]:
    existing = {
        record.name: record
        for record in ln.Record.filter(type=type_record, is_type=False)
    }
    records = []
    missing = []
    for name in names:
        record = existing.get(name)
        if record is None:
            record = ln.Record(name=name, type=type_record)
            missing.append(record)
        records.append(record)
    if missing:
        ln.save(missing)
    return records


def _row(pools: dict, name: str, rng: random.Random, workload: dict[str, int]) -> dict:
    lo_g, hi_g = workload["genes_per_record_min"], workload["genes_per_record_max"]
    lo_m = workload["small_molecules_per_record_min"]
    hi_m = workload["small_molecules_per_record_max"]
    treated = date(2024, 1, 1) + timedelta(days=rng.randint(0, 700))
    row = {
        NAME_FIELD: name,
        "dose": f"{round(rng.uniform(0.1, 100.0), 2)} uM",
        "protocol": rng.choice(PROTOCOLS),
        "treatment_date": treated,
        "treatment_time_min": rng.randint(5, 1440),
        "supplier": rng.choice(pools["suppliers"]).name,
        "genes": [
            gene.name for gene in rng.sample(pools["genes"], rng.randint(lo_g, hi_g))
        ],
        "small_molecules": [
            molecule.name
            for molecule in rng.sample(pools["molecules"], rng.randint(lo_m, hi_m))
        ],
    }
    row["treatment_started_at"] = datetime.combine(
        treated, datetime.min.time(), tzinfo=UTC
    ) + timedelta(seconds=rng.randrange(86400))
    return row


def _save_batch(
    intervention: ln.Record,
    rows: list[dict],
    attempts: list[dict],
) -> None:
    frame = pd.DataFrame(rows)
    for attempt in range(1, MAX_BATCH_ATTEMPTS + 1):
        started = time.perf_counter()
        retry_delay = 0
        sqlstate = ""
        try:
            with transaction.atomic(using=intervention._state.db):
                ln.Record.from_dataframe(frame, type=intervention).save()
            return
        except OperationalError as error:
            cause = error.__cause__
            sqlstate = (
                getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None) or ""
            )
            if sqlstate not in RETRYABLE_SQLSTATES or attempt == MAX_BATCH_ATTEMPTS:
                raise
            retry_delay = 2 ** (attempt - 1)
            print(
                f"[warn] batch save failed ({sqlstate}); "
                f"retrying in {retry_delay}s (attempt {attempt + 1}/{MAX_BATCH_ATTEMPTS})"
            )
        finally:
            attempts.append(
                {
                    "save_ms": (time.perf_counter() - started) * 1000,
                    "retry_wait_ms": retry_delay * 1000,
                    "sqlstate": sqlstate,
                }
            )
        if retry_delay:
            time.sleep(retry_delay)


def _query_metadata(query_name, gene, query_settings):
    """Keep filters and limits identical in timing tables and diagnostics."""
    if query_name in RECENT_QUERIES:
        return {
            "result_limit": RECENT_LIMIT,
            "order_by": query_name.removeprefix("recent_by_"),
        }
    if query_name not in (GENE_QUERY, COHORT_QUERY):
        return {}
    metadata = {"result_limit": RECENT_LIMIT, "order_by": "created_at"}
    if gene is not None:
        metadata.update(query_gene=gene.name, query_gene_uid=gene.uid)
    if query_name == COHORT_QUERY:
        metadata.update(query_settings or {})
        metadata.update(order_by="treatment_started_at", include_features=True)
    return metadata


def _record_explain(
    sampled_by_query: dict[str, list[str]],
    n_rows: int,
    prefix: str,
    workload: dict[str, int],
    **lookup_context,
) -> None:
    reports, rows = [], []
    for query_name, sampled in sampled_by_query.items():
        try:
            facts = explain_lookup(
                query_name,
                sampled,
                lambda query_name=query_name, sampled=sampled: _lookup(
                    query_name,
                    sampled,
                    **lookup_context,
                ),
            )
        except (OperationalError, ValueError, RuntimeError) as error:
            print(f"[warn] explain {query_name} failed: {error}")
            reports.append({"query_name": query_name, "error": str(error)})
            continue
        metadata = _query_metadata(
            query_name,
            lookup_context.get("gene"),
            lookup_context.get("query_settings"),
        )
        reports.append(
            {
                "query_name": query_name,
                "statements": facts["statements"],
                "parameters": metadata,
            }
        )
        features = {
            "measurement_kind": "explain",
            "query_name": f"explain_{query_name}",
            "source_query": query_name,
            "name_prefix": prefix,
            "n_rows": n_rows,
            "n_uids": len(sampled),
            "used_index": facts["used_index"],
            "used_seq_scan": facts["used_seq_scan"],
            "n_statements": facts["n_statements"],
            "plan_source": facts["plan_source"],
            "query_plan": facts["query_plan"],
            **workload,
            **dict.fromkeys(
                (
                    "result_limit",
                    "order_by",
                    "query_gene",
                    "query_gene_uid",
                    "query_protocol",
                    "query_treatment_start",
                    "query_treatment_end",
                )
            ),
            "include_features": query_name == COHORT_QUERY,
            **metadata,
            "sampled_uids": json.dumps(sampled),
            **{key: facts[key] for key in BUFFER_FIELDS},
        }
        execution = (
            facts["execution_time_ms"]
            if facts["execution_time_ms"] is not None
            else 0.0
        )
        scan = _scan_label(facts["used_index"], facts["used_seq_scan"])
        rows.append(
            {
                "repetition": 1,
                "latency_ms": execution,
                "value": facts["n_statements"],
                **features,
                "execution_time_ms": facts["execution_time_ms"],
                "planning_time_ms": facts["planning_time_ms"],
                "sql": facts["sql"],
                "explain_analyze": facts["explain_analyze"],
                "statements_json": json.dumps(facts["statements"]),
            }
        )
        print(
            f"explain {query_name} scan={scan} "
            f"execution={facts['execution_time_ms']} ms "
            f"statements={facts['n_statements']} source={facts['plan_source']}"
        )
    if rows:
        written = write_measurement(
            "explain",
            rows,
            columns=tuple(rows[0]),
            key=f"benchmarks/measurements/{NAME_PREFIX}/explain.parquet",
            description="EXPLAIN ANALYZE diagnostics for all logical lookups at this milestone.",
            features={},
        )
        print(
            "explain measurement not saved; results directory is unset"
            if written is None
            else f"measurement written: {written.name}"
        )
    directory = results_dir()
    if directory is not None:
        (directory / "query_plans.html").write_text(
            render_explain_report(reports, n_rows)
        )


def _record_database(n_rows: int, prefix: str) -> None:
    from experiments.record_scalability.observe import database_facts

    try:
        facts = database_facts()
    except (OperationalError, ValueError, RuntimeError) as error:
        print(f"[warn] database facts failed: {error}")
        return
    n_records = ln.Record.filter(name__startswith=prefix).count()
    distribution = facts.pop("distribution")
    features = {
        "measurement_kind": "health",
        "query_name": "health",
        "name_prefix": prefix,
        "n_rows": n_rows,
        "n_records": n_records,
        "n_events": facts["n_events"],
        "n_auxiliary_entries": facts["n_auxiliary_entries"],
        "n_total_rows": facts["n_total_rows"],
        **_insert_rates(),
    }
    if n_records:
        features["write_events_per_record"] = round(facts["n_events"] / n_records, 6)
    for key in ("disk_space_bytes", "cache_hit_ratio", "active_connections"):
        if key in facts:
            features[key] = facts[key]
    written = write_measurement(
        "health",
        [{"repetition": 1, "latency_ms": 0.0, "value": n_records}],
        key=f"benchmarks/measurements/{NAME_PREFIX}/health.parquet",
        description=(
            f"Server health after {n_records} intervention records named {prefix}*"
        ),
        features=features,
    )
    print(
        f"health records={n_records} events={facts['n_events']} "
        f"auxiliary={facts['n_auxiliary_entries']} "
        f"disk={facts.get('disk_space_bytes')} "
        f"cache_hit={facts.get('cache_hit_ratio')}"
    )
    print(
        "health measurement not saved; results directory is unset"
        if written is None
        else f"measurement written: {written.name}"
    )
    write_measurement(
        "table_distribution",
        distribution,
        columns=tuple(distribution[0]) if distribution else ("table_name",),
        key=f"benchmarks/measurements/{NAME_PREFIX}/table_distribution.parquet",
        description="One row per database table, with counts, sizes and bloat estimates.",
        features={},
    )
    print(f"distribution written for {len(distribution)} tables")


def _insert_rates() -> dict:
    directory = results_dir()
    if directory is None:
        return {}
    for item in load_measurements(directory):
        if item.get("file") != "insert.parquet":
            continue
        features = item.get("features") or {}
        return {
            key: features[key]
            for key in ("insert_seconds", "insert_records_per_sec")
            if key in features
        }
    return {}


def _scan_label(used_index: bool, used_seq_scan: bool) -> str:
    if used_index and not used_seq_scan:
        return "index"
    if used_seq_scan and not used_index:
        return "seq"
    if used_seq_scan and used_index:
        return "mixed"
    return "unknown"


def _lookup(
    query_name: str,
    uids: list[str],
    *,
    intervention=None,
    timestamp_feature=None,
    gene=None,
    gene_feature=None,
    query_settings=None,
    query_bounds=None,
) -> int:
    if query_name == COHORT_QUERY:
        if any(
            value is None
            for value in (
                intervention,
                timestamp_feature,
                gene,
                gene_feature,
                query_settings,
                query_bounds,
            )
        ):
            raise ValueError(
                "The cohort query requires its type, timestamp feature, gene and query settings"
            )
        start, end = query_bounds
        cohort = (
            ln.Record.filter(type=intervention, is_type=False)
            .filter(
                gene_feature == gene,
                protocol=query_settings["query_protocol"],
                treatment_started_at__gte=start,
                treatment_started_at__lt=end,
            )
            .order_by(-timestamp_feature, "-uid")
        )
        # Bound selection before feature export: the SDK cannot export a sliced RecordSet.
        selected_uids = list(cohort.values_list("uid", flat=True)[:RECENT_LIMIT])
        frame = (
            ln.Record.filter(uid__in=selected_uids)
            .order_by(-timestamp_feature, "-uid")
            .to_dataframe(include="features")
        )
        return len(frame)
    if query_name == GENE_QUERY:
        if intervention is None or gene is None or gene_feature is None:
            raise ValueError(
                "The gene query requires the intervention type, target gene and gene feature"
            )
        return len(
            list(
                ln.Record.filter(type=intervention, is_type=False)
                .filter(gene_feature == gene)
                .order_by("-created_at", "-uid")[:RECENT_LIMIT]
            )
        )
    if query_name in RECENT_QUERIES:
        if intervention is None or timestamp_feature is None:
            raise ValueError(
                "Recent-record queries require the intervention type and timestamp feature"
            )
        query = ln.Record.filter(type=intervention, is_type=False)
        order = (
            -timestamp_feature
            if query_name == "recent_by_treatment_started_at"
            else "-created_at"
        )
        return len(list(query.order_by(order, "-uid")[:RECENT_LIMIT]))
    if query_name == "uid_batch_with_features":
        frame = ln.Record.filter(uid__in=uids).to_dataframe(include="features")
        return len(frame)
    query = ln.Record.filter(uid=uids[0])
    if query_name == "uid_lookup":
        return 0 if query.one_or_none() is None else 1
    frame = query.to_dataframe(include="features")
    return len(frame)


def main(argv=None):
    args = child_arguments(argv)
    params = resolve_parameters(read_config(args.config))
    targets = params["n_rows"]
    workload = workload_parameters(params)
    ln.connect(instance_from_env(LOAD_INSTANCE_ENV, LOAD_INSTANCE))
    intervention, features, pools = ensure_intervention_sheet(workload)

    def current_count():
        total = ln.Record.filter(type=intervention, is_type=False).count()
        benchmark = ln.Record.filter(
            type=intervention, is_type=False, name__startswith=f"{NAME_PREFIX}_"
        ).count()
        if total != benchmark:
            raise ValueError(
                "Intervention registry contains records outside this benchmark"
            )
        return benchmark

    starting = current_count()
    # Preserve the starting population without creating an empty observation.
    write_run_information(args.output, {"starting_rows": starting})
    if starting:
        annotated = ln.Record.filter(
            type=intervention,
            is_type=False,
            values_json__feature=features["treatment_started_at"],
        ).count()
        if annotated != starting:
            raise ValueError(
                "Existing interventions lack treatment_started_at; reset the load instance "
                "or backfill that feature before resuming this workload"
            )
    if "existing_rows" in params and params["existing_rows"] != starting:
        raise ValueError(
            f"Configured existing_rows differs from detected count {starting}"
        )
    stored = (intervention.extra_data or {}).get("benchmark_workload")
    if starting and stored is not None and stored != workload:
        raise ValueError(
            "Resume workload differs from the existing intervention registry"
        )
    for key, names in (
        ("suppliers", SUPPLIERS),
        ("genes", [f"gene_{i:03d}" for i in range(workload["n_genes"])]),
        ("molecules", [f"mol_{i:02d}" for i in range(workload["n_molecules"])]),
    ):
        kind = ensure_type(
            {"suppliers": "Supplier", "genes": "Gene", "molecules": "SmallMolecule"}[
                key
            ]
        )
        if ln.Record.filter(type=kind, is_type=False).count() != len(names):
            raise ValueError(
                f"Resume {key} catalog does not match the configured workload"
            )
    intervention.extra_data = {
        **(intervention.extra_data or {}),
        "benchmark_workload": workload,
    }
    intervention.save(update_fields=["extra_data"])
    print(f"Detected {starting:,} existing interventions", flush=True)
    eligible = [target for target in targets if target >= starting]
    if not eligible:
        raise ValueError(f"All milestones are below the detected population {starting}")
    batches, current = [], starting
    before_events = count_rows(EVENTS_TABLE)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    for target in eligible:
        if current_count() != current:
            raise RuntimeError(
                "Population changed outside this execution; refusing to continue"
            )
        metadata = {
            "n_rows": target,
            "starting_rows": starting,
            "existing_rows": starting,
            "n_inserted_records": target - starting,
            "n_repetitions": params["n_repetitions"],
            "n_batch": params["n_batch"],
            **query_parameters(params),
            **workload,
        }
        with observation(
            args.output,
            f"record_scalability_n_rows_{target:_}",
            metadata,
            timestamped=True,
            timestamp_before="_n_rows_",
        ):
            _insert_rows(
                intervention,
                pools,
                target,
                workload,
                batches,
                existing_rows=current,
                run_stamp=stamp,
            )
            if current_count() != target:
                raise RuntimeError(f"Population did not reach {target}")
            current = target
            elapsed = sum(batch["latency_ms"] for batch in batches) / 1000
            # Always save the complete insertion history from this execution.
            if batches:
                write_measurement(
                    "insert_batches",
                    batches,
                    columns=tuple(batches[0]),
                    key=f"benchmarks/measurements/{NAME_PREFIX}/insert_batches.parquet",
                    description="All insert batches since this execution started.",
                    features={},
                )
            else:
                pd.DataFrame(
                    columns=[
                        "repetition",
                        "latency_ms",
                        "value",
                        "batch_index",
                        "batch_size",
                        "records_before",
                        "records_after",
                        "succeeded",
                    ]
                ).to_parquet(results_dir() / "insert_batches.parquet", index=False)
            _write_insert(
                target,
                target - starting,
                elapsed,
                before_events,
                count_rows(EVENTS_TABLE),
                workload,
                batches,
                existing_rows=starting,
            )
            # Isolate elevated maintenance credentials from the query process.
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from experiments.record_scalability.run import analyze_populated_tables; analyze_populated_tables()",
                ],
                cwd=ROOT,
                check=True,
            )
            query_at({**params, "n_rows": target})
        print(f"Published observation for {target:_} records", flush=True)


if __name__ == "__main__":
    main()
