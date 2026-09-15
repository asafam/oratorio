"""Headless Claude Code invocation for a webhook-triggered role run.

VERIFY AT DEPLOY TIME: --output-format/--permission-prompts flag names were
current as of when this was written but not independently re-confirmed
against `claude --help` in this environment (no `claude` binary available
here). Check `code.claude.com/docs/en/cli-reference` before relying on this.
"""
from __future__ import annotations

import os

from orchestration.receiver.runners import RunResult, run_subprocess


def run(prompt: str, *, cwd: str, mcp_config_path: str, agent_token: str, timeout_seconds: int) -> RunResult:
    env = {**os.environ, "ORCH_AGENT_TOKEN": agent_token}
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--permission-prompts", "none",
        "--mcp-config", mcp_config_path,
    ]
    return run_subprocess(cmd, cwd=cwd, timeout_seconds=timeout_seconds, env=env)
