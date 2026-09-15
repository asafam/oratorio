"""HTTP delivery of a wake-up webhook to a role's receiver, HMAC-signed.

Deliberately does NOT include message content in the payload -- staleness-
proof by construction, since the receiver's own read_messages call (once
the agent process is alive) is the actual source of truth. See
orchestration/receiver/server.py.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

import httpx

from orchestration.board_core.webhook_auth import SIGNATURE_HEADER, sign

RETRY_DELAYS_SECONDS = (5, 15, 45, 135)  # exponential backoff, capped
REQUEST_TIMEOUT_SECONDS = 10  # the receiver must respond 202 fast; the
                               # actual agent run happens after it returns

# 401 (bad/rotated webhook secret) and 404 (role not configured on this
# receiver) will never succeed on retry with the same request -- retrying
# them burns the full ~3.5 minute backoff schedule on an error that needs a
# config fix, not patience. Only genuinely transient conditions (network
# errors, 5xx, timeouts) get the retry loop.
PERMANENT_FAILURE_STATUS_CODES = (401, 404)


@dataclass
class WakeResult:
    delivered: bool
    status_code: int | None
    error: str | None
    attempts: int


def build_payload(*, message_id: int, retry_count: int, mcp_url: str | None = None) -> bytes:
    payload = {
        "event_id": f"evt_{uuid.uuid4().hex}",
        "message_id": message_id,
        "delivered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "retry_count": retry_count,
    }
    if mcp_url:
        payload["board"] = {"mcp_url": mcp_url}
    # Deterministic key order so the same logical payload always signs
    # identically -- not required for correctness (the signature is sent
    # alongside the exact bytes), but makes test fixtures/logs comparable.
    return json.dumps(payload, sort_keys=True).encode("utf-8")


def deliver_once(webhook_url: str, secret: str, body: bytes, *, timeout: float = REQUEST_TIMEOUT_SECONDS) -> httpx.Response:
    signature = sign(secret, body)
    return httpx.post(
        webhook_url,
        content=body,
        headers={
            "Content-Type": "application/json",
            SIGNATURE_HEADER: signature,
        },
        timeout=timeout,
    )


def deliver_with_retry(webhook_url: str, secret: str, *, message_id: int, mcp_url: str | None = None) -> WakeResult:
    """Synchronous retry loop with the plan's fixed backoff schedule. Reuses
    the SAME event_id across retries (the payload is built once) so the
    receiver can de-dupe a delivery that succeeded server-side but whose
    response never made it back."""
    body = build_payload(message_id=message_id, retry_count=0, mcp_url=mcp_url)
    last_error: str | None = None
    for attempt, delay in enumerate((0,) + RETRY_DELAYS_SECONDS, start=1):
        if delay:
            time.sleep(delay)
        try:
            resp = deliver_once(webhook_url, secret, body)
            if resp.status_code in (200, 202):
                return WakeResult(delivered=True, status_code=resp.status_code, error=None, attempts=attempt)
            if resp.status_code in PERMANENT_FAILURE_STATUS_CODES:
                return WakeResult(
                    delivered=False, status_code=resp.status_code,
                    error=f"permanent failure, not retrying: HTTP {resp.status_code}: {resp.text[:200]}",
                    attempts=attempt,
                )
            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except httpx.HTTPError as e:
            last_error = str(e)
    return WakeResult(delivered=False, status_code=None, error=last_error, attempts=len(RETRY_DELAYS_SECONDS) + 1)
