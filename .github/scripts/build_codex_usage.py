#!/usr/bin/env python3
"""Build a correlated usage record from trusted Codex CLI JSONL output."""

import argparse
import json
from pathlib import Path


ALLOWED_MODELS = {"gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"}
USAGE_SOURCE = "codex_exec_jsonl_turn_completed"


def _token(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Invalid {name}")
    return value


def parse_codex_usage(jsonl_text):
    completed = []
    for line_number, raw_line in enumerate(jsonl_text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed JSONL at line {line_number}") from exc
        if not isinstance(event, dict):
            raise ValueError(f"Invalid JSONL event at line {line_number}")
        if event.get("type") == "turn.completed":
            completed.append(event)
    if len(completed) != 1:
        raise ValueError("Expected exactly one turn.completed event")
    usage = completed[0].get("usage")
    if not isinstance(usage, dict):
        raise ValueError("turn.completed usage is missing or invalid")
    parsed = {
        "input_tokens": _token(usage.get("input_tokens"), "input_tokens"),
        "cached_input_tokens": _token(
            usage.get("cached_input_tokens"), "cached_input_tokens"
        ),
        "output_tokens": _token(usage.get("output_tokens"), "output_tokens"),
    }
    if parsed["cached_input_tokens"] > parsed["input_tokens"]:
        raise ValueError("cached_input_tokens exceeds input_tokens")
    return parsed


def build_usage(*, jsonl_text, process_exit_code, task_reference, launch_token,
                branch_name, workflow_run_id, model):
    if process_exit_code != 0:
        raise ValueError("Codex process did not exit successfully")
    if model not in ALLOWED_MODELS:
        raise ValueError("Invalid trusted model")
    if isinstance(workflow_run_id, bool) or workflow_run_id <= 0:
        raise ValueError("Invalid workflow run id")
    return {
        "schema_version": 1,
        "task_reference": task_reference,
        "launch_token": launch_token,
        "branch_name": branch_name,
        "workflow_run_id": workflow_run_id,
        "model": model,
        **parse_codex_usage(jsonl_text),
        "usage_source": USAGE_SOURCE,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--jsonl-file", required=True)
    parser.add_argument("--output-file", required=True)
    parser.add_argument("--process-exit-code", required=True, type=int)
    parser.add_argument("--task-reference", required=True)
    parser.add_argument("--launch-token", required=True)
    parser.add_argument("--branch-name", required=True)
    parser.add_argument("--workflow-run-id", required=True, type=int)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    usage = build_usage(
        jsonl_text=Path(args.jsonl_file).read_text(encoding="utf-8"),
        process_exit_code=args.process_exit_code,
        task_reference=args.task_reference,
        launch_token=args.launch_token,
        branch_name=args.branch_name,
        workflow_run_id=args.workflow_run_id,
        model=args.model,
    )
    Path(args.output_file).write_text(
        json.dumps(usage, ensure_ascii=True, sort_keys=True), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
