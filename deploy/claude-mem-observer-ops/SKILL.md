---
name: claude-mem-observer-ops
description: 适用于在 Windows 上检查 claude-mem observer 健康状态、延迟批处理并在明确授权后恢复 parked 历史队列；不适用于 DCU 节点部署、推理服务发布或未授权的自动重放。
---

# Claude Mem Observer Ops

Use this skill when operating the local claude-mem observer, diagnosing deferred batches, or recovering parked historical events after the user explicitly asks to process them.

The worker is a separate local process. Check `GET http://127.0.0.1:37777/api/health` before claiming it is running, and use the returned `workerPath` to identify the actual build. The patched worker must contain the deferred observer implementation (`observer_deferred_outbox`, `configureDeferredObserver`, and `summary_or_session_end`). A marketplace `plugin/scripts/worker-service.cjs` can be stale; prefer the matching installed cache build when the health response points there.

The normal runtime uses the Claude provider through the configured gateway, Haiku 5.5, `CLAUDE_MEM_OBSERVER_TRIGGER=summary_or_session_end`, a 30-minute max wait, and at most 16 observations per V2 request. A batch size is an upper bound. It does not force the worker to wait for 16 events, and project/session boundaries remain hard isolation boundaries.

## 批大小和等待时间

这些键保存在 `~/.claude-mem/settings.json`（Windows 通常为 `%USERPROFILE%\\.claude-mem\\settings.json`），值使用字符串：

```json
{
  "CLAUDE_MEM_OBSERVER_TRIGGER": "summary_or_session_end",
  "CLAUDE_MEM_OBSERVER_MAX_WAIT_MS": "1800000",
  "CLAUDE_MEM_CLAUDE_BATCH_V2_ENABLED": "true",
  "CLAUDE_MEM_CLAUDE_BATCH_V2_MAX_CHARS": "96000"
}
```

- `CLAUDE_MEM_OBSERVER_MAX_WAIT_MS` 是**最早一条 waiting 事件的最大等待时间**，单位为毫秒。`1800000` 是 30 分钟；`600000` 是 10 分钟；`7200000` 是 2 小时；`0` 表示不设定时器，一直等到 summary/session-end 边界。允许范围是 `0` 到 `2147483647`。它控制队列何时 flush，不是上游模型请求的网络超时。
- `CLAUDE_MEM_CLAUDE_BATCH_V2_MAX_CHARS` 控制 V2 请求的输入字符预算，允许 `12000` 到 `96000`；它控制内容大小，不是事件条数。
- 当前 V2 路径的 observation 条数上限是代码中的 `16`，因此设置文件里的 `CLAUDE_MEM_CLAUDE_OBSERVATION_BATCH_SIZE` 在 V2 开启时不会改变这个上限。该键的 `1` 到 `32` 配置只适用于 V2 关闭时的旧 Claude batching 路径。
- 如果要把 V2 的 16 改成其他事件数，需要修改 worker 的 V2 event-limit 实现并重新构建/安装 worker；目前没有对应的 settings 键。单纯把 `CLAUDE_MEM_CLAUDE_OBSERVATION_BATCH_SIZE` 改成 8 不会改变 V2。

修改 `settings.json` 后重启 claude-mem worker 才会重新读取 `CLAUDE_MEM_OBSERVER_TRIGGER`、`CLAUDE_MEM_OBSERVER_MAX_WAIT_MS` 和 V2 设置；不需要重启 IDE。启动后用 `GET http://127.0.0.1:37777/api/health` 验证 worker，再查看 `observer_batch_runs.event_count` 确认实际批大小。若要调的是单次模型请求的网络 deadline，另改 `CLAUDE_MEM_LLM_TIMEOUT_MS`（500 到 300000 毫秒，环境变量优先于 settings.json）。

Always inspect SQLite before and after a recovery. In `observer_deferred_outbox`, `waiting` and `released` are live work, `inflight` is an uncertain send, `parked` is intentionally quarantined, and `done` is completed. `observer_batch_runs` is an audit ledger: historical `sent` or `attribution_error` rows can remain after all outbox rows are done.

Never replay parked rows automatically. When the user explicitly requests historical processing:

1. Verify the worker is healthy, then stop it before changing the database.
2. Verify the parked count and that every selected row has a non-empty `message_json`.
3. Create a SQLite backup and use [`scripts/replay_parked_outbox.py`](scripts/replay_parked_outbox.py) with an expected count and `--confirm`. The script changes only `parked` rows to `released` and preserves the prior reason in the new reason value.
4. Start the patched worker build, then monitor `/api/health`, outbox states, batch ledger rows, and worker logs until no selected rows remain outside `done` or a clearly recorded failure remains.
5. If a model response is quarantined for attribution, do a second explicitly authorized, separately audited replay. Do not blindly resend an uncertain paid send without that authorization.

Keep API keys, gateway URLs containing credentials, and raw message bodies out of logs, reports, and skill files. Read [`references/recovery.md`](references/recovery.md) for the state model, verification queries, and Windows start/stop examples.
