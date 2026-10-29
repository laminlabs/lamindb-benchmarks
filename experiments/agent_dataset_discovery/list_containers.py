"""List running discovery experiment containers; use --all to include stopped ones."""

from __future__ import annotations

import argparse
import json
import subprocess


def list_containers(include_stopped: bool = False) -> list[dict]:
    command = [
        "docker",
        "ps",
        "--filter",
        "name=dataset-discovery-",
        "--format",
        "{{json .}}",
    ]
    if include_stopped:
        command.append("--all")
    output = subprocess.check_output(command, text=True)
    containers = [json.loads(line) for line in output.splitlines() if line.strip()]
    # Docker's name filter matches substrings; restrict to the runner's prefix.
    return [row for row in containers if row["Names"].startswith("dataset-discovery-")]


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--all", action="store_true", help="Also include stopped containers"
    )
    args = parser.parse_args(argv)
    containers = list_containers(args.all)
    if not containers:
        print(
            "No discovery experiment containers found."
            if args.all
            else "No running discovery experiment containers found."
        )
        return
    print("ID\tNAME\tIMAGE\tSTATUS")
    for row in containers:
        print("\t".join(row[key] for key in ("ID", "Names", "Image", "Status")))


if __name__ == "__main__":
    main()
