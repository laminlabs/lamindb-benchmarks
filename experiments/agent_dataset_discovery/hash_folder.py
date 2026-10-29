"""Write a recursive file checksum manifest and print its MD5 checksum."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import tempfile
from pathlib import Path


def file_md5(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(
            stream, lambda: hashlib.md5(usedforsecurity=False)
        ).hexdigest()


def raise_walk_error(error: OSError) -> None:
    raise error


def write_manifest(folder: Path, output: Path) -> str:
    folder = folder.resolve(strict=True)
    if not folder.is_dir():
        raise ValueError(f"Input must be a folder: {folder}")
    if output.is_symlink():
        raise ValueError(f"Output must not be a symbolic link: {output}")
    output = output.resolve()
    if output.exists() and not output.is_file():
        raise ValueError(f"Output must be a file: {output}")

    files = []
    for directory, _, filenames in os.walk(folder, onerror=raise_walk_error):
        for filename in filenames:
            path = Path(directory) / filename
            if path == output or not stat.S_ISREG(path.lstat().st_mode):
                continue
            relative = path.relative_to(folder).as_posix()
            if "\n" in relative or "\r" in relative:
                raise ValueError(f"File path contains a line break: {relative!r}")
            files.append((relative, path))
    files.sort(key=lambda item: item[0])

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=output.parent,
            prefix=f".{output.name}-",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            for relative, path in files:
                stream.write(f"{relative}={file_md5(path)}\n")
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return file_md5(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="Folder to scan recursively")
    parser.add_argument("output", type=Path, help="Destination manifest text file")
    args = parser.parse_args()
    try:
        checksum = write_manifest(args.folder, args.output)
    except (OSError, ValueError, UnicodeError) as error:
        parser.exit(1, f"Manifest failed: {error}\n")
    print(checksum)


if __name__ == "__main__":
    main()
