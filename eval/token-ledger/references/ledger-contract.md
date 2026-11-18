# 标准记录 v1 与账本语义

本格式用于可选的批量汇总脚本；有现成工具时可沿用其数据格式，并验证相同的计数规则。脚本从技能目录运行：`python scripts/ledger.py records.jsonl --out OUTPUT_DIR`，输出 `requests.jsonl`、`trials.csv` 和 `summary.json`。退出码 2 表示输入或计数契约错误；修正或披露错误，不能静默丢行后宣称完整。

每行一个累计用量快照（流式增量先按源语义归并）：

```json
{"run_id":"r1","trial_id":"t1","session_id":"s1","request_id":"m1","attempt_id":"1","revision":0,"kind":"main","status":"success","format":"openai","usage":{"prompt_tokens":100,"completion_tokens":20,"prompt_tokens_details":{"cached_tokens":80}},"source":{"path":"trace.jsonl","locator":"line:5"}}
```

必填：run_id、trial_id、session_id、request_id、attempt_id（非空字符串），revision（非负整数），kind（main/subagent/compaction/unknown），status（success/failed/cancelled/unknown），format（openai/anthropic/normalized），usage（对象或 null），source（非空 path 和 locator）。ID 缺失时用原始文件+事件位置生成稳定 ID，并报告无法跨文件去重的局限，不按文本相似度去重。

去重键包含 run/trial/session/request/attempt。attempt_id 代表真实模型调用尝试，同请求重试不能合并；同一调用复制到另一文件时保留相同键。revision 表示源事件顺序，不是输入文件遍历顺序。重复 revision 的内容若冲突，脚本报错。末尾 usage 为空时保留最近非空快照；多个非空快照以最高 revision 的完整快照为准，不逐字段取最大或累加。缺少字段的增量必须由适配器依据源语义先合并。

summary 的去重审计字段分别为 duplicate_records（相同 attempt/revision 的重复记录）、superseded_snapshots（同 attempt 的其他 revision 快照数）和 collapsed_records（归并总行数，等于前两项之和）。不能把快照历史计作真正重复日志。

计数即使不完整也必须自洽：已知的 read、creation、uncached 不能超过已知 total；已知 read+creation 不能超过 total；creation 不能超过 uncached；三者齐备时 total=read+uncached。未知字段保留 null，不为通过校验强行补零。

格式定义：任意来源都可以适配到 `normalized`；另外两个名称只指定已核实的字段布局。
- openai：input_total=prompt_tokens；output=completion_tokens；cache_read=prompt_tokens_details.cached_tokens；input_uncached=input_total-cache_read。cache_creation 缺省为 null，因为该接口通常未单列；未缓存总量可由差值确定。
- anthropic：input_total=input_tokens+cache_read_input_tokens+cache_creation_input_tokens；input_uncached=input_tokens+cache_creation_input_tokens；output=output_tokens。三个输入字段任意缺失，则相应无法计算的和为 null。
- normalized：usage 直接给 input_total、cache_read、cache_creation、input_uncached、output，所有值为非负整数或 null；并保留适配依据。不允许从服务端 prefill batch 计数伪装成逐请求 usage。

OpenAI 输出 detail 中的 reasoning_tokens 通常是 completion_tokens 子项，不额外相加；Anthropic 或网关转换的具体口径先核实。脚本不计算 reasoning 拆分，也不进行价格计算。

汇总每个指标为 {known_subtotal, known_requests, total_requests, complete_total}；有任何请求未知时 complete_total=null。零请求的 complete_total=null。命中率仅在 total/read 两项都已知的请求子集计算，附 eligible_requests 和 eligible_input_total，名为 cache_hit_rate_known_subset；输入总量为零时比例为 null。

status 是执行状态，不决定消耗是否计入。原始题目 reward/exception 另存，不把 failed 自动映射为零 token。summary 按 run 以及调用 kind 汇总，跨 run 的数值不能用作一个实验结论。

对于没有天然试验编号的日志，可以用稳定的会话或采集批次作为聚合组，并标注其含义。`trials.csv` 只包含日志里见到的组；只有掌握完整任务清单才可报告总体和零调用组。按双方共同样本分析时另列分母，不能以成功样本代替全量。
