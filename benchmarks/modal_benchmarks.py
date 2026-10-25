"""Run a benchmark config on Modal using the project's locked Lamin release.

Usage:
    uv run --with modal python -m modal run --env Benchmarks benchmarks/modal_benchmarks.py --config experiments/record_scalability/example.yml --region eu-west

Create the Modal secret lamin-benchmarks with LAMIN_API_KEY in the Benchmarks
environment first.
For Notion, pass --notion-secret notion-benchmarks to mount NOTION_API_KEY and,
optionally, NOTION_PARENT_PAGE_ID or NOTION_DATASET_PAGE_ID. The secret is only
mounted when this option is supplied; other experiments do not need it.
This launcher does not reset the load database.
It launches the parent runner, which ingests run_info.yml and atomic observation
folders while the experiment child runs. Runs are labelled environment=modal.
Only benchmarks/, experiments/, pyproject.toml, and uv.lock are uploaded.
Repository metadata is captured locally for each launch; .git is never uploaded.
Config files must be under benchmarks/ or experiments/.
Local config/ and configs/ folders are excluded. The selected YAML is passed
as a function argument and written inside the container before the runner starts.
The default compute region is eu-west; --region overrides it.

After updating project dependencies, regenerate the remote environment with:
    uv export --frozen --no-dev --no-hashes --no-emit-project --output-file requirements-modal.txt
"""

import sys
from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
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
        "**/config/**",
        "**/configs/**",
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
def run(config: str, config_text: str, git_provenance: dict):
    import json
    import os
    import subprocess
    import sys
    import tempfile

    print(
        f"Modal region: {os.environ.get('MODAL_REGION', 'unknown')}; "
        f"cloud provider: {os.environ.get('MODAL_CLOUD_PROVIDER', 'unknown')}"
    )
    root = Path(REMOTE_ROOT).resolve()
    config_path = (root / config).resolve()
    if not config_path.is_relative_to(root):
        raise ValueError("Config must be inside the repository.")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(config_text)

    with tempfile.TemporaryDirectory(prefix="benchmark-provenance-") as directory:
        snapshot = Path(directory) / "git.json"
        snapshot.write_text(json.dumps(git_provenance))
        env = os.environ.copy()
        env["BENCHMARK_GIT_PROVENANCE"] = str(snapshot)
        env["BENCHMARK_EXECUTION_ENVIRONMENT"] = "modal"
        subprocess.run(
            [sys.executable, "benchmarks/run_benchmark.py", str(config_path)],
            cwd=root,
            env=env,
            check=True,
        )


@app.local_entrypoint()
def main(
    config: str = "experiments/record_scalability/example.yml",
    region: str = DEFAULT_REGION,
    notion_secret: str = "",
):
    from benchmarks.config import read_config
    from benchmarks.provenance import repository_provenance

    config_path = (ROOT / config).resolve()
    if not config_path.is_relative_to(ROOT):
        raise ValueError("Config must be inside the repository.")
    relative_path = config_path.relative_to(ROOT)
    if not relative_path.parts or relative_path.parts[0] not in {
        "benchmarks",
        "experiments",
    }:
        raise ValueError("Config must be under benchmarks/ or experiments/.")
    if not config_path.is_file():
        raise FileNotFoundError(config_path)

    # Validate the child contract before submitting a remote job. Parameter
    # resolution stays remote because some experiments need mounted secrets.
    experiment = read_config(config_path).get("experiment")
    if (
        not isinstance(experiment, str)
        or not experiment.strip()
        or Path(experiment).is_absolute()
    ):
        raise ValueError(
            "Modal configs must specify a repository-relative experiment folder"
        )
    experiment_path = (ROOT / experiment).resolve()
    if not experiment_path.is_relative_to(ROOT):
        raise ValueError("Experiment must be inside the repository")
    experiment_relative = experiment_path.relative_to(ROOT)
    if not experiment_relative.parts or experiment_relative.parts[0] not in {
        "benchmarks",
        "experiments",
    }:
        raise ValueError(
            "Modal experiments must be under benchmarks/ or experiments/ so their code is uploaded"
        )
    for filename in ("README.md", "run.py"):
        if not (experiment_path / filename).is_file():
            raise ValueError(f"Experiment is missing {filename}: {experiment_path}")

    options = {}
    if region != DEFAULT_REGION:
        options["region"] = region
    if notion_secret:
        options["secrets"] = [
            modal.Secret.from_name("lamin-benchmarks"),
            modal.Secret.from_name(notion_secret),
        ]
    function = run.with_options(**options) if options else run
    function.remote(
        config=relative_path.as_posix(),
        config_text=config_path.read_text(),
        git_provenance=repository_provenance(ROOT),
    )
