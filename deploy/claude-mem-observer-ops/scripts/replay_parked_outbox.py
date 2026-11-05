#!/usr/bin/env python3
"""Safely move an explicitly approved parked observer outbox to released."""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys


def counts(connection: sqlite3.Connection) -> dict[str, int]:
    return dict(connection.execute(
        "SELECT state, count(*) FROM observer_deferred_outbox GROUP BY state"
    ).fetchall())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="claude-mem SQLite database")
    parser.add_argument("--expected", type=int, required=True, help="expected parked row count")
    parser.add_argument("--backup", help="backup SQLite path (required with --confirm)")
    parser.add_argument("--reason-prefix", default="manual_replay", help="audit reason prefix")
    parser.add_argument("--confirm", action="store_true", help="perform the state transition")
    args = parser.parse_args()

    db_path = pathlib.Path(args.db).expanduser()
    if not db_path.is_file():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 2

    connection = sqlite3.connect(str(db_path))
    connection.execute("PRAGMA busy_timeout=5000")
    try:
        before = counts(connection)
        parked = before.get("parked", 0)
        payloadless = connection.execute(
            "SELECT count(*) FROM observer_deferred_outbox "
            "WHERE state='parked' AND length(message_json)=0"
        ).fetchone()[0]
        report = {"db": str(db_path), "before": before, "parked": parked, "payloadless": payloadless}

        if parked != args.expected:
            print(json.dumps({**report, "error": "parked count changed"}, ensure_ascii=False))
            return 3
        if payloadless:
            print(json.dumps({**report, "error": "parked row has empty payload"}, ensure_ascii=False))
            return 4
        if not args.confirm:
            print(json.dumps({**report, "dry_run": True}, ensure_ascii=False))
            return 0
        if not args.backup:
            print("--backup is required with --confirm", file=sys.stderr)
            return 2

        backup_path = pathlib.Path(args.backup).expanduser()
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        if backup_path.exists():
            backup_path.unlink()
        backup = sqlite3.connect(str(backup_path))
        connection.backup(backup)
        backup.close()

        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            "UPDATE observer_deferred_outbox "
            "SET state='released', reason=? || '_from_' || coalesce(reason, 'unknown') "
            "WHERE state='parked'",
            (args.reason_prefix,),
        ).rowcount
        connection.commit()
        print(json.dumps({**report, "backup": str(backup_path), "released": changed, "after": counts(connection)}, ensure_ascii=False))
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
