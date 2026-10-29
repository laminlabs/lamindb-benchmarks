"""Local Docker transport; the worker itself is reusable in a remote sandbox."""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from experiments.agent_dataset_discovery.codex_auth import (
    CONTAINER_AUTH_FILE,
    chatgpt_auth_file,
)

WORKER = Path(__file__).with_name("chat_worker.py")
OUTPUT_FILES = (
    "answer.txt",
    "chat.jsonl",
    "stderr.txt",
    "execution.json",
    "packages.txt",
)


def failure_detail(output: Path) -> str:
    """Read only already-redacted logs, preferring the terminal turn failure."""
    path = output / "chat.jsonl"
    if path.is_file():
        events = []
        for line in path.read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
        for event in reversed(events):
            if event.get("type") == "turn.failed":
                error = event.get("error")
                if isinstance(error, dict) and isinstance(error.get("message"), str):
                    return error["message"][:1000]
            if event.get("type") == "error" and isinstance(event.get("message"), str):
                return event["message"][:1000]
    return f"See retained observation files at {output}"


def run_chat(params: dict, prompt: str, env: dict, output: Path) -> dict:
    auth_mode = params.get("codex_auth", "chatgpt")
    auth_path, auth_secrets = None, []
    if auth_mode == "chatgpt":
        auth_path, auth_secrets = chatgpt_auth_file(os.environ)
        # Never let a leftover API key override subscription authentication.
        env = {
            key: value
            for key, value in env.items()
            if key not in {"CODEX_API_KEY", "OPENAI_API_KEY"}
        }
    image = json.loads(
        subprocess.check_output(["docker", "image", "inspect", params["image"]])
    )[0]
    labels = image["Config"].get("Labels") or {}
    if (
        labels.get("org.lamin.benchmark.backend") != params["backend"]
        or labels.get("org.lamin.benchmark.harness") != "codex"
    ):
        raise ValueError("Image labels do not match the selected backend and harness")
    name = f"dataset-discovery-{uuid.uuid4().hex}"
    command = [
        "docker",
        "create",
        "--name",
        name,
        "--platform",
        "linux/amd64",
        "--user",
        "1000:1000",
        "--workdir",
        "/workspace",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "512",
        "--cpus",
        str(params["cpus"]),
        "--memory",
        f"{params['memory_mb']}m",
    ]
    for key in sorted(env):
        command.extend(["--env", key])
    if auth_path is not None:
        # Mount just the login, never the host's config, skills, or chat history.
        # Docker's --mount uses CSV; quote the source to support commas in paths.
        import csv
        import io

        spec = io.StringIO()
        csv.writer(spec, lineterminator="").writerow(
            [
                "type=bind",
                f"source={auth_path}",
                f"target={CONTAINER_AUTH_FILE}",
                "readonly",
            ]
        )
        command.extend(["--mount", spec.getvalue()])
    # Run the inspected image ID, so changing a tag cannot change this run's image.
    command.extend([image["Id"], "sleep", "infinity"])
    started = time.perf_counter()
    created = False
    try:
        subprocess.run(
            command, env={**os.environ, **env}, check=True, stdout=subprocess.DEVNULL
        )
        created = True
        subprocess.run(["docker", "start", name], check=True, stdout=subprocess.DEVNULL)
        preparation_seconds = time.perf_counter() - started
        payload = {key: params[key] for key in ("model", "effort", "timeout_seconds")}
        payload["prompt"] = prompt
        payload["codex_auth"] = auth_mode
        with (output / "transport.log").open("wb") as log:
            try:
                completed = subprocess.run(
                    [
                        "docker",
                        "exec",
                        "--interactive",
                        name,
                        "python",
                        "-c",
                        WORKER.read_text(),
                    ],
                    input=json.dumps(payload).encode(),
                    check=False,
                    stdout=log,
                    stderr=log,
                    timeout=params["timeout_seconds"] + 120,
                )
            finally:
                # Copy only known outputs; never copy the home directory/auth state.
                for filename in OUTPUT_FILES:
                    subprocess.run(
                        [
                            "docker",
                            "cp",
                            f"{name}:/outputs/{filename}",
                            str(output / filename),
                        ],
                        stdout=log,
                        stderr=log,
                        check=False,
                    )
        result = {
            "image_id": image["Id"],
            "image_reference": params["image"],
            "architecture": image["Architecture"],
            "container_preparation_seconds": preparation_seconds,
            "worker_exit_code": completed.returncode,
            "codex_auth": auth_mode,
        }
        (output / "container.json").write_text(json.dumps(result, indent=2) + "\n")
        if completed.returncode:
            redact_secrets(output, env, auth_secrets)
            raise RuntimeError(
                f"Chat failed with exit code {completed.returncode}: {failure_detail(output)}"
            )
        return result
    finally:
        if created:
            subprocess.run(
                ["docker", "rm", "--force", name],
                check=False,
                stdout=subprocess.DEVNULL,
            )
        redact_secrets(output, env, auth_secrets)


def redact_secrets(
    output: Path, env: dict, extra_secrets: list[str] | tuple[str, ...] = ()
) -> None:
    """Remove literal known secret values from text logs before publication."""
    secrets = [
        *extra_secrets,
        *[
            value
            for key, value in env.items()
            if any(word in key for word in ("KEY", "TOKEN")) and value
        ],
    ]
    for path in output.iterdir():
        if path.is_symlink():
            raise ValueError(f"Unexpected symlink in chat output: {path.name}")
        if path.is_file():
            text = path.read_text()
            for secret in secrets:
                text = text.replace(secret, "[REDACTED]")
            path.write_text(text)
