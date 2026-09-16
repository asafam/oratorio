#!/usr/bin/env bash
# Manual end-to-end check for ONE role (monitor -- the simplest) once a
# real board-host + receiver + tunnel exist. This is deliberately NOT run
# automatically by anything -- it hits real infrastructure and, once the
# receiver's runner is wired to a real `claude`/`codex` binary, spends
# real API budget. Run it by hand, read the output at each step.
#
# What the automated test suite ALREADY proves, so this script does not
# need to re-prove it: schema/durability (tests/test_board.py), the real
# MCP protocol (tests/test_mcp.py), HMAC sign/verify
# (tests/test_webhook_auth.py), single-flight/queueing
# (tests/test_receiver.py), fail-fast retries (tests/test_deliver.py), and
# the real LISTEN/NOTIFY loop waking a real socket (tests/test_dispatcher.py).
# What ONLY a live run can prove: the actual `claude -p`/`codex exec`
# invocation works with today's CLI flags, and the whole chain survives a
# REAL Cloudflare Tunnel hop instead of localhost.
set -euo pipefail

: "${ORCH_BOARD_DSN:?set to the board-host's Postgres DSN}"
: "${ORCH_MCP_URL:?set to the board-host's tunneled MCP endpoint}"
: "${MONITOR_WEBHOOK_URL:?set to monitor's tunneled receiver, e.g. https://<host>/webhook/monitor}"
: "${MONITOR_WEBHOOK_SECRET:?the webhook secret sync_roles.py printed for 'monitor'}"

echo "== 1. Confirm the role is synced =="
python -m orchestration.roles.sync_roles --dry-run | grep monitor

echo "== 2. Manual curl POST, signed by hand (no dispatcher involved yet) =="
BODY=$(python -c "
from orchestration.dispatcher.deliver import build_payload
import sys
sys.stdout.buffer.write(build_payload(message_id=0, retry_count=0, mcp_url='${ORCH_MCP_URL}'))
")
SIG=$(python -c "
from orchestration.board_core.webhook_auth import sign
import sys
print(sign('${MONITOR_WEBHOOK_SECRET}', sys.argv[1].encode()))
" "$BODY")
curl -sS -X POST "$MONITOR_WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -H "X-Board-Signature: $SIG" \
  --data-binary "$BODY" | tee /tmp/verify_monitor_step2.json
echo
echo "Expect: 202 with {\"accepted\": true, \"queued\": false}. If queued:true, another run"
echo "was already in flight -- wait for it to finish and retry."

echo "== 3. Confirm a real claude/codex process actually ran =="
echo "Check the receiver's own logs (journalctl --user -u oratorio-receiver@<label> -f,"
echo "or wherever it's running) for a real invocation, and confirm it's using current CLI"
echo "flags (this is the part the automated suite can't verify -- see"
echo "orchestration/receiver/runners/claude_runner.py's VERIFY AT DEPLOY TIME comment)."

echo "== 4. Confirm the run posted back to the board =="
python - <<PY
import os
from orchestration.board_core import db, registry
with db.get_pool().connection() as conn:
    rows = registry.read_all_messages(conn, "overseer", limit=5)
    for r in rows[-5:]:
        print(r["id"], r["sender"], "->", r.get("recipient") or r.get("topic"), ":", r["content"][:80])
PY
echo "Expect to see a message FROM monitor near the top, posted after step 2's webhook."

echo "== 5. Now trigger it for real: post a message that makes the dispatcher wake monitor =="
python - <<PY
from orchestration.board_core import db, messages
with db.get_pool().connection() as conn:
    result = messages.post_message(conn, "overseer", recipient="monitor", content="e2e verification ping")
    conn.commit()
    print("posted message id:", result["message_id"])
PY
echo "Watch the dispatcher's own logs for a wake-up delivery to monitor's real webhook URL,"
echo "and repeat step 4 to confirm monitor replied."

echo "All steps issued. Review each step's output above by hand -- this script intentionally"
echo "does not auto-assert success, since 'a real headless agent did the right thing' isn't"
echo "something a shell script can judge."
