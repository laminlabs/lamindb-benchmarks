"""Run a benchmark config on Modal using the project's locked Lamin release.

Usage:
    modal run --env Benchmarks modal_benchmarks.py --config experiments/experiment_002/config/n_rows_100.yml --region eu-west

Create the Modal secret lamin-benchmarks with LAMIN_API_KEY in the Benchmarks
environment first.
This launcher does not reset the load database.
Only benchmarks/, experiments/, pyproject.toml, and uv.lock are uploaded.
Repository metadata is captured locally for each launch; .git is never uploaded.
Config files must be under benchmarks/ or experiments/.
The default compute region is eu-west; --region overrides it.

After updating project dependencies, regenerate the remote environment with:
    uv export --frozen --no-dev --no-hashes --no-emit-project --output-file requirements-modal.txt
"""

from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parent
REMOTE_ROOT = "/workspace/notion-benchmarking"
DEFAULT_REGION = "eu-west"

app = modal.App("notion-benchmarking")
image = modal.Image.debian_slim(python_version="3.13")
if modal.is_local():
    ignore = modal.FilePatternMatcher(
        *(ROOT / ".gitignore").read_text().splitlines(),
        ".git",
        "**/.git",
        "**/.env",
        "**/.env.*",
        "**/.venv",
        "**/__pycache__",
    )
    image = (
        image.pip_install_from_requirements(str(ROOT / "requirements-modal.txt"))
        .env(
            {
                "PYTHONUNBUFFERED": "1",
                "LAMIN_CURRENT_INSTANCE": "laminlabs/lamindb-benchmarks",
                "LAMIN_LOAD_INSTANCE": "laminlabs/lamindb-load-test",
            }
        )
        .add_local_dir(
            ROOT / "benchmarks",
            remote_path=f"{REMOTE_ROOT}/benchmarks",
            ignore=ignore,
        )
        .add_local_dir(
            ROOT / "experiments",
            remote_path=f"{REMOTE_ROOT}/experiments",
            ignore=ignore,
        )
        .add_local_file(ROOT / "pyproject.toml", f"{REMOTE_ROOT}/pyproject.toml")
        .add_local_file(ROOT / "uv.lock", f"{REMOTE_ROOT}/uv.lock")
    )


@app.function(
    image=image,
    region=DEFAULT_REGION,
    secrets=[modal.Secret.from_name("lamin-benchmarks")],
    cpu=1,
    memory=8192,
    timeout=24 * 60 * 60,
    retries=0,
    max_containers=1,
)
def run(config: str, git_provenance: dict):
    import json
    import os
    import subprocess
    import sys
    import tempfile

    print(
        f"Modal region: {os.environ.get('MODAL_REGION', 'unknown')}; "
        f"cloud provider: {os.environ.get('MODAL_CLOUD_PROVIDER', 'unknown')}"
    )
    root = Path(REMOTE_ROOT)
    config_path = (root / config).resolve()
    if not config_path.is_relative_to(root):
        raise ValueError("Config must be inside the repository.")
    if not config_path.is_file():
        raise FileNotFoundError(config_path)

    with tempfile.TemporaryDirectory(prefix="benchmark-provenance-") as directory:
        snapshot = Path(directory) / "git.json"
        snapshot.write_text(json.dumps(git_provenance))
        env = os.environ.copy()
        env["BENCHMARK_GIT_PROVENANCE"] = str(snapshot)
        subprocess.run(
            [sys.executable, "benchmarks/run_benchmark.py", str(config_path)],
            cwd=root,
            env=env,
            check=True,
        )


@app.local_entrypoint()
def main(
    config: str = "experiments/experiment_002/config/n_rows_100.yml",
    region: str = DEFAULT_REGION,
):
    from benchmarks.provenance import repository_provenance

    config_path = (ROOT / config).resolve()
    if not config_path.is_relative_to(ROOT):
        raise ValueError("Config must be inside the repository.")
    relative_path = config_path.relative_to(ROOT)
    if not relative_path.parts or relative_path.parts[0] not in {"benchmarks", "experiments"}:
        raise ValueError("Config must be under benchmarks/ or experiments/.")
    if not config_path.is_file():
        raise FileNotFoundError(config_path)

    function = run if region == DEFAULT_REGION else run.with_options(region=region)
    function.remote(config=relative_path.as_posix(), git_provenance=repository_provenance(ROOT))
