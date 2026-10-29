# Run dataset discovery locally

The existing `benchmarks/run_benchmark.py` tracks the config and execution in
`laminlabs/lamindb-benchmarks`. The discovery child resolves the named prompt there,
runs one fresh Codex chat in Docker, and publishes an observation folder. The
child and chat do not create their own Lamin runs. The parent alone uploads results.

## Prepare

1. Start Docker Desktop (`open -a Docker` on macOS).
2. Build both images: `python experiments/agent_dataset_discovery/containers/build.py`.
3. Copy `.env.example` in this folder to `.env` and fill in the credentials locally.
4. Choose `run-lamin.yml` or `run-s3.yml`; set `model` to a model your Codex account can
   access and choose its supported `effort`. The example model is not silently
   replaced if unavailable. Keep these settings identical across paired runs.
5. Populate the load-test instance and the S3 prefix with equivalent corpora before
   making a real comparison. The runner does not populate them.

The prompt `Human scRNA-seq datasets` contains `Find human scRNA-seq datasets.`
and is saved at:
https://lamin.ai/laminlabs/lamindb-benchmarks/record/oRe23IXrq2aLa2qL

`example.yml` remains the **corpus generator** config. `run-lamin.yml` and
`run-s3.yml` are the **chat experiment** configs. Their fields are validated before
launch. `image` optionally selects another built image; the default is
`agent-dataset-discovery:codex-<backend>`. The actual image ID is recorded and used
to launch the container. `with_index` labels an S3 condition; it does not create,
hide, or verify an index. Point the environment variable at the appropriate corpus.

## Credentials and locations

| Host variable | Required for | Purpose |
| --- | --- | --- |
| `LAMIN_CURRENT_INSTANCE` | Optional tracking override | Defaults to `laminlabs/lamindb-benchmarks` in the parent runner |
| `LAMIN_API_KEY` | Host authentication | Identity with benchmark write access and prompt read access; existing host Lamin login can also be used |
| `DISCOVERY_CODEX_AUTH_FILE` | Optional, `codex_auth: chatgpt` | Existing ChatGPT login file; defaults to `$CODEX_HOME/auth.json` or `~/.codex/auth.json` |
| `DISCOVERY_CODEX_API_KEY` | Only `codex_auth: api_key` | OpenAI API key; forwarded as `CODEX_API_KEY` |
| `DISCOVERY_LAMIN_INSTANCE` | Lamin | Set to `laminlabs/lamindb-load-test`; instance contains only the corpus |
| `DISCOVERY_LAMIN_API_KEY` | Lamin | Dedicated identity with read access to that instance and its data storage |
| `DISCOVERY_S3_URI` | S3 | `s3://bucket/corpus-prefix/`; requires a nonempty prefix |
| `DISCOVERY_AWS_ACCESS_KEY_ID` | S3 | Access key for the restricted corpus reader |
| `DISCOVERY_AWS_SECRET_ACCESS_KEY` | S3 | Matching secret key |
| `DISCOVERY_AWS_SESSION_TOKEN` | Temporary AWS credentials | STS session token; omit for a long-lived IAM-user key |
| `DISCOVERY_AWS_REGION` | S3 | Bucket region; forwarded to both AWS region environment variables |

Only the selected backend's reader credentials are forwarded: `DISCOVERY_AWS_*`
and the S3 URI go exclusively to S3 containers; the Lamin reader key and instance
go exclusively to Lamin containers. Both use the selected Codex authentication mode. Neither the host's
ordinary `AWS_*` credentials nor its benchmark `LAMIN_API_KEY` reaches the chat.
Both sets may safely coexist in the launching environment.

For a **tracked Lamin run**, the complete environment is:

```sh
LAMIN_API_KEY='<benchmark writer API key>'
DISCOVERY_LAMIN_INSTANCE=laminlabs/lamindb-load-test
DISCOVERY_LAMIN_API_KEY='<load-test reader API key>'
```

Save these in `.env` and export them using the commands below. Your existing Codex
ChatGPT login supplies model authentication. No OpenAI API key, S3 URI, or AWS
key is needed for this case. The Lamin reader identity must have instance read
access and access to any registered storage needed for metadata files. This
runner relies on the instance's Lamin-managed storage access; it does not inject
separate AWS keys into Lamin. A storage setup requiring those keys would need
separate, explicitly scoped Lamin storage configuration before it can be used.

Create Lamin keys for the respective identities in Lamin account settings. A key
authenticates its identity; that identity's instance/storage permissions determine
what it can do. Do not assume a key's name makes it read-only.

The example configs use `codex_auth: chatgpt` (also the default). This uses your
existing ChatGPT subscription login and its Codex usage limits. No dedicated API
credits are needed. `DISCOVERY_CODEX_API_KEY` is ignored in this mode, even when
left in an old `.env` file. Authentication mode is recorded as a run parameter.

Check your host login with `codex login status`. The runner needs a file-backed
ChatGPT login at `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`). If your
login lives only in the OS keychain, create a file-backed login on the host:

```sh
codex -c 'cli_auth_credentials_store="file"' login
```

Only that file is mounted read-only at `/run/discovery-codex-auth.json`; the worker
copies it with owner-only permissions into its fresh Codex home before starting
the timer. Personal configuration, history, skills, and MCP settings are not
mounted. The container can refresh its private copy, but refreshed credentials
are discarded at cleanup and are never uploaded or written back to the host.
If a subsequent run reports an expired/reused refresh token, refresh the host
login before retrying. Avoid concurrent jobs sharing an aging login cache.
Treat `auth.json` as a password and keep it outside corpus and output folders.

For API billing instead, set `codex_auth: api_key` in the run YAML and supply
`DISCOVERY_CODEX_API_KEY`. That mode mounts no credentials file. Keep the auth
mode identical across paired comparisons. This remains an automated `codex exec`
experiment; the browser login is done interactively on the host beforehand.
See [Codex authentication and copying login caches](https://learn.chatgpt.com/docs/auth).

The `.env` file is gitignored. The runner does not automatically read it: export
it into the launching shell, or supply these variables through your secret manager.
Do not put secrets or backend locations in the experiment YAML.

```sh
set -a
source experiments/agent_dataset_discovery/.env
set +a

uv run python benchmarks/run_benchmark.py experiments/agent_dataset_discovery/run-lamin.yml
# Or:
uv run python benchmarks/run_benchmark.py experiments/agent_dataset_discovery/run-s3.yml
```

For a local child-only run that reads the prompt but does not upload or track:

```sh
uv run python experiments/agent_dataset_discovery/run.py experiments/agent_dataset_discovery/run-lamin.yml /tmp/discovery-trial
```

Use a new output directory per child-only run. It still needs benchmark prompt
read access. The parent passes the resolved tracking instance through
`BENCHMARK_TRACKING_INSTANCE`; standalone child runs default to the benchmark
instance. The dataset reader instance always comes from `DISCOVERY_LAMIN_INSTANCE`.

## Restrict S3 access to a prefix

Populate the prefix with `setup_s3.py` as described in
[the corpus README](README.md#upload-a-corpus-to-s3). Uploads use your host's AWS
writer profile or standard AWS environment, independently of the restricted
`DISCOVERY_AWS_*` reader credentials used by the experiment.

Yes: scope an IAM identity's permissions to the prefix, then supply that identity's
credentials. Two keys belonging to the same IAM user have the same permissions;
creating another key for an administrator does not produce a restricted key.
Prefer temporary credentials from a dedicated read-only role. A dedicated IAM
user with the restricted policy also works for a local prototype.

`s3-reader-policy.json` is an example identity policy. Replace `YOUR_BUCKET` and
`YOUR_CORPUS_PREFIX` (without leading/trailing slashes). It permits listing with
the corpus prefix, reading objects beneath it, and getting the bucket region.
Listing the bucket root or other prefixes is explicitly denied. It grants no
writes and no permission to enumerate all buckets.

Attach it to a **dedicated identity with no broader policies or resource-policy
grants**. The GetObject allow does not revoke read permissions granted elsewhere;
check the identity's complete effective permissions. For cross-account buckets,
the bucket policy must also permit the reader. SSE-KMS objects additionally need
appropriate decrypt permission on the encryption key. Keep the unindexed corpus
in a separate prefix without the index, and restrict each run to its own prefix.

Before a benchmark, verify that listing and reading within the prefix succeeds,
and listing the root, reading a known outside-prefix object, and writing fail.
Use the trailing slash in the URI: the policy intentionally does not allow a
shorter prefix that could also match sibling corpus names.

[AWS prefix restriction examples](https://docs.aws.amazon.com/AmazonS3/latest/userguide/amazon-s3-policy-keys.html)
explain the list condition and explicit deny. Enable S3 access logging, relevant
CloudTrail data events, and CloudWatch metrics if you need an independent access
audit. These can add logging/monitoring costs; normal S3 request and transfer costs
also apply to discovery runs. No AWS policies or keys are created by this code.

## Instructions and outputs

`instructions/common.txt` is shared across conditions. `instructions/s3.txt` and
`instructions/lamin.txt` supply minimal connection context. The runner inserts the
location from the environment and appends the unchanged prompt text from Lamin.
The complete submitted prompt is saved and its tokens are included in CLI usage.

The observation contains `prompt.txt`, `rendered_prompt.txt`, `prompt.json`,
`answer.txt`, `chat.jsonl`, `stderr.txt`, `transport.log`, `execution.json`,
`container.json`, `packages.txt`, and three Parquet tables:

- `metrics.parquet`: chat duration, container preparation duration, input/output
  tokens, cache-read/write tokens when reported, reasoning-output tokens when
  reported, total tokens, turn count, completed tool-call count, exit status, and
  CLI version.
- `turns.parquet`: per-turn usage, including the raw usage object.
- `tools.parquet`: unique completed tool events and their reported status.

For Codex usage, total tokens = input tokens + output tokens. Cached input is a
subset of input, and reasoning output is a subset of output; neither is added
again. Cache creation/write counts, if exposed by the CLI, are reported separately
and are not added to the total. Unsupported/missing fields are null, not zero.
Only `turn.completed` supplies usage; started/updated tool events are not counted
as additional calls. Raw events are retained for checking these interpretations.

The chat timer starts immediately before launching Codex inside the container and
ends when it exits. It includes CLI startup, connection setup, reasoning, tools,
and retrieval. It excludes container preparation, package checks, prompt lookup,
and uploads. Both images start with fresh local state, but provider/database
caches are not guaranteed cold. Correctness is not scored yet.

Containers have only the read-only login-file mount in ChatGPT mode, run as non-root with dropped capabilities, and
receive only the explicitly mapped variables. Codex uses container-wide access
so its Python tools can reach the chosen remote backend without interactive
approvals; isolation is provided by Docker and backend reader permissions.
Public-web searching is prohibited by the instructions, not a network firewall.

Known literal backend secrets and ChatGPT tokens are redacted from collected text before upload. Do
not deliberately print secrets: encoding or transforming them can evade literal
redaction. Only known output files are copied back, never Codex authentication
state. A crash/timeout fails the run, removes the container, and retains collected
logs under the parent's local `.pending` observation directory; failed bundles
are not published automatically.

## Inspect remaining containers

```sh
uv run python experiments/agent_dataset_discovery/list_containers.py
# Include stopped containers too:
uv run python experiments/agent_dataset_discovery/list_containers.py --all
```

Lists ID, name, image, and status for containers named `dataset-discovery-*`,
including ones left behind by an interrupted run. It only lists containers;
it never stops or removes them. Docker must be running. Existing containers from
before a credential configuration change retain their original environment.

## Modal

The local implementation uses Docker. It deliberately rejects execution inside
the existing Modal worker, which has no Docker daemon. Do not mount a host Docker
socket or run Docker-in-Docker there.

The next transport can launch a **separate Modal Sandbox** from the same published
image, send the same worker/prompt, retrieve its output files, and terminate it.
The benchmark function orchestrates it through Modal's API; the chat does not
inherit the function's filesystem or writer credentials. This is a sibling
workload, not nested containers. Modal must be able to pull the image from a
registry; a local Docker tag is not enough. Sandbox compute, image storage, and
network transfer may incur Modal charges.

See [Modal Sandboxes](https://modal.com/docs/guide/sandboxes) and
[using existing container images](https://modal.com/docs/guide/existing-images).
Modal transport is not implemented or remotely tested in this first local version.

## Checks

```sh
uv run python -m unittest discover -s experiments/agent_dataset_discovery/tests
uv run python -m experiments.agent_dataset_discovery.tests.docker_smoke
```

The second check runs the real Docker transport and observation metrics with a
fake Codex in each image. It uses no credentials and makes no model/data requests.
