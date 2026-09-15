#!/usr/bin/env python3
"""Webhook receiver: turns "new message for role X" into a headless
claude/codex run for that role, single-flight per role, draining any
backlog that queued up while busy.

One process per host, fronting every role that host runs locally (e.g. your
main compute host: experiment-manager/dataset/evaluator/monitor; a second
host: overseer). Reached by the board-host's dispatcher through this
host's own Cloudflare Tunnel -- see orchestration/README.md. Never has a
direct Postgres connection (see board_client.py); drains via an MCP call
over the same network path a launched agent process would use.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request, Response

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestration.board_core.webhook_auth import SIGNATURE_HEADER, VerificationError, verify  # noqa: E402
from orchestration.receiver import board_client  # noqa: E402
from orchestration.receiver.config import RoleConfig, load_config  # noqa: E402
from orchestration.receiver.lock import RoleRunLock  # noqa: E402
from orchestration.receiver.runners import RunResult, claude_runner, codex_runner  # noqa: E402

logger = logging.getLogger("orchestration.receiver")

REPO_ROOT = Path(__file__).resolve().parents[2]
MAX_DRAIN_ITERATIONS = 20  # guards against a code bug turning drain into an infinite loop;
                            # legitimate cascades are already bounded by depth_remaining/wake_budget
                            # upstream of this process (see the dispatcher), this is a second, local backstop

RunnerFn = Callable[..., RunResult]

_RUNNERS: dict[str, RunnerFn] = {"claude": claude_runner.run, "codex": codex_runner.run}


def render_prompt(role_cfg: RoleConfig, mcp_url: str) -> str:
    brief = (REPO_ROOT / role_cfg.brief_path).read_text()
    return (
        f"{brief}\n\n---\n\n"
        f"You have been woken because new messages are waiting for you on the "
        f"orchestration board. Call read_messages against your configured MCP "
        f"server ({mcp_url}) to see what's pending, act per your brief above, "
        f"and call post_message to report back when done."
    )


class ReceiverApp:
    def __init__(
        self,
        roles: dict[str, RoleConfig],
        mcp_url: str,
        runners: dict[str, RunnerFn] | None = None,
    ):
        self.roles = roles
        self.mcp_url = mcp_url
        self.runners = runners or _RUNNERS
        self.locks = {role: RoleRunLock(role, cfg.max_runtime_minutes) for role, cfg in roles.items()}
        self.executor = ThreadPoolExecutor(max_workers=max(len(roles), 1), thread_name_prefix="board-receiver")

    def handle_wakeup(self, role: str, event_id: str) -> dict:
        """Returns the JSON body to send back; caller (the route handler)
        is responsible for the 202/401/404 status code."""
        lock = self.locks[role]
        if lock.try_acquire(event_id):
            self.executor.submit(self._run_and_drain, role, event_id)
            return {"accepted": True, "queued": False, "event_id": event_id}
        lock.mark_pending()
        return {"accepted": True, "queued": True, "event_id": event_id}

    def _run_and_drain(self, role: str, event_id: str) -> None:
        role_cfg = self.roles[role]
        lock = self.locks[role]
        iterations = 0
        current_event_id = event_id
        while True:
            iterations += 1
            prompt = render_prompt(role_cfg, self.mcp_url)
            result = self._invoke_runner(role_cfg, prompt)
            if not result.ok:
                logger.warning("role=%s run failed (event_id=%s): %s", role, current_event_id, result.stderr[:500])

            had_pending = lock.release_and_check_pending()
            if iterations >= MAX_DRAIN_ITERATIONS:
                logger.error("role=%s hit MAX_DRAIN_ITERATIONS -- stopping drain loop, may have unread work left", role)
                return

            # Drain condition is the board's own cursor (message_delivery),
            # not "was a delivery queued while I was busy" -- a queued
            # delivery could itself have already been superseded by
            # something the just-finished run already read and acted on.
            caught_up = asyncio.run(board_client.is_caught_up(self.mcp_url, role_cfg.agent_token))
            if caught_up and not had_pending:
                return
            if caught_up and had_pending:
                # A wakeup arrived but the run already consumed it (race
                # between the dispatcher's NOTIFY and this run's own
                # read_messages) -- nothing left to do.
                return

            # Not caught up: one more pass. Re-acquire the lock for the
            # next iteration so a NEW wakeup arriving mid-drain still
            # queues correctly rather than racing this loop.
            current_event_id = f"{event_id}-drain{iterations}"
            if not lock.try_acquire(current_event_id):
                # Someone else's wakeup is already driving another
                # iteration; let it finish, this thread is done.
                return

    def _invoke_runner(self, role_cfg: RoleConfig, prompt: str) -> RunResult:
        runner = self.runners[role_cfg.runner]
        mcp_config_path = str(REPO_ROOT / "orchestration" / "mcp" / "mcp_config.example.json")
        return runner(
            prompt,
            cwd=str(REPO_ROOT),
            mcp_config_path=mcp_config_path,
            agent_token=role_cfg.agent_token,
            timeout_seconds=role_cfg.max_runtime_minutes * 60,
        )


def build_app(receiver: ReceiverApp) -> FastAPI:
    app = FastAPI()

    @app.post("/webhook/{role}")
    async def webhook(role: str, request: Request) -> Response:
        if role not in receiver.roles:
            return Response(status_code=404, content=f"unknown role {role!r}")

        body = await request.body()
        signature = request.headers.get(SIGNATURE_HEADER)
        try:
            payload = verify(receiver.roles[role].webhook_secret, body, signature)
        except VerificationError as e:
            logger.warning("role=%s webhook rejected: %s", role, e)
            return Response(status_code=401, content=str(e))

        result = receiver.handle_wakeup(role, payload["event_id"])
        import json
        return Response(status_code=202, content=json.dumps(result), media_type="application/json")

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    mcp_url = os.environ.get("ORCH_MCP_URL")
    if not mcp_url:
        print("ORCH_MCP_URL is not set (the board-host's MCP endpoint) -- refusing to start.", file=sys.stderr)
        sys.exit(1)
    roles = load_config()
    receiver = ReceiverApp(roles, mcp_url)
    app = build_app(receiver)

    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("ORCH_RECEIVER_PORT", "8400")))


if __name__ == "__main__":
    main()
