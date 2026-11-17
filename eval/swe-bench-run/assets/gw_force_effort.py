"""litellm pre-call hook: 强制 reasoning_effort=max。

原因：litellm 的 /v1/messages 适配器会用 Claude Code 发来的 thinking.budget_tokens 反推 effort（最多到 high/xhigh，
不会是 max），而且这一步在 pre-call 钩子之后、在 yaml litellm_params 合并之后执行，单改 reasoning_effort 会被覆盖。
所以这里把 thinking 去掉（不再反推），再写 reasoning_effort=max；再用 chat_template_kwargs.thinking=True 显式打开 DSV4 思考（客户端明确 disabled 时不开）。
"""
from litellm.integrations.custom_logger import CustomLogger


class ForceEffort(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if isinstance(data, dict) and str(data.get("model", "")).lower() == "deepseek-v4-flash-0731":
            th = data.pop("thinking", None)
            data["reasoning_effort"] = "max"
            if not (isinstance(th, dict) and th.get("type") == "disabled"):  # 去掉 thinking 后要显式打开 DSV4 思考
                data["chat_template_kwargs"] = {**(data.get("chat_template_kwargs") or {}), "thinking": True}
        return data


proxy_handler_instance = ForceEffort()
