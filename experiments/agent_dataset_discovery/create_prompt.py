"""Save a named discovery prompt in the benchmark Lamin instance."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.config import BENCHMARK_INSTANCE

EXPERIMENT = "agent_dataset_discovery"


def validate_prompt(name: str, text: str) -> None:
    if not name.strip() or len(name.strip()) > 150:
        raise ValueError("Name must contain 1–150 characters")
    if not text.strip():
        raise ValueError("Prompt text must not be empty")


def ensure_type(ln: Any, name: str, parent: Any = None) -> Any:
    record = ln.Record.filter(name=name, type=parent).one_or_none()
    if record is None:
        return ln.Record(name=name, type=parent, is_type=True).save()
    if not record.is_type:
        raise ValueError(f"{name!r} exists but is not a record type")
    return record


def create_prompt(ln: Any, name: str, text: str) -> Any:
    """Create or reuse a prompt, preserving its exact text and previous records."""
    validate_prompt(name, text)
    name = name.strip()
    parent = ensure_type(ln, "Load Benchmarks")
    experiment = ensure_type(ln, EXPERIMENT, parent)
    prompts = ensure_type(ln, "prompts", experiment)
    feature = ln.Feature.filter(name="prompt", type=None, is_type=False).one_or_none()
    if feature is None:
        feature = ln.Feature(name="prompt", dtype="str").save()
    elif feature.dtype_as_str != "str":
        raise ValueError("Existing 'prompt' feature must have dtype str")

    if prompts.schema is None:
        prompts.schema = ln.Schema([feature], minimal_set=True).save()
        prompts.save()
    elif feature not in list(prompts.schema.members.all()):
        raise ValueError("Existing prompts schema does not contain the prompt feature")

    record = ln.Record.filter(name=name, type=prompts).one_or_none()
    if record is not None:
        if record.is_type or record.features.get_values().get("prompt") != text:
            raise ValueError(
                f"{name!r} already exists with different content; use a new name"
            )
        return record
    return ln.Record(name=name, type=prompts, features={feature: text}).save()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="Short, descriptive prompt name")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", help="Exact prompt text")
    source.add_argument(
        "--text-file", type=Path, help="UTF-8 file containing the exact prompt"
    )
    parser.add_argument("--instance", default=BENCHMARK_INSTANCE)
    args = parser.parse_args(argv)
    try:
        text = (
            args.text_file.read_bytes().decode("utf-8")
            if args.text_file is not None
            else args.text
        )
        validate_prompt(args.name, text)

        import lamindb_setup

        lamindb_setup.connect(args.instance)
        import lamindb as ln

        ln.track(path=Path(__file__).resolve(), new_run=True)
        record = create_prompt(ln, args.name, text)
        ln.finish()
        print(f"https://lamin.ai/{args.instance}/record/{record.uid}")
    except (OSError, UnicodeError, ValueError, RuntimeError) as error:
        parser.exit(1, f"Could not save prompt: {error}\n")


if __name__ == "__main__":
    main()
