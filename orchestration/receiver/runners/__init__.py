from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_subprocess(cmd: list[str], *, cwd: str, timeout_seconds: int, env: dict | None = None) -> RunResult:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, timeout=timeout_seconds, capture_output=True, text=True, env=env,
        )
        return RunResult(proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired as e:
        return RunResult(-1, e.stdout or "", f"timed out after {timeout_seconds}s: {e.stderr or ''}")
    except FileNotFoundError as e:
        return RunResult(-1, "", f"runner binary not found: {e}")
