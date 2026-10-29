"""Exercise the manifest CLI and its exact byte format."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "hash_folder.py"


def md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


class HashFolderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / "input"
        self.folder.mkdir()

    def run_script(self, folder: Path, output: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(folder), str(output)],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_recursive_sorted_manifest_and_stdout(self) -> None:
        (self.folder / "z.bin").write_bytes(bytes(range(256)))
        (self.folder / "nested").mkdir()
        (self.folder / "nested/é = sample.txt").write_bytes(b"abc")
        (self.folder / "empty-directory").mkdir()
        (self.folder / ".hidden").write_bytes(b"")
        output = self.root / "results" / "manifest.txt"
        result = self.run_script(self.folder, output)
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = (
            ".hidden=d41d8cd98f00b204e9800998ecf8427e\n"
            "nested/é = sample.txt=900150983cd24fb0d6963f7d28e17f72\n"
            f"z.bin={md5(bytes(range(256)))}\n"
        ).encode()
        self.assertEqual(output.read_bytes(), expected)
        self.assertEqual(result.stdout, md5(expected) + "\n")
        self.assertEqual(result.stderr, "")

    def test_internal_output_is_excluded_on_every_run(self) -> None:
        (self.folder / "file.txt").write_bytes(b"abc")
        output = self.folder / "manifest.txt"
        first = self.run_script(self.folder, output)
        second = self.run_script(self.folder, output)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(
            output.read_text(), "file.txt=900150983cd24fb0d6963f7d28e17f72\n"
        )
        self.assertEqual(
            sorted(p.name for p in self.folder.iterdir()), ["file.txt", "manifest.txt"]
        )

    def test_empty_folder_and_symlinks(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("outside")
        (self.folder / "linked-file").symlink_to(outside)
        (self.folder / "linked-directory").symlink_to(
            self.root, target_is_directory=True
        )
        output = self.root / "manifest.txt"
        result = self.run_script(self.folder, output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_bytes(), b"")
        self.assertEqual(result.stdout, "d41d8cd98f00b204e9800998ecf8427e\n")

    def test_errors_only_use_stderr_and_preserve_previous_output(self) -> None:
        output = self.root / "manifest.txt"
        output.write_bytes(b"previous manifest")
        for folder in (self.root / "missing", output):
            result = self.run_script(folder, output)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertIn("Manifest failed:", result.stderr)
        (self.folder / "bad\nname").write_bytes(b"abc")
        result = self.run_script(self.folder, output)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("line break", result.stderr)
        self.assertEqual(output.read_bytes(), b"previous manifest")


if __name__ == "__main__":
    unittest.main()
