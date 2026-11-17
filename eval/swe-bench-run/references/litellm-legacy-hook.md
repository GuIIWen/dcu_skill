# LiteLLM 1.109.0 / DSV4 历史钩子

`assets/gw_force_effort.py` 是 1203 字节原始恢复版：

1. 仅在 `str(data['model']).lower() == 'deepseek-v4-flash-0731'` 时生效；这是钩子收到的模型别名，不是后端权重名。
2. `th = data.pop('thinking', None)`，阻止旧 `/v1/messages` adapter 再按 budget 反推 effort。
3. 设置 `reasoning_effort='max'`。
4. 客户端没有明确 `thinking.type='disabled'` 时，合并 `chat_template_kwargs.thinking=True`。
5. YAML 对该 model entry 声明 `reasoning_effort_levels: ['low','high','max']`，避免未知模型能力降档。

历史仅设置 YAML max、或钩子只写 max，都曾被后续转换覆盖；只移除 thinking 又曾导致返回缺少 thinking block。最终方案是上面的组合。

原始代码**在 disabled 时只跳过注入 True，并不写 False，也仍强制 max**；如果其它配置已有 thinking=True，disabled 不会被完整覆盖。它是“固定 max 对照实验”钩子，不是通用 effort/禁用思考实现。要尊重任意客户端 effort 或支持其它模型，创建单独修改版，明确优先级并测开/关/max 各分支，保留原件和变更记录。

换模型别名时同步修改钩子匹配或设计显式映射，且重新验证。不要因为 YAML 有 callback 字样就认为加载成功；看实际网关导入日志与上游 wire 请求。新版 LiteLLM 若已修复，先测原生行为再决定是否需要该旧补丁。


## 文件与来源

钩子为 `assets/gw_force_effort.py`，示例为 `assets/litellm-dsv4.example.yaml`。提取时间与 SHA256 见 `assets/gw_force_effort.provenance.json`。原件未修改；这些元数据只能确认文件身份，不能代替当前版本的集成测试。
