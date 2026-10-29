"""Authentication mode separation and file validation, with synthetic tokens only."""

import json
import tempfile
import unittest
from pathlib import Path

from experiments.agent_dataset_discovery.codex_auth import chatgpt_auth_file
from experiments.agent_dataset_discovery.docker_chat import redact_secrets
from experiments.agent_dataset_discovery.parameters import resolve_parameters
from experiments.agent_dataset_discovery.runtime import runtime_environment
from experiments.agent_dataset_discovery.tests.test_runtime import config


class CodexAuthTests(unittest.TestCase):
    def test_chatgpt_default_does_not_forward_api_key(self):
        params = config()
        del params["codex_auth"]
        params = resolve_parameters(params)
        self.assertEqual(params["codex_auth"], "chatgpt")
        for api_key in (None, "obsolete-api-key"):
            source = {
                "DISCOVERY_LAMIN_INSTANCE": "owner/corpus",
                "DISCOVERY_LAMIN_API_KEY": "reader",
                **({"DISCOVERY_CODEX_API_KEY": api_key} if api_key else {}),
            }
            _, env = runtime_environment(params, source)
            self.assertNotIn("CODEX_API_KEY", env)
            self.assertNotIn("OPENAI_API_KEY", env)

    def test_api_key_mode_still_requires_explicit_key(self):
        with self.assertRaisesRegex(ValueError, "DISCOVERY_CODEX_API_KEY"):
            runtime_environment(config(), {})
        with self.assertRaisesRegex(ValueError, "codex_auth"):
            resolve_parameters({**config(), "codex_auth": "auto"})

    def test_login_discovery_validation_and_redaction(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            auth_file = root / "auth.json"
            auth = {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "fake-access",
                    "refresh_token": "fake-refresh",
                },
            }
            auth_file.write_text(json.dumps(auth))
            path, secrets = chatgpt_auth_file({"CODEX_HOME": raw})
            self.assertEqual(path, auth_file.resolve())
            self.assertEqual(
                chatgpt_auth_file({"DISCOVERY_CODEX_AUTH_FILE": str(auth_file)})[0],
                path,
            )
            output = root / "outputs"
            output.mkdir()
            log = output / "chat.jsonl"
            log.write_text("fake-access fake-refresh")
            redact_secrets(output, {}, secrets)
            self.assertEqual(log.read_text(), "[REDACTED] [REDACTED]")
            self.assertEqual(json.loads(auth_file.read_text()), auth)
            for invalid in (
                {"auth_mode": "apikey", "OPENAI_API_KEY": "fake-key"},
                [],
                {"tokens": {"access_token": ""}},
            ):
                auth_file.write_text(json.dumps(invalid))
                with self.assertRaisesRegex(ValueError, "ChatGPT login"):
                    chatgpt_auth_file({"CODEX_HOME": raw})
            auth_file.unlink()
            with self.assertRaisesRegex(ValueError, "Cannot read Codex auth.json"):
                chatgpt_auth_file({"CODEX_HOME": raw})


if __name__ == "__main__":
    unittest.main()
