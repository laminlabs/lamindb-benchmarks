"""Check config validation, format equivalence, reproducibility, and copying."""

from __future__ import annotations

import csv
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import anndata as ad
import pyarrow.parquet as pq
import yaml

EXPERIMENT = Path(__file__).resolve().parents[1]
SETUP = EXPERIMENT / "setup.py"
DUMMY = EXPERIMENT / "raw" / "dummy.h5ad"
DEFAULT = {
    "num_studies": 2,
    "num_datasets_per_study": 4,
    "with_index": True,
    "seed": 0,
    "table_type": "csv",
}


def read_table(path: Path) -> list[dict[str, str]]:
    if path.suffix == ".parquet":
        return pq.read_table(path).to_pylist()
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()


class SetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_setup(self, output: Path, config: dict) -> subprocess.CompletedProcess:
        config_path = self.root / "config.yml"
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(SETUP), str(output), str(config_path)],
            capture_output=True,
            text=True,
            check=False,
        )

    def generate(self, name: str, **parameters) -> Path:
        output = self.root / name
        result = self.run_setup(output, {**DEFAULT, **parameters})
        self.assertEqual(result.returncode, 0, result.stderr)
        return output

    def test_index_is_exact_union_and_conditions_are_equivalent(self) -> None:
        for extension in ("csv", "parquet"):
            with self.subTest(table_type=extension):
                indexed = self.generate(f"indexed-{extension}", table_type=extension)
                unindexed = self.generate(
                    f"unindexed-{extension}", table_type=extension, with_index=False
                )
                self.assertFalse((unindexed / "index").exists())
                self.assertEqual(
                    (indexed / "README.md").read_bytes(),
                    (unindexed / "README.md").read_bytes(),
                )
                for readme in indexed.glob("studies/*/README.md"):
                    self.assertEqual(
                        readme.read_bytes(),
                        (unindexed / readme.relative_to(indexed)).read_bytes(),
                    )
                expected = []
                for manifest in sorted(
                    (indexed / "studies").glob(f"*/datasets.{extension}")
                ):
                    self.assertEqual(
                        manifest.read_bytes(),
                        (unindexed / manifest.relative_to(indexed)).read_bytes(),
                    )
                    for row in read_table(manifest):
                        relative = (manifest.parent / row["file_path"]).relative_to(
                            indexed
                        )
                        expected.append(
                            {
                                **row,
                                "study_id": manifest.parent.name,
                                "file_path": relative.as_posix(),
                            }
                        )
                        self.assertEqual(md5(indexed / relative), md5(DUMMY))
                        self.assertEqual(md5(unindexed / relative), md5(DUMMY))
                        reference, fragment = row["evidence_reference"].split("#")
                        self.assertEqual(fragment, f"dataset_id={row['dataset_id']}")
                        self.assertEqual(indexed / reference, manifest)
                self.assertEqual(
                    read_table(indexed / f"index/datasets.{extension}"), expected
                )
                self.assertEqual(len(expected), 8)
                matches = [
                    row["dataset_id"]
                    for row in expected
                    if (
                        row["organism"],
                        row["source_tissue"],
                        row["assay"],
                        row["counts_availability"],
                    )
                    == ("human", "peripheral blood", "scRNA-seq", "available")
                ]
                self.assertEqual(matches, ["D0001-0001", "D0001-0002"])

    def test_formats_preserve_metadata(self) -> None:
        csv_root = self.generate("csv", num_datasets_per_study=6)
        parquet_root = self.generate(
            "parquet", table_type="parquet", num_datasets_per_study=6
        )
        csv_rows = read_table(csv_root / "index/datasets.csv")
        parquet_rows = read_table(parquet_root / "index/datasets.parquet")
        for row in parquet_rows:
            row["evidence_reference"] = row["evidence_reference"].replace(
                ".parquet#", ".csv#"
            )
        self.assertEqual(csv_rows, parquet_rows)
        self.assertEqual(csv_rows[2]["counts_availability"], "normalized_only")
        self.assertEqual(csv_rows[3]["counts_availability"], "unknown")
        self.assertEqual(csv_rows[3]["counts_location"], "")

    def test_study_descriptions_explain_composition(self) -> None:
        output = self.generate("described")
        first = (output / "studies/STUDY-0001/README.md").read_text()
        second = (output / "studies/STUDY-0002/README.md").read_text()
        self.assertIn("STUDY-0001: human — peripheral blood", first)
        self.assertIn("| human | peripheral blood | scRNA-seq | available | 2 |", first)
        self.assertIn("| human | peripheral blood | scRNA-seq | unknown | 1 |", first)
        self.assertIn("| human | lung | scRNA-seq | normalized_only | 1 |", second)
        self.assertIn(
            "| mouse | peripheral blood | scRNA-seq | available | 1 |", second
        )
        self.assertIn(
            "| human | peripheral blood | snRNA-seq | available | 1 |", second
        )
        self.assertIn("not a real biological study", first)

    def test_seed_and_scaling_preserve_existing_rows(self) -> None:
        small = self.generate("small", num_datasets_per_study=5, seed=17)
        large = self.generate("large", num_studies=3, num_datasets_per_study=8, seed=17)
        changed = self.generate(
            "changed", num_studies=3, num_datasets_per_study=8, seed=18
        )
        small_rows = read_table(small / "index/datasets.csv")
        large_rows = read_table(large / "index/datasets.csv")
        changed_rows = read_table(changed / "index/datasets.csv")
        self.assertEqual(len(large_rows), 24)
        by_id = {row["dataset_id"]: row for row in large_rows}
        for row in small_rows:
            self.assertEqual(row, by_id.pop(row["dataset_id"]))
        self.assertTrue(all(row["organism"] == "mouse" for row in by_id.values()))
        self.assertNotEqual(large_rows, changed_rows)
        for first, second in zip(large_rows, changed_rows, strict=True):
            if (
                first["study_id"] in ("STUDY-0001", "STUDY-0002")
                and int(first["dataset_id"].split("-")[1]) <= 4
            ):
                self.assertEqual(first, second)

    def test_repeated_writes_have_same_bytes_in_current_environment(self) -> None:
        for extension in ("csv", "parquet"):
            with self.subTest(table_type=extension):
                first = self.generate(
                    f"first-{extension}",
                    table_type=extension,
                    num_datasets_per_study=6,
                    seed=42,
                )
                second = self.generate(
                    f"second-{extension}",
                    table_type=extension,
                    num_datasets_per_study=6,
                    seed=42,
                )
                first_files = {
                    p.relative_to(first): md5(p)
                    for p in first.rglob("*")
                    if p.is_file()
                }
                second_files = {
                    p.relative_to(second): md5(p)
                    for p in second.rglob("*")
                    if p.is_file()
                }
                self.assertEqual(first_files, second_files)

    def test_dummy_is_valid_and_never_rewritten(self) -> None:
        before = (md5(DUMMY), DUMMY.stat().st_mtime_ns)
        data = ad.read_h5ad(DUMMY)
        self.assertEqual(data.shape, (3, 4))
        self.assertTrue(data.uns["placeholder"])
        output = self.root / "empty"
        output.mkdir()
        result = self.run_setup(output, DEFAULT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, (md5(DUMMY), DUMMY.stat().st_mtime_ns))
        for path in output.rglob("*.h5ad"):
            self.assertEqual(path.read_bytes(), DUMMY.read_bytes())

    def test_rejects_bad_config_and_preserves_existing_output(self) -> None:
        output = self.generate("existing")
        before = (output / "index/datasets.csv").read_bytes()
        result = self.run_setup(output, {**DEFAULT, "with_index": False})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((output / "index/datasets.csv").read_bytes(), before)
        for overrides in (
            {"num_studies": 1},
            {"num_studies": True},
            {"num_datasets_per_study": 0},
            {"with_index": "false"},
            {"with_index": 1},
            {"seed": 1.2},
            {"seed": True},
            {"table_type": "xlsx"},
            {"unknown": 1},
        ):
            with self.subTest(overrides=overrides):
                result = self.run_setup(self.root / "invalid", {**DEFAULT, **overrides})
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse((self.root / "invalid").exists())
        result = self.run_setup(self.root / "invalid", {"with_index": True})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing config fields", result.stderr)
        self.assertFalse((self.root / "invalid").exists())


if __name__ == "__main__":
    unittest.main()
