# Deferred observer recovery

The durable queue is the `observer_deferred_outbox` table in the local claude-mem SQLite database. The queue is keyed by the original `session_db_id`; do not merge rows from different sessions or projects by hand.

## State checks

Use a read-only connection for inspection:

```sql
SELECT state, count(*)
FROM observer_deferred_outbox
GROUP BY state
ORDER BY state;

SELECT state, coalesce(reason, '<none>'), count(*)
FROM observer_deferred_outbox
GROUP BY state, reason
ORDER BY state, reason;

SELECT status, count(*)
FROM observer_batch_runs
GROUP BY status
ORDER BY status;
```

`done` means the source event was committed. `waiting` is waiting for a summary/session boundary or the max-wait timer. `released` is a durable snapshot ready for a worker recovery pass. `inflight` means the process may have sent a paid request and must be treated as uncertain after a crash. `parked` is deliberately excluded from automatic recovery. `observer_batch_runs` keeps historical outcomes even after its source rows are later recovered.

## Windows worker commands

Resolve the worker path from `/api/health`; use that same build for stop/start. Typical commands are:

```powershell
& "$env:USERPROFILE\.bun\bin\bun.exe" $worker stop
Start-Process -FilePath "$env:USERPROFILE\.bun\bin\bun.exe" `
  -ArgumentList @($worker, '--daemon') `
  -WorkingDirectory (Split-Path $worker -Parent) `
  -WindowStyle Hidden
```

The process must be able to write `~/.claude-mem/logs` and the SQLite database. If a restricted shell reports `EPERM`, run the already-authorized service operation with the user's normal Windows permissions rather than switching to a different worker build.

## Recovery invariants

- Stop the worker before mutating `observer_deferred_outbox`.
- Require an expected parked count so a stale command cannot release newly created failures.
- Back up the database before changing state.
- Release only rows with preserved payloads; never fabricate an event body.
- Let the worker perform the normal source attribution and transactional commit path after release.
- Treat any remaining parked rows as a result requiring a new explicit decision, not as an automatic retry loop.
