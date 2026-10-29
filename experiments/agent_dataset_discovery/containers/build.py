"""Build isolated discovery images, then run smoke checks without credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
from pathlib import Path

DIRECTORY = Path(__file__).resolve().parent


def build_plan(args: argparse.Namespace) -> tuple[list[list[str]], list[str]]:
    """Compose independent backend and harness layers over a shared base."""
    commands = []
    prefix = args.tag_prefix
    base = f"{prefix}:base"

    def build(file: str, tag: str, **build_args: str) -> None:
        command = [
            "docker",
            "build",
            "--platform",
            args.platform,
            "--file",
            str(DIRECTORY / file),
            "--tag",
            tag,
        ]
        for key, value in build_args.items():
            command.extend(["--build-arg", f"{key}={value}"])
        command.append(str(DIRECTORY))
        commands.append(command)

    build("Dockerfile.base", base, PYTHON_IMAGE=args.python_image)
    backends = ("s3", "lamin") if args.backend == "all" else (args.backend,)
    images = []
    for backend in backends:
        backend_image = f"{prefix}:{backend}"
        build("Dockerfile.backend", backend_image, BASE_IMAGE=base, BACKEND=backend)
        image = f"{prefix}:{args.harness}-{backend}"
        build(
            f"Dockerfile.{args.harness}",
            image,
            BASE_IMAGE=backend_image,
            NODE_IMAGE=args.node_image,
            CODEX_VERSION=args.codex_version,
        )
        images.append(image)
    return commands, images


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("s3", "lamin", "all"), default="all")
    parser.add_argument("--harness", choices=("codex",), default="codex")
    parser.add_argument("--codex-version", default="0.165.0")
    parser.add_argument("--tag-prefix", default="agent-dataset-discovery")
    parser.add_argument("--platform", choices=("linux/amd64",), default="linux/amd64")
    parser.add_argument("--python-image", default="python:3.13-slim-bookworm")
    parser.add_argument("--node-image", default="node:22-bookworm-slim")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.codex_version or args.codex_version == "latest":
        parser.error("Use an explicit Codex release version")
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    commands, images = build_plan(args)
    for command in commands:
        print(shlex.join(command), flush=True)
        if not args.dry_run:
            subprocess.run(command, check=True)
    for image in images:
        command = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--platform",
            args.platform,
            "--interactive",
            image,
            "python",
            "-",
        ]
        print(f"{shlex.join(command)} < {DIRECTORY / 'smoke.py'}", flush=True)
        if not args.dry_run:
            subprocess.run(
                command, input=(DIRECTORY / "smoke.py").read_bytes(), check=True
            )
    if args.dry_run:
        return
    # Keep exact local image identities for later benchmark provenance.
    image_info = json.loads(
        subprocess.check_output(["docker", "image", "inspect", *images])
    )
    report = {
        "build_options": vars(args),
        "images": [
            {
                "id": image["Id"],
                "tags": image["RepoTags"],
                "architecture": image["Architecture"],
            }
            for image in image_info
        ],
        "requirements_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((DIRECTORY / "requirements").glob("*.txt"))
        },
    }
    output = DIRECTORY / ".build" / "images.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Image identities saved to {output}")


if __name__ == "__main__":
    main()
