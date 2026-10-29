"""Generate a local S3-style dataset discovery corpus."""

from __future__ import annotations

import argparse
from pathlib import Path

from generators.config import Config
from generators.corpus import create_corpus


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="New or empty output folder")
    parser.add_argument("config", type=Path, help="YAML configuration file")
    args = parser.parse_args()
    try:
        config = Config.load(args.config)
        create_corpus(args.folder, config)
    except (ValueError, TypeError, OSError) as error:
        parser.exit(1, f"Setup failed: {error}\n")
    print(
        f"Created corpus at {args.folder.resolve()} "
        f"({config.num_studies} studies, "
        f"{config.num_studies * config.num_datasets_per_study} datasets, "
        f"{config.table_type}, with_index={config.with_index}, seed={config.seed})."
    )


if __name__ == "__main__":
    main()
