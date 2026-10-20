"""Run a benchmark config on Modal using the project's locked Lamin release.

Usage:
    modal run --env Benchmarks modal_benchmarks.py --config experiments/experiment_002/config/n_rows_10.yml

Create the Modal secret lamin-benchmarks with LAMIN_API_KEY in the Benchmarks
environment first.
This launcher does not reset the load database.

After updating project dependencies, regenerate the remote environment with:
    uv export --frozen --no-dev --no-hashes --no-emit-project --output-file requirements-modal.txt
"""

from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parent
REMOTE_ROOT = "/workspace/notion-benchmarking"

app = modal.App("notion-benchmarking")
image = (
    modal.Image.debian_slim(python_version="3.13")
    .apt_install("git")
    .pip_install_from_requirements(str(ROOT / "requirements-modal.txt"))
    .env(
        {
            "PYTHONUNBUFFERED": "1",
            "LAMIN_CURRENT_INSTANCE": "laminlabs/lamindb-benchmarks",
            "LAMIN_LOAD_INSTANCE": "laminlabs/lamindb-load-test",
        }
    )
    .add_local_dir(
        ROOT,
        remote_path=REMOTE_ROOT,
        ignore=modal.FilePatternMatcher(
            *(ROOT / ".gitignore").read_text().splitlines(),
            ".git/fsmonitor--daemon*",
            ".git/gk",
            ".git/hooks",
            ".git/worktrees",
        ),
    )
)


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("lamin-benchmarks")],
    cpu=1,
    memory=8192,
    timeout=24 * 60 * 60,
    retries=0,
    max_containers=1,
)
def run(config: str = "experiments/experiment_002/config/n_rows_10.yml"):
    import subprocess
    import sys

    root = Path(REMOTE_ROOT)
    config_path = (root / config).resolve()
    if not config_path.is_relative_to(root):
        raise ValueError("Config must be inside the repository.")
    if not config_path.is_file():
        raise FileNotFoundError(config_path)

    subprocess.run(
        [sys.executable, "benchmarks/run_benchmark.py", str(config_path)],
        cwd=root,
        check=True,
    )
