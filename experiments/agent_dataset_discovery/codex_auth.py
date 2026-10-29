"""Locate a file-backed ChatGPT login without exposing credentials in output."""

from __future__ import annotations

import json
from pathlib import Path

CONTAINER_AUTH_FILE = "/run/discovery-codex-auth.json"


def chatgpt_auth_file(environ: dict) -> tuple[Path, list[str]]:
    home = Path(environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    path = (
        Path(environ.get("DISCOVERY_CODEX_AUTH_FILE") or home / "auth.json")
        .expanduser()
        .resolve()
    )
    try:
        auth = json.loads(path.read_text())
    except (OSError, ValueError):
        raise ValueError(
            "Cannot read Codex auth.json. Run codex -c 'cli_auth_credentials_store=\"file\"' login "
            "on the host, or set DISCOVERY_CODEX_AUTH_FILE to an existing ChatGPT auth.json."
        ) from None
    tokens = auth.get("tokens") if isinstance(auth, dict) else None
    if (
        not isinstance(tokens, dict)
        or not isinstance(tokens.get("access_token"), str)
        or not tokens["access_token"].strip()
        or auth.get("auth_mode") not in (None, "chatgpt")
        or auth.get("OPENAI_API_KEY")
    ):
        raise ValueError(
            "Codex auth.json must contain a ChatGPT login, not API-key credentials"
        )
    # Retain values in memory solely for literal redaction; never record them.
    secrets = [value for value in tokens.values() if isinstance(value, str) and value]
    return path, secrets
