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
# is gone. `oratorio up` starts one beside the listeners of each
# workspace; only one of them works at a time (it holds a lock), the
# others wait and take over if it stops.
#
# Usage:  ORATORIO_BOARD_HOST=<server> board_tunnel.sh [up|down|status|keep]
set -euo pipefail

: "${ORATORIO_BOARD_HOST:?set to the address of the board server}"
SSH_PORT="${ORATORIO_BOARD_SSH_PORT:-443}"
LOCAL_PORT="${ORATORIO_BOARD_LOCAL_PORT:-5433}"
SOCK="${TMPDIR:-/tmp}/oratorio-tunnel-${LOCAL_PORT}.sock"
LOCK="${SOCK%.sock}.keeper"
CHECK_SECONDS="${ORATORIO_TUNNEL_CHECK_SECONDS:-10}"
CHECK_TIMEOUT="${ORATORIO_TUNNEL_CHECK_TIMEOUT:-10}"

# Is the tunnel up? Gives up after CHECK_TIMEOUT seconds: a check on a
# hung tunnel can wait forever, and then nothing reopens it.
check() {
  ssh -S "$SOCK" -O check "root@$ORATORIO_BOARD_HOST" 2>/dev/null &
  local pid=$! waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if (( waited >= CHECK_TIMEOUT )); then
      kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
      echo "$(date '+%H:%M:%S') the check hung -- closing the old tunnel"
      pkill -f -- "-M -S $SOCK" 2>/dev/null || true  # the hung tunnel itself
      rm -f "$SOCK"
      return 1
    fi
    sleep 1; waited=$((waited + 1))
  done
  wait "$pid"
}

# Take the keeper lock if no other live keeper holds it.
take_lock() {
  if mkdir "$LOCK" 2>/dev/null; then echo $$ > "$LOCK/pid"; return 0; fi
  local owner; owner=$(cat "$LOCK/pid" 2>/dev/null || true)
  if [[ "$owner" == "$$" ]]; then return 0; fi
  if [[ -n "$owner" ]] && kill -0 "$owner" 2>/dev/null; then return 1; fi
  rm -rf "$LOCK"  # its keeper is gone
  mkdir "$LOCK" 2>/dev/null && echo $$ > "$LOCK/pid"
}

case "${1:-up}" in
  up)
    if check; then
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
    trap '[[ "$(cat "$LOCK/pid" 2>/dev/null)" == "$$" ]] && rm -rf "$LOCK"' EXIT
    waiting=""
    while true; do
      if ! take_lock; then
        [[ -n "$waiting" ]] || echo "another keeper (pid $(cat "$LOCK/pid" 2>/dev/null)) keeps the tunnel; waiting"
        waiting=1; sleep "$CHECK_SECONDS"; continue
      fi
      [[ "$waiting" == "on" ]] || echo "keeping the tunnel up (checking every ${CHECK_SECONDS}s)"
      waiting=on
      if ! check; then
        echo "$(date '+%H:%M:%S') tunnel is down -- reopening"
        # BatchMode: never stop to ask for a password here; fail and try again.
        ssh -p "$SSH_PORT" -f -N -M -S "$SOCK" -o BatchMode=yes -o ConnectTimeout=10 \
            -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
            -L "$LOCAL_PORT:localhost:5432" "root@$ORATORIO_BOARD_HOST" \
          && echo "$(date '+%H:%M:%S') tunnel up" \
          || echo "$(date '+%H:%M:%S') could not reopen it; trying again"
      fi
      sleep "$CHECK_SECONDS"
    done ;;
  *) echo "usage: $0 [up|down|status|keep]" >&2; exit 2 ;;
esac
