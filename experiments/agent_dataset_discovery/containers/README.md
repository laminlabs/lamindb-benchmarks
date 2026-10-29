# Discovery chat images

Self-contained Docker build definitions for the agent's environment. The build
context is this directory, not the repository. `.dockerignore` allows only the
dependency locks into the context. No datasets, prompts, repository code, skills,
host configuration, or credentials are copied into the images.

```text
base: Python 3.13 + Scanpy / AnnData / pandas / DuckDB / PyArrow
├── s3: boto3 + AWS CLI
│   └── codex-s3: Codex CLI
└── lamin: LaminDB + Lamin setup with AWS support + bionty
    └── codex-lamin: Codex CLI
```

`Dockerfile.backend` selects a backend requirements lock. Each harness has its
own Dockerfile; currently only `Dockerfile.codex` is supported. A future harness
can reuse either backend image without changing the data-science base. The Lamin
image also includes AWS libraries required by Lamin's storage access; package
presence is not an access-control boundary.

## Build

Start Docker, then run from the repository root:

```sh
python experiments/agent_dataset_discovery/containers/build.py
```

This builds both `agent-dataset-discovery:codex-s3` and
`agent-dataset-discovery:codex-lamin`, along with their shared/intermediate layers.
It then smoke-tests both final images with networking disabled and writes their
exact image IDs and lock-file hashes to `.build/images.json` (ignored by Git).
It does not connect to Lamin, access S3, authenticate Codex, or start a chat.

Build one variant, select a Codex release, or inspect commands without Docker:

```sh
python experiments/agent_dataset_discovery/containers/build.py --backend lamin --harness codex --codex-version 0.165.0
python experiments/agent_dataset_discovery/containers/build.py --dry-run
```

`--tag-prefix` changes the image repository name. The initial target is
`linux/amd64` with Python 3.13, including on Apple Silicon via Docker's emulation.
Use the same architecture for comparison runs; emulated local timing should not
be compared with native server timing. Locks must be regenerated and tested before
adding another architecture or Python version.

Python dependencies and their transitive dependencies are pinned with hashes.
Backend locks constrain every package already present in the base to the same
version. The base deliberately uses a compatible Scanpy/AnnData/pandas stack and
dateutil below 3 to accommodate the AWS dependencies. LaminDB is pinned to the
repository's 2.12.0 release; Codex defaults to 0.165.0.

OS and Node image tags and Debian package repositories can change. For repeatable
benchmark executions, reuse built image IDs/digests and save them with results.
`--python-image` and `--node-image` accept digest-pinned image references. A fresh
build from the same source is not guaranteed to be byte-identical.

## Runtime contract

Images run as the non-root `agent` user (UID/GID 1000) in an empty `/workspace`.
`/outputs` is writable. Codex state lives in `/home/agent/.codex`, initially empty.
The default command is a shell, so the runner can invoke `codex exec` directly.
Model, effort, prompt, and dataset locations are runtime parameters, not image
build arguments. Container creation and image builds belong outside chat timing.

Create a new container for each observation. Do not reuse its home directory or
mount the repository, Docker socket, or your personal Codex/AWS configuration.
The image definition alone does not enforce network or dataset access: the runner
must inject only the chosen backend's credentials and grant their intended scope.
It should collect outputs before removing the container.

Pass credentials only at runtime, using an explicit environment allowlist or a
secret mechanism. Never use Docker build arguments for credentials. A smoke shell
for the Lamin condition can be started with:

```sh
docker run --rm -it --platform linux/amd64 \
  --env LAMIN_API_KEY \
  --env LAMIN_CURRENT_INSTANCE=laminlabs/lamindb-load-test \
  agent-dataset-discovery:codex-lamin
```

Here `LAMIN_API_KEY` must already be set in the launching shell and belong to the
load-test reader identity. Do not pass the runner's benchmark-writer identity.
For S3, pass the scoped reader's `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
optional `AWS_SESSION_TOKEN`, and region explicitly to the `codex-s3` image.
Codex authentication must also be configured at runtime. Credentials and host
authentication files are never stored in these images or the build report.

Both indexed and unindexed S3 conditions use the same S3 image; their accessible
bucket/prefix differs. The Lamin condition targets `laminlabs/lamindb-load-test`.
Image builds do not create or alter the remote corpus. The experiment runner and
observation collection will be wired separately.

## Dependency updates and checks

Edit `requirements/*.in`, then regenerate the base before the backend locks:

```sh
cd experiments/agent_dataset_discovery/containers
uv pip compile requirements/base.in --python-version 3.13 --python-platform x86_64-unknown-linux-gnu --generate-hashes --no-annotate -o requirements/base.txt
uv pip compile requirements/s3.in -c requirements/base.txt --python-version 3.13 --python-platform x86_64-unknown-linux-gnu --generate-hashes --no-annotate -o requirements/s3.txt
uv pip compile requirements/lamin.in -c requirements/base.txt --python-version 3.13 --python-platform x86_64-unknown-linux-gnu --generate-hashes --no-annotate -o requirements/lamin.txt
python -m unittest discover -s tests
```

`build.py` runs `smoke.py` through stdin inside each final image. These checks
verify the non-root user, empty workspace/Codex state, data-science imports,
package consistency, backend tools, and Codex executable. They need no credentials
and make no model requests. The offline tests check image composition, dry-run
behavior, and shared package-version parity.
