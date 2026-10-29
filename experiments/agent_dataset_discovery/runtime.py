"""Backend configuration and read-only prompt resolution for discovery runs."""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlsplit

DIRECTORY = Path(__file__).resolve().parent


def runtime_environment(params: dict, environ: dict) -> tuple[str, dict]:
    """Only explicitly supplied reader/model secrets cross the container boundary."""

    def require(name):
        value = environ.get(name, "")
        if not value.strip():
            raise ValueError(f"Set {name} in the launching environment")
        return value

    env = {}
    if params.get("codex_auth", "chatgpt") == "api_key":
        env["CODEX_API_KEY"] = require("DISCOVERY_CODEX_API_KEY")
    if params["backend"] == "lamin":
        location = require("DISCOVERY_LAMIN_INSTANCE")
        if len(location.split("/")) != 2 or any(
            not part for part in location.split("/")
        ):
            raise ValueError("DISCOVERY_LAMIN_INSTANCE must be account/instance")
        env.update(
            LAMIN_CURRENT_INSTANCE=location,
            LAMIN_API_KEY=require("DISCOVERY_LAMIN_API_KEY"),
        )
    else:
        location = require("DISCOVERY_S3_URI")
        uri = urlsplit(location)
        if (
            uri.scheme != "s3"
            or not uri.netloc
            or not uri.path.strip("/")
            or uri.query
            or uri.fragment
            or uri.username
        ):
            raise ValueError("DISCOVERY_S3_URI must be s3://bucket/corpus-prefix/")
        location = location.rstrip("/") + "/"
        env["DISCOVERY_S3_URI"] = location
        for key in ("ACCESS_KEY_ID", "SECRET_ACCESS_KEY", "REGION"):
            env[f"AWS_{key}"] = require(f"DISCOVERY_AWS_{key}")
        if token := environ.get("DISCOVERY_AWS_SESSION_TOKEN"):
            env["AWS_SESSION_TOKEN"] = token
    if "AWS_REGION" in env:
        env["AWS_DEFAULT_REGION"] = env["AWS_REGION"]
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    return location, env


def read_prompt(ln, name: str) -> dict:
    parent = None
    for type_name in ("Load Benchmarks", "agent_dataset_discovery", "prompts"):
        parent = ln.Record.filter(
            name=type_name, type=parent, is_type=True
        ).one_or_none()
        if parent is None:
            raise ValueError(f"Missing prompt hierarchy type: {type_name}")
    record = ln.Record.filter(name=name, type=parent, is_type=False).one_or_none()
    if record is None:
        raise ValueError(f"No discovery prompt named {name!r}")
    text = record.features.get_values().get("prompt")
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"Prompt {name!r} has no non-empty prompt feature")
    return {
        "name": name,
        "uid": record.uid,
        "text": text,
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


def render_prompt(backend: str, location: str, text: str) -> str:
    common = (DIRECTORY / "instructions/common.txt").read_text()
    introduction = (
        (DIRECTORY / "instructions" / f"{backend}.txt")
        .read_text()
        .format(location=location)
    )
    return f"{introduction}\n{common}\nTask:\n{text}"
