"""Offline importer tests: never import Lamin or connect to an instance."""

from __future__ import annotations

import builtins
import contextlib
import csv
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pyarrow.parquet as pq

SCRIPT = Path(__file__).resolve().parents[1] / "setup_lamin.py"
SPEC = importlib.util.spec_from_file_location("discovery_lamin_importer", SCRIPT)
importer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = importer
SPEC.loader.exec_module(importer)


def saved_object(**values):
    result = SimpleNamespace(**values)
    result.save = lambda: result
    return result


class LaminSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "README.md").write_text("Synthetic corpus\n")
        self.rows = []
        for number in (1, 2):
            study = self.root / "studies" / f"STUDY-{number}"
            (study / "data").mkdir(parents=True)
            (study / "README.md").write_text(
                f"# STUDY-{number}\n\nSynthetic study description.\n"
            )
            (study / "data" / f"D{number}.h5ad").write_bytes(b"identical dummy payload")
            row = {
                "dataset_id": f"D{number}",
                "file_path": f"data/D{number}.h5ad",
                "organism": "human",
                "source_tissue": "peripheral blood",
                "assay": "scRNA-seq",
                "counts_availability": "unknown",
                "counts_location": "",
                "evidence": "Fixture leaves counts unknown.",
                "evidence_reference": f"studies/STUDY-{number}/datasets.csv#dataset_id=D{number}",
            }
            self.rows.append(row)
            self.write_csv(study / "datasets.csv", [row])
        # Fail immediately if any offline test accidentally imports the real SDK.
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.split(".")[0] in {"lamindb", "lamindb_setup"}:
                raise AssertionError("Offline tests must not import Lamin")
            return original_import(name, *args, **kwargs)

        self.import_guard = patch("builtins.__import__", side_effect=guarded_import)
        self.import_guard.start()
        self.addCleanup(self.import_guard.stop)

    @staticmethod
    def write_csv(path, rows):
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def test_preserves_strings_and_derives_shared_schema(self) -> None:
        corpus = importer.load_corpus(self.root)
        self.assertEqual(len(corpus.datasets), 2)
        self.assertEqual(corpus.columns, (*self.rows[0], "study_id"))
        self.assertEqual(
            corpus.datasets[0].features, {**self.rows[0], "study_id": "STUDY-1"}
        )
        self.assertEqual(corpus.datasets[0].features["counts_location"], "")
        self.assertEqual(len(corpus.supporting_files), 5)

    def test_parquet_and_index_parity(self) -> None:
        before = importer.load_corpus(self.root)
        for csv_path in self.root.glob("studies/*/datasets.csv"):
            _, rows = importer.read_table(csv_path)
            pq.write_table(pa.Table.from_pylist(rows), csv_path.with_suffix(".parquet"))
            csv_path.unlink()
        after = importer.load_corpus(self.root)
        self.assertEqual(before.datasets, after.datasets)
        index_rows = [
            {
                **dataset.features,
                "file_path": dataset.path.relative_to(self.root).as_posix(),
            }
            for dataset in after.datasets
        ]
        (self.root / "index").mkdir()
        index = self.root / "index" / "datasets.parquet"
        pq.write_table(pa.Table.from_pylist(index_rows), index)
        self.assertEqual(importer.load_corpus(self.root), after)
        index_rows[0]["organism"] = "mouse"
        pq.write_table(pa.Table.from_pylist(index_rows), index)
        with self.assertRaisesRegex(ValueError, "Index contents differ"):
            importer.load_corpus(self.root)

    def test_rejects_different_columns_duplicates_and_missing_payloads(self) -> None:
        second = self.root / "studies/STUDY-2/datasets.csv"
        self.write_csv(second, [{**self.rows[1], "unexpected": "value"}])
        with self.assertRaisesRegex(ValueError, "Different metadata columns"):
            importer.load_corpus(self.root)
        self.write_csv(second, [{**self.rows[1], "dataset_id": "D1"}])
        with self.assertRaisesRegex(ValueError, "duplicate dataset_id"):
            importer.load_corpus(self.root)
        self.write_csv(second, [self.rows[1]])
        (self.root / "studies/STUDY-2/data/D2.h5ad").unlink()
        with self.assertRaises(FileNotFoundError):
            importer.load_corpus(self.root)

    def test_rejects_paths_outside_study_and_null_values(self) -> None:
        second = self.root / "studies/STUDY-2/datasets.csv"
        self.write_csv(
            second, [{**self.rows[1], "file_path": "../STUDY-1/data/D1.h5ad"}]
        )
        with self.assertRaisesRegex(ValueError, "Invalid study-relative"):
            importer.load_corpus(self.root)
        second.unlink()
        pq.write_table(
            pa.Table.from_pylist([{**self.rows[1], "counts_location": None}]),
            second.with_suffix(".parquet"),
        )
        with self.assertRaisesRegex(ValueError, "string value"):
            importer.load_corpus(self.root)

    def test_dry_run_never_imports_lamin(self) -> None:
        output = io.StringIO()
        with (
            patch.object(sys, "argv", [str(SCRIPT), str(self.root), "--dry-run"]),
            contextlib.redirect_stdout(output),
        ):
            importer.main()
        self.assertIn("Validated 2 datasets across 2 studies", output.getvalue())
        self.assertIn("Lamin was not imported or contacted", output.getvalue())

    def test_registration_contract_with_fake_backend(self) -> None:
        corpus = importer.load_corpus(self.root)
        ln = MagicMock()
        ln.Artifact.filter.return_value.exists.return_value = False
        ln.Feature.filter.return_value.one_or_none.return_value = None
        ln.Feature.side_effect = lambda **kwargs: saved_object(**kwargs)
        ln.Record.filter.return_value.one_or_none.return_value = None
        records = []

        def record_factory(**kwargs):
            record = saved_object(
                uid=f"record-{len(records)}", artifacts=MagicMock(), **kwargs
            )
            records.append(record)
            return record

        ln.Record.side_effect = record_factory
        schemas = []

        def schema_factory(*args, **kwargs):
            schema = saved_object(uid=f"schema-{len(schemas)}", **kwargs)
            schemas.append((args, schema))
            return schema

        ln.Schema.side_effect = schema_factory
        artifacts = []

        def artifact_factory(path, **kwargs):
            artifact = saved_object(
                uid=f"artifact-{len(artifacts)}", path=path, **kwargs
            )
            artifacts.append(artifact)
            return artifact

        ln.Artifact.side_effect = artifact_factory
        result = importer.register_corpus(ln, corpus, "benchmark/test")
        self.assertEqual(result["datasets"], 2)
        self.assertEqual(len(artifacts), 7)
        self.assertTrue(all(artifact.skip_hash_lookup for artifact in artifacts))
        self.assertEqual(
            [artifact.features for artifact in artifacts[:2]],
            [dataset.features for dataset in corpus.datasets],
        )
        self.assertEqual(
            [artifact.key for artifact in artifacts[:2]],
            [
                "benchmark/test/studies/STUDY-1/data/D1.h5ad",
                "benchmark/test/studies/STUDY-2/data/D2.h5ad",
            ],
        )
        self.assertEqual(schemas[1][1].slots, {"__external__": schemas[0][1]})
        self.assertEqual(schemas[1][1].suffix, ".h5ad")
        self.assertTrue(schemas[0][1].minimal_set)
        self.assertTrue(schemas[0][1].maximal_set)
        self.assertFalse(schemas[0][1].coerce)
        self.assertTrue(
            all(
                feature.dtype == "str" and not feature.nullable
                for feature in schemas[0][0][0]
            )
        )
        ln.track.assert_called_once()
        ln.finish.assert_called_once()
        self.assertEqual(len(result["studies"]), 2)
        self.assertEqual(len(records), 3)  # type plus one record per study
        for number, record in enumerate(records[1:], start=1):
            self.assertEqual(record.reference, f"STUDY-{number}")
            self.assertIn("Synthetic study description", record.description)
            members = record.artifacts.add.call_args.args
            self.assertEqual(len(members), 3)
            self.assertTrue(
                all(f"/STUDY-{number}/" in artifact.key for artifact in members)
            )

    def test_studies_only_preserves_existing_dataset_artifacts(self) -> None:
        corpus = importer.load_corpus(self.root)
        ln = MagicMock()
        existing = {}
        for dataset in corpus.datasets:
            artifact = MagicMock()
            artifact.features.get_values.return_value = dataset.features
            existing[f"benchmark/{dataset.path.relative_to(self.root).as_posix()}"] = (
                artifact
            )
        for path in corpus.supporting_files:
            if path.name == "README.md" and path.parent != self.root:
                continue
            artifact = MagicMock()
            artifact.cache.return_value = path
            existing[f"benchmark/{path.relative_to(self.root).as_posix()}"] = artifact

        def lookup(**kwargs):
            return SimpleNamespace(one_or_none=lambda: existing.get(kwargs["key"]))

        ln.Artifact.filter.side_effect = lookup
        ln.Record.filter.return_value.one_or_none.return_value = None
        result = importer.add_studies_to_existing(ln, corpus, "benchmark")
        self.assertEqual(len(result), 2)
        self.assertEqual(ln.Artifact.call_count, 2)
        for call in ln.Artifact.call_args_list:
            self.assertEqual(call.args[0].name, "README.md")
            self.assertNotEqual(call.args[0].parent, self.root)
        ln.Feature.assert_not_called()
        ln.Schema.assert_not_called()

    def test_studies_only_checks_metadata_before_writing(self) -> None:
        corpus = importer.load_corpus(self.root)
        ln = MagicMock()
        ln.Artifact.filter.return_value.one_or_none.return_value = None
        with self.assertRaisesRegex(ValueError, "Existing dataset missing"):
            importer.add_studies_to_existing(ln, corpus, "benchmark")
        ln.Artifact.assert_not_called()
        ln.Record.assert_not_called()
        ln.track.assert_not_called()

    def test_existing_prefix_and_incompatible_feature_stop_before_writes(self) -> None:
        corpus = importer.load_corpus(self.root)
        ln = MagicMock()
        ln.Artifact.filter.return_value.exists.return_value = True
        with self.assertRaisesRegex(ValueError, "Artifacts already exist"):
            importer.register_corpus(ln, corpus, "benchmark")
        ln.Artifact.assert_not_called()
        ln.track.assert_not_called()
        ln.Artifact.filter.return_value.exists.return_value = False
        ln.Feature.filter.return_value.one_or_none.return_value = SimpleNamespace(
            dtype="int", nullable=False
        )
        with self.assertRaisesRegex(ValueError, "Existing feature"):
            importer.register_corpus(ln, corpus, "benchmark")
        ln.Feature.assert_not_called()
        ln.track.assert_not_called()


if __name__ == "__main__":
    unittest.main()
