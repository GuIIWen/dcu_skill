# 验收证据 v1

小批审查可直接交付证据表。有批量判定需求时，可整理成 JSON 数组，再从技能目录运行 `python scripts/verdict.py cases.json --out verdict.json`。每项来自已核验的原始证据，不应凭空填写 observed 或 passed。

```json
{
  "case_id": "cancel-releases-resources",
  "scope": "offline",
  "trigger": {"required": 1, "observed": 3, "evidence": "events.jsonl:10-20"},
  "signals": [
    {"name":"inflight-count", "available":true, "valid":true, "evidence":"metrics.json:inflight"}
  ],
  "assertions": [
    {"name":"returns-to-baseline", "passed":true, "evidence":"metrics.json:inflight", "requires":["inflight-count"]}
  ],
  "execution_error": null
}
```

scope 为 offline/live/historical。trigger.required >=1，observed >=0，均为整数（不接受 bool）。evidence 为非空定位字符串。signals 名称唯一，available/valid 为布尔值；assertions 非空、名称唯一，passed 为 true/false/null，requires 非空且引用存在的 signal。按断言核验解析成功、数据完整、数值有限及采样时效；只写 available=true 不构成证据。

整套输入先做结构校验：任意 case 格式错误或 case_id 重复，整体 INCONCLUSIVE/退出码 2，逐 case 结果仍保留合法 case 已证实的 FAIL；invalid_case_count 表示格式错误 case 数（重复 ID 单列 reasons）。case_id 必须是非空字符串，错误类型不能使判定器崩溃。所有 case 格式合法后，整套状态按 FAIL 优先于 INCONCLUSIVE 优先于 PASS；有效失败与另一个缺指标的合法 case 并存时仍为 FAIL/1。

单 case 判定顺序：
1. 格式不合法：INCONCLUSIVE，不能继续解释实验。
2. 有效触发达到 required，且某条 assertion 的依赖信号完整、passed=false：FAIL。其他信号缺失或另有执行错误不会抹掉已证实失败，但需保留诊断。
3. 触发不足、执行异常、缺失/无效信号、未判定断言：INCONCLUSIVE。
4. 有效触发、证据完整、所有断言 true：PASS。

客户端取消/超时可以是预期刺激，若实验计划如此，应作为 trigger 证据；不是一律记 execution_error。异常发生在服务恢复/对照请求则依其证据判定。不能因 expected failure 使脚本固定 exit 0。

脚本只判定已整理的证据，不执行测试或读取原始文件。核验来源和定位，不能仅相信填入的布尔值。旧版 FAIL/新版 PASS 还要根据已有配置、日志或清单确认条件可比。

## 按场景选用

- 服务与分布式任务：模拟传输、就绪检查和真实目标路径分开报告。对取消、超时或清理，核验各相关执行单元、稳定基线和采样窗口；只在请求完成后更新的计数不能证明取消后的实时行为。
- 数值对照：核验参考输出、误差口径和参与计算的范围，再解释阈值结果。
- 批量评测：保存完整任务清单、原始评分和异常；分别核验环境、服务、执行和评分阶段，类别要有证据。保留原始分母，子集和缺失项另报。用量缺失不自动否定评分结果。
- 实际执行：审阅已有证据时保持只读；检查验收脚本时先确认导入和运行的副作用。涉及线上状态变化时，沿用授权范围并明确目标、恢复和停止条件。
