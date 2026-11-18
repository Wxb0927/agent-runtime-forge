"""Line A：业界主流入口 —— ``create_agent`` + 中间件 + checkpointer。

对照关系（这是本项目要产出的"硬对比"的 A 侧）：

| 关注点 | 主流做法 | 落点 |
|---|---|---|
| 循环 | 框架持有：model → tools → model，直到不再发 tool_calls | ``create_agent`` |
| 技能 | **工具**，schema 由代码派生 | ``tools.all_tools`` |
| 工具面 | 按路由 **裁剪 bind_tools 子集** | ``router.route`` → ``tools_for`` |
| 治理 | **中间件**（⛔ 改模型节点本体） | ``ModelCallLimitMiddleware`` / ``HumanInTheLoopMiddleware`` |
| 跨轮恢复 | checkpointer | ``checkpoint.saver()`` |
| 写操作 | 人工确认闸（approve / edit / reject 三态） | HITL 中间件盯 ``submit_intake_draft`` |

⚠️ 与 Line B 的分工：这条线**有环**（模型决定还调不调），那条线**无环**（框架一次出计划）。
同一个业务问题两边各跑一遍，数字必须逐字一致——见 ``tests/test_graph_parity.py``。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from langchain.agents import create_agent
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware,
    ModelCallLimitMiddleware,
)
from langchain_core.language_models.chat_models import BaseChatModel

from forge.graph.tools import all_tools
from forge.model.scripted import ScriptedChatModel

WRITE_TOOL = "submit_intake_draft"

SYSTEM_PROMPT = """你是制造业项目数据的查询助手。

规则（⛔ 违反其中任何一条即为错误回答）：
1. 任何数字都必须来自工具返回值，⛔ 自己计算、估算、四舍五入。
2. 工具返回 value 为 null 表示**无数据**，必须照实说"无数据"，⛔ 说成 0。
3. 已经拿到足够指标就直接作答，⛔ 反复调用同一个工具。
4. 写操作（submit_intake_draft）只在用户明确要求立项时调用，其余一律先问。
"""


def build_react_agent(*, model: Optional[BaseChatModel] = None,
                      skills: Optional[list[str]] = None,
                      checkpointer: Any = None,
                      scratch_dir: str | Path = "data/react",
                      hitl: bool = True,
                      thread_call_limit: int = 8):
    """返回 (agent, 说明用的元信息)。元信息给测试断言"这一轮到底绑了哪些工具"。"""
    tools = all_tools(skills, scratch_dir)
    middleware: list[Any] = [ModelCallLimitMiddleware(thread_limit=thread_call_limit,
                                                     exit_behavior="end")]
    if hitl:
        middleware.append(HumanInTheLoopMiddleware(
            interrupt_on={WRITE_TOOL: {"allowed_decisions": ["approve", "edit", "reject"]}}))
    agent = create_agent(
        model=model or ScriptedChatModel(),
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        middleware=middleware,
        checkpointer=checkpointer,
    )
    return agent, {"tools": [t.name for t in tools],
                   "middleware": [type(m).__name__ for m in middleware]}
