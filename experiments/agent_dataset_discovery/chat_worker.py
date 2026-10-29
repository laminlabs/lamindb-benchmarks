"""Standard-library-only worker executed inside the isolated chat container."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

AUTH_MOUNT = Path("/run/discovery-codex-auth.json")


def prepare_auth() -> None:
    # The host mount is read-only. Codex can renew tokens in its disposable home.
    target = Path(os.environ["CODEX_HOME"]) / "auth.json"
    shutil.copyfile(AUTH_MOUNT, target)
    target.chmod(0o600)


def redact_auth_tokens() -> None:
    """Include renewed tokens in redaction before any collected files leave Docker."""
    secrets = []
    for path in (AUTH_MOUNT, Path(os.environ["CODEX_HOME"]) / "auth.json"):
        if not path.is_file():
            continue
        auth = json.loads(path.read_text())
        secrets.extend(
            value
            for value in (auth.get("tokens") or {}).values()
            if isinstance(value, str) and value
        )
    if not secrets:
        return
    for name in (
        "answer.txt",
        "chat.jsonl",
        "stderr.txt",
        "execution.json",
        "packages.txt",
    ):
        path = Path("/outputs") / name
        if path.is_file():
            text = path.read_text()
            for secret in secrets:
                text = text.replace(secret, "[REDACTED]")
            path.write_text(text)


def main() -> None:
    payload = json.load(sys.stdin)
    output = Path("/outputs")
    output.mkdir(exist_ok=True)
    auth_mode = payload.get("codex_auth", "chatgpt")
    if auth_mode == "chatgpt":
        prepare_auth()
    version = subprocess.check_output(["codex", "--version"], text=True).strip()
    packages = subprocess.check_output(["python", "-m", "pip", "freeze"], text=True)
    (output / "packages.txt").write_text(packages)
    command = [
        "codex",
        "--ask-for-approval",
        "never",
        "exec",
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "danger-full-access",
        "--model",
        payload["model"],
        "-c",
        f"model_reasoning_effort={json.dumps(payload['effort'])}",
        "-c",
        'cli_auth_credentials_store="file"',
        "-c",
        f"forced_login_method={json.dumps('chatgpt' if auth_mode == 'chatgpt' else 'api')}",
        "-c",
        'shell_environment_policy.inherit="all"',
        "-c",
        "shell_environment_policy.ignore_default_excludes=true",
        "-c",
        'shell_environment_policy.exclude=["CODEX_API_KEY"]',
        "--output-last-message",
        str(output / "answer.txt"),
        "-",
    ]
    # The outer container supplies isolation, no host mounts, and reader credentials.
    # Timing includes CLI startup and all agent work, but not container/package setup.
    with (
        (output / "chat.jsonl").open("wb") as events,
        (output / "stderr.txt").open("wb") as errors,
    ):
        started_at = datetime.now(UTC).isoformat()
        started = time.perf_counter()
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=events,
            stderr=errors,
            start_new_session=True,
        )
        timed_out = False
        try:
            process.communicate(
                payload["prompt"].encode(), timeout=payload["timeout_seconds"]
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        duration = time.perf_counter() - started
    result = {
        "started_at": started_at,
        "chat_seconds": duration,
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "codex_version": version,
        "command": command,
        "codex_auth": auth_mode,
    }
    (output / "execution.json").write_text(json.dumps(result, indent=2) + "\n")
    raise SystemExit(1 if timed_out else process.returncode)


if __name__ == "__main__":
    try:
        main()
    finally:
        redact_auth_tokens()
