"""Offline coverage of config, credential boundaries, prompt lookup, and usage."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import yaml

from benchmarks.observations import read_observation
from experiments.agent_dataset_discovery import run
from experiments.agent_dataset_discovery.docker_chat import (
    failure_detail,
    redact_secrets,
)
from experiments.agent_dataset_discovery.metrics import parse_events, write_metrics
from experiments.agent_dataset_discovery.parameters import resolve_parameters
from experiments.agent_dataset_discovery.runtime import (
    read_prompt,
    render_prompt,
    runtime_environment,
)


def config(backend="lamin"):
    return {
        "chat_cli": "codex",
        "codex_auth": "api_key",
        "backend": backend,
        "model": "test-model",
        "effort": "medium",
        "prompt": "Human scRNA-seq datasets",
        **({"with_index": False} if backend == "s3" else {}),
    }


class RuntimeTests(unittest.TestCase):
    def test_config_defaults_and_location_rejection(self):
        params = resolve_parameters(config())
        self.assertEqual(params["image"], "agent-dataset-discovery:codex-lamin")
        for extra in (
            {"lamin_instance": "owner/db"},
            {"s3_uri": "s3://bucket/prefix"},
            {"timeout_seconds": True},
            {"chat_cli": "unsupported"},
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                resolve_parameters({**config(), **extra})

    def test_s3_condition_requires_explicit_index_flag(self):
        params = config("s3")
        del params["with_index"]
        with self.assertRaisesRegex(ValueError, "with_index"):
            resolve_parameters(params)

    def test_reader_credentials_do_not_inherit_writer_credentials(self):
        location, env = runtime_environment(
            config(),
            {
                "DISCOVERY_CODEX_API_KEY": "model-reader",
                "DISCOVERY_LAMIN_INSTANCE": "laminlabs/lamindb-load-test",
                "DISCOVERY_LAMIN_API_KEY": "dataset-reader",
                "LAMIN_API_KEY": "benchmark-writer",
                "AWS_SECRET_ACCESS_KEY": "host-secret",
                "DISCOVERY_AWS_ACCESS_KEY_ID": "s3-reader-id",
                "DISCOVERY_AWS_SECRET_ACCESS_KEY": "s3-reader-secret",
                "DISCOVERY_AWS_SESSION_TOKEN": "s3-reader-session",
                "DISCOVERY_AWS_REGION": "eu-west-1",
                "DISCOVERY_S3_URI": "s3://other/condition/",
                "OTHER_SECRET": "unrelated",
            },
        )
        self.assertEqual(location, "laminlabs/lamindb-load-test")
        self.assertEqual(env["LAMIN_API_KEY"], "dataset-reader")
        self.assertNotIn("benchmark-writer", env.values())
        self.assertNotIn("host-secret", env.values())
        self.assertNotIn("OTHER_SECRET", env)
        self.assertEqual(
            env,
            {
                "CODEX_API_KEY": "model-reader",
                "LAMIN_CURRENT_INSTANCE": "laminlabs/lamindb-load-test",
                "LAMIN_API_KEY": "dataset-reader",
                "AWS_EC2_METADATA_DISABLED": "true",
            },
        )

    def test_s3_env_mapping_and_prefix(self):
        source = {
            "DISCOVERY_CODEX_API_KEY": "model-reader",
            "DISCOVERY_S3_URI": "s3://bucket/corpus",
            "DISCOVERY_AWS_ACCESS_KEY_ID": "reader-id",
            "DISCOVERY_AWS_SECRET_ACCESS_KEY": "reader-secret",
            "DISCOVERY_AWS_SESSION_TOKEN": "reader-session",
            "DISCOVERY_AWS_REGION": "eu-west-1",
            "DISCOVERY_LAMIN_API_KEY": "not-for-s3",
        }
        location, env = runtime_environment(config("s3"), source)
        self.assertEqual(location, "s3://bucket/corpus/")
        self.assertEqual(env["AWS_SESSION_TOKEN"], "reader-session")
        self.assertEqual(
            env,
            {
                "CODEX_API_KEY": "model-reader",
                "DISCOVERY_S3_URI": "s3://bucket/corpus/",
                "AWS_ACCESS_KEY_ID": "reader-id",
                "AWS_SECRET_ACCESS_KEY": "reader-secret",
                "AWS_SESSION_TOKEN": "reader-session",
                "AWS_REGION": "eu-west-1",
                "AWS_DEFAULT_REGION": "eu-west-1",
                "AWS_EC2_METADATA_DISABLED": "true",
            },
        )
        with self.assertRaisesRegex(ValueError, "corpus-prefix"):
            runtime_environment(
                config("s3"), {**source, "DISCOVERY_S3_URI": "s3://bucket"}
            )

    def test_prompt_is_resolved_under_the_experiment(self):
        ln = MagicMock()
        parent, experiment, prompts, record = (MagicMock() for _ in range(4))
        record.uid = "prompt-uid"
        record.features.get_values.return_value = {"prompt": " Find datasets.\n"}
        ln.Record.filter.return_value.one_or_none.side_effect = [
            parent,
            experiment,
            prompts,
            record,
        ]
        result = read_prompt(ln, "Test")
        self.assertEqual(result["text"], " Find datasets.\n")
        self.assertEqual(
            ln.Record.filter.call_args.kwargs,
            {"name": "Test", "type": prompts, "is_type": False},
        )
        self.assertEqual(ln.Record.filter.call_args_list[1].kwargs["type"], parent)

    def test_prompt_rendering_preserves_task(self):
        text = " Find datasets.\n"
        for backend, location in [("s3", "s3://bucket/prefix/"), ("lamin", "owner/db")]:
            prompt = render_prompt(backend, location, text)
            self.assertIn(location, prompt)
            self.assertTrue(prompt.endswith(text))

    def test_exact_secret_redaction(self):
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw)
            log = output / "stderr.txt"
            log.write_text("secret-123 at owner/db")
            redact_secrets(
                output,
                {"LAMIN_API_KEY": "secret-123", "LAMIN_CURRENT_INSTANCE": "owner/db"},
            )
            self.assertEqual(log.read_text(), "[REDACTED] at owner/db")

    def test_terminal_failure_is_reported_after_redaction(self):
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw)
            (output / "chat.jsonl").write_text(
                json.dumps({"type": "error", "message": "Retrying"})
                + "\n"
                + json.dumps(
                    {"type": "turn.failed", "error": {"message": "Rejected secret-123"}}
                )
                + "\ntruncated final line"
            )
            redact_secrets(output, {"CODEX_API_KEY": "secret-123"})
            self.assertEqual(failure_detail(output), "Rejected [REDACTED]")


class ObservationTests(unittest.TestCase):
    def test_child_publishes_bundle_without_tracking_or_uploading(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config_path, output = root / "config.yml", root / "results"
            config_path.write_text(yaml.safe_dump(config()))
            sdk, setup = MagicMock(), MagicMock()
            source = {
                "DISCOVERY_CODEX_API_KEY": "test-model-key",
                "DISCOVERY_LAMIN_INSTANCE": "owner/corpus",
                "DISCOVERY_LAMIN_API_KEY": "test-reader-key",
                "BENCHMARK_TRACKING_INSTANCE": "owner/benchmark",
                "LAMIN_CURRENT_INSTANCE": "owner/load",
            }
            prompt = {
                "uid": "uid-1",
                "name": "Human scRNA-seq datasets",
                "text": "Find human datasets.",
                "sha256": "source-hash",
            }

            def chat(params, rendered, env, directory):
                self.assertNotIn("owner/benchmark", env.values())
                self.assertIn("owner/corpus", rendered)
                (directory / "answer.txt").write_text("An answer")
                (directory / "chat.jsonl").write_text(
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {"input_tokens": 10, "output_tokens": 2},
                        }
                    )
                    + "\n"
                )
                (directory / "execution.json").write_text(
                    json.dumps(
                        {
                            "exit_code": 0,
                            "timed_out": False,
                            "chat_seconds": 0.5,
                            "codex_version": "fixture",
                        }
                    )
                )
                return {"container_preparation_seconds": 0.2}

            with (
                patch.dict(os.environ, source, clear=True),
                patch.dict(sys.modules, {"lamindb_setup": setup, "lamindb": sdk}),
                patch.object(run, "read_prompt", return_value=prompt),
                patch.object(run, "run_chat", side_effect=chat),
            ):
                run.main([str(config_path), str(output)])
            setup.connect.assert_called_once_with("owner/benchmark")
            sdk.track.assert_not_called()
            sdk.Artifact.assert_not_called()
            values, _, files = read_observation(output / "observations/chat")
            self.assertEqual(values["prompt_uid"], "uid-1")
            self.assertIn("metrics.parquet", {file.name for file in files})
            self.assertTrue((output / "run_info.yml").is_file())


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)

    def events(self, events):
        path = self.output / "chat.jsonl"
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        return path

    def test_cache_and_tool_events_not_double_counted(self):
        item = {"id": "tool-1", "type": "command_execution", "exit_code": 0}
        events = [
            {"type": "item.started", "item": item},
            {"type": "item.completed", "item": item},
            {"type": "item.completed", "item": item},
        ]
        for inputs, cached, outputs in [(100, 80, 10), (150, 120, 20)]:
            events.append(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": inputs,
                        "cached_input_tokens": cached,
                        "output_tokens": outputs,
                        "reasoning_output_tokens": 5,
                    },
                }
            )
        self.events(events)
        (self.output / "answer.txt").write_text("Answer")
        (self.output / "execution.json").write_text(
            json.dumps(
                {
                    "exit_code": 0,
                    "timed_out": False,
                    "chat_seconds": 1.2,
                    "codex_version": "test",
                }
            )
        )
        metrics = write_metrics(self.output, {"container_preparation_seconds": 0.5})
        self.assertEqual(metrics["total_tokens"], 280)
        self.assertEqual(metrics["input_tokens"], 250)
        self.assertEqual(metrics["output_tokens"], 30)
        self.assertEqual(metrics["cache_read_tokens"], 200)
        self.assertIsNone(metrics["cache_write_tokens"])
        self.assertEqual(metrics["tool_call_count"], 1)
        self.assertEqual(len(pd.read_parquet(self.output / "turns.parquet")), 2)

    def test_cache_write_is_separate_and_missing_usage_is_null(self):
        turns, _ = parse_events(
            self.events(
                [
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 5,
                            "cache_creation_input_tokens": 40,
                        },
                    }
                ]
            )
        )
        self.assertEqual(turns[0]["total_tokens"], 105)
        self.assertEqual(turns[0]["cache_write_tokens"], 40)
        self.assertIsNone(turns[0]["cache_read_tokens"])
        turns, _ = parse_events(self.events([{"type": "turn.completed"}]))
        self.assertIsNone(turns[0]["total_tokens"])

    def test_incomplete_or_failed_chat_is_rejected(self):
        for events in (
            [],
            [{"type": "turn.failed"}],
            [
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 2, "cached_input_tokens": 5},
                }
            ],
        ):
            with self.subTest(events=events), self.assertRaises(ValueError):
                parse_events(self.events(events))


if __name__ == "__main__":
    unittest.main()
