"""Run one fresh discovery chat: run.py CONFIG OUTPUT_DIRECTORY.

This child reads the prompt and writes observations; only run_benchmark.py tracks
the run and uploads artifacts to the benchmark instance.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.config import BENCHMARK_INSTANCE, read_config
from benchmarks.observations import child_arguments, observation, write_run_information
from experiments.agent_dataset_discovery.docker_chat import redact_secrets, run_chat
from experiments.agent_dataset_discovery.metrics import write_metrics
from experiments.agent_dataset_discovery.parameters import resolve_parameters
from experiments.agent_dataset_discovery.runtime import (
    read_prompt,
    render_prompt,
    runtime_environment,
)


def main(argv=None):
    args = child_arguments(argv)
    params = resolve_parameters(read_config(args.config))
    location, env = runtime_environment(params, os.environ)
    if os.environ.get("MODAL_TASK_ID"):
        raise ValueError(
            "Local Docker runtime only: Modal needs a separate Sandbox transport, not nested Docker"
        )
    # The parent sets this explicitly before redirecting LAMIN_CURRENT_INSTANCE
    # to the load server for other experiments. No child tracking is started.
    tracking_instance = os.environ.get(
        "BENCHMARK_TRACKING_INSTANCE", BENCHMARK_INSTANCE
    )
    import lamindb_setup

    lamindb_setup.connect(tracking_instance)
    import lamindb as ln

    prompt = read_prompt(ln, params["prompt"])
    rendered = render_prompt(params["backend"], location, prompt["text"])
    provenance = {
        "prompt_uid": prompt["uid"],
        "prompt_sha256": prompt["sha256"],
        "rendered_prompt_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
        "dataset_location": location,
    }
    write_run_information(args.output, provenance)
    with observation(
        args.output, "chat", {"backend": params["backend"], **provenance}
    ) as output:
        (output / "prompt.txt").write_text(prompt["text"])
        (output / "rendered_prompt.txt").write_text(rendered)
        (output / "prompt.json").write_text(json.dumps(prompt, indent=2) + "\n")
        try:
            container = run_chat(params, rendered, env, output)
        finally:
            redact_secrets(output, env)
        write_metrics(output, container)


if __name__ == "__main__":
    main()
