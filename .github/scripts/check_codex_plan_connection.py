"""Manual plan-credential canary, never an approval or billing attestation.

The CLI receives a fixed prompt in an empty directory, no repository content
and no inherited API, GitHub or deployment credentials. Command execution and
external integrations are disabled. Read-only sandboxing and denied approvals
block writes; remaining built-in tools are not claimed to be disabled, and any
tool event fails this check. The empty directory is not a filesystem jail.
All model output is private and discarded; only fixed status and usage escape.
"""
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import tempfile
import time

EXPECTED = "PLAN_CODEX_CONNECTION_OK"
MAX_OUTPUT_BYTES = 65_536
TIMEOUT_SECONDS = 90


class ConnectionFailure(Exception):
    """Only a fixed, non-sensitive failure is ever printed by main."""


def validate_environment(environ):
    expected = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": "borovikov88/service2",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_ACTOR": "borovikov88",
        "GITHUB_TRIGGERING_ACTOR": "borovikov88",
    }
    if any(environ.get(key) != value for key, value in expected.items()):
        raise ConnectionFailure()
    if not re.fullmatch(r"[0-9a-f]{40}", environ.get("GITHUB_SHA", "")):
        raise ConnectionFailure()
    for key in environ:
        if key.startswith(("OPENAI_", "AZURE_OPENAI_", "CODEX_CONFIG")) or key in {
            "CODEX_API_KEY", "CODEX_HOME", "CODEX_MODEL_PROVIDER", "CODEX_BASE_URL",
        }:
            raise ConnectionFailure()
    token = environ.get("CODEX_ACCESS_TOKEN", "")
    if not token or token != token.strip() or token.startswith("sk-"):
        raise ConnectionFailure()
    binary = Path(environ.get("SERVICE2_CODEX_BINARY", ""))
    if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise ConnectionFailure()
    return token, str(binary)


def command(binary, directory, output_file):
    # In the pinned CLI, ephemeral auth storage never loads persisted logins.
    # With the clean child environment, CODEX_ACCESS_TOKEN is the only route.
    config = {
        "cli_auth_credentials_store": '"ephemeral"',
        "web_search": '"disabled"',
        "mcp_servers": "{}",
    }
    for name in (
        "shell_tool", "unified_exec", "apply_patch_freeform", "apps", "connectors",
        "plugins", "hooks", "codex_hooks", "plugin_hooks", "js_repl", "code_mode",
        "multi_agent", "view_image", "image_generation", "imagegenext", "browser_use",
        "computer_use", "skill_search", "memory_tool", "tool_search", "remote_plugin",
        "search_tool", "shell_snapshot",
    ):
        config[f"features.{name}"] = "false"
    result = [binary, "--ask-for-approval", "never"]
    for key, value in config.items():
        result.extend(["-c", f"{key}={value}"])
    result.extend([
        "exec", "--strict-config", "--json", "--ephemeral", "--ignore-user-config",
        "--ignore-rules", "--sandbox", "read-only", "--skip-git-repo-check",
        "--cd", str(directory), "--model", "gpt-5.6-sol", "--color", "never",
        "--output-last-message", str(output_file),
        f"Return exactly {EXPECTED}. Do not call any tools or inspect any files.",
    ])
    return result


def capture_process(argv, environ, directory):
    """Bound both pipes while running; never include child output in errors."""
    process = subprocess.Popen(
        argv, cwd=directory, env=environ, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    stdout = bytearray()
    total = 0
    deadline = time.monotonic() + TIMEOUT_SECONDS
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, True)
            selector.register(process.stderr, selectors.EVENT_READ, False)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ConnectionFailure()
                for key, _ in selector.select(min(remaining, 0.2)):
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > MAX_OUTPUT_BYTES:
                        raise ConnectionFailure()
                    if key.data:
                        stdout.extend(chunk)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or process.wait(timeout=remaining) != 0:
                raise ConnectionFailure()
        return bytes(stdout)
    finally:
        # Kill descendants too, including when the CLI has exited after spawning
        # a child that still owns one of its output pipes.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()
        process.stderr.close()


def validate_result(raw, last_message):
    if len(raw) > MAX_OUTPUT_BYTES or last_message.strip() != EXPECTED:
        raise ConnectionFailure()
    events = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    stage = 0
    messages = 0
    usage = None
    for event in events:
        if not isinstance(event, dict):
            raise ConnectionFailure()
        kind = event.get("type")
        if kind == "thread.started" and stage == 0:
            if not isinstance(event.get("thread_id"), str) or not event["thread_id"]:
                raise ConnectionFailure()
            stage = 1
        elif kind == "turn.started" and stage == 1:
            stage = 2
        elif kind == "item.completed" and stage == 2:
            item = event.get("item")
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                raise ConnectionFailure()
            if item.get("type") == "agent_message" and item["text"].strip() == EXPECTED:
                messages += 1
            elif item.get("type") != "reasoning":
                raise ConnectionFailure()
        elif kind == "turn.completed" and stage == 2 and messages == 1:
            usage = event.get("usage")
            if not isinstance(usage, dict):
                raise ConnectionFailure()
            for name in ("input_tokens", "cached_input_tokens", "output_tokens"):
                if type(usage.get(name)) is not int or usage[name] < 0:
                    raise ConnectionFailure()
            if not usage["output_tokens"] or usage["cached_input_tokens"] > usage["input_tokens"]:
                raise ConnectionFailure()
            stage = 3
        else:
            raise ConnectionFailure()
    if stage != 3:
        raise ConnectionFailure()
    return {name: usage[name] for name in ("input_tokens", "cached_input_tokens", "output_tokens")}


def check_connection(environ):
    token, binary = validate_environment(environ)
    with tempfile.TemporaryDirectory(prefix="service2-plan-check-") as temporary:
        directory = Path(temporary)
        output_file = directory / "last-message.txt"
        child_environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "TMPDIR": str(directory),
            "CODEX_ACCESS_TOKEN": token,
        }
        raw = capture_process(command(binary, directory, output_file), child_environment, directory)
        if output_file.is_symlink() or not output_file.is_file() or output_file.stat().st_size > 1024:
            raise ConnectionFailure()
        return validate_result(raw, output_file.read_text(encoding="utf-8"))


def main():
    try:
        usage = check_connection(dict(os.environ))
    except Exception:
        print("CODEX_PLAN_CONNECTION_FAILED: credential, configuration or response was rejected.")
        return 1
    print("CODEX_PLAN_CONNECTION_OK: fixed prompt completed; no review or deployment was authorized.")
    print("Usage: " + json.dumps(usage, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
