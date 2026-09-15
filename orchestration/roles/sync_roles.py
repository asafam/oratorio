#!/usr/bin/env python3
"""Parse orchestration/roles/*.md (Markdown + YAML frontmatter) and upsert
them into board.agent / board.subscription.

Git is authoritative for role definitions; the DB row is a derived cache
(needed because a Streamable-HTTP-connected agent may not have this repo
checked out at all). `role_version` is stamped with the file's git blob
sha so a stale DB copy is detectable.

On first sync for a role, generates a fresh bearer token and prints it
once -- that's the only time it's ever shown in plaintext. Distribute it to
the role's own launch environment (its receiver process's env) out of band;
it is never written to disk by this script and never re-derivable from the
stored hash.

Usage:
    python -m orchestration.roles.sync_roles [--dry-run] [--rotate AGENT_ID]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestration.board_core import auth, db, registry  # noqa: E402

ROLES_DIR = Path(__file__).resolve().parent


def parse_role_file(path: Path) -> tuple[dict, str]:
    text = path.read_text()
    if not text.startswith("---"):
        raise ValueError(f"{path}: missing YAML frontmatter (must start with '---')")
    _, frontmatter, body = text.split("---", 2)
    meta = yaml.safe_load(frontmatter) or {}
    if "agent_id" not in meta:
        raise ValueError(f"{path}: frontmatter missing required 'agent_id'")
    return meta, body.strip()


def git_blob_sha(path: Path) -> str:
    try:
        return subprocess.run(
            ["git", "hash-object", str(path)],
            capture_output=True, text=True, check=True, cwd=path.parent,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rotate", metavar="AGENT_ID", help="force a new token for one agent")
    args = parser.parse_args()

    role_files = sorted(ROLES_DIR.glob("*.md"))
    if not role_files:
        print(f"No role files found under {ROLES_DIR}", file=sys.stderr)
        sys.exit(1)

    pool = None if args.dry_run else db.get_pool()

    for path in role_files:
        meta, body = parse_role_file(path)
        agent_id = meta["agent_id"]
        role_version = git_blob_sha(path)

        if args.dry_run:
            print(f"[dry-run] would sync {agent_id!r} from {path.name} (version={role_version})")
            continue

        with pool.connection() as conn:
            existing = registry.get_role(conn, agent_id)
            need_token = existing is None or agent_id == args.rotate
            if need_token:
                token = auth.generate_token()
                token_hash = auth.hash_token(token)
                webhook_secret = auth.generate_token()  # separate secret, see schema comment
            else:
                row = conn.execute(
                    "SELECT auth_token_hash, webhook_secret FROM board.agent WHERE agent_id = %s",
                    (agent_id,),
                ).fetchone()
                token_hash, webhook_secret = row[0], row[1]

            registry.upsert_agent(
                conn,
                agent_id=agent_id,
                role_doc_path=str(path.relative_to(ROLES_DIR.parents[1])),
                role_version=role_version,
                brief=body,
                peers=meta.get("peers", []),
                topics=meta.get("topics", []),
                auth_token_hash=token_hash,
                is_auditor=bool(meta.get("is_auditor", False)),
                webhook_secret=webhook_secret,
            )
            conn.commit()

        print(f"synced {agent_id!r} (version={role_version})")
        if need_token:
            print(
                f"  NEW TOKEN for {agent_id!r} (shown once -- this is the MCP bearer\n"
                f"  token; copy into that role's launch environment as ORCH_AGENT_TOKEN):\n"
                f"  {token}"
            )
            print(
                f"  NEW WEBHOOK SECRET for {agent_id!r} (shown once -- a DIFFERENT secret\n"
                f"  from the token above; copy into that role's receiver process as\n"
                f"  ORCH_WEBHOOK_SECRET, used to verify the dispatcher's HMAC signature):\n"
                f"  {webhook_secret}"
            )


if __name__ == "__main__":
    main()
