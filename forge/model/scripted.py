"""离线打桩模型：让**全部测试**不联网、不烧钱、逐字节可复现。

⚠️ 为什么必须自己写 ``bind_tools``：实测 ``BaseChatModel.bind_tools`` 的基类实现就是
一行 ``raise NotImplementedError``（见 tools/d2_probe3.py 读数）——provider 各自实现它。
打桩模型也得实现，否则 ``create_agent`` 第一步就炸。这里把 OpenAI 形状的工具规格原样
透传给 ``_generate`` 的 kwargs，于是测试能断言"**图这一轮到底把哪些工具交给了模型**"，
这正是"按路由裁剪 bind_tools 子集"那条主流做法的唯一取证点。

⛔ 用它冒充真模型：它只会照脚本走，没有任何推理能力，价值全在确定性。
"""
from __future__ import annotations

import json
from typing import Any, Optional, Sequence

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field


class ScriptedChatModel(BaseChatModel):
    """按脚本逐步产出 tool_calls，脚本走完就给最终答案。

    脚本步形如 ``{"tool_calls": [(name, args), ...], "content": ""}``；
    最后一步只给 ``{"content": "..."}``。超出发泡时按 ``fallback`` 收尾（⛔ 无限环）。
    """

    script: list[dict[str, Any]] = Field(default_factory=list)
    calls: int = 0
    fallback: str = "无更多数据，按已取到的指标作答。"

    # 观测点：每轮真正被 bind 上去的工具名，用来断言"子集裁剪"确实发生了
    bound_history: list[list[str]] = Field(default_factory=list)
    seen_messages: list[int] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:  # pragma: no cover - 框架要求
        return "forge-scripted"

    def bind_tools(self, tools: Sequence[Any], *, tool_choice: Optional[str] = None,
                   **kwargs: Any) -> Any:
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools],
                         tool_choice=tool_choice, **kwargs)

    def _generate(self, messages: list[BaseMessage], stop: Optional[list[str]] = None,
                  run_manager: Optional[CallbackManagerForLLMRun] = None,
                  **kwargs: Any) -> ChatResult:
        specs = kwargs.get("tools") or []
        self.bound_history.append(sorted(t["function"]["name"] for t in specs))
        self.seen_messages.append(len(messages))

        if self.calls >= len(self.script):
            msg = AIMessage(content=self.fallback)
        else:
            step = self.script[self.calls]
            self.calls += 1
            calls = step.get("tool_calls") or []
            msg = AIMessage(
                content=step.get("content", ""),
                tool_calls=[{"name": n, "args": a, "id": f"call_{self.calls}_{i}", "type": "tool_call"}
                            for i, (n, a) in enumerate(calls)],
            )
        return ChatResult(generations=[ChatGeneration(message=msg)])

    # 便于测试里打印"这一路模型看到过哪些工具名"
    def tool_names_by_turn(self) -> list[list[str]]:
        return [list(turn) for turn in self.bound_history]

    def dump(self) -> str:  # pragma: no cover - 调试用
        return json.dumps({"calls": self.calls, "bound": self.bound_history},
                          ensure_ascii=False)
