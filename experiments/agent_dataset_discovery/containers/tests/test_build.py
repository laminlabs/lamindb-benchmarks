"""Build-plan checks that require neither Docker nor credentials."""

import contextlib
import importlib.util
import io
import re
import unittest
from pathlib import Path
from unittest.mock import patch

DIRECTORY = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "discovery_images", DIRECTORY / "build.py"
)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class BuildTests(unittest.TestCase):
    def test_builds_two_branches_from_one_base(self):
        commands, images = builder.build_plan(builder.parse_args([]))
        self.assertEqual(len(commands), 5)
        self.assertEqual(
            images,
            ["agent-dataset-discovery:codex-s3", "agent-dataset-discovery:codex-lamin"],
        )
        for index, backend in [(1, "s3"), (3, "lamin")]:
            self.assertIn("BASE_IMAGE=agent-dataset-discovery:base", commands[index])
            self.assertIn(f"BACKEND={backend}", commands[index])
            self.assertIn(
                f"BASE_IMAGE=agent-dataset-discovery:{backend}", commands[index + 1]
            )
        for command in commands:
            self.assertEqual(command[-1], str(DIRECTORY))

    def test_single_backend_and_explicit_harness_version(self):
        commands, images = builder.build_plan(
            builder.parse_args(
                [
                    "--backend",
                    "lamin",
                    "--codex-version",
                    "0.165.0",
                    "--tag-prefix",
                    "trial",
                ]
            )
        )
        self.assertEqual(len(commands), 3)
        self.assertEqual(images, ["trial:codex-lamin"])
        self.assertIn("CODEX_VERSION=0.165.0", commands[-1])

    def test_dry_run_does_not_launch_processes(self):
        with (
            patch.object(builder.subprocess, "run") as run,
            patch.object(builder.subprocess, "check_output") as inspect,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            builder.main(["--dry-run"])
        run.assert_not_called()
        inspect.assert_not_called()

    def test_backend_locks_preserve_every_base_package_version(self):
        def pins(name):
            text = (DIRECTORY / "requirements" / f"{name}.txt").read_text()
            return dict(
                re.findall(r"^([a-zA-Z0-9_.-]+)==([^\s;\\]+)", text, re.MULTILINE)
            )

        base = pins("base")
        self.assertTrue(base)
        for backend in ("s3", "lamin"):
            packages = pins(backend)
            for name, version in base.items():
                self.assertEqual(packages.get(name), version, f"{backend}: {name}")


if __name__ == "__main__":
    unittest.main()
