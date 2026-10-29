"""Parse Codex exec events without counting usage or tool events twice."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

TOOL_TYPES = {"command_execution", "mcp_tool_call", "web_search", "file_change"}


def count(value):
    return value if type(value) is int and value >= 0 else None


def parse_events(path: Path) -> tuple[list[dict], list[dict]]:
    turns, tools = [], {}
    with path.open() as stream:
        for line in stream:
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("type") in {"turn.failed", "error"}:
                raise ValueError("Codex emitted a failure event; see chat.jsonl")
            if event.get("type") == "turn.completed":
                usage = event.get("usage") or {}
                inputs, outputs = (
                    count(usage.get("input_tokens")),
                    count(usage.get("output_tokens")),
                )
                cached = count(usage.get("cached_input_tokens"))
                if inputs is not None and cached is not None and cached > inputs:
                    raise ValueError("Cached input tokens exceed total input tokens")
                turns.append(
                    {
                        "turn": len(turns) + 1,
                        "input_tokens": inputs,
                        "output_tokens": outputs,
                        "cache_read_tokens": cached,
                        "cache_write_tokens": count(
                            usage.get(
                                "cache_creation_input_tokens",
                                usage.get("cache_write_input_tokens"),
                            )
                        ),
                        "reasoning_output_tokens": count(
                            usage.get("reasoning_output_tokens")
                        ),
                        "total_tokens": inputs + outputs
                        if inputs is not None and outputs is not None
                        else None,
                        "raw_usage": json.dumps(usage, sort_keys=True),
                    }
                )
            if event.get("type") == "item.completed":
                item = event.get("item") or {}
                if item.get("type") in TOOL_TYPES:
                    # Started/updated events describe the same call; count completion once.
                    key = item.get("id")
                    if not key:
                        raise ValueError("Tool event has no item ID")
                    tools[key] = {
                        "item_id": key,
                        "type": item["type"],
                        "status": item.get("status"),
                        "exit_code": item.get("exit_code"),
                    }
    if not turns:
        raise ValueError("No completed Codex turn found")
    return turns, list(tools.values())


def write_metrics(output: Path, container: dict) -> dict:
    turns, tools = parse_events(output / "chat.jsonl")
    execution = json.loads((output / "execution.json").read_text())
    if execution["exit_code"] != 0 or execution["timed_out"]:
        raise ValueError("Chat did not finish successfully")
    if not (output / "answer.txt").read_text().strip():
        raise ValueError("Codex did not produce a final answer")
    metrics = {
        "chat_seconds": execution["chat_seconds"],
        "container_preparation_seconds": container["container_preparation_seconds"],
        "turn_count": len(turns),
        "tool_call_count": len(tools),
        "exit_code": execution["exit_code"],
        "codex_version": execution["codex_version"],
    }
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    ):
        values = [turn[key] for turn in turns]
        metrics[key] = (
            sum(values) if all(value is not None for value in values) else None
        )
    pd.DataFrame([metrics]).to_parquet(output / "metrics.parquet", index=False)
    pd.DataFrame(turns).to_parquet(output / "turns.parquet", index=False)
    pd.DataFrame(tools, columns=["item_id", "type", "status", "exit_code"]).to_parquet(
        output / "tools.parquet", index=False
    )
    return metrics
