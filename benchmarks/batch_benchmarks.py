"""Wipe the load instance and run every config in a folder.

Each config is a separate run_benchmark.py process. That process records its
own run. This script does not call ln.track(). It clears
LAMIN_INITIATED_BY_RUN_UID so the configs are not children of one parent run.
The load instance is wiped before every config,
including the first. A failed benchmark does not stop the ladder; a failed
reset does.

Usage:
  python benchmarks/batch_benchmarks.py --yes
  python benchmarks/batch_benchmarks.py --yes experiments/experiment_002/config
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
RESET_SCRIPT = _HERE / "reset.py"
RUN_SCRIPT = _HERE / "run_benchmark.py"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run each config in a folder as its own benchmark."
    )
    parser.add_argument(
        "folder",
        nargs="?",
        default="configs",
        help="Folder of YAML configs. Default: ./configs.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required. Each config wipes the load instance first.",
    )
    return parser.parse_args(argv)


def config_folder(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if not path.is_dir():
        sys.exit(f"config folder not found: {path}")
    return path


def config_files(folder: Path) -> list[Path]:
    files = [
        path
        for path in folder.iterdir()
        if path.is_file() and path.suffix in {".yml", ".yaml"}
    ]
    if not files:
        sys.exit(f"no YAML configs in {folder}")
    return sorted(files, key=_sort_key)


def _sort_key(path: Path) -> list[int | str]:
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", path.name)
    ]


def _run(script: Path, args: list[str], env: dict[str, str]) -> int:
    completed = subprocess.run(
        [sys.executable, str(script), *args],
        check=False,
        env=env,
    )
    return completed.returncode


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if not args.yes:
        sys.exit(
            "refusing without --yes. Wipes the load instance before each config.\n"
            "  python benchmarks/batch_benchmarks.py --yes [folder]"
        )
    files = config_files(config_folder(args.folder))
    env = os.environ.copy()
    env.pop("LAMIN_INITIATED_BY_RUN_UID", None)

    failures: list[str] = []
    for path in files:
        print(f"reset before {path.name}")
        reset_code = _run(RESET_SCRIPT, ["--yes"], env)
        if reset_code != 0:
            sys.exit(f"reset failed before {path.name} (exit {reset_code})")
        print(f"run {path}")
        run_code = _run(RUN_SCRIPT, [str(path)], env)
        if run_code != 0:
            failures.append(f"{path.name} exit {run_code}")
            print(f"[warn] {path.name} exited {run_code}; continuing")
    if failures:
        sys.exit("batch finished with failures: " + ", ".join(failures))
    print(f"batch complete: {len(files)} configs")


if __name__ == "__main__":
    main()
