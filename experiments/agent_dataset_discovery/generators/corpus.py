"""Generate metadata tables and copy the shared dummy payload."""

from __future__ import annotations

import csv
import hashlib
import shutil
import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .config import Config
from .studies import study_readme

DUMMY_PATH = Path(__file__).resolve().parents[1] / "raw" / "dummy.h5ad"

FIELDS = [
    "dataset_id",
    "file_path",
    "organism",
    "source_tissue",
    "assay",
    "counts_availability",
    "counts_location",
    "evidence",
    "evidence_reference",
]

# Four anchors in each of the first two studies. Additional rows are nonmatches,
# so increasing either dimension preserves the answers and dataset identities.
ANCHORS = [
    ("human", "peripheral blood", "scRNA-seq", "available", "X"),
    ("human", "peripheral blood", "scRNA-seq", "available", "layers/counts"),
    ("human", "peripheral blood", "scRNA-seq", "normalized_only", ""),
    ("human", "peripheral blood", "scRNA-seq", "unknown", ""),
    ("human", "lung", "scRNA-seq", "available", "raw/X"),
    ("human", "lung", "scRNA-seq", "normalized_only", ""),
    ("mouse", "peripheral blood", "scRNA-seq", "available", "X"),
    ("human", "peripheral blood", "snRNA-seq", "available", "X"),
]

CORPUS_README = """# Synthetic single-cell discovery benchmark

This is a complete, closed inventory of synthetic datasets. It contains no real
study observations or biological evidence. Every .h5ad is an identical copy of
one valid placeholder with 3 synthetic cells and 4 synthetic genes. It does not
implement the dataset-specific properties or hypothetical counts locations in
the tables. Discovery tasks must use the documented metadata only. Dataset
identity is dataset_id and file_path, not a payload content hash.

## Layout and paths

Every study is under studies/<study-id>/, with datasets.{table_type} and data/*.h5ad.
Each study's README.md describes its composition using only facts in that table.
Study table file_path values are relative to that study's directory. When provided,
index/datasets.{table_type} combines all study manifests, adds study_id, and resolves
file_path relative to this corpus root. The index adds no biological facts.
evidence_reference is always relative to the corpus root; its dataset_id fragment
identifies the row containing the supporting evidence statement. Tables use
ordered string columns; an empty string denotes an unspecified counts location.

## Metadata definitions

- dataset_id: unique, stable synthetic dataset identifier.
- organism: human or mouse; one organism per dataset.
- source_tissue: peripheral blood or lung; one tissue per dataset.
- assay: scRNA-seq (single-cell RNA sequencing) or snRNA-seq (single-nucleus RNA
  sequencing). A request for scRNA-seq excludes snRNA-seq in this benchmark.
- counts_availability: available means documented unnormalized counts;
  normalized_only means only normalized expression is documented; unknown means
  metadata does not establish counts availability. Unknown is not normalized_only.
- counts_location: hypothetical X, layers/counts, or raw/X in the represented
  dataset; blank for normalized_only and unknown. These are fixture claims, not
  descriptions of the shared placeholder. The location alone never establishes eligibility.
- evidence: synthetic fixture declaration supporting counts_availability.
- evidence_reference: location of that declaration in the study manifest.

The payloads contain no dataset-specific annotations. Their contents cannot
verify or contradict fixture eligibility, and must not resolve uncertainty.

## Eligibility and absence

Apply all requested predicates together. A known failing predicate excludes a
dataset even if another field is unknown. A potentially matching dataset with
unknown counts availability is uncertain, not a confirmed match or exclusion.
Report confirmed matches and unresolved candidates separately. Definitive absence
requires every dataset in this complete inventory to be excluded by metadata.
"""


def metadata(study_number: int, dataset_number: int, config: Config) -> dict[str, str]:
    study_id = f"STUDY-{study_number:04d}"
    dataset_id = f"D{study_number:04d}-{dataset_number:04d}"
    if study_number <= 2 and dataset_number <= 4:
        profile = ANCHORS[(study_number - 1) * 4 + dataset_number - 1]
    else:
        # Per-ID hashing makes distractors independent of traversal order and scale.
        digest = hashlib.sha256(f"{config.seed}:{dataset_id}".encode()).digest()
        availability = ("available", "normalized_only", "unknown")[digest[2] % 3]
        location = ("X", "layers/counts", "raw/X")[digest[3] % 3]
        profile = (
            "mouse",
            ("lung", "peripheral blood")[digest[0] % 2],
            ("scRNA-seq", "snRNA-seq")[digest[1] % 2],
            availability,
            location if availability == "available" else "",
        )
    organism, tissue, assay, availability, location = profile
    evidence = {
        "available": f"Synthetic fixture declares unnormalized counts in {location}.",
        "normalized_only": "Synthetic fixture declares normalized expression only; no counts retained.",
        "unknown": "Synthetic fixture does not document whether unnormalized counts are retained.",
    }[availability]
    return dict(
        zip(
            FIELDS,
            (
                dataset_id,
                f"data/{dataset_id}.h5ad",
                organism,
                tissue,
                assay,
                availability,
                location,
                evidence,
                f"studies/{study_id}/datasets.{config.table_type}#dataset_id={dataset_id}",
            ),
            strict=True,
        )
    )


def write_table(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    if path.suffix == ".parquet":
        schema = pa.schema(
            [pa.field(name, pa.string(), nullable=False) for name in fields]
        )
        table = pa.Table.from_pylist(rows, schema=schema)
        pq.write_table(
            table,
            path,
            compression="snappy",
            version="2.6",
            row_group_size=65_536,
            use_dictionary=True,
            write_statistics=True,
            data_page_version="1.0",
            store_schema=True,
        )
        return
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def check_destination(folder: Path) -> None:
    if folder.is_symlink():
        raise ValueError(f"Output folder must not be a symlink: {folder}")
    if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
        raise ValueError(f"Output folder must be new or empty: {folder}")


def create_corpus(folder: Path, config: Config) -> None:
    if not DUMMY_PATH.is_file():
        raise ValueError(f"Shared dummy file is missing: {DUMMY_PATH}")
    folder = Path(folder).absolute()
    check_destination(folder)
    folder.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{folder.name}-", dir=folder.parent))
    try:
        (staging / "README.md").write_text(
            CORPUS_README.format(table_type=config.table_type), encoding="utf-8"
        )
        index_rows = []
        for study_number in range(1, config.num_studies + 1):
            study_id = f"STUDY-{study_number:04d}"
            study_path = staging / "studies" / study_id
            (study_path / "data").mkdir(parents=True)
            rows = []
            for dataset_number in range(1, config.num_datasets_per_study + 1):
                row = metadata(study_number, dataset_number, config)
                shutil.copyfile(DUMMY_PATH, study_path / row["file_path"])
                rows.append(row)
                if config.with_index:
                    index_rows.append(
                        {
                            **row,
                            "study_id": study_id,
                            "file_path": f"studies/{study_id}/{row['file_path']}",
                        }
                    )
            write_table(study_path / f"datasets.{config.table_type}", FIELDS, rows)
            (study_path / "README.md").write_text(
                study_readme(study_id, rows), encoding="utf-8"
            )
        if config.with_index:
            (staging / "index").mkdir()
            write_table(
                staging / "index" / f"datasets.{config.table_type}",
                ["study_id", *FIELDS],
                index_rows,
            )
        check_destination(folder)
        if folder.exists():
            folder.rmdir()
        staging.rename(folder)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
