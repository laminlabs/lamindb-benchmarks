"""Register a discovery corpus as schema-validated Lamin artifact metadata.

Lamin is imported only after local validation and outside --dry-run mode.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


@dataclass(frozen=True)
class Dataset:
    path: Path
    features: dict[str, str]


@dataclass(frozen=True)
class Corpus:
    root: Path
    columns: tuple[str, ...]
    datasets: list[Dataset]
    supporting_files: list[Path]

    @property
    def study_ids(self) -> list[str]:
        return sorted({dataset.features["study_id"] for dataset in self.datasets})


def read_table(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if path.suffix == ".csv":
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            columns = reader.fieldnames or []
            rows = list(reader)
    else:
        import pyarrow.parquet as pq

        table = pq.read_table(path)
        columns, rows = table.column_names, table.to_pylist()
    if (
        not columns
        or any(not name for name in columns)
        or len(set(columns)) != len(columns)
    ):
        raise ValueError(f"Missing, empty, or duplicate column names: {path}")
    for number, row in enumerate(rows, start=1):
        if set(row) != set(columns) or any(
            not isinstance(value, str) for value in row.values()
        ):
            raise ValueError(
                f"Expected a string value for every column in {path}, row {number}"
            )
    return columns, rows


def table_in(folder: Path) -> Path:
    candidates = [folder / f"datasets.{suffix}" for suffix in ("csv", "parquet")]
    found = [path for path in candidates if path.is_file()]
    if len(found) != 1:
        raise ValueError(
            f"Expected exactly one datasets.csv or datasets.parquet in {folder}"
        )
    return found[0]


def load_corpus(folder: Path) -> Corpus:
    """Inspect local files only, preserving all metadata strings, including blanks."""
    root = folder.resolve(strict=True)
    studies = root / "studies"
    readme = root / "README.md"
    if not studies.is_dir() or not readme.is_file():
        raise ValueError("Corpus must contain studies/ and README.md")
    if not readme.resolve().is_relative_to(root):
        raise ValueError("README.md must be inside the corpus")
    datasets = []
    supporting = [readme]
    columns = None
    dataset_ids = set()
    payloads = set()
    for study in sorted(path for path in studies.iterdir() if path.is_dir()):
        if not study.resolve().is_relative_to(root):
            raise ValueError(f"Study directory escapes the corpus: {study}")
        manifest = table_in(study)
        if not manifest.resolve().is_relative_to(root):
            raise ValueError(f"Metadata table escapes the corpus: {manifest}")
        study_columns, rows = read_table(manifest)
        if not {"dataset_id", "file_path"}.issubset(study_columns):
            raise ValueError(
                f"dataset_id and file_path columns are required: {manifest}"
            )
        if columns is None:
            columns = study_columns
        elif set(study_columns) != set(columns):
            raise ValueError(
                f"Different metadata columns in {manifest}: "
                f"missing={sorted(set(columns) - set(study_columns))}, "
                f"extra={sorted(set(study_columns) - set(columns))}"
            )
        if not rows:
            raise ValueError(f"Study metadata table is empty: {manifest}")
        supporting.append(manifest)
        study_readme = study / "README.md"
        if study_readme.exists():
            if not study_readme.is_file() or not study_readme.resolve().is_relative_to(
                root
            ):
                raise ValueError(f"Invalid study README: {study_readme}")
            supporting.append(study_readme)
        for row in rows:
            dataset_id = row["dataset_id"]
            if not dataset_id or dataset_id in dataset_ids:
                raise ValueError(f"Empty or duplicate dataset_id: {dataset_id!r}")
            if "study_id" in row and row["study_id"] != study.name:
                raise ValueError(f"study_id disagrees with directory for {dataset_id}")
            relative = PurePosixPath(row["file_path"])
            if not row["file_path"] or relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Invalid study-relative file_path for {dataset_id}")
            path = study / relative
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(study.resolve()) or not resolved.is_file():
                raise ValueError(f"Dataset must be a file inside its study: {path}")
            if path.suffix != ".h5ad" or resolved in payloads:
                raise ValueError(
                    f"Expected a distinct .h5ad path for {dataset_id}: {path}"
                )
            dataset_ids.add(dataset_id)
            payloads.add(resolved)
            datasets.append(Dataset(path, {**row, "study_id": study.name}))
    if not datasets or columns is None:
        raise ValueError("No study datasets found")
    feature_columns = tuple(dict.fromkeys([*columns, "study_id"]))
    if (root / "index").exists():
        index = table_in(root / "index")
        if not index.resolve().is_relative_to(root):
            raise ValueError(f"Index escapes the corpus: {index}")
        index_columns, index_rows = read_table(index)
        expected = {
            dataset.features["dataset_id"]: {
                **dataset.features,
                "file_path": dataset.path.relative_to(root).as_posix(),
            }
            for dataset in datasets
        }
        if set(index_columns) != set(feature_columns) or len(index_rows) != len(
            expected
        ):
            raise ValueError("Index columns or row count differ from study metadata")
        actual = {row["dataset_id"]: row for row in index_rows}
        if actual != expected:
            raise ValueError("Index contents differ from study metadata")
    return Corpus(root, feature_columns, datasets, supporting)


def validate_prefix(prefix: str) -> str:
    path = PurePosixPath(prefix)
    if (
        not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != prefix
    ):
        raise ValueError(
            "key-prefix must be a nonempty relative path without . or .. components"
        )
    return prefix


def register_corpus(ln: Any, corpus: Corpus, prefix: str) -> dict[str, Any]:
    """Apply the plan to an already selected instance; never used by dry-run."""
    prefix = validate_prefix(prefix)
    if ln.Artifact.filter(key__startswith=f"{prefix}/").exists():
        raise ValueError(
            f"Artifacts already exist under {prefix}/; choose a fresh key-prefix"
        )

    # Preflight every name before creating features, so existing definitions are not changed.
    existing = {}
    for name in corpus.columns:
        feature = ln.Feature.filter(name=name, is_type=False).one_or_none()
        if feature is not None and (feature.dtype != "str" or feature.nullable):
            raise ValueError(
                f"Existing feature {name!r} must have dtype='str' and nullable=False; "
                "use a dedicated benchmark instance"
            )
        existing[name] = feature

    ln.track(path=Path(__file__).resolve(), new_run=True)
    features = [
        existing[name]
        if existing[name] is not None
        else ln.Feature(name=name, dtype="str", nullable=False).save()
        for name in corpus.columns
    ]
    signature = hashlib.sha256("\n".join(sorted(corpus.columns)).encode()).hexdigest()[
        :12
    ]
    metadata_schema = ln.Schema(
        features,
        name=f"agent_dataset_discovery.metadata.v1.{signature}",
        minimal_set=True,
        maximal_set=True,
        coerce=False,
    ).save()
    artifact_schema = ln.Schema(
        name=f"agent_dataset_discovery.artifact.v1.{signature}",
        slots={"__external__": metadata_schema},
        suffix=".h5ad",
    ).save()
    artifact_uids = set()
    artifacts_by_path = {}
    for dataset in corpus.datasets:
        key = f"{prefix}/{dataset.path.relative_to(corpus.root).as_posix()}"
        artifact = ln.Artifact(
            dataset.path,
            key=key,
            schema=artifact_schema,
            features=dataset.features,
            description=f"Synthetic discovery dataset {dataset.features['dataset_id']}",
            skip_hash_lookup=True,
        ).save()
        if artifact.uid in artifact_uids:
            raise RuntimeError(
                "Different datasets unexpectedly resolved to the same artifact"
            )
        artifact_uids.add(artifact.uid)
        artifacts_by_path[dataset.path] = artifact
    # Preserve interpretation rules and the exact source evidence tables, without
    # registering the redundant optional shared index as an alternate discovery table.
    for path in corpus.supporting_files:
        artifacts_by_path[path] = ln.Artifact(
            path,
            key=f"{prefix}/{path.relative_to(corpus.root).as_posix()}",
            description="Dataset discovery source metadata",
            skip_hash_lookup=True,
        ).save()
    studies = register_studies(ln, corpus, prefix, artifacts_by_path)
    ln.finish()
    return {
        "datasets": len(artifact_uids),
        "schema_uid": artifact_schema.uid,
        "studies": studies,
    }


def register_studies(
    ln: Any, corpus: Corpus, prefix: str, artifacts: dict[Path, Any]
) -> list[str]:
    """Link study records to their dataset artifacts and source descriptions."""
    study_type = ln.Record(name="Agent dataset discovery study", is_type=True).save()
    study_uids = []
    for study_id in corpus.study_ids:
        directory = corpus.root / "studies" / study_id
        readme = directory / "README.md"
        description = (
            readme.read_text(encoding="utf-8")
            if readme.is_file()
            else f"Synthetic study {study_id}. See its dataset metadata table."
        )
        name = f"{prefix}/studies/{study_id}"
        record = ln.Record.filter(name=name, is_type=False).one_or_none()
        if record is None:
            record = ln.Record(
                name=name,
                type=study_type,
                description=description,
                reference=study_id,
                reference_type="study_id",
            ).save()
        elif record.description != description or record.type != study_type:
            raise ValueError(f"Existing study record differs from source: {name}")
        members = [
            artifact
            for path, artifact in artifacts.items()
            if path.is_relative_to(directory)
        ]
        record.artifacts.add(*members)
        study_uids.append(record.uid)
    return study_uids


def add_studies_to_existing(ln: Any, corpus: Corpus, prefix: str) -> list[str]:
    """Add study descriptions to an existing import without recreating datasets."""
    prefix = validate_prefix(prefix)
    artifacts = {}
    for dataset in corpus.datasets:
        key = f"{prefix}/{dataset.path.relative_to(corpus.root).as_posix()}"
        artifact = ln.Artifact.filter(key=key, is_latest=True).one_or_none()
        if (
            artifact is None
            or artifact.features.get_values(external_only=True) != dataset.features
        ):
            raise ValueError(f"Existing dataset missing or metadata differs: {key}")
        artifacts[dataset.path] = artifact
    for study_id in corpus.study_ids:
        if not (corpus.root / "studies" / study_id / "README.md").is_file():
            raise ValueError(f"A study README is required for {study_id}")
    missing = []
    for path in corpus.supporting_files:
        key = f"{prefix}/{path.relative_to(corpus.root).as_posix()}"
        artifact = ln.Artifact.filter(key=key, is_latest=True).one_or_none()
        if artifact is None:
            if path.name != "README.md" or path.parent == corpus.root:
                raise ValueError(f"Existing source metadata artifact is missing: {key}")
            missing.append((path, key))
        else:
            if artifact.cache(is_run_input=False).read_bytes() != path.read_bytes():
                raise ValueError(f"Existing source file differs: {key}")
            artifacts[path] = artifact
    ln.track(path=Path(__file__).resolve(), new_run=True)
    for path, key in missing:
        artifacts[path] = ln.Artifact(
            path,
            key=key,
            description="Synthetic study description",
            skip_hash_lookup=True,
        ).save()
    studies = register_studies(ln, corpus, prefix, artifacts)
    ln.finish()
    return studies


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--instance", help="Existing instance slug (account/name)")
    target.add_argument(
        "--new-instance", help="Name for an instance initialized by Lamin"
    )
    parser.add_argument("--storage", help="Storage path or URI for --new-instance")
    parser.add_argument(
        "--db-env", help="Environment variable with a PostgreSQL DSN; otherwise SQLite"
    )
    parser.add_argument("--key-prefix", default="agent_dataset_discovery")
    parser.add_argument(
        "--studies-only",
        action="store_true",
        help="Add study descriptions to an existing import; preserve dataset artifacts",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate local files without importing Lamin",
    )
    args = parser.parse_args()
    try:
        prefix = validate_prefix(args.key_prefix)
        corpus = load_corpus(args.folder)
        print(
            f"Validated {len(corpus.datasets)} datasets across {len(corpus.study_ids)} studies."
        )
        print(
            "Shared external feature schema (required strings): "
            + ", ".join(corpus.columns)
        )
        if args.dry_run:
            print("Dry run complete; Lamin was not imported or contacted.")
            return
        if args.studies_only and not args.instance:
            raise ValueError("--studies-only requires --instance")
        if not args.instance and not args.new_instance:
            raise ValueError("Specify --instance or --new-instance, or use --dry-run")
        if args.new_instance and not args.storage:
            raise ValueError("--new-instance requires --storage")
        if args.instance and (args.storage or args.db_env):
            raise ValueError("--storage and --db-env apply only to --new-instance")
        db = os.environ.get(args.db_env) if args.db_env else None
        if args.db_env and not db:
            raise ValueError(f"Environment variable {args.db_env} is unset or empty")
        if (
            args.storage
            and "://" not in args.storage
            and Path(args.storage).resolve().is_relative_to(corpus.root)
        ):
            raise ValueError("Lamin storage must be outside the source corpus")

        import lamindb_setup as setup

        if args.new_instance:
            setup.init(name=args.new_instance, storage=args.storage, db=db)
        else:
            setup.connect(args.instance)
        import lamindb as ln

        if args.studies_only:
            study_uids = add_studies_to_existing(ln, corpus, prefix)
            print("Registered study records: " + ", ".join(study_uids))
            return
        result = register_corpus(ln, corpus, prefix)
        print(
            f"Registered {result['datasets']} dataset artifacts; schema UID: {result['schema_uid']}"
        )
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"Lamin setup failed: {error}\n")


if __name__ == "__main__":
    main()
