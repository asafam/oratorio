#!/usr/bin/env bash
# Open (or close) the SSH tunnel to a board that lives on a remote server.
#
# The board's Postgres listens only on that server's localhost; this
# forwards local port 5433 to it, so ORCH_BOARD_DSN can point at
# localhost:5433 as if the database were local. Nothing but this
# forwarder runs on your machine.
#
# SSH goes over port 443 by default, because some networks only let web
# ports out. The server's sshd must listen there (see the README).
#
# The tunnel is one ssh process: it ends when the network changes, the
# laptop sleeps, or the server stops answering, and nothing brings it
# back. `keep` does: it stays running and reopens the tunnel whenever it
# is gone. `oratorio up` starts one beside the listeners.
#
# Usage:  ORATORIO_BOARD_HOST=<server> board_tunnel.sh [up|down|status|keep]
set -euo pipefail

: "${ORATORIO_BOARD_HOST:?set to the address of the board server}"
SSH_PORT="${ORATORIO_BOARD_SSH_PORT:-443}"
LOCAL_PORT="${ORATORIO_BOARD_LOCAL_PORT:-5433}"
SOCK="${TMPDIR:-/tmp}/oratorio-tunnel-${LOCAL_PORT}.sock"

case "${1:-up}" in
  up)
    if ssh -S "$SOCK" -O check "root@$ORATORIO_BOARD_HOST" 2>/dev/null; then
      echo "tunnel already up (localhost:$LOCAL_PORT)"
    else
      ssh -p "$SSH_PORT" -f -N -M -S "$SOCK" \
          -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
          -L "$LOCAL_PORT:localhost:5432" "root@$ORATORIO_BOARD_HOST"
      echo "tunnel up (localhost:$LOCAL_PORT -> $ORATORIO_BOARD_HOST)"
    fi ;;
  down)
    ssh -S "$SOCK" -O exit "root@$ORATORIO_BOARD_HOST" 2>/dev/null || true
    echo "tunnel down" ;;
  status)
    ssh -S "$SOCK" -O check "root@$ORATORIO_BOARD_HOST" 2>&1 || true ;;
  keep)
    echo "keeping the tunnel up (checking every ${ORATORIO_TUNNEL_CHECK_SECONDS:-10}s)"
    while true; do
      if ! ssh -S "$SOCK" -O check "root@$ORATORIO_BOARD_HOST" 2>/dev/null; then
        echo "$(date '+%H:%M:%S') tunnel is down -- reopening"
        # BatchMode: never stop to ask for a password here; fail and try again.
        ssh -p "$SSH_PORT" -f -N -M -S "$SOCK" -o BatchMode=yes -o ConnectTimeout=10 \
            -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
            -L "$LOCAL_PORT:localhost:5432" "root@$ORATORIO_BOARD_HOST" \
          && echo "$(date '+%H:%M:%S') tunnel up" \
          || echo "$(date '+%H:%M:%S') could not reopen it; trying again"
      fi
      sleep "${ORATORIO_TUNNEL_CHECK_SECONDS:-10}"
    done ;;
  *) echo "usage: $0 [up|down|status|keep]" >&2; exit 2 ;;
esac
