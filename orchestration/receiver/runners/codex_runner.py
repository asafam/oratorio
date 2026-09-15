"""Headless Codex CLI invocation for a webhook-triggered role run.

VERIFY AT DEPLOY TIME: `codex exec` was the documented headless subcommand
as of when this was written, sourced from secondary docs, not
independently re-confirmed against `codex --help` in this environment (no
`codex` binary available here). A newer `codex remote-control` app-server
may be a better fit for a long-lived receiver than shelling out per-call --
reconsider at deploy time.
"""
from __future__ import annotations

import os

from orchestration.receiver.runners import RunResult, run_subprocess


def run(prompt: str, *, cwd: str, mcp_config_path: str, agent_token: str, timeout_seconds: int) -> RunResult:
    env = {**os.environ, "ORCH_AGENT_TOKEN": agent_token}
    cmd = ["codex", "exec", "--mcp-config", mcp_config_path, prompt]
    return run_subprocess(cmd, cwd=cwd, timeout_seconds=timeout_seconds, env=env)
