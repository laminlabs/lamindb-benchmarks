"""Offline API-contract and measurement tests; no Notion/Lamin credentials needed.

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import ast
import copy
import importlib
import json
import os
import random
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

import httpx
import pandas as pd

from benchmarks.measurements import load_measurements
from experiments.experiment_005 import query, setup
from experiments.experiment_005.analysis import figure, warm_summary
from experiments.experiment_005.client import NotionClient, NotionError
from experiments.experiment_005.parameters import resolve_parameters
from experiments.experiment_005.workload import inventory, treatment, uid

ROOT = Path(__file__).resolve().parents[1]
PARENT = str(UUID(int=1))


class Clock:
    value = 0.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class FakeNotion:
    """A minimal schema-aware, paginated implementation of the used REST API."""

    def __init__(self):
        self.next_id = 10
        self.pages, self.databases, self.sources = {}, {}, {}
        self.calls = []
        self.fault = None
        self.clock = Clock()

    def new_id(self):
        self.next_id += 1
        return str(UUID(int=self.next_id))

    @staticmethod
    def match(page, condition):
        if "and" in condition:
            return all(FakeNotion.match(page, child) for child in condition["and"])
        if "or" in condition:
            return any(FakeNotion.match(page, child) for child in condition["or"])
        prop = page["properties"][condition["property"]]
        if "rich_text" in condition:
            return setup.plain(prop) == condition["rich_text"]["equals"]
        value = prop["number"]
        op, expected = next(iter(condition["number"].items()))
        if op == "is_empty":
            return value is None
        if value is None:
            return False
        return value < expected if op == "less_than" else value >= expected

    @staticmethod
    def page_properties(properties):
        return {
            name: {**copy.deepcopy(value), "type": next(iter(value))}
            for name, value in properties.items()
        }

    def handle(self, request):
        self.clock.value += 0.01
        body = json.loads(request.content) if request.content else {}
        path = request.url.path.removeprefix("/v1/")
        self.calls.append((request.method, path, body))
        if self.fault:
            result = self.fault(request, path, body)
            if result is not None:
                return result
        if request.method == "POST" and path == "pages":
            page_id = self.new_id()
            page = {
                "id": page_id,
                "parent": body["parent"],
                "properties": self.page_properties(body["properties"]),
            }
            self.pages[page_id] = page
            return httpx.Response(200, json=page)
        if request.method == "GET" and path.startswith("pages/"):
            return httpx.Response(200, json=self.pages[path.split("/")[1]])
        if path.startswith("blocks/"):
            parent = path.split("/")[1]
            blocks = [
                {
                    "id": db["id"],
                    "type": "child_database",
                    "child_database": {"title": db["table"]},
                }
                for db in self.databases.values()
                if db["parent"]["page_id"] == parent
            ]
            return httpx.Response(200, json={"results": blocks, "has_more": False})
        if request.method == "POST" and path == "databases":
            database_id, source_id = self.new_id(), self.new_id()
            db = {
                "id": database_id,
                "parent": body["parent"],
                "table": body["title"][0]["text"]["content"],
                "data_sources": [{"id": source_id}],
            }
            self.databases[database_id] = db
            self.sources[source_id] = {
                "id": source_id,
                "properties": self.page_properties(
                    body["initial_data_source"]["properties"]
                ),
            }
            return httpx.Response(200, json=db)
        if request.method == "GET" and path.startswith("databases/"):
            return httpx.Response(200, json=self.databases[path.split("/")[1]])
        if request.method == "GET" and path.startswith("data_sources/"):
            return httpx.Response(200, json=self.sources[path.split("/")[1]])
        if path.endswith("/query"):
            source = path.split("/")[1]
            pages = [
                copy.deepcopy(page)
                for page in self.pages.values()
                if page["parent"].get("data_source_id") == source
            ]
            if "filter" in body:
                pages = [page for page in pages if self.match(page, body["filter"])]
            # Force multiple pages even on tiny fixtures to exercise pagination.
            start = int(body.get("start_cursor", 0))
            selected = pages[start : start + 2]
            properties = request.url.params.get_list("filter_properties")
            if properties:
                for page in selected:
                    page["properties"] = {
                        k: v for k, v in page["properties"].items() if k in properties
                    }
            more = start + 2 < len(pages)
            return httpx.Response(
                200,
                json={
                    "results": selected,
                    "has_more": more,
                    "next_cursor": str(start + 2) if more else None,
                },
            )
        raise AssertionError(f"Unexpected API call: {request.method} {path}")


class NotionExperimentTests(unittest.TestCase):
    def setUp(self):
        self.raw = tempfile.TemporaryDirectory()
        self.directory = Path(self.raw.name)
        self.env = patch.dict(
            os.environ,
            {
                "NOTION_API_KEY": "test-token-never-uploaded",
                "NOTION_PARENT_PAGE_ID": PARENT,
                "NOTION_DATASET_PAGE_ID": "",
                "BENCHMARK_RESULTS_DIR": self.raw.name,
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.raw.cleanup)
        self.server = FakeNotion()
        self.params = resolve_parameters(
            {
                "experiment": "experiments/experiment_005",
                "n_rows": 5,
                "n_batch": 3,
                "insert_batch_size": 2,
            }
        )

    def client(self, params=None):
        return NotionClient(
            params or self.params,
            transport=httpx.MockTransport(self.server.handle),
            clock=self.server.clock.now,
            sleep=self.server.clock.sleep,
        )

    def build(self):
        api = self.client()
        try:
            dataset, sources = setup.ensure_dataset(api, self.params)
            catalogs = setup.ensure_catalogs(api, self.params, sources)
            state = {
                "dataset_page_id": dataset,
                "sources": sources,
                "catalogs": catalogs,
                "completed_rows": 0,
            }
            setup.populate(
                api,
                self.params,
                sources["interventions"]["data_source_id"],
                catalogs,
                state,
            )
            return state
        finally:
            api.close("setup")

    def test_fresh_population_queries_and_artifact_contract(self):
        state = self.build()
        api = self.client()
        try:
            query.record_queries(api, self.params, state)
            query.record_health(api, self.params, state)
        finally:
            api.close("query")
        items = {entry["file"]: entry for entry in load_measurements(self.directory)}
        batches = pd.read_parquet(items["insert_batches.parquet"]["path"])
        self.assertEqual(batches.batch_size.tolist(), [2, 2, 1])
        self.assertEqual(batches.records_after.tolist(), [2, 4, 5])
        self.assertTrue(batches.succeeded.all())
        for name in query.LOOKUPS:
            frame = pd.read_parquet(items[f"{name}.parquet"]["path"])
            self.assertEqual(len(frame), 11)
            self.assertTrue(
                (
                    frame.value == (3 if name.endswith("batch_with_features") else 1)
                ).all()
            )
            unsupported = pd.read_parquet(items[f"explain_{name}.parquet"]["path"])
            self.assertFalse(unsupported.supported.any())
            self.assertTrue(unsupported.latency_ms.isna().all())
        self.assertEqual(
            pd.read_parquet(items["health.parquet"]["path"]).value.iloc[0], 5
        )
        self.assertFalse(
            items["health.parquet"]["features"]["server_metrics_available"]
        )
        self.assertEqual(len([name for name in items if name.startswith("table_")]), 7)
        all_data = (self.directory / "measurements.json").read_text()
        for entry in items.values():
            all_data += pd.read_parquet(entry["path"]).to_json()
        self.assertNotIn("test-token-never-uploaded", all_data)

    def test_resume_preserves_existing_and_continues_random_sequence(self):
        state = self.build()
        old_pages = copy.deepcopy(self.server.pages)
        params = resolve_parameters(
            {
                **self.params,
                "n_rows": 8,
                "existing_rows": 5,
                "notion_dataset_page_id": state["dataset_page_id"],
            }
        )
        api = self.client(params)
        try:
            dataset, sources = setup.ensure_dataset(api, params)
            catalogs = setup.ensure_catalogs(api, params, sources)
            self.assertEqual(dataset, state["dataset_page_id"])
            setup.populate(
                api, params, sources["interventions"]["data_source_id"], catalogs, state
            )
            self.assertEqual(
                len(
                    list(inventory(api, sources["interventions"]["data_source_id"], 8))
                ),
                8,
            )
        finally:
            api.close("setup")
        for page_id, page in old_pages.items():
            self.assertEqual(self.server.pages[page_id], page)
        batches = pd.read_parquet(self.directory / "insert_batches.parquet")
        self.assertEqual(batches.records_before.tolist(), [5, 7])
        self.assertEqual(batches.records_after.tolist(), [7, 8])
        rng = random.Random(0)
        rows = [treatment(i, rng, params) for i in range(8)]
        last = next(
            page
            for page in self.server.pages.values()
            if "benchmark_uid" in page["properties"]
            and setup.plain(page["properties"]["benchmark_uid"]) == uid(7)
        )
        self.assertEqual(setup.plain(last["properties"]["dose"]), rows[-1]["dose"])

    def test_resume_wrong_count_refuses_inserts(self):
        state = self.build()
        api = self.client()
        before = len(self.server.pages)
        with self.assertRaisesRegex(ValueError, "outside|Expected"):
            setup.populate(
                api,
                {**self.params, "existing_rows": 4},
                state["sources"]["interventions"]["data_source_id"],
                state["catalogs"],
                state,
            )
        self.assertEqual(len(self.server.pages), before)
        api.close("setup")

    def test_partial_batch_is_saved_without_ambiguous_write_retry(self):
        api = self.client()
        dataset, sources = setup.ensure_dataset(api, self.params)
        catalogs = setup.ensure_catalogs(api, self.params, sources)
        source = sources["interventions"]["data_source_id"]
        attempts = []

        def fault(request, path, body):
            if path == "pages" and body["parent"].get("data_source_id") == source:
                attempts.append(body["properties"]["benchmark_index"]["number"])
                if attempts[-1] == 1:
                    return httpx.Response(503, json={"code": "service_unavailable"})
            return None

        self.server.fault = fault
        with self.assertRaises(NotionError):
            setup.populate(
                api, self.params, source, catalogs, {"dataset_page_id": dataset}
            )
        api.close("setup")
        self.assertEqual(attempts, [0, 1])
        batches = pd.read_parquet(self.directory / "insert_batches.parquet")
        self.assertEqual(batches.value.tolist(), [1])
        self.assertFalse(batches.succeeded.iloc[0])
        self.assertEqual(batches.records_after.iloc[0], 1)

    def test_retry_after_and_http_metrics(self):
        calls = []

        def handler(request):
            calls.append(request)
            self.server.clock.value += 0.01
            if len(calls) == 1:
                return httpx.Response(
                    429, headers={"retry-after": "120"}, json={"code": "rate_limited"}
                )
            return httpx.Response(200, json={"id": "ok"})

        api = NotionClient(
            self.params,
            transport=httpx.MockTransport(handler),
            clock=self.server.clock.now,
            sleep=self.server.clock.sleep,
        )
        result = api.request("POST", "pages", body={})
        self.assertEqual(result["id"], "ok")
        self.assertGreaterEqual(api.totals["rate_limit_wait_ms"], 120000)
        self.assertEqual(api.totals["retry_count"], 1)
        self.assertEqual(api.totals["api_request_count"], 2)
        self.assertAlmostEqual(api.totals["api_request_ms"], 20)
        api.close("setup")

    def test_blocked_connection_not_retried(self):
        api = NotionClient(
            self.params,
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    429,
                    json={
                        "code": "rate_limited",
                        "additional_data": {
                            "rate_limit_reason": "public_api_request_blocked"
                        },
                    },
                )
            ),
            clock=self.server.clock.now,
            sleep=self.server.clock.sleep,
        )
        with self.assertRaises(NotionError):
            api.request("GET", "pages/test", read_only=True)
        self.assertEqual(len(api.rows), 1)
        api.close("setup")

    def test_query_cap_is_not_silently_accepted(self):
        api = NotionClient(
            self.params,
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    json={
                        "results": [],
                        "has_more": False,
                        "request_status": {"type": "incomplete"},
                    },
                )
            ),
            clock=self.server.clock.now,
            sleep=self.server.clock.sleep,
        )
        with self.assertRaisesRegex(NotionError, "capped"):
            list(api.query("source"))
        api.close("setup")

    def test_same_synthetic_values_as_experiment_2(self):
        tree = ast.parse((ROOT / "experiments/experiment_002/setup.py").read_text())
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_row"
        )
        namespace = {
            "random": random,
            "date": date,
            "timedelta": timedelta,
            "PROTOCOLS": [f"protocol_{letter}" for letter in "ABCD"],
            "NAME_FIELD": "__lamindb_record_name__",
        }
        # Execute only the repository's pure generator AST, avoiding Lamin's import-time connection.
        exec(  # noqa: S102
            compile(
                ast.Module(body=[function], type_ignores=[]),
                "<experiment2-row>",
                "exec",
            ),
            namespace,
        )
        pools = {
            "suppliers": [SimpleNamespace(name=f"supplier_{i:02d}") for i in range(10)],
            "genes": [SimpleNamespace(name=f"gene_{i:03d}") for i in range(100)],
            "molecules": [SimpleNamespace(name=f"mol_{i:02d}") for i in range(20)],
        }
        left, right = random.Random(0), random.Random(0)
        for i in range(20):
            original = namespace["_row"](pools, uid(i), left, self.params)
            notion = treatment(i, right, self.params)
            for name in (
                "dose",
                "protocol",
                "treatment_time_min",
                "supplier",
                "genes",
                "small_molecules",
            ):
                self.assertEqual(original[name], notion[name])
            self.assertEqual(
                original["treatment_date"].isoformat(), notion["treatment_date"]
            )

    def test_no_lamin_imports_in_population_or_query(self):
        code = [
            ROOT / "experiments/experiment_005" / name
            for name in (
                "setup.py",
                "query.py",
                "client.py",
                "parameters.py",
                "workload.py",
            )
        ]
        for path in code:
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertFalse(
                        any(alias.name.startswith("lamindb") for alias in node.names)
                    )
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or "").startswith("lamindb"))

    def test_parameters_reject_secrets_and_invalid_ranges(self):
        self.assertEqual(self.params["n_repetitions"], 11)
        for changes in (
            {"notion_api_key": "secret"},
            {"existing_rows": 6},
            {"n_batch": 101},
            {"n_genes": 100, "genes_per_record_max": 26},
        ):
            with self.assertRaises(ValueError):
                resolve_parameters({**self.params, **changes})

    def test_analysis_summaries_and_all_four_figures(self):
        state = self.build()
        api = self.client()
        query.record_queries(api, self.params, state)
        query.record_health(api, self.params, state)
        api.close("query")
        samples = pd.concat(
            [
                pd.read_parquet(self.directory / f"{name}.parquet").assign(
                    n_rows=5, query_name=name, run_uid="offline-run"
                )
                for name in query.LOOKUPS
            ],
            ignore_index=True,
        )
        summary = warm_summary(samples)
        self.assertEqual(len(summary), 3)
        snapshot = pd.DataFrame(
            [
                {
                    "n_rows": 5,
                    "n_records": 5,
                    "n_inserted_records": 5,
                    "insert_seconds": 1,
                    "insert_records_per_sec": 5,
                    "n_relation_links": 35,
                }
            ]
        )
        for kind in (
            "latency_median_p95",
            "latency_boxplot",
            "setup_summary",
            "dashboard",
        ):
            plot = figure(kind, samples, snapshot)
            plot.savefig(self.directory / f"{kind}.svg")
            self.assertGreater((self.directory / f"{kind}.svg").stat().st_size, 1000)

    def run_generic_runner(self, *, fail_insert=False):
        """Exercise the real runner and uploader; replace only external services."""
        fake_ln = MagicMock()
        run = SimpleNamespace(
            uid="offline-notion-run",
            name="test",
            reference="https://example.test/revision",
            extra_data={},
            save=MagicMock(),
        )
        fake_ln.context.run = run
        uploaded = []

        def artifact_factory(path, **kwargs):
            # Reading the parquet also checks that the uploader receives valid files.
            pd.read_parquet(path)
            artifact = SimpleNamespace(uid=f"artifact{len(uploaded)}", **kwargs)
            artifact.save = lambda: artifact
            uploaded.append(artifact)
            return artifact

        fake_ln.Artifact.from_dataframe.side_effect = artifact_factory
        spec = importlib.util.spec_from_file_location(
            "benchmarks._notion_runner_test", ROOT / "benchmarks/run_benchmark.py"
        )
        runner = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"lamindb": fake_ln}):
            spec.loader.exec_module(runner)
        config = self.directory / "config.yml"
        config.write_text(json.dumps(self.params))
        stages = []

        def execute(args, *, env, **kwargs):
            stage = Path(args[1]).name
            stages.append(stage)
            with patch.dict(os.environ, env):
                try:
                    with (
                        patch.object(setup, "NotionClient", side_effect=self.client),
                        patch.object(query, "NotionClient", side_effect=self.client),
                    ):
                        (setup.main if stage == "setup.py" else query.main)()
                except NotionError:
                    return SimpleNamespace(returncode=1)
            return SimpleNamespace(returncode=0)

        if fail_insert:

            def fault(request, path, body):
                if path == "pages" and "benchmark_uid" in body.get("properties", {}):
                    return httpx.Response(503, json={"code": "service_unavailable"})
                return None

            self.server.fault = fault
        provenance = {
            "dirty": False,
            "published": True,
            "editable_dependencies_clean": True,
        }
        with (
            patch.object(runner, "benchmark_provenance", return_value=provenance),
            patch.object(runner, "attach_git_provenance"),
            patch.object(runner, "track_experiment"),
            patch.object(runner, "sync_experiment"),
            patch.object(
                runner, "record_run", return_value=SimpleNamespace(uid="sheet-row")
            ) as record_run,
            patch.object(
                runner, "_ensure_feature", side_effect=lambda name, dtype: name
            ),
            patch.object(runner.subprocess, "run", side_effect=execute),
            self.assertRaises(SystemExit) as result,
        ):
            runner.main([str(config)])
        self.assertEqual(result.exception.code, 1 if fail_insert else 0)
        self.assertEqual(
            stages, ["setup.py"] if fail_insert else ["setup.py", "query.py"]
        )
        self.assertEqual(
            record_run.call_args.kwargs["exit_code"], 1 if fail_insert else 0
        )
        fake_ln.finish.assert_called_once()
        fake_ln.connect.assert_called_once_with("laminlabs/lamindb-benchmarks")
        self.assertTrue(uploaded)
        for artifact in uploaded:
            self.assertTrue(
                artifact.key.startswith(
                    "benchmarks/measurements/experiment_005/offline-notion-run/"
                )
            )
            self.assertIs(artifact.run, run)
            self.assertNotIn("test-token-never-uploaded", str(artifact.features))
        return uploaded

    def test_generic_runner_uploads_notion_outputs_to_benchmark_instance(self):
        uploaded = self.run_generic_runner()
        self.assertEqual(len(uploaded), 19)

    def test_generic_runner_uploads_partial_outputs_and_skips_queries_on_failure(self):
        uploaded = self.run_generic_runner(fail_insert=True)
        self.assertIn("insert_batches.parquet", {Path(a.key).name for a in uploaded})
        self.assertNotIn("uid_lookup.parquet", {Path(a.key).name for a in uploaded})

    def test_modal_entrypoint_does_not_require_yaml_and_only_mounts_explicit_secret(
        self,
    ):
        from benchmarks import provenance

        tree = ast.parse((ROOT / "modal_benchmarks.py").read_text())
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        function.decorator_list = []
        run = MagicMock()
        namespace = {
            "ROOT": ROOT,
            "Path": Path,
            "DEFAULT_REGION": "eu-west",
            "run": run,
            "modal": SimpleNamespace(
                Secret=SimpleNamespace(from_name=lambda name: name)
            ),
        }
        exec(  # noqa: S102
            compile(
                ast.Module(body=[function], type_ignores=[]), "<modal-main>", "exec"
            ),
            namespace,
        )
        with (
            patch.dict(sys.modules, {"yaml": None}),
            patch.object(provenance, "repository_provenance", return_value={}),
        ):
            namespace["main"](
                config="experiments/experiment_005/config/n_rows_50.yml",
                notion_secret="notion-benchmark",
            )
            self.assertEqual(
                run.with_options.call_args.kwargs["secrets"],
                ["lamin-benchmarks", "notion-benchmark"],
            )
            run.with_options.return_value.remote.assert_called_once()
            run.reset_mock()
            namespace["main"](config="experiments/experiment_002/config/n_rows_100.yml")
            run.with_options.assert_not_called()
            run.remote.assert_called_once()


if __name__ == "__main__":
    unittest.main()
