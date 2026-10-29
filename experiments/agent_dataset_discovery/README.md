# Agent dataset discovery

Compare discovery through a Lamin catalog, S3 with a shared metadata index, and
S3 with study-level metadata only. The setup script creates a local S3-style
folder tree; it does not upload files or register them in Lamin.

## Generate a corpus

From the repository root, with dependencies installed by `uv sync`:

```sh
uv run python experiments/agent_dataset_discovery/setup.py experiments/agent_dataset_discovery/raw/pilot experiments/agent_dataset_discovery/example.yml
```

The positional arguments are `FOLDER CONFIG.yml`. All generation parameters come
from the YAML file; there are no parameter overrides on the command line.

```yaml
num_studies: 4
num_datasets_per_study: 8
with_index: true
seed: 0
table_type: csv
```

All five fields are required; unknown fields and incorrect types are rejected.

| Parameter | Meaning |
| --- | --- |
| `num_studies` | Integer, at least 2 |
| `num_datasets_per_study` | Integer, at least 4 |
| `with_index` | YAML boolean `true` or `false`; controls the shared index |
| `seed` | Integer controlling nonmatching metadata; fixed targets are unchanged |
| `table_type` | `csv` or `parquet`, applied to all study tables and the shared index |

Use separate config files for different scales, seeds, formats, and index modes.
For a paired comparison, change only `with_index`. To compare CSV with Parquet,
change only `table_type`. Keep format fixed when comparing discovery conditions.

The output folder must be new or empty. Setup writes to a temporary sibling
folder and publishes it after all files are complete. It refuses to overwrite
existing content, including a previous index. It requires no credentials or
network access.

## Source and generated files

- `setup.py`: command-line entry point.
- `example.yml`: example configuration.
- `generators/config.py`: configuration loading and validation.
- `generators/corpus.py`: study metadata and combined index generation.
- `generators/studies.py`: study descriptions derived from the dataset metadata.
- `generators/create_dummy.py`: creates the shared dummy once, refusing to overwrite it.
- `raw/dummy.h5ad`: shared, version-controlled dummy payload copied by setup.
- Other contents of `raw/`: generated corpora, excluded from Git.

```text
<output-folder>/
├── README.md
├── studies/
│   └── <study-id>/
│       ├── README.md             # study description and dataset composition
│       ├── datasets.csv          # or datasets.parquet
│       └── data/
│           └── <dataset-id>.h5ad
└── index/                        # only when with_index: true
    └── datasets.csv              # or datasets.parquet
```

The first four datasets in each of the first two studies are fixed anchors:
counts in human peripheral blood, normalized-only and unknown-counts blood
metadata, lung datasets, a mouse dataset, and a single-nucleus dataset. This
requires a minimum of 2 studies and 4 datasets per study. All additional datasets
are mouse distractors, with tissue, assay, counts status, and counts location
chosen deterministically from the seed and dataset ID. Growing either dimension
preserves existing metadata and the human target datasets. The seed has no effect
on a minimal 2-by-4 corpus because it contains only fixed anchors.

The index is the exact union of study metadata with study IDs and dataset paths
resolved relative to the output folder. Study-table paths are relative to their
study directory. Evidence references use the selected table extension and point
to the supporting synthetic statement in the study table. Both formats preserve
ordered string columns and empty strings. Answer keys and results must remain
outside the agent-visible corpus.

Each study also has a `README.md` summarizing its organisms, tissues, assays,
and counts-availability groups directly from its dataset table. These descriptions
are identical across the indexed and unindexed conditions and are carried into
Lamin. They describe synthetic fixtures; they do not invent donors, treatments,
institutions, or publications. When additional mouse datasets are generated,
the descriptions reflect the resulting composition.

## Shared dummy payload

Every generated `.h5ad` is a byte-for-byte copy of `raw/dummy.h5ad`: a valid
AnnData file containing a 3-by-4 zero matrix and an explicit placeholder label.
Setup never opens or regenerates it. Dataset-specific biology and counts
locations exist only in the metadata tables; the dummy does not implement those
claims. This benchmark therefore tests metadata discovery, not payload validity
or biological analysis.

All copies have the same content hash. Dataset identity must be based on the
metadata ID and path, not the payload hash. The Lamin setup uses
`skip_hash_lookup=True` to preserve separate artifact identities despite
identical file contents.

If the shared asset is missing, recreate it explicitly:

```sh
uv run python experiments/agent_dataset_discovery/generators/create_dummy.py
```

Retain the original asset for exact byte identity across environments; rebuilding
an HDF5 file with another writer version is not a byte-stability guarantee.

## Parquet reproducibility

Identical table contents do not generally imply identical Parquet bytes or MD5.
Writer versions, schema metadata, row/column order, row groups, compression, and
encoding choices can change the serialized file. We specify string schemas,
row/column order, Snappy compression, Parquet version 2.6, data page version 1.0,
and a row-group limit of 65,536. The repository lockfile captures dependencies.
Tests check repeated-write byte equality in the current environment; this is not
a guarantee across library versions or platforms.

When exact file hashes matter, generate once and copy the saved file. For logical
table equivalence, compare normalized records or hash a defined canonical
serialization (including schema, ordering, and null/empty-string semantics).
See [PyArrow writer options](https://arrow.apache.org/docs/python/generated/pyarrow.parquet.write_table.html).

## Folder checksum manifest

```sh
uv run python experiments/agent_dataset_discovery/hash_folder.py experiments/agent_dataset_discovery/raw/pilot /tmp/pilot-manifest.txt
```

Both arguments are required: the input folder and output text file. The script
recursively scans regular files (including hidden files), sorts their relative
paths, and writes UTF-8 lines with LF endings:

```text
README.md=<lowercase-md5>
studies/STUDY-0001/data/D0001-0001.h5ad=<lowercase-md5>
```

Paths use `/` separators with no leading slash. Directories, symbolic links,
and special files are skipped. The output manifest itself is excluded if it
is inside the input folder, including on repeated runs. Filenames containing
line breaks are rejected because the format requires one line per file.

The script prints only the lowercase MD5 of the completed manifest to stdout,
followed by a newline. Errors go to stderr with a nonzero exit code. Output
parent directories are created as needed, and an existing manifest is replaced
only after all files have been hashed successfully. Keep input files unchanged
while the script runs.

## Populate a Lamin instance

`setup_lamin.py` consumes an existing corpus folder. It reads the study-level
CSV or Parquet tables, validates their contents locally, and registers one
artifact per dataset. It also registers the root README, study READMEs, and study
tables to retain terminology and source evidence. Each study becomes a typed
`ln.Record` containing its description and linking its datasets and source files
through `record.artifacts`. The optional combined index is
checked for equivalence but is not registered, so indexed and unindexed input
folders produce equivalent catalog information.

Inspect the input without importing Lamin or connecting to any instance:

```sh
uv run python experiments/agent_dataset_discovery/setup_lamin.py /path/to/corpus --dry-run
```

When ready to write, explicitly select an existing instance:

```sh
uv run python experiments/agent_dataset_discovery/setup_lamin.py /path/to/corpus --instance account/instance-name
```

Or initialize an instance with a separate storage location:

```sh
uv run python experiments/agent_dataset_discovery/setup_lamin.py /path/to/corpus --new-instance discovery-benchmark --storage /path/to/lamin-storage
```

Instance initialization uses Lamin's normal authentication and setup behavior.
Without a database argument it uses SQLite. For PostgreSQL, add
`--db-env DISCOVERY_DATABASE_URL` and supply that environment variable through
your normal secret management; do not put the connection string in the command
or repository. Storage can be a local path or a supported URI. Database and
storage options apply only to instance initialization. Local storage must be
outside the source corpus. The write path tracks its own script run.

To add study descriptions to an existing import without recreating its datasets:

```sh
uv run python experiments/agent_dataset_discovery/setup_lamin.py /path/to/corpus --instance account/instance-name --key-prefix agent_dataset_discovery/snapshot-name --studies-only
```

This mode requires a README for every study. It checks that existing dataset
features and supporting files match the local corpus, uploads missing study
READMEs, and creates study records and links. Conflicting existing study records
are rejected. Use the original import's key prefix.

The default artifact key prefix is `agent_dataset_discovery`; override it with
`--key-prefix agent_dataset_discovery/snapshot-name`. The script refuses to
populate a prefix that already contains artifacts, preventing repeated imports
from producing duplicate dataset records. A failed live import can leave partial
database/storage writes; there is no automatic rollback or resume. Inspect any
partial import before retrying with a fresh prefix. Prefer a dedicated benchmark
instance to avoid conflicting feature definitions or unrelated discovery data.

### Artifact feature schema

All generated studies currently have the same nine columns:

```text
dataset_id, file_path, organism, source_tissue, assay,
counts_availability, counts_location, evidence, evidence_reference
```

Every column becomes a required, non-nullable string feature, preserving the
exact value, including empty strings. `study_id` is added from the study directory
name. `file_path` remains relative to its study; the artifact key contains the
resolved corpus-relative path under the chosen prefix. No biological vocabulary
enrichment or type inference is performed. Additional string columns are
included if present in every study; different column sets cause an error.

The importer creates a flat `ln.Schema` of these features with
`minimal_set=True`, `maximal_set=True`, and `coerce=False`. A second schema wraps
it in `slots={"__external__": metadata_schema}` with suffix `.h5ad`. Each dataset
is created with `schema=artifact_schema` and `features=row_values`, which uses
Lamin's external metadata validation. This validates artifact annotations, not
the dummy's expression matrix. All source fields remain strings: this schema
enforces structure and types, not biological value vocabularies.

The script targets the repository's LaminDB 2.12.0 API. Its reader and
registration wiring have offline tests with mock objects. A live CSV import of
2 studies with 4 datasets each was verified in `allavinner/playground` under
`agent_dataset_discovery/csv-2x4-seed0`: 8 distinct dataset UIDs sharing one payload
hash and exact feature parity with the source tables. A subsequent study update
added 2 study records and their README artifacts, bringing the supporting-artifact
count to 5 while preserving all 8 dataset identities.
See the [verified import run](https://lamin.ai/allavinner/playground/run/C7n5b3t04yDOU7eq),
[study update run](https://lamin.ai/allavinner/playground/run/yqIo6fNC59TaiIeU),
[Lamin schemas](https://docs.lamin.ai/lamindb.schema), and
[artifact creation](https://docs.lamin.ai/lamindb.artifact).

## Validation

Run the end-to-end repeatability check with the example config:

```sh
uv run python experiments/agent_dataset_discovery/test_reproducibility.py
```

Or supply a base folder and optionally another config:

```sh
uv run python experiments/agent_dataset_discovery/test_reproducibility.py /tmp/discovery-check --config experiments/agent_dataset_discovery/example.yml
```

The script runs `setup.py` twice into `case_1/` and `case_2/`, then runs
`hash_folder.py` on each. The manifests are saved beside the case folders as
`case_1.md5.txt` and `case_2.md5.txt`. It compares their MD5 hashes and, on a
mismatch, reports changed paths with both file hashes and paths present in only
one case. It exits with status 0 for a match, 1 for a mismatch, or 2 for an
execution error. Existing case folders or manifests are not overwritten.

Without a base folder, it creates a temporary folder and prints its location.
Generated files are retained in either mode so failures can be inspected.

Run the automated unit tests:

```sh
uv run python -m unittest discover -s experiments/agent_dataset_discovery/tests
```
