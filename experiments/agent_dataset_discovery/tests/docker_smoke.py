"""Exercise real Docker transport using a fake Codex; no model/data credentials.

Run explicitly after building images: python -m experiments.agent_dataset_discovery.tests.docker_smoke
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from experiments.agent_dataset_discovery import docker_chat
from experiments.agent_dataset_discovery.metrics import write_metrics
from experiments.agent_dataset_discovery.parameters import resolve_parameters

FAKE = """#!/usr/local/bin/python
import json, os, sys
from pathlib import Path
if "--version" in sys.argv:
    print("codex test-fixture")
else:
    auth = Path('/home/agent/.codex/auth.json')
    if auth.exists():
        assert not os.environ.get('CODEX_API_KEY')
        assert not Path('/home/agent/.codex/config.toml').exists()
        mount = Path('/run/discovery-codex-auth.json')
        try:
            mount.write_text('must not overwrite the host login')
        except OSError:
            pass
        else:
            raise AssertionError('Login mount must be read-only')
        data = json.loads(auth.read_text())
        data['tokens']['access_token'] = 'fixture-renewed-access'
        auth.write_text(json.dumps(data))
        print(json.dumps({'type': 'fixture', 'message': 'fixture-renewed-access fixture-access'}))
    text = sys.stdin.read()
    Path(sys.argv[sys.argv.index("--output-last-message") + 1]).write_text("Fixture answer: " + text)
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2}}))
"""


def main():
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        worker = root / "worker.py"
        original = docker_chat.WORKER.read_text()
        # Install a fake harness inside this disposable test container only.
        preamble = (
            "import os\nfrom pathlib import Path\n"
            "Path('/tmp/fixture-bin').mkdir()\n"
            f"Path('/tmp/fixture-bin/codex').write_text({FAKE!r})\n"
            "Path('/tmp/fixture-bin/codex').chmod(0o755)\n"
            "os.environ['PATH'] = '/tmp/fixture-bin:' + os.environ['PATH']\n"
        )
        worker.write_text(
            original.replace("from __future__ import annotations", preamble)
        )
        auth_file = root / "auth.json"
        original_auth = json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "fixture-access",
                    "refresh_token": "fixture-refresh",
                },
            }
        )
        auth_file.write_text(original_auth)
        for backend, auth_mode in (
            ("s3", "api_key"),
            ("lamin", "api_key"),
            ("lamin", "chatgpt"),
        ):
            output = root / f"{backend}-{auth_mode}"
            output.mkdir()
            params = resolve_parameters(
                {
                    "chat_cli": "codex",
                    "codex_auth": auth_mode,
                    "model": "fixture",
                    "effort": "medium",
                    "backend": backend,
                    "prompt": "fixture",
                    **({"with_index": False} if backend == "s3" else {}),
                }
            )
            with (
                patch.object(docker_chat, "WORKER", worker),
                patch.dict(os.environ, {"DISCOVERY_CODEX_AUTH_FILE": str(auth_file)}),
            ):
                container = docker_chat.run_chat(
                    params, "Find human datasets.", {}, output
                )
            metrics = write_metrics(output, container)
            assert auth_file.read_text() == original_auth
            assert "fixture-access" not in (output / "chat.jsonl").read_text()
            assert "fixture-renewed-access" not in (output / "chat.jsonl").read_text()
            assert not (output / "auth.json").exists()
            assert metrics["total_tokens"] == 12
            assert metrics["cache_read_tokens"] == 4
            assert (
                output / "answer.txt"
            ).read_text() == "Fixture answer: Find human datasets."
            print(
                json.dumps(
                    {
                        "backend": backend,
                        "codex_auth": auth_mode,
                        "status": "passed",
                        "image_id": container["image_id"],
                    }
                )
            )


if __name__ == "__main__":
    main()
