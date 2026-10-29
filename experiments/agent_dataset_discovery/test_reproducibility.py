"""Run setup twice and compare the resulting folder checksum manifests."""

from __future__ import annotations

import argparse
import difflib
import subprocess
import sys
import tempfile
from pathlib import Path

EXPERIMENT = Path(__file__).resolve().parent


def run_script(script: str, *arguments: Path) -> str:
    result = subprocess.run(
        [sys.executable, str(EXPERIMENT / script), *map(str, arguments)],
        capture_output=True,
        text=True,
        check=True,
    )
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.stdout.strip()


def report_differences(first: Path, second: Path) -> None:
    first_lines = first.read_text(encoding="utf-8").splitlines(keepends=True)
    second_lines = second.read_text(encoding="utf-8").splitlines(keepends=True)
    first_files = dict(line.rstrip("\n").rsplit("=", 1) for line in first_lines)
    second_files = dict(line.rstrip("\n").rsplit("=", 1) for line in second_lines)
    for path in sorted(first_files.keys() | second_files.keys()):
        if path not in first_files:
            print(f"Only in case_2: {path}")
        elif path not in second_files:
            print(f"Only in case_1: {path}")
        elif first_files[path] != second_files[path]:
            print(f"Different: {path}")
            print(f"  case_1: {first_files[path]}")
            print(f"  case_2: {second_files[path]}")
    if first_files == second_files:
        print("File checksums match; manifest ordering or formatting differs.")
        print(
            "".join(
                difflib.unified_diff(
                    first_lines, second_lines, fromfile=str(first), tofile=str(second)
                )
            ),
            end="",
        )


def run_test(base: Path, config: Path) -> bool:
    if not config.is_file():
        raise ValueError(f"Config file does not exist: {config}")
    cases = [base / "case_1", base / "case_2"]
    manifests = [base / "case_1.md5.txt", base / "case_2.md5.txt"]
    for path in [*cases, *manifests]:
        if path.exists() or path.is_symlink():
            raise ValueError(
                f"Test output already exists; use a fresh base folder: {path}"
            )
    base.mkdir(parents=True, exist_ok=True)
    print(f"Test files: {base}", file=sys.stderr)
    print(f"Config: {config}", file=sys.stderr)
    for case in cases:
        print(run_script("setup.py", case, config), file=sys.stderr)
    hashes = [
        run_script("hash_folder.py", case, manifest)
        for case, manifest in zip(cases, manifests, strict=True)
    ]
    for case, checksum in zip(cases, hashes, strict=True):
        print(f"{case.name}: {checksum}")
    if hashes[0] == hashes[1]:
        print("PASS: both folder manifests have the same MD5.")
        return True
    print("FAIL: folder manifest MD5 hashes differ.")
    report_differences(*manifests)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "base_folder",
        nargs="?",
        type=Path,
        help="Parent folder for both cases; defaults to a new temporary folder",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=EXPERIMENT / "example.yml",
        help="Setup YAML config; defaults to the experiment's example.yml",
    )
    args = parser.parse_args()
    try:
        base = (
            args.base_folder.resolve()
            if args.base_folder is not None
            else Path(tempfile.mkdtemp(prefix="agent-dataset-discovery-"))
        )
        matched = run_test(base, args.config.resolve())
    except subprocess.CalledProcessError as error:
        for message in (error.stdout, error.stderr):
            if message:
                print(message, end="", file=sys.stderr)
        parser.exit(2, f"Test execution failed: {error}\n")
    except (OSError, ValueError) as error:
        parser.exit(2, f"Test execution failed: {error}\n")
    raise SystemExit(0 if matched else 1)


if __name__ == "__main__":
    main()
