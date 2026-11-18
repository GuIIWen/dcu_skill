# manifest v1

需要交换结构化清单、且下游未指定格式时，可用 UTF-8 JSON。原始证据无需符合此格式。示例：

```json
{
  "schema_version": 1,
  "run_id": "unique-run-id",
  "mode": "reconstructed",
  "question": "比较什么",
  "intended_variables": ["runtime.routing"],
  "time_range": {"start": null, "end": null, "timezone": "Asia/Shanghai"},
  "fields": {
    "request.effort": {
      "requested": "max",
      "observed": null,
      "status": "declared",
      "evidence": [{"path": "source.json", "locator": "parameters.reasoning_effort", "timestamp": null}],
      "note": "仅有调用方配置，未保存网关后的请求"
    }
  },
  "artifacts": [],
  "limitations": []
}
```

字段 status：verified（该轮运行证据）、declared（配置/人员陈述）、unknown（缺信息）、not_applicable（有理由不适用）。requested 和 observed 可分别不同；已观测但未知道请求值时，requested 为 null。不要把 unknown 字符串当真实配置参与比较。evidence 可有多条，需说明冲突而非静默挑选。

按任务选择字段，影响比较的字段不能静默省略：

| 字段组 | 建议内容 |
|---|---|
| model | 名称/修订/量化、tokenizer 和模板标识或小文件哈希 |
| runtime | 主机、卡型/数量、driver/runtime、镜像 ID、引擎版本、补丁哈希、实际 cmdline、拓扑/TP/DP/EP |
| cache | 层级与 backend、路由、session/DP 绑定、预热/清理方式与成功证据 |
| request | 温度、seed（区分 omitted）、输出限制、effort、工具、模板转换、重试 |
| workload | dataset 修订、题目列表哈希、agent/harness 版本、并发/请求率、超时、轮数 |
| measurement | 单位、采样窗口、同步/预热、计时方法、统计口径 |

算子任务用 shape、dtype、stride、layout、算子实现和计时替代不适用的模型字段。原始请求/工具输出可能包含敏感内容，只保存在获准目录；共享清单使用路径或哈希。

按问题选用核验细节：配置写了某值不证明运行进程读取了它；同名组件不证明版本、内容或转换规则一致。精确回放核对实际输入及转换链路；统计型负载核对输入分布。固定随机种子也不能替代对重复波动的检查。区分未知、明确未设置与明确关闭。

comparison.md 每项写：字段、A 值与证据等级、B 值与证据等级、是否为计划差异、对目标指标的影响/未知性。结论为以下一种，附理由：
- suitable_for_target_comparison：支持指定范围的对照，不暗示所有条件完全一致。
- descriptive_only：可报告结果差异，存在混杂因素，不能单因素归因。
- insufficient_evidence：关键条件或测量无法确认，补充最少证据后再判断。

清单结束时列出：已经保存的原始文件、摘要/哈希、采集工具或脚本版本、缺失项。冻结的是该轮证据，不是未来始终不变的机器信息。
