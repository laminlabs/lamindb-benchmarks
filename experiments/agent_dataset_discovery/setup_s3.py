"""Upload a validated local discovery corpus to an empty S3 prefix."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.agent_dataset_discovery.setup_lamin import load_corpus, validate_prefix


def destination(uri: str) -> tuple[str, str]:
    parsed = urlsplit(uri)
    if (
        parsed.scheme != "s3"
        or not parsed.netloc
        or parsed.username
        or ":" in parsed.netloc
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in uri)
    ):
        raise ValueError("Destination must be s3://bucket/corpus-prefix/")
    prefix = validate_prefix(parsed.path.removeprefix("/").removesuffix("/"))
    return parsed.netloc, prefix + "/"


def upload_plan(folder: Path, prefix: str) -> list[tuple[Path, str]]:
    root = folder.resolve(strict=True)
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(
                f"Only regular files and directories are supported: {path}"
            )
        if path.is_file():
            files.append((path, prefix + path.relative_to(root).as_posix()))
    # Reuse the Lamin importer's metadata, payload-path, and index-parity checks.
    load_corpus(root)
    return files


def upload_corpus(
    client, bucket: str, prefix: str, files: list[tuple[Path, str]]
) -> None:
    # Checking the whole prefix prevents stale files (especially an old index).
    existing = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    if existing.get("Contents"):
        raise ValueError(
            f"Destination s3://{bucket}/{prefix} is not empty; use a fresh prefix"
        )
    for path, key in files:
        client.upload_file(str(path), bucket, key)
        print(f"Uploaded s3://{bucket}/{key}", file=sys.stderr)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="Generated local corpus folder")
    parser.add_argument(
        "s3_uri", help="Existing bucket and fresh prefix: s3://bucket/prefix/"
    )
    parser.add_argument(
        "--profile",
        help="AWS writer profile (otherwise use the default credential chain)",
    )
    parser.add_argument("--region", help="AWS region (otherwise use AWS configuration)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print paths without contacting AWS",
    )
    args = parser.parse_args(argv)
    bucket, prefix = destination(args.s3_uri)
    files = upload_plan(args.folder, prefix)
    if args.dry_run:
        for path, key in files:
            print(f"{path} -> s3://{bucket}/{key}")
        print(
            f"Validated {len(files)} files. AWS was not contacted; destination emptiness was not checked."
        )
        return

    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    upload_corpus(session.client("s3"), bucket, prefix, files)
    print(f"Uploaded {len(files)} files to s3://{bucket}/{prefix}")


if __name__ == "__main__":
    main()
