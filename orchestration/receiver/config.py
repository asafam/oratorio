"""Per-host receiver configuration: which roles this receiver process
fronts, and each role's webhook secret / MCP token / runner / brief.

Loaded from a local YAML file that is NEVER committed (it holds secrets) --
see roles.example.yaml for the template. Path comes from
ORCH_RECEIVER_CONFIG, defaulting to orchestration/receiver/roles.local.yaml
next to this file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "roles.local.yaml"


@dataclass
class RoleConfig:
    role: str
    webhook_secret: str
    agent_token: str  # this role's MCP bearer token, passed to the launched
                       # claude/codex process as ORCH_AGENT_TOKEN
    runner: str  # "claude" | "codex"
    brief_path: str
    max_runtime_minutes: int = 30  # stale-lock ceiling, see lock.py


def load_config(path: Path | None = None) -> dict[str, RoleConfig]:
    path = path or Path(os.environ.get("ORCH_RECEIVER_CONFIG", DEFAULT_CONFIG_PATH))
    if not path.exists():
        raise FileNotFoundError(
            f"receiver config not found at {path} -- copy roles.example.yaml, fill in "
            f"the secrets/tokens printed by `python -m orchestration.roles.sync_roles`, "
            f"and set ORCH_RECEIVER_CONFIG if not using the default path"
        )
    raw = yaml.safe_load(path.read_text()) or {}
    return {
        role: RoleConfig(
            role=role,
            webhook_secret=cfg["webhook_secret"],
            agent_token=cfg["agent_token"],
            runner=cfg["runner"],
            brief_path=cfg["brief_path"],
            max_runtime_minutes=cfg.get("max_runtime_minutes", 30),
        )
        for role, cfg in raw.items()
    }
