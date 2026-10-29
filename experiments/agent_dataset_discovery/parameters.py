"""Validate runtime parameters without connecting to any services."""

from __future__ import annotations


def resolve_parameters(params: dict) -> dict:
    allowed = {
        "experiment",
        "name",
        "chat_cli",
        "codex_auth",
        "model",
        "effort",
        "prompt",
        "backend",
        "with_index",
        "image",
        "timeout_seconds",
        "cpus",
        "memory_mb",
    }
    unknown = set(params) - allowed
    if unknown:
        raise ValueError(f"Unknown discovery parameters: {', '.join(sorted(unknown))}")
    result = {
        "timeout_seconds": 600,
        "cpus": 2,
        "memory_mb": 8192,
        "codex_auth": "chatgpt",
        **params,
    }
    for key in ("chat_cli", "model", "effort", "prompt", "backend"):
        if not isinstance(result.get(key), str) or not result[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if result["chat_cli"] != "codex":
        raise ValueError("Only chat_cli: codex is supported")
    if result["codex_auth"] not in {"chatgpt", "api_key"}:
        raise ValueError("codex_auth must be chatgpt or api_key")
    if result["backend"] not in {"s3", "lamin"}:
        raise ValueError("backend must be s3 or lamin")
    if result["effort"] not in {"minimal", "low", "medium", "high", "xhigh"}:
        raise ValueError("Unsupported effort; use minimal, low, medium, high, or xhigh")
    if result["backend"] == "s3":
        if type(result.get("with_index")) is not bool:
            raise ValueError("S3 runs require with_index: true or false")
    elif "with_index" in result:
        raise ValueError("with_index applies only to S3")
    for key in ("timeout_seconds", "cpus", "memory_mb"):
        if type(result[key]) is not int or result[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    result.setdefault("image", f"agent-dataset-discovery:codex-{result['backend']}")
    if not isinstance(result["image"], str) or not result["image"].strip():
        raise ValueError("image must be a non-empty image reference")
    return result
